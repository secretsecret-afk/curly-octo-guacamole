import os
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from dotenv import load_dotenv

# Загружаем переменные окружения из .env.prem
load_dotenv(dotenv_path=".env.prem")
API_TOKEN = os.getenv("BOT_TOKEN")

bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)

@dp.message_handler(commands=["start"])
async def send_welcome(message: types.Message):
    lang = message.from_user.language_code  # язык интерфейса Telegram
    await message.answer(
        f"Привет, {message.from_user.first_name}!\n"
        f"У тебя язык интерфейса: {lang}"
    )

if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True)
