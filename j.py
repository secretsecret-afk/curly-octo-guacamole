import os
import asyncio
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from dotenv import load_dotenv

# Загружаем переменные из .env.prem
load_dotenv(".env.prem")

API_TOKEN = os.getenv("BOT_TOKEN2")

bot = Bot(token=API_TOKEN)
dp = Dispatcher()


# Обработчик команды /start
@dp.message(Command("start"))
async def send_welcome(message: Message):
    caption = (
        "Добро пожаловать! Я платёжный бот Gene's Land!\n\n"
        "Здесь вы можете приобрести Premium-версию Gene Brawl!\n\n"
        "Все товары из списка ниже выдаются навсегда.\n"
        "(Нажмите на товар, чтобы узнать подробности)"
    )

    # Кнопки
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🫣 Premium - 500stars", callback_data="premium")],
            [InlineKeyboardButton(text="🛠 Поддержка", url="https://t.me/YourSupportLink")]
        ]
    )

    # Отправляем фото с подписью
    with open("IMG_20250825_170645_742.jpg", "rb") as photo:
        await message.answer_photo(photo, caption=caption, reply_markup=keyboard)


# Обработчик нажатия кнопки "premium"
@dp.callback_query(F.data == "premium")
async def process_premium(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer("Вы выбрали Premium за 500⭐")


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
