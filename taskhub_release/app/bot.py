import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandStart
from datetime import datetime, timezone
import hashlib
from .db import connect
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)

log = logging.getLogger("taskhub.telegram")


async def start_bot():
    """Start polling and configure Telegram-native Mini App entry points."""
    token = os.getenv("BOT_TOKEN", "").strip()
    url = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
    if not token:
        log.error("TELEGRAM_BOT_DISABLED: BOT_TOKEN is missing")
        return None, None
    if not url.startswith("https://"):
        log.error("TELEGRAM_BOT_DISABLED: PUBLIC_URL must be an https:// URL")
        return None, None

    bot = Bot(token=token)
    dp = Dispatcher()
    web_app = WebAppInfo(url=url)

    @dp.message(Command("connect"))
    async def connect_room(message: Message):
        if message.chat.type not in ("group", "supergroup"):
            await message.answer("Команду /connect нужно отправить в групповой Telegram-чате.")
            return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) != 2:
            await message.answer("Формат: /connect код. Получи одноразовый код в настройках комнаты TaskHub.")
            return
        token_hash = hashlib.sha256(parts[1].strip().encode()).hexdigest()
        try:
            with connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                code = conn.execute(
                    """SELECT c.room_id,c.created_by,c.expires_at,u.telegram_id
                       FROM telegram_room_link_codes c
                       JOIN users u ON u.id=c.created_by
                       JOIN room_members rm ON rm.room_id=c.room_id AND rm.user_id=c.created_by
                       WHERE c.code_hash=? AND rm.left_at IS NULL
                         AND rm.room_role IN ('owner','admin')""", (token_hash,)
                ).fetchone()
                if not code:
                    await message.answer("Код не найден или уже использован. Создай новый в TaskHub.")
                    return
                expires = datetime.fromisoformat(code["expires_at"])
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                if expires <= datetime.now(timezone.utc):
                    conn.execute("DELETE FROM telegram_room_link_codes WHERE room_id=?", (code["room_id"],))
                    await message.answer("Код истёк. Создай новый в TaskHub.")
                    return
                if int(message.from_user.id) != int(code["telegram_id"]):
                    await message.answer("Подключить группу может только администратор, создавший код в TaskHub.")
                    return
                conn.execute(
                    """INSERT INTO telegram_room_links(room_id,chat_id,chat_title,connected_by)
                       VALUES(?,?,?,?) ON CONFLICT(room_id) DO UPDATE SET
                       chat_id=excluded.chat_id,chat_title=excluded.chat_title,
                       connected_by=excluded.connected_by,connected_at=CURRENT_TIMESTAMP""",
                    (code["room_id"], message.chat.id, message.chat.title or "Telegram-группа", code["created_by"]),
                )
                conn.execute("DELETE FROM telegram_room_link_codes WHERE room_id=?", (code["room_id"],))
            await message.answer("Группа подключена к TaskHub. Сюда будут приходить уведомления о задачах.")
        except Exception:
            log.exception("TELEGRAM_ROOM_CONNECT_FAILED")
            await message.answer("Не удалось подключить группу. Создай новый код в TaskHub.")

    @dp.message(CommandStart())

    async def start(message: Message):
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🚀 Открыть TaskHub", web_app=web_app)]
            ]
        )
        await message.answer(
            "Добро пожаловать в TaskHub! Нажми кнопку ниже, чтобы открыть приложение.",
            reply_markup=keyboard,
        )

    try:
        me = await bot.get_me()
        log.info("TELEGRAM_BOT_CONNECTED username=@%s id=%s", me.username, me.id)

        # Set the Telegram chat-menu button as a real Web App button.
        # This is not the same as a normal URL button: Telegram supplies initData.
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="TaskHub", web_app=web_app)
            )
            log.info("TELEGRAM_WEBAPP_MENU_BUTTON_CONFIGURED")
        except Exception:
            log.exception("TELEGRAM_WEBAPP_MENU_BUTTON_CONFIGURATION_FAILED")

        polling_task = asyncio.create_task(
            dp.start_polling(bot, handle_signals=False, close_bot_session=False),
            name="telegram-polling",
        )
        await asyncio.sleep(0)
        if polling_task.done():
            exc = polling_task.exception()
            if exc:
                raise exc
            raise RuntimeError("Telegram polling stopped immediately")
        log.info("TELEGRAM_POLLING_STARTED")
        return bot, polling_task
    except Exception:
        log.exception("TELEGRAM_BOT_START_FAILED")
        await bot.session.close()
        raise
