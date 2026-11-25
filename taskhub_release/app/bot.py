import logging
import os
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, WebAppInfo

log = logging.getLogger("taskhub.telegram")

async def run_bot():
    token = os.getenv("BOT_TOKEN", "").strip()
    url = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
    if not token:
        log.error("BOT_NOT_STARTED: BOT_TOKEN is missing")
        return
    if not url.startswith("https://"):
        log.error("BOT_NOT_STARTED: PUBLIC_URL must start with https://")
        return

    bot = Bot(token=token)
    dp = Dispatcher()

    @dp.message(CommandStart())
    async def start(message: Message):
        keyboard = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="🚀 Открыть TaskHub", web_app=WebAppInfo(url=url))]],
            resize_keyboard=True,
        )
        await message.answer("Добро пожаловать в TaskHub! Нажми кнопку ниже, чтобы открыть приложение.", reply_markup=keyboard)

    try:
        me = await bot.get_me()
        log.info("TELEGRAM_BOT_CONNECTED username=@%s id=%s", me.username, me.id)
        log.info("TELEGRAM_POLLING_STARTED")
        await dp.start_polling(bot, handle_signals=False, close_bot_session=False)
    except Exception:
        log.exception("TELEGRAM_BOT_FATAL_ERROR")
        raise
    finally:
        await bot.session.close()
