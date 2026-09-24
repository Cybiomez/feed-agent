"""Контракт сборщика источника.

Сборщик знает, как из одного источника достать список новостей (Item). Механизм
пайплайна от способа чтения не зависит: добавить новый тип источника = добавить
класс-сборщик и одну строку в реестр (collectors/__init__.py). Ядро не меняется.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..config import Source
from ..models import Item


class Collector(ABC):
    """Базовый сборщик: из описания источника вернуть свежие новости."""

    def __init__(self, source: Source) -> None:
        self.source = source

    @abstractmethod
    def fetch(self) -> list[Item]:
        """Достать новости из источника. Сетевые/парсинговые ошибки — гасить внутри
        и вернуть, что удалось (пустой список — норм), чтобы один битый источник не
        ронял весь прогон."""
        raise NotImplementedError
