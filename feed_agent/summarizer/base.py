"""Контракт суммаризатора: из статьи сделать русскую выжимку.

Суммаризатор от конкретной модели не зависит — сменить = поменять одну строку в
settings. Возвращает словарь {title, summary, takeaways[list], conclusion} или None,
если сделать выжимку не удалось (тогда новость просто не попадёт в дайджест).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Item


class Summarizer(ABC):
    """Базовый суммаризатор: статья -> русская выжимка."""

    name = "base"

    def filter_relevant(self, items: list[Item], profile: str) -> list[Item]:
        """Оставить только релевантные профилю новости. По умолчанию — все (без фильтра);
        модель переопределяет. Фильтр не должен терять: при недоступности вернуть всё."""
        return items

    def group_duplicates(self, items: list[Item]) -> list[list[int]]:
        """Сгруппировать индексы новостей, освещающих ОДНО событие (дубли из разных каналов).
        По умолчанию — каждая сама по себе (без слияния); модель переопределяет."""
        return [[i] for i in range(len(items))]

    def dedup_against(self, new_titles: list[str], delivered_titles: list[str]) -> list[str]:
        """Сверить новые заголовки с показанными за период: 'new'|'repeat'|'update' на каждый.
        По умолчанию — всё 'new' (без дедупа); модель переопределяет."""
        return ["new"] * len(new_titles)

    def summarize_batch(self, articles: list[tuple[Item, str]]) -> list[dict | None]:
        """Выжимки по нескольким статьям. По умолчанию — по одной (self.summarize); модель
        переопределяет пачкой (экономия). Возвращает список той же длины."""
        return [self.summarize(it, body) for it, body in articles]

    @abstractmethod
    def summarize(self, item: Item, fulltext: str) -> dict | None:
        """Сделать русскую выжимку по полному тексту статьи.

        Возвращает {"title": str, "summary": str (2–3 предложения),
        "takeaways": list[str], "conclusion": str} или None при неудаче."""
        raise NotImplementedError
