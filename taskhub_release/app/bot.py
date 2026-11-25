import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
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
