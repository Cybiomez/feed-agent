"""Сборщик публичных Telegram-каналов через веб-превью t.me/s/<канал>.

Без бота и без API: публичная лента канала доступна по https://t.me/s/<username> — берём
последние посты (текст + ссылка). Для приватных каналов не годится (нужен был бы аккаунт);
для публичных — самый простой путь. Пост в канале обычно самодостаточен, поэтому текст
поста используется и как «полный текст» для выжимки (article-fetch не нужен).
"""

from __future__ import annotations

import re

import lxml.html
import requests

from ..models import Item
from .base import Collector

_UA = "Mozilla/5.0 (compatible; feed-agent/1.0)"


def _username(raw: str) -> str:
    """Достать username канала из разных форматов: @name, name, https://t.me/name, t.me/s/name."""
    s = raw.strip()
    s = re.sub(r"^https?://", "", s)
    s = re.sub(r"^t\.me/", "", s)
    s = re.sub(r"^s/", "", s)
    s = s.lstrip("@").strip("/")
    return s.split("/")[0]


class TelegramCollector(Collector):
    """Читает публичный Telegram-канал (последние посты) через t.me/s/."""

    def fetch(self) -> list[Item]:
        user = _username(self.source.url)
        if not user:
            return []
        try:
            r = requests.get(f"https://t.me/s/{user}", headers={"User-Agent": _UA}, timeout=15)
            if r.status_code != 200:
                return []
            doc = lxml.html.fromstring(r.text)
        except Exception:
            return []

        items: list[Item] = []
        for p in doc.xpath("//div[@data-post]"):
            post = p.get("data-post")  # вида "channel/123"
            tnodes = p.xpath(".//div[contains(@class,'tgme_widget_message_text')]")
            if not post or not tnodes:
                continue  # медиа-пост без текста — пропускаем
            text = re.sub(r"\s+\n", "\n", tnodes[0].text_content()).strip()
            if not text:
                continue
            # Заголовок = первая строка/первые ~120 символов (у постов нет отдельного заголовка).
            first = text.split("\n", 1)[0].strip()
            title = (first if len(first) >= 20 else text.replace("\n", " "))[:120]
            items.append(Item(
                source_name=self.source.name,
                title=title,
                url=f"https://t.me/{post}",
                summary=text[:1500],
            ))
        return items
