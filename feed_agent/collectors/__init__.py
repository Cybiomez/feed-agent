"""Реестр сборщиков источников.

Единственное место, где ядро узнаёт о типах источников. Добавить тип (например
"telegram") = написать класс-сборщик и дописать строку в COLLECTORS.
"""

from __future__ import annotations

from ..config import Source
from .base import Collector
from .rss import RssCollector

# Тип источника (Source.type) -> класс сборщика.
COLLECTORS: dict[str, type[Collector]] = {
    "rss": RssCollector,
    # "telegram": TelegramCollector,   # этап 2
}


def make_collector(source: Source) -> Collector | None:
    """Собрать нужный сборщик по типу источника. Неизвестный тип — None (пропустим)."""
    cls = COLLECTORS.get(source.type)
    return cls(source) if cls else None
