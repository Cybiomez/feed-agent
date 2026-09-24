"""Офлайн-заглушка оценщика: грубая эвристика без сети и без ключа.

Нужна, чтобы гонять весь пайплайн в сухом прогоне и в тестах, не дёргая модель.
Это НЕ решение по качеству — только чтобы механизм ехал. Логика простая: берём
из профиля слова-подсказки «интересно» и «мусор», считаем попадания в заголовке
и описании, двигаем базовую оценку. Смещение в полноту: база 55, порог обычно ниже.
"""

from __future__ import annotations

import re

from ..models import Item
from .base import LLMProvider

_WORD_RE = re.compile(r"[a-zA-Zа-яА-ЯёЁ][a-zA-Zа-яА-ЯёЁ-]{3,}")


def _section_words(profile: str, header_contains: str) -> set[str]:
    """Вытащить слова-подсказки из секции профиля, чей заголовок содержит подстроку."""
    words: set[str] = set()
    grab = False
    for line in profile.splitlines():
        s = line.strip()
        if s.startswith("##"):
            grab = header_contains.lower() in s.lower()
            continue
        if grab:
            for w in _WORD_RE.findall(s.lower()):
                words.add(w)
    return words


class StubProvider(LLMProvider):
    """Эвристический оценщик по словам профиля."""

    name = "stub"

    def score_batch(self, items: list[Item], profile: str) -> dict[str, tuple[int, str]]:
        interesting = _section_words(profile, "Интересно")
        junk = _section_words(profile, "Мусор")
        out: dict[str, tuple[int, str]] = {}
        for it in items:
            text = f"{it.title} {it.summary}".lower()
            tokens = set(_WORD_RE.findall(text))
            hits_i = tokens & interesting
            hits_j = tokens & junk
            score = 55 + 12 * len(hits_i) - 20 * len(hits_j)
            score = max(0, min(100, score))
            if hits_j and not hits_i:
                reason = "похоже на мусор: " + ", ".join(sorted(hits_j))
            elif hits_i:
                reason = "по интересам: " + ", ".join(sorted(hits_i))
            else:
                reason = "нейтрально (нет явных сигналов)"
            out[it.uid] = (score, reason)
        return out
