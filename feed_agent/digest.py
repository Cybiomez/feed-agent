"""Сборка дайджеста в HTML — формат, который понимает шов notify (parse_mode=HTML)."""

from __future__ import annotations

from html import escape

from .config import Settings
from .models import Item, Score

# Telegram режет сообщение на 4096 символах — держим запас.
_MAX_LEN = 3900


def build_digest(pairs: list[tuple[Item, Score]], settings: Settings) -> str:
    """Собрать текст дайджеста из отобранных пар (новость, оценка)."""
    head = f"<b>{escape(settings.title)}</b> · отобрано {len(pairs)}"
    blocks = [head]
    for n, (item, score) in enumerate(pairs, 1):
        title = escape(item.title)
        url = escape(item.url, quote=True)
        src = escape(item.source_name)
        reason = escape(score.reason or "")
        line = (
            f"{n}. <a href=\"{url}\">{title}</a>\n"
            f"<i>{src} · {score.score}</i>" + (f" — {reason}" if reason else "")
        )
        blocks.append(line)

    text = "\n\n".join(blocks)
    if len(text) > _MAX_LEN:
        text = text[:_MAX_LEN].rsplit("\n\n", 1)[0] + "\n\n…"
    return text
