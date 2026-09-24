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
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from . import config
from .models import Enriched
from .pipeline import collect, enrich
from .storage import Storage
from .summarizer import make_summarizer

log = logging.getLogger("feed-agent.bot")

# Прогоны 3 раза в день: 07:30, 13:30, 17:30 МСК = 04:30, 10:30, 14:30 UTC
# (бокс в UTC; МСК = UTC+3, без переходов на летнее время).
SCHEDULE_HOURS_UTC = "4,10,14"
SCHEDULE_MINUTE = 30


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
        return storage.enriched_for_digest(500)   # всё готовое; постранично отправит бот
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

def _digest_message(items: list[Enriched], start: int = 1) -> tuple[str, object]:
    """Текст одной страницы дайджеста + инлайн-клавиатура (ряд кнопок на каждую новость).
    start — номер первой новости на странице (для сквозной нумерации)."""
    lines = ["🗞 <b>Дайджест</b>"]
    kb = InlineKeyboardBuilder()
    for offset, e in enumerate(items):
        n = start + offset
        src = escape(e.sources or e.source_name)
        lines.append(f"\n<b>{n}. {escape(e.ru_title)}</b>\n{escape(e.ru_summary)}\n<i>{src}</i>")
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
    src = escape(e.sources or e.source_name)
    parts += ["", f"<i>{src}</i> · <a href=\"{escape(e.url, quote=True)}\">оригинал</a>"]
    return "\n".join(parts)


# --- хэндлеры и планировщик ---

def _addressed_to_me(msg: Message, username: str, bot_id: int) -> bool:
    """Обратились ли к нам ЯВНО: тег @username, text_mention на id, или команда вида
    /cmd@username. Голая команда (/cmd) и упоминание другого бота — не про нас."""
    text = msg.text or msg.caption or ""
    ents = msg.entities or msg.caption_entities or []
    want = ("@" + username).lower()
    for e in ents:
        frag = text[e.offset:e.offset + e.length].lower()
        if e.type == "mention" and frag == want:
            return True
        if e.type == "bot_command" and frag.endswith(want):   # /news@наш_бот
            return True
        if e.type == "text_mention" and e.user and e.user.id == bot_id:
            return True
    return False


def _command_word(text: str) -> str:
    """Первое слово-команда сообщения без ведущего / и без @suffix (или пусто)."""
    for tok in text.split():
        if tok.startswith("/"):
            return tok[1:].split("@")[0].lower()
    return ""


def build_dispatcher(chat_id: int, thread_id: int | None,
                     bot_username: str, bot_id: int) -> Dispatcher:
    dp = Dispatcher()

    async def _deliver(msg: Message) -> None:
        await msg.answer("Собираю дайджест…")   # answer сам отвечает в ту же тему
        n = await send_digest(msg.bot, chat_id, thread_id)
        if n == 0:
            await msg.answer("Пока нечего слать — свежих новостей по профилю нет.")

    @dp.message(F.text | F.caption)
    async def on_command(msg: Message) -> None:
        """Команда срабатывает, только если к боту обратились ЯВНО (тег или /cmd@бот) И это
        известная команда. Пример: «@MyCeliumCharlieNewsBot /news» или «/news@…»."""
        if not _addressed_to_me(msg, bot_username, bot_id):
            return
        if _command_word(msg.text or msg.caption or "") == "news":
            await _deliver(msg)

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
    """Прогнать конвейер и отправить ВСЕ готовые новости постранично (ничего не режем).
    Возвращает число отправленных."""
    items = await asyncio.to_thread(_prepare_digest)
    if not items:
        log.info("digest: нечего слать")
        return 0
    page = max(1, config.load_settings().digest_max_items)
    sent: list[str] = []
    start = 1
    for i in range(0, len(items), page):
        chunk = items[i : i + page]
        text, markup = _digest_message(chunk, start=start)
        await bot.send_message(chat_id, text, message_thread_id=thread_id,
                               reply_markup=markup, disable_web_page_preview=True)
        sent += [e.uid for e in chunk]
        start += len(chunk)
        await asyncio.sleep(0.5)   # мягко к лимитам Telegram
    await asyncio.to_thread(_mark_delivered, sent)
    log.info("digest: отправлено %d (страниц %d)", len(items), (len(items) + page - 1) // page)
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
    me = await bot.get_me()
    dp = build_dispatcher(chat_id, thread_id, me.username, me.id)
    log.info("бот @%s — реагирует на упоминание (@%s) в сообщении", me.username, me.username)

    # Расписание прогонов внутри бота (он всегда на связи для callback'ов).
    scheduler = AsyncIOScheduler()
    scheduler.add_job(send_digest, CronTrigger(hour=SCHEDULE_HOURS_UTC, minute=SCHEDULE_MINUTE),
                      args=[bot, chat_id, thread_id], id="digest",
                      max_instances=1, coalesce=True)
    scheduler.start()
    log.info("бот запущен; прогоны 07:30/13:30/17:30 МСК (%s:%s UTC)",
             SCHEDULE_HOURS_UTC, SCHEDULE_MINUTE)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
