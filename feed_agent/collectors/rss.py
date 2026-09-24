"""Сборщик RSS/Atom-лент через feedparser."""

from __future__ import annotations

import re
from html import unescape

import feedparser

from ..models import Item
from .base import Collector

_TAG_RE = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    """Привести текст ленты к чистому виду: убрать теги, раскодировать html-сущности
    (`&#8217;` → `’`), схлопнуть пробелы. Экранирование обратно — уже на этапе дайджеста,
    один раз, иначе получалось бы двойное (`&amp;#8217;`)."""
    return re.sub(r"\s+", " ", unescape(_TAG_RE.sub(" ", text or ""))).strip()


class RssCollector(Collector):
    """Читает одну RSS/Atom-ленту и отдаёт её записи как Item."""

    def fetch(self) -> list[Item]:
        try:
            parsed = feedparser.parse(self.source.url)
        except Exception:
            return []  # битая лента/сеть — молча пропускаем этот источник
        items: list[Item] = []
        for e in parsed.entries:
            title = _clean(getattr(e, "title", ""))
            link = getattr(e, "link", "").strip()
            if not title or not link:
                continue
            summary = _clean(getattr(e, "summary", ""))[:500]
            published = getattr(e, "published", "") or getattr(e, "updated", "")
            items.append(Item(
                source_name=self.source.name,
                title=title,
                url=link,
                summary=summary,
                published=published,
            ))
        return items
