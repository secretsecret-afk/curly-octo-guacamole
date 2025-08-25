from aiogram import Bot, Dispatcher
from aiogram.types import Message
import asyncio, os
from dotenv import load_dotenv

load_dotenv(dotenv_path=".env.prem")
API_TOKEN = os.getenv("BOT_TOKEN2")

bot = Bot(token=API_TOKEN)
dp = Dispatcher()

@dp.message()
async def start_handler(message: Message):
    if message.text == "/start":
        lang = message.from_user.language_code
        await message.answer(f"Привет, {message.from_user.first_name}!\nЯзык интерфейса: {lang}")

async def on_startup():
    print("Бот запущен!")

async def main():
    dp.startup.register(on_startup)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
