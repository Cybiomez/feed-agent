"""Простые структуры данных, которыми обменивается пайплайн.

Держим их отдельно и без зависимостей — чтобы сборщики, оценщик, хранилище и
доставка говорили на одном языке, но не знали друг о друге (минимальная связанность).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field


def _normalize_title(title: str) -> str:
    """Свести заголовок к виду для сравнения: нижний регистр, схлопнутые пробелы.
    Нужно, чтобы одна и та же новость с разных лент не задваивалась из-за мелочей."""
    return re.sub(r"\s+", " ", title or "").strip().lower()


@dataclass
class Item:
    """Одна новость из источника."""

    source_name: str
    title: str
    url: str
    summary: str = ""              # краткое описание из ленты (может быть пустым)
    published: str = ""            # ISO-время публикации, если лента его дала
    collected_at: str = ""         # когда мы её забрали (проставляет пайплайн)

    # Идентификатор для дедупликации: хеш от url + нормализованный заголовок.
    # Свойство, а не поле — считается на лету, всегда согласован с содержимым.
    @property
    def uid(self) -> str:
        base = f"{self.url}\n{_normalize_title(self.title)}"
        return hashlib.sha256(base.encode("utf-8")).hexdigest()[:16]


@dataclass
class Score:
    """Оценка новости моделью: релевантность 0..100 и краткая причина."""

    item_uid: str
    score: int
    reason: str = ""
    model: str = ""
    scored_at: str = ""
