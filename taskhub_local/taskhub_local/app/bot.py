import asyncio, os
from aiogram import Bot,Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import Message,ReplyKeyboardMarkup,KeyboardButton,WebAppInfo
async def run_bot():
 token=os.getenv("BOT_TOKEN"); url=os.getenv("PUBLIC_URL","")
 if not token or not url.startswith("https://"): return
 bot=Bot(token); dp=Dispatcher()
 @dp.message(CommandStart())
 async def start(m:Message):
  kb=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🚀 Открыть TaskHub",web_app=WebAppInfo(url=url))]],resize_keyboard=True)
  await m.answer("Добро пожаловать в TaskHub!",reply_markup=kb)
 try: await dp.start_polling(bot)
 finally: await bot.session.close()
