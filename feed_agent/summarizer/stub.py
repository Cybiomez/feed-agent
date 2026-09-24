"""Офлайн-заглушка суммаризатора — без Claude, для сухого прогона и тестов конвейера.

Не переводит и не анализирует (это делает Claude). Просто берёт начало текста как
«summary», чтобы прогнать скачивание → хранилище → дайджест → доставку без модели.
"""

from __future__ import annotations

from ..models import Item
from .base import Summarizer


class StubSummarizer(Summarizer):
    name = "stub"

    def summarize(self, item: Item, fulltext: str) -> dict | None:
        body = (fulltext or item.summary or item.title).strip()
        summary = " ".join(body.split()[:40])  # первые ~40 слов как «суть»
        return {
            "title": item.title,
            "summary": summary or item.title,
            "takeaways": [],
            "conclusion": "",
        }
