import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from datetime import datetime, timezone
import hashlib
from .db import connect
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    CallbackQuery,
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

    async def open_app(message: Message, text: str, tab: str = "exchange", action: str | None = None):
        query=f"?tab={tab}"+(f"&action={action}" if action else "")
        app_link=WebAppInfo(url=url+query)
        keyboard=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Открыть TaskHub",web_app=app_link)]
        ])
        await message.answer(text,reply_markup=keyboard)

    @dp.message(CommandStart())
    async def start(message: Message):
        telegram_user=message.from_user
        with connect() as conn:
            conn.execute("""INSERT INTO users(telegram_id,username,first_name,role)
                VALUES(?,?,?,'helper') ON CONFLICT(telegram_id) DO UPDATE SET
                username=excluded.username,first_name=excluded.first_name""",
                (telegram_user.id,telegram_user.username,telegram_user.first_name or ""))
        await open_app(
            message,
            "Привет! Я Квестовик, бот TaskHub. 👋\n\nTaskHub — площадка, где заказчики публикуют разовые задания и находят исполнителей, а исполнители ищут подработку, откликаются на задания и общаются с заказчиком в чате сделки.\n\nПодойдёт тем, кому нужно делегировать небольшие задачи, найти помощь для проекта или команды, а также тем, кто хочет брать разовые заказы и зарабатывать. Можно пользоваться биржей самостоятельно или работать в командных комнатах.\n\nОплата за работу происходит напрямую между заказчиком и исполнителем.\n\nОткрой приложение, чтобы выбрать интересующие направления и начать пользоваться TaskHub.",
            "exchange"
        )

    @dp.message(Command("tasks"))
    async def tasks_command(message: Message):
        await open_app(message,"Открывай ленту заданий TaskHub:")

    @dp.message(Command("mine"))
    async def mine_command(message: Message):
        await open_app(message,"Твои задания и отклики находятся в профиле TaskHub:","profile")

    @dp.message(Command("chats"))
    async def chats_command(message: Message):
        await open_app(message,"Открой свои диалоги в TaskHub:","chats")

    @dp.message(Command("menu"))
    async def menu_command(message: Message):
        await open_app(message,"Меню TaskHub:")

    @dp.callback_query(F.data.startswith("sub:off:"))
    async def unsubscribe_category(callback: CallbackQuery):
        parts=callback.data.split(":")
        if len(parts)!=3 or not parts[2].isdigit():
            await callback.answer("Некорректная категория",show_alert=True)
            return
        category_id=int(parts[2])
        with connect() as conn:
            account=conn.execute("SELECT id FROM users WHERE telegram_id=?",(callback.from_user.id,)).fetchone()
            if account:
                conn.execute("DELETE FROM subscriptions WHERE user_id=? AND category_id=?",(account["id"],category_id))
                category=conn.execute("SELECT name FROM categories WHERE id=?",(category_id,)).fetchone()
            else:
                category=None
        await callback.answer("Подписка отключена")
        await callback.message.answer("🔕 Подписка отключена: "+(category["name"] if category else "категория"))

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
