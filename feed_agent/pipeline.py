"""Оркестрация прогона: собрать → обогатить русской выжимкой → дайджест → доставить.

Обогащение (дорогая часть: скачать полный текст + Claude) делаем только по топ-N свежих
новостей за прогон. Части друг о друге не знают — связь только здесь.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from . import config
from .collectors import make_collector
from .delivery import deliver
from .digest import build_digest
from .extract import fetch_article
from .storage import Storage
from .summarizer import make_summarizer


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def collect(storage: Storage, sources) -> int:
    """Обойти источники, добавить новые новости в базу. Вернуть число новых."""
    new = 0
    for src in sources:
        collector = make_collector(src)
        if collector is None:
            print(f"collect: пропущен источник неизвестного типа: {src.type} ({src.name})")
            continue
        for item in collector.fetch():
            item.collected_at = _now()
            if storage.add_item(item):
                new += 1
    return new


def _body_and_media(item) -> tuple[str, list, str]:
    """Тело для выжимки + картинки (URL) + видео. Пост канала самодостаточен (не качаем,
    медиа уже собраны при сборе); статью — качаем (og:image добавляем к списку)."""
    if "t.me/" in item.url:
        return (item.summary or item.title), list(item.images or []), (item.video or "")
    fulltext, image = fetch_article(item.url)
    imgs = list(item.images or [])
    if image and image not in imgs:
        imgs.insert(0, image)
    return (fulltext or item.summary or item.title), imgs, ""


def enrich(storage: Storage, settings, summarizer, profile: str,
           unlimited: bool = False) -> tuple[int, int, int]:
    """Отсечь несвежее, просеять по релевантности, сгруппировать дубли и обогатить каждую
    группу русской выжимкой (слив тексты источников). unlimited=True — без предохранителя
    (последний прогон дня выдаёт всю очередь). Возвращает (обогащено, отсеяно, старьё)."""
    candidates = storage.to_enrich(settings.prefilter_pool)
    if not candidates:
        return 0, 0, 0

    # Окно свежести: старьё в дайджест не идёт (чинит «нет новых постов, а новости есть»).
    # Помечаем старое обработанным, чтобы не всплывало. Неизвестное время (0) считаем свежим.
    cutoff = time.time() - settings.freshness_hours * 3600
    fresh, stale = [], 0
    for it in candidates:
        if it.published_ts and it.published_ts < cutoff:
            storage.save_enrichment(it.uid, "", {})
            stale += 1
        else:
            fresh.append(it)
    if not fresh:
        return 0, 0, stale

    relevant = summarizer.filter_relevant(fresh, profile)
    relevant_uids = {it.uid for it in relevant}
    dropped = 0
    for it in fresh:
        if it.uid not in relevant_uids:
            storage.save_enrichment(it.uid, "", {})     # нерелевантно — обработано
            dropped += 1
    if not relevant:
        return 0, dropped, stale

    groups = summarizer.group_duplicates(relevant)
    if not unlimited:
        cap = settings.enrich_per_run
        if len(groups) > cap:
            print(f"enrich: групп {len(groups)} > предохранителя {cap}; остаток уйдёт позже")
            groups = groups[:cap]

    # Готовим тела/медиа/ссылки всех групп (скачивание статей — не вызовы модели).
    prepared = []  # (rep, members, combined, source_links, images, video)
    for group in groups:
        members = [relevant[i] for i in group]
        blocks, links, images, video = [], [], [], ""
        for m in members:
            body, imgs, vid = _body_and_media(m)
            blocks.append(f"[{m.source_name}]\n{body}")
            links.append({"name": m.source_name, "url": m.url})
            for u in imgs:
                if u not in images:
                    images.append(u)
            if not video and vid:
                video = vid
        combined = ("Материал по одному событию из нескольких источников — объедини и "
                    "взаимодополни:\n\n" if len(members) > 1 else "") + "\n\n".join(blocks)
        prepared.append((members[0], members, combined, links, images[:10], video))

    # Выжимки ПАЧКАМИ (одна загрузка модели на пачку — экономия лимита).
    done = 0
    CHUNK = 10
    for start in range(0, len(prepared), CHUNK):
        chunk = prepared[start:start + CHUNK]
        results = summarizer.summarize_batch([(rep, combined) for rep, _, combined, _, _, _ in chunk])
        for (rep, members, combined, links, images, video), ru in zip(chunk, results):
            if not ru or not ru.get("summary"):
                continue  # эту не вышло — уйдёт следующим прогоном
            storage.save_enrichment(rep.uid, combined, ru,
                                    source_links=links, images=images, video=video)
            for m in members[1:]:                      # прочих членов группы — слиты в rep
                storage.save_enrichment(m.uid, "", {})
            done += 1
    return done, dropped, stale


def run_once(dry_run: bool = False, collect_only: bool = False) -> dict:
    """Один полный прогон. Возвращает сводку (для лога/отчёта)."""
    settings = config.load_settings()
    sources = config.load_sources()
    profile = config.load_profile()
    storage = Storage()
    summary: dict[str, object] = {"dry_run": dry_run, "collect_only": collect_only}

    try:
        new = collect(storage, sources)
        removed = storage.purge_old(settings.history_days)
        summary.update(sources=len(sources), new_items=new, purged=removed)

        if collect_only:
            storage.kv_set("last_collect", _now())
            return summary

        # Фильтр по вкусу + обогащение (в сухом прогоне — офлайн-заглушкой).
        summarizer = make_summarizer(settings, force_stub=dry_run)
        enriched_n, dropped_n, stale_n = enrich(storage, settings, summarizer, profile)
        summary.update(summarizer=summarizer.name, enriched=enriched_n,
                       dropped=dropped_n, stale=stale_n)

        # Дайджест из обогащённых, ещё не отправленных новостей.
        items = storage.enriched_for_digest(settings.digest_max_items)
        summary["ready"] = len(items)

        if items:
            text, included = build_digest(items, settings)
            ok = deliver(text, settings.notify_cmd,
                         to_chat=settings.target_chat, to_thread=settings.target_thread,
                         dry_run=dry_run)
            summary["delivered"] = ok
            summary["in_digest"] = len(included)
            if ok and not dry_run:
                storage.mark_delivered([e.uid for e in included])
        else:
            summary["delivered"] = None  # нечего слать

        storage.kv_set("last_run", _now())
        return summary
    finally:
        storage.close()
