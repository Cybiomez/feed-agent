"""Сборщик публичных Telegram-каналов через веб-превью t.me/s/<канал>.

Без бота и без API: публичная лента канала доступна по https://t.me/s/<username> — берём
последние посты (текст, время, ссылки на картинки/видео). Картинки НЕ качаем: тянем только
их URL из той же страницы (нулевая доп. нагрузка), отдавать их будет Telegram.
Пост в канале обычно самодостаточен, поэтому его текст используется и как «полный текст».
"""

from __future__ import annotations

import re
from datetime import datetime

import lxml.html
import requests

from ..models import Item
from .base import Collector

_UA = "Mozilla/5.0 (compatible; feed-agent/1.0)"
_IMG_RE = re.compile(r"background-image:url\('([^']+)'\)")


def _username(raw: str) -> str:
    s = raw.strip()
    s = re.sub(r"^https?://", "", s)
    s = re.sub(r"^t\.me/", "", s)
    s = re.sub(r"^s/", "", s)
    return s.lstrip("@").strip("/").split("/")[0]


def _published_ts(node) -> tuple[str, float]:
    """(ISO, epoch) времени поста из <time datetime=…>; (пусто, 0) если нет."""
    dt = node.xpath(".//time/@datetime")
    if not dt:
        return "", 0.0
    try:
        d = datetime.fromisoformat(dt[0])
        return dt[0], d.timestamp()
    except ValueError:
        return dt[0], 0.0


def _images(node) -> list[str]:
    """URL картинок поста (из background-image превью) — без скачивания."""
    out = []
    for style in node.xpath(".//a[contains(@class,'tgme_widget_message_photo_wrap')]/@style"):
        m = _IMG_RE.search(style)
        if m:
            out.append(m.group(1))
    return out


def _video(node) -> str:
    """Ссылка на видео поста, если есть (прямое видео или плеер-обёртка)."""
    v = node.xpath(".//video/@src") or node.xpath(
        ".//a[contains(@class,'tgme_widget_message_video_player')]/@href")
    return v[0] if v else ""


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
            post = p.get("data-post")  # "channel/123"
            tnodes = p.xpath(".//div[contains(@class,'tgme_widget_message_text')]")
            if not post or not tnodes:
                continue  # медиа-пост без текста — нечего суммировать
            text = re.sub(r"\s+\n", "\n", tnodes[0].text_content()).strip()
            if not text:
                continue
            first = text.split("\n", 1)[0].strip()
            title = (first if len(first) >= 20 else text.replace("\n", " "))[:120]
            iso, ts = _published_ts(p)
            items.append(Item(
                source_name=self.source.name,
                title=title,
                url=f"https://t.me/{post}",
                summary=text[:1500],
                published=iso,
                published_ts=ts,
                images=_images(p),
                video=_video(p),
            ))
        return items
