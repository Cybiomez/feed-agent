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
import time
from html import escape

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InputMediaPhoto, Message)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from . import config
from .models import Enriched
from .pipeline import collect, enrich
from .storage import Storage
from .summarizer import make_summarizer

log = logging.getLogger("feed-agent.bot")

# Рассылка 3×/день (МСК = UTC+3, без переходов): 07:30 и 13:30 — с лимитом; 17:30 —
# последний прогон дня, БЕЗ лимита (выдаёт всю очередь, чтобы ничего не потерять).
DIGEST_HOURS_UTC = "4,10"      # 07:30, 13:30 МСК — с предохранителем
FINAL_HOUR_UTC = "14"          # 17:30 МСК — финальный, без лимита
DIGEST_MINUTE = 30
# Частый сбор в базу (дёшево, без модели) — чтобы посты не «уехали» из t.me/s/ между рассылками.
COLLECT_MINUTE = 5             # каждый час в :05


# --- синхронная работа с БД/сетью (вызывается через asyncio.to_thread) ---

def _prepare_digest(unlimited: bool = False) -> tuple[list[Enriched], dict]:
    """Прогон конвейера (сбор → свежесть → фильтр → обогащение) и выборка готового к отправке.
    unlimited=True — обработать всю очередь (финальный прогон дня). Возвращает (новости, статы),
    статы = {enriched, dropped, stale, pending} для строки видимости очереди."""
    settings = config.load_settings()
    sources = config.load_sources()
    profile = config.load_profile()
    storage = Storage()
    try:
        collect(storage, sources)
        storage.purge_old(settings.history_days)
        st = enrich(storage, settings, make_summarizer(settings), profile, unlimited=unlimited)
        items = storage.enriched_for_digest(1000)
        st["pending"] = storage.pending_count()
        return items, st
    finally:
        storage.close()


def _collect_only() -> int:
    """Только собрать новые посты в базу (частый дешёвый заход, без модели/рассылки)."""
    settings = config.load_settings()
    storage = Storage()
    try:
        n = collect(storage, config.load_sources())
        storage.purge_old(settings.history_days)
        return n
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

def _sources_html(e: Enriched) -> str:
    """Строка источников: каждое название — ссылка на оригинальный пост/статью."""
    parts = []
    for l in (e.source_links or []):
        name, url = l.get("name", ""), l.get("url", "")
        if name and url:
            parts.append(f'<a href="{escape(url, quote=True)}">{escape(name)}</a>')
    if parts:
        return "Источники: " + " · ".join(parts)
    return f"<i>{escape(e.sources or e.source_name)}</i>"


def _item_caption(e: Enriched) -> str:
    """Основное сообщение новости: чистый заголовок + строка ключевых цифр + источники.
    Для обновления уже показанной темы — пометка 🔄."""
    mark = "🔄 " if e.is_update else ""
    parts = [f"{mark}<b>{escape(e.ru_title)}</b>"]
    if e.ru_key:
        parts.append(escape(e.ru_key))
    parts.append(_sources_html(e))
    return "\n".join(parts)


def _item_keyboard(e: Enriched):
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="📖 Подробнее", callback_data=f"det:{e.uid}"),
        InlineKeyboardButton(text="👍", callback_data=f"up:{e.uid}"),
        InlineKeyboardButton(text="👎", callback_data=f"down:{e.uid}"),
    )
    return kb.as_markup()


def _detail_text(e: Enriched) -> str:
    """Развёрнутый разбор для «Подробнее»: полное описание + тейки + вывод + источники."""
    parts = [f"<b>{escape(e.ru_title)}</b>"]
    if e.ru_summary:
        parts += ["", escape(e.ru_summary)]
    if e.ru_takeaways:
        parts += ["", "<b>Главное:</b>"] + [f"• {escape(t)}" for t in e.ru_takeaways]
    if e.ru_conclusion:
        parts += ["", f"<b>Вывод:</b> {escape(e.ru_conclusion)}"]
    parts += ["", _sources_html(e)]
    if e.video:
        parts.append(f'🎬 <a href="{escape(e.video, quote=True)}">видео</a>')
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
                     bot_username: str, bot_id: int, owner_id: int = 0) -> Dispatcher:
    dp = Dispatcher()

    def _is_owner(user) -> bool:
        return not owner_id or (user is not None and user.id == owner_id)

    @dp.message(F.text | F.caption)
    async def on_command(msg: Message) -> None:
        """Команда срабатывает, только если к боту обратились ЯВНО (тег или /cmd@бот),
        это известная команда И зовёт владелец. Ответ идёт ТУДА, где вызвали."""
        if not _addressed_to_me(msg, bot_username, bot_id):
            return
        if not _is_owner(msg.from_user):
            return  # чужой — молчим
        if _command_word(msg.text or msg.caption or "") != "news":
            return
        dst_chat, dst_thread = msg.chat.id, msg.message_thread_id
        await msg.answer("Собираю дайджест…")
        n = await send_digest(msg.bot, dst_chat, dst_thread, final=False)
        if n == 0:
            await msg.answer("Пока нечего слать — свежих новостей по профилю нет.")

    @dp.callback_query(F.data.startswith("det:"))
    async def on_detail(cb: CallbackQuery) -> None:
        if not _is_owner(cb.from_user):
            await cb.answer("Только для владельца", show_alert=False)
            return
        uid = cb.data.split(":", 1)[1]
        e = await asyncio.to_thread(_get_enriched, uid)
        if not e:
            await cb.answer("Новость не найдена", show_alert=False)
            return
        # отвечаем в тот чат/тему, где нажали кнопку
        dc = cb.message.chat.id if cb.message else chat_id
        dt = cb.message.message_thread_id if cb.message else thread_id
        text = _detail_text(e)
        imgs = e.images or []
        try:
            if len(imgs) >= 2:
                media = []
                for idx, u in enumerate(imgs[:10]):
                    cap = text if (idx == 0 and len(text) <= 1024) else None
                    media.append(InputMediaPhoto(media=u, caption=cap, parse_mode="HTML"))
                await cb.bot.send_media_group(dc, media, message_thread_id=dt)
                if len(text) > 1024:
                    await cb.bot.send_message(dc, text, message_thread_id=dt,
                                              disable_web_page_preview=True)
            elif len(imgs) == 1 and len(text) <= 1024:
                await cb.bot.send_photo(dc, photo=imgs[0], caption=text, message_thread_id=dt)
            else:
                if imgs:
                    try:
                        await cb.bot.send_photo(dc, photo=imgs[0], message_thread_id=dt)
                    except Exception:
                        pass
                await cb.bot.send_message(dc, text, message_thread_id=dt,
                                          disable_web_page_preview=True)
        except Exception as ex:
            log.warning("detail send failed: %s", ex)
            try:
                await cb.bot.send_message(dc, text, message_thread_id=dt,
                                          disable_web_page_preview=True)
            except Exception:
                pass
            await cb.answer()
            return
        await cb.answer("Развернул ниже")

    @dp.callback_query(F.data.startswith("up:") | F.data.startswith("down:"))
    async def on_react(cb: CallbackQuery) -> None:
        if not _is_owner(cb.from_user):
            await cb.answer("Только для владельца", show_alert=False)
            return
        action, uid = cb.data.split(":", 1)
        await asyncio.to_thread(_react, uid, "up" if action == "up" else "down")
        await cb.answer("👍 учтено" if action == "up" else "👎 учтено")

    return dp


async def _deliver_item(bot: Bot, dc: int, dt: int | None, e: Enriched) -> bool:
    """Отправить одну новость с учётом flood-control (ждём Retry-After и повторяем) и битой
    картинки (шлём текстом). True — доставлено."""
    caption = _item_caption(e)
    kb = _item_keyboard(e)
    want_photo = bool(e.images) and len(caption) <= 1024
    for _ in range(5):
        try:
            if want_photo:
                await bot.send_photo(dc, photo=e.images[0], caption=caption,
                                     reply_markup=kb, message_thread_id=dt)
            else:
                await bot.send_message(dc, caption, reply_markup=kb,
                                       message_thread_id=dt, disable_web_page_preview=True)
            return True
        except TelegramRetryAfter as fl:              # флуд-лимит — ждём столько, сколько велят
            log.info("flood: жду %sс", fl.retry_after)
            await asyncio.sleep(fl.retry_after + 1)
        except TelegramBadRequest as ex:
            if want_photo:                            # чаще всего — битый URL картинки
                want_photo = False                    # повторим текстом
                continue
            log.warning("send %s failed: %s", e.uid, ex)
            return False
        except Exception as ex:
            log.warning("send %s failed: %s", e.uid, ex)
            return False
    return False


async def send_digest(bot: Bot, chat_id: int, thread_id: int | None, final: bool = False) -> int:
    """Прогнать конвейер и отправить КАЖДУЮ новость отдельным сообщением. final=True —
    финальный прогон дня: без лимита, выдаёт всю очередь. В конце — строка видимости очереди."""
    items, stats = await asyncio.to_thread(_prepare_digest, final)
    sent: list[str] = []
    for e in items:
        if await _deliver_item(bot, chat_id, thread_id, e):
            sent.append(e.uid)
        await asyncio.sleep(3.0)   # база между отправками (даже при неудаче — не долбим)
    if sent:
        await asyncio.to_thread(_mark_delivered, sent)

    # Строка видимости очереди — чтобы регулировать лимит по утру/дню.
    if sent or stats["pending"] or final:
        status = (f"📊 Показано: {len(sent)} · в очереди: {stats['pending']}"
                  f" · повторов отсеяно: {stats['repeat']} · обновлений: {stats['updates']}")
        status += ("\n✅ Финальный прогон дня — очередь выдана полностью." if final
                   else "\nОстаток уйдёт следующими прогонами (финальный в 17:30 отдаёт всё).")
        try:
            await bot.send_message(chat_id, status, message_thread_id=thread_id)
        except Exception:
            pass
    log.info("digest%s: отправлено %d, в очереди %d, повторов %d, обновлений %d",
             " (final)" if final else "", len(sent), stats["pending"],
             stats["repeat"], stats["updates"])
    return len(sent)


async def collect_job() -> None:
    """Частый дешёвый сбор в базу (без модели/рассылки) — чтобы посты не «уехали» из t.me/s/."""
    n = await asyncio.to_thread(_collect_only)
    log.info("collect: +%d новых", n)


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
    dp = build_dispatcher(chat_id, thread_id, me.username, me.id, settings.owner_id)
    log.info("бот @%s — /news по тегу и только от владельца (%s), ответ в чате вызова",
             me.username, settings.owner_id or "все")

    # Расписание внутри бота (он всегда на связи для callback'ов).
    scheduler = AsyncIOScheduler()
    # Частый сбор в базу (дёшево, без модели) — чтобы ничего не «уехало» из t.me/s/.
    scheduler.add_job(collect_job, CronTrigger(minute=COLLECT_MINUTE), id="collect",
                      max_instances=1, coalesce=True)
    # Утро/день — рассылка с лимитом.
    scheduler.add_job(send_digest, CronTrigger(hour=DIGEST_HOURS_UTC, minute=DIGEST_MINUTE),
                      args=[bot, chat_id, thread_id], kwargs={"final": False},
                      id="digest", max_instances=1, coalesce=True)
    # Вечер — финальный прогон, без лимита: выдаёт всю очередь.
    scheduler.add_job(send_digest, CronTrigger(hour=FINAL_HOUR_UTC, minute=DIGEST_MINUTE),
                      args=[bot, chat_id, thread_id], kwargs={"final": True},
                      id="digest_final", max_instances=1, coalesce=True)
    scheduler.start()
    log.info("бот запущен; сбор ежечасно (:%s), рассылка 07:30/13:30 (лимит) + 17:30 (вся очередь) МСК",
             COLLECT_MINUTE)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
