import asyncio
from os import getenv
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, WebAppInfo

load_dotenv()
TOKEN = getenv("BOT_TOKEN", "")
URL = getenv("PUBLIC_URL", "")

async def main():
    if not TOKEN:
        raise RuntimeError("Set BOT_TOKEN in .env")
    if not URL.startswith("https://"):
        print("PUBLIC_URL is not HTTPS yet. Bot will start, but Mini App button won't be available until you set a tunnel URL.")
    bot = Bot(TOKEN)
    dp = Dispatcher()

    @dp.message(CommandStart())
    async def start(message: Message):
        if URL.startswith("https://"):
            keyboard = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🚀 Открыть TaskHub", web_app=WebAppInfo(url=URL))]], resize_keyboard=True)
            await message.answer("Добро пожаловать в TaskHub!\nОткрывай приложение кнопкой ниже.", reply_markup=keyboard)
        else:
            await message.answer("TaskHub запущен локально. Сначала добавь HTTPS-ссылку в PUBLIC_URL (.env), чтобы открыть Mini App.")

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
