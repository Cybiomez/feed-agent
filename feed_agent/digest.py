"""Сборка дайджеста в HTML — формат, который понимает шов notify (parse_mode=HTML).

Важно: собираем ровно столько пунктов, сколько влезает в одно сообщение Telegram
(лимит 4096 символов), и возвращаем список реально вошедших. Пайплайн помечает
«доставлено» только их — иначе обрезанные хвостом пункты потерялись бы (а мы не теряем).
Непоместившееся останется неотправленным и уйдёт следующим прогоном.
"""

from __future__ import annotations

from html import escape

from .config import Settings
from .models import Item, Score

# Telegram режет сообщение на 4096 символах — держим запас.
_MAX_LEN = 3900


def build_digest(pairs: list[tuple[Item, Score]], settings: Settings) -> tuple[str, list[tuple[Item, Score]]]:
    """Собрать дайджест из отобранных пар (новость, оценка).

    Возвращает (текст, вошедшие_пары). Вошедшие — те, что реально попали в сообщение
    (по лимиту символов); их и помечать доставленными."""
    head = f"<b>{escape(settings.title)}</b>"
    text = head
    included: list[tuple[Item, Score]] = []

    for item, score in pairs:
        title = escape(item.title)
        url = escape(item.url, quote=True)
        src = escape(item.source_name)
        reason = escape(score.reason or "")
        n = len(included) + 1
        block = (
            f"{n}. <a href=\"{url}\">{title}</a>\n"
            f"<i>{src} · {score.score}</i>" + (f" — {reason}" if reason else "")
        )
        candidate = text + "\n\n" + block
        if len(candidate) > _MAX_LEN:
            break                     # дальше не влезает — остальное уйдёт следующим прогоном
        text = candidate
        included.append((item, score))

    # Заголовок с фактическим числом вошедших (проставляем после подсчёта).
    text = text.replace(head, f"{head} · {len(included)}", 1)
    return text, included
