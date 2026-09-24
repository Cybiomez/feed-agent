"""Сборка компактного русского дайджеста в HTML (формат шва notify).

Каждый пункт — самодостаточная суть на русском: заголовок + 2–3 предложения. Ссылка на
оригинал — сноской (открывать не обязательно). Расширенный разбор и картинка — по
«Подробнее» (v2.2). Собираем ровно столько, сколько влезает в одно сообщение Telegram,
и возвращаем реально вошедшие — их и помечать доставленными (иначе хвост потерялся бы).
"""

from __future__ import annotations

from html import escape

from .config import Settings
from .models import Enriched

_MAX_LEN = 3900


def build_digest(items: list[Enriched], settings: Settings) -> tuple[str, list[Enriched]]:
    """Собрать дайджест из обогащённых новостей. Возвращает (текст, вошедшие)."""
    head = f"<b>{escape(settings.title)}</b>"
    text = head
    included: list[Enriched] = []

    for it in items:
        n = len(included) + 1
        title = escape(it.ru_title or "")
        summary = escape(it.ru_summary or "")
        src = escape(it.source_name)
        url = escape(it.url, quote=True)
        block = (
            f"<b>{n}. {title}</b>\n"
            f"{summary}\n"
            f"<i>{src}</i> · <a href=\"{url}\">оригинал</a>"
        )
        candidate = text + "\n\n" + block
        if len(candidate) > _MAX_LEN:
            break                     # остальное уйдёт следующим прогоном
        text = candidate
        included.append(it)

    text = text.replace(head, f"{head} · {len(included)}", 1)
    return text, included
