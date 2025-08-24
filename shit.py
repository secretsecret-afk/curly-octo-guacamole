import os
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.types import Message
from aiogram import F
from dotenv import load_dotenv

# Загружаем переменные окружения
load_dotenv(dotenv_path=".env.geneprem")
API_TOKEN = os.getenv("BOT_TOKEN2")

bot = Bot(token=API_TOKEN)
dp = Dispatcher()

@dp.message(F.text == "/start")
async def send_welcome(message: Message):
    lang = message.from_user.language_code
    await message.answer(
        f"Привет, {message.from_user.first_name}!\n"
        f"У тебя язык интерфейса: {lang}"
    )

async def main():
    dp.startup.register(lambda _: print("Бот запущен!"))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
