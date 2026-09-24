"""Фабрика суммаризаторов. Выбор — по settings.summarizer.provider.

Добавить провайдера (другая модель) = класс + ветка. force_stub=True — офлайн (сухой прогон).
"""

from __future__ import annotations

from ..config import Settings
from .base import Summarizer
from .stub import StubSummarizer


def make_summarizer(settings: Settings, force_stub: bool = False) -> Summarizer:
    if force_stub or settings.summarizer == "stub":
        return StubSummarizer()
    if settings.summarizer == "claude":
        from .claude_cli import ClaudeSummarizer

        return ClaudeSummarizer(timeout_s=settings.summarizer_timeout_s,
                                model=settings.summarizer_model)
    raise ValueError(f"Неизвестный суммаризатор: {settings.summarizer!r}")
