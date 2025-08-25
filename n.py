import os
import asyncio
import logging
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
)
from aiogram.filters import Command

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

load_dotenv(".env.prem")
API_TOKEN = os.getenv("BOT_TOKEN2")
if not API_TOKEN:
    logger.error("BOT_TOKEN2 не найден в .env.prem")
    raise SystemExit(1)

bot = Bot(token=API_TOKEN)
dp = Dispatcher()


@dp.message(Command("start"))
async def send_welcome(message: Message):
    caption = (
        "Добро пожаловать! Я платёжный бот Gene's Land!\n\n"
        "Здесь вы можете приобрести Premium-версию Gene Brawl!\n\n"
        "Gene Premium Ultimate выдается навсегда.\n"
        "(Нажмите на товар, чтобы узнать подробности)"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🫣 Premium - 500stars", callback_data="premium")],
            [InlineKeyboardButton(text="🛠 Поддержка", url="https://t.me/YourSupportLink")],
        ]
    )

    image_path = "IMG_20250825_170645_742.jpg"
    if not os.path.isfile(image_path):
        await message.answer(caption, reply_markup=keyboard)
        return

    photo = FSInputFile(image_path)
    await message.answer_photo(photo, caption=caption, reply_markup=keyboard)


@dp.callback_query(F.data == "premium")
async def process_premium(callback: CallbackQuery):
    await callback.answer()
    if callback.message:
        await callback.message.answer("Вы выбрали Premium за 500⭐")
    else:
        await callback.bot.send_message(callback.from_user.id, "Вы выбрали Premium за 500⭐")


async def main():
    logger.info("Запуск бота...")
    try:
        await dp.start_polling(bot)
    finally:
        logger.info("Остановка бота...")
        try:
            await bot.session.close()
        except Exception as e:
            logger.exception("Ошибка при закрытии bot.session: %s", e)
        try:
            await bot.close()
        except Exception:
            pass
        logger.info("Завершено.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Завершение по сигналу остановки.")
