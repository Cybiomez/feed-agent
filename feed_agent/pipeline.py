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
           unlimited: bool = False) -> dict:
    """Просеять по релевантности, сгруппировать дубли, СВЕРИТЬ с показанным за N часов (не
    вбрасывать ту же информацию) и обогатить оставшееся. unlimited=True — без предохранителя
    (последний прогон дня выдаёт всю очередь). Возвращает статы {enriched,dropped,repeat,updates}."""
    stats = {"enriched": 0, "dropped": 0, "repeat": 0, "updates": 0}
    candidates = storage.to_enrich(settings.prefilter_pool)
    if not candidates:
        return stats

    relevant = summarizer.filter_relevant(candidates, profile)
    relevant_uids = {it.uid for it in relevant}
    for it in candidates:
        if it.uid not in relevant_uids:
            storage.save_enrichment(it.uid, "", {})     # нерелевантно — обработано
            stats["dropped"] += 1
    if not relevant:
        return stats

    groups = summarizer.group_duplicates(relevant)
    if not unlimited:
        cap = settings.enrich_per_run
        if len(groups) > cap:
            print(f"enrich: групп {len(groups)} > предохранителя {cap}; остаток уйдёт позже")
            groups = groups[:cap]

    # Сверка с ПОКАЗАННЫМ за последние N часов: точный повтор пропускаем, обновление помечаем.
    reps = [relevant[g[0]] for g in groups]
    delivered = storage.recent_delivered(settings.freshness_hours)
    verdicts = summarizer.dedup_against([r.title for r in reps], delivered)

    # Готовим тела/медиа/ссылки для тех групп, что идут дальше (не повтор).
    prepared = []  # (rep, members, combined, links, images, video, is_update)
    for group, verdict in zip(groups, verdicts):
        members = [relevant[i] for i in group]
        if verdict == "repeat":
            for m in members:
                storage.save_enrichment(m.uid, "", {})   # уже показано — не повторяем
            stats["repeat"] += 1
            continue
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
        prepared.append((members[0], members, combined, links, images[:10], video,
                         verdict == "update"))

    # Выжимки ПАЧКАМИ (одна загрузка модели на пачку — экономия лимита).
    CHUNK = 10
    for start in range(0, len(prepared), CHUNK):
        chunk = prepared[start:start + CHUNK]
        results = summarizer.summarize_batch([(rep, comb) for rep, _, comb, _, _, _, _ in chunk])
        for (rep, members, comb, links, images, video, is_upd), ru in zip(chunk, results):
            if not ru or not ru.get("summary"):
                continue  # эту не вышло — уйдёт следующим прогоном
            storage.save_enrichment(rep.uid, comb, ru, source_links=links, images=images,
                                    video=video, is_update=is_upd)
            for m in members[1:]:                      # прочих членов группы — слиты в rep
                storage.save_enrichment(m.uid, "", {})
            stats["enriched"] += 1
            if is_upd:
                stats["updates"] += 1
    return stats


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
        st = enrich(storage, settings, summarizer, profile)
        summary.update(summarizer=summarizer.name, **st)

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
