"""Оркестрация одного прогона: собрать → сохранить → оценить → дайджест → доставить.

Собирает вместе все части, но сами они друг о друге не знают (связь только здесь).
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import config
from .collectors import make_collector
from .delivery import deliver
from .digest import build_digest
from .evaluator import evaluate
from .providers import make_provider
from .storage import Storage


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

        # Оценка новых новостей (в сухом прогоне — офлайн-заглушкой).
        candidates = storage.unscored_items(settings.max_items_per_run)
        provider = make_provider(settings, force_stub=dry_run)
        scored = evaluate(candidates, profile, provider, settings, storage)

        # Отбор прошедших порог и сборка дайджеста.
        selected = storage.selected_for_digest(settings.threshold, settings.digest_max_items)
        summary.update(scored=scored, provider=provider.name, selected=len(selected))

        if selected:
            text = build_digest(selected, settings)
            ok = deliver(text, settings.notify_cmd, dry_run=dry_run)
            summary["delivered"] = ok
            if ok and not dry_run:
                storage.mark_delivered([it.uid for it, _ in selected])
        else:
            summary["delivered"] = None  # нечего слать

        storage.kv_set("last_run", _now())
        return summary
    finally:
        storage.close()
