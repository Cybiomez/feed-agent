"""Контракт провайдера модели-оценщика.

Провайдер получает пачку новостей и профиль вкуса, возвращает по каждой оценку
0..100 и короткую причину. Механизм пайплайна от конкретной модели не зависит:
сменить провайдера = поменять одну строку в settings.toml.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Item


class LLMProvider(ABC):
    """Базовый оценщик новостей по профилю вкуса."""

    name = "base"

    @abstractmethod
    def score_batch(self, items: list[Item], profile: str) -> dict[str, tuple[int, str]]:
        """Оценить пачку новостей.

        Возвращает словарь: uid новости -> (оценка 0..100, причина).
        Новость, которую модель пропустила/не разобрала, можно не класть в ответ —
        пайплайн подстрахует нейтральной оценкой (смещение в полноту: не терять).
        """
        raise NotImplementedError
