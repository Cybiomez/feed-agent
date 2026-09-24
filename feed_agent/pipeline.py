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
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


def enrich(storage: Storage, settings, summarizer) -> int:
    """Скачать полный текст и сделать русскую выжимку по топ-N свежих новостей."""
    done = 0
    for item in storage.to_enrich(settings.enrich_per_run):
        fulltext, image = fetch_article(item.url)
        ru = summarizer.summarize(item, fulltext)
        if not ru or not ru.get("summary"):
            continue  # не вышло — пропускаем, не роняя прогон (уйдёт в следующий раз)
        storage.save_enrichment(item.uid, fulltext, image, ru)
        done += 1
    return done


def run_once(dry_run: bool = False, collect_only: bool = False) -> dict:
    """Один полный прогон. Возвращает сводку (для лога/отчёта)."""
    settings = config.load_settings()
    sources = config.load_sources()
    storage = Storage()
    summary: dict[str, object] = {"dry_run": dry_run, "collect_only": collect_only}

    try:
        new = collect(storage, sources)
        removed = storage.purge_old(settings.history_days)
        summary.update(sources=len(sources), new_items=new, purged=removed)

        if collect_only:
            storage.kv_set("last_collect", _now())
            return summary

        # Обогащение: полный текст + русская выжимка (в сухом прогоне — офлайн-заглушкой).
        summarizer = make_summarizer(settings, force_stub=dry_run)
        enriched_n = enrich(storage, settings, summarizer)
        summary.update(summarizer=summarizer.name, enriched=enriched_n)

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
