"""Telegram-бот фид-агента: пост-дайджест с кнопками и разворотом «Подробнее».

Зачем отдельный бот, а не шов notify: notify умеет только ОТПРАВЛЯТЬ текст, а тут нужны
инлайн-кнопки и приём нажатий (callback) — [Подробнее] разворачивает новость в тему,
[👍]/[👎] копят сигнал о вкусе. Бот же по расписанию сам гоняет прогон (сбор → фильтр по
профилю → русская выжимка) и шлёт дайджест.

Токен — из окружения FEED_BOT_TOKEN (в бою наполняет брокер `secret` из Vaultwarden,
через EnvironmentFile systemd). В коде токена нет.
"""

from __future__ import annotations

import asyncio
import logging
import os
from html import escape

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.types import CallbackQuery, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from . import config
from .models import Enriched
from .pipeline import collect, enrich
from .storage import Storage
from .summarizer import make_summarizer

log = logging.getLogger("feed-agent.bot")

# Часы прогонов (локальное время бокса, UTC). 9/14/20 UTC = 12/17/23 МСК.
SCHEDULE_HOURS = "9,14,20"


# --- синхронная работа с БД/сетью (вызывается через asyncio.to_thread) ---

def _prepare_digest() -> list[Enriched]:
    """Один прогон конвейера (сбор → фильтр → обогащение) и выборка готового к отправке.
    Возвращает обогащённые новости (данные, без соединения с БД). Доставку/пометку —
    делает бот после успешной отправки."""
    settings = config.load_settings()
    sources = config.load_sources()
    profile = config.load_profile()
    storage = Storage()
    try:
        collect(storage, sources)
        storage.purge_old(settings.history_days)
        enrich(storage, settings, make_summarizer(settings), profile)
        return storage.enriched_for_digest(settings.digest_max_items)
    finally:
        storage.close()


def _mark_delivered(uids: list[str]) -> None:
    storage = Storage()
    try:
        storage.mark_delivered(uids)
    finally:
        storage.close()


def _get_enriched(uid: str) -> Enriched | None:
    storage = Storage()
    try:
        return storage.get_enriched(uid)
    finally:
        storage.close()


def _react(uid: str, reaction: str) -> None:
    storage = Storage()
    try:
        storage.add_reaction(uid, reaction)
    finally:
        storage.close()


# --- сборка сообщений ---

def _digest_message(items: list[Enriched]) -> tuple[str, object]:
    """Текст дайджеста + инлайн-клавиатура (ряд кнопок на каждую новость)."""
    lines = [f"🗞 <b>Дайджест</b> · {len(items)}"]
    kb = InlineKeyboardBuilder()
    for n, e in enumerate(items, 1):
        lines.append(f"\n<b>{n}. {escape(e.ru_title)}</b>\n{escape(e.ru_summary)}")
        kb.row(
            InlineKeyboardButton(text=f"{n} 📖 Подробнее", callback_data=f"det:{e.uid}"),
            InlineKeyboardButton(text="👍", callback_data=f"up:{e.uid}"),
            InlineKeyboardButton(text="👎", callback_data=f"down:{e.uid}"),
        )
    return "\n".join(lines), kb.as_markup()


def _detail_text(e: Enriched) -> str:
    """Развёрнутый разбор новости для «Подробнее»."""
    parts = [f"<b>{escape(e.ru_title)}</b>", "", escape(e.ru_summary)]
    if e.ru_takeaways:
        parts += ["", "<b>Главное:</b>"] + [f"• {escape(t)}" for t in e.ru_takeaways]
    if e.ru_conclusion:
        parts += ["", f"<b>Вывод:</b> {escape(e.ru_conclusion)}"]
    parts += ["", f"<i>{escape(e.source_name)}</i> · <a href=\"{escape(e.url, quote=True)}\">оригинал</a>"]
    return "\n".join(parts)


# --- хэндлеры и планировщик ---

def build_dispatcher(chat_id: int, thread_id: int | None) -> Dispatcher:
    dp = Dispatcher()

    @dp.callback_query(F.data.startswith("det:"))
    async def on_detail(cb: CallbackQuery) -> None:
        uid = cb.data.split(":", 1)[1]
        e = await asyncio.to_thread(_get_enriched, uid)
        if not e:
            await cb.answer("Новость не найдена", show_alert=False)
            return
        text = _detail_text(e)
        try:
            if e.image_url and len(text) <= 1024:
                await cb.bot.send_photo(chat_id, photo=e.image_url, caption=text,
                                        message_thread_id=thread_id)
            else:
                if e.image_url:
                    try:
                        await cb.bot.send_photo(chat_id, photo=e.image_url,
                                                message_thread_id=thread_id)
                    except Exception:
                        pass  # битая картинка — не мешаем тексту
                await cb.bot.send_message(chat_id, text, message_thread_id=thread_id,
                                          disable_web_page_preview=True)
        except Exception as ex:
            log.warning("detail send failed: %s", ex)
            await cb.answer("Не удалось отправить", show_alert=False)
            return
        await cb.answer("Развернул ниже")

    @dp.callback_query(F.data.startswith("up:") | F.data.startswith("down:"))
    async def on_react(cb: CallbackQuery) -> None:
        action, uid = cb.data.split(":", 1)
        await asyncio.to_thread(_react, uid, "up" if action == "up" else "down")
        await cb.answer("👍 учтено" if action == "up" else "👎 учтено")

    return dp


async def send_digest(bot: Bot, chat_id: int, thread_id: int | None) -> int:
    """Прогнать конвейер и отправить дайджест с кнопками. Возвращает число отправленных."""
    items = await asyncio.to_thread(_prepare_digest)
    if not items:
        log.info("digest: нечего слать")
        return 0
    text, markup = _digest_message(items)
    await bot.send_message(chat_id, text, message_thread_id=thread_id,
                           reply_markup=markup, disable_web_page_preview=True)
    await asyncio.to_thread(_mark_delivered, [e.uid for e in items])
    log.info("digest: отправлено %d", len(items))
    return len(items)


async def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = os.environ.get("FEED_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Нет FEED_BOT_TOKEN в окружении (наполняется из Vaultwarden через secret).")

    settings = config.load_settings()
    chat_id = int(settings.target_chat)
    thread_id = int(settings.target_thread) if settings.target_thread else None

    bot = Bot(token, default=DefaultBotProperties(parse_mode="HTML"))
    dp = build_dispatcher(chat_id, thread_id)

    # Расписание прогонов внутри бота (он всегда на связи для callback'ов).
    scheduler = AsyncIOScheduler()
    scheduler.add_job(send_digest, CronTrigger(hour=SCHEDULE_HOURS, minute=0),
                      args=[bot, chat_id, thread_id], id="digest")
    scheduler.start()
    log.info("бот запущен; расписание часов (UTC): %s", SCHEDULE_HOURS)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
