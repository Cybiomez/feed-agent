"""Оркестрация прогона: собрать → обогатить русской выжимкой → дайджест → доставить.

Обогащение (дорогая часть: скачать полный текст + Claude) делаем только по топ-N свежих
новостей за прогон. Части друг о друге не знают — связь только здесь.
"""

from __future__ import annotations

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


def _body_and_image(item) -> tuple[str, str]:
    """Тело для выжимки и картинка. Пост канала самодостаточен (не качаем), статью — качаем."""
    if "t.me/" in item.url:
        return item.summary or item.title, ""
    fulltext, image = fetch_article(item.url)
    return fulltext or item.summary or item.title, image


def enrich(storage: Storage, settings, summarizer, profile: str) -> tuple[int, int]:
    """Просеять по релевантности, сгруппировать дубли (одно событие из разных каналов),
    обогатить КАЖДУЮ группу русской выжимкой (слив тексты источников). Цель — не потерять
    ничего: обрабатываем все релевантные, лимит — лишь предохранитель от разовой лавины.
    Возвращает (обогащено_групп, отсеяно)."""
    candidates = storage.to_enrich(settings.prefilter_pool)
    if not candidates:
        return 0, 0

    relevant = summarizer.filter_relevant(candidates, profile)
    relevant_uids = {it.uid for it in relevant}

    # Нерелевантные помечаем обработанными (пустая выжимка) — в кандидаты больше не попадут.
    dropped = 0
    for it in candidates:
        if it.uid not in relevant_uids:
            storage.save_enrichment(it.uid, "", "", {})
            dropped += 1
    if not relevant:
        return 0, dropped

    # Группируем дубли: одно событие в нескольких каналах — в одну группу.
    groups = summarizer.group_duplicates(relevant)

    # Предохранитель: не больше N групп за прогон. Остаток НЕ теряется — уйдёт следующим
    # прогоном (об усечении сообщаем в лог, без «тихого» лимита).
    cap = settings.enrich_per_run
    if len(groups) > cap:
        print(f"enrich: групп {len(groups)} > предохранителя {cap}; остаток уйдёт позже")
        groups = groups[:cap]

    # Готовим тела всех групп (скачивание статей — это не вызовы модели).
    prepared = []  # (rep, members, image, combined, sources)
    for group in groups:
        members = [relevant[i] for i in group]
        blocks, image, names = [], "", []
        for m in members:
            body, img = _body_and_image(m)
            blocks.append(f"[{m.source_name}]\n{body}")
            names.append(m.source_name)
            if not image and img:
                image = img
        combined = ("Материал по одному событию из нескольких источников — объедини и "
                    "взаимодополни:\n\n" if len(members) > 1 else "") + "\n\n".join(blocks)
        prepared.append((members[0], members, image, combined,
                         " + ".join(dict.fromkeys(names))))

    # Выжимки ПАЧКАМИ: одна загрузка модели на пачку вместо вызова на каждую статью
    # (главная экономия лимита). CHUNK держит размер запроса/ответа управляемым.
    done = 0
    CHUNK = 10
    for start in range(0, len(prepared), CHUNK):
        chunk = prepared[start:start + CHUNK]
        results = summarizer.summarize_batch([(rep, combined) for rep, _, _, combined, _ in chunk])
        for (rep, members, image, combined, sources), ru in zip(chunk, results):
            if not ru or not ru.get("summary"):
                continue  # эту не вышло — уйдёт следующим прогоном
            storage.save_enrichment(rep.uid, combined, image, ru, sources=sources)
            for m in members[1:]:                      # прочих членов группы — слиты в rep
                storage.save_enrichment(m.uid, "", "", {})
            done += 1
    return done, dropped


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
        enriched_n, dropped_n = enrich(storage, settings, summarizer, profile)
        summary.update(summarizer=summarizer.name, enriched=enriched_n, dropped=dropped_n)

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
