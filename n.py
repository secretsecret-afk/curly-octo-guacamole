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
)
from aiogram.filters import Command

# ---------- Настройка логирования ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger(__name__)

# ---------- Загрузка токена ----------
load_dotenv(".env.prem")
API_TOKEN = os.getenv("BOT_TOKEN2")
if not API_TOKEN:
    logger.error("Переменная окружения BOT_TOKEN2 не задана. Проверь .env.prem")
    raise SystemExit(1)

# ---------- Инициализация бота и диспетчера ----------
bot = Bot(token=API_TOKEN)
dp = Dispatcher()


# ---------- Обработчики ----------
@dp.message(Command("start"))
async def send_welcome(message: Message):
    caption = (
        "Добро пожаловать! Я платёжный бот Gene's Land!\n\n"
        "Здесь вы можете приобрести Premium-версию Gene Brawl!\n\n"
        "Все товары из списка ниже выдаются навсегда.\n"
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
        # Если картинки нет — отправим просто текст (чтобы бот не падал)
        logger.warning("Файл изображения не найден: %s. Отправляем текст без фото.", image_path)
        await message.answer(caption, reply_markup=keyboard)
        return

    # Отправляем фото с подписью
    with open(image_path, "rb") as photo:
        await message.answer_photo(photo, caption=caption, reply_markup=keyboard)


@dp.callback_query(F.data == "premium")
async def process_premium(callback: CallbackQuery):
    # acknowledge the callback (so loading spinner on client disappears)
    await callback.answer()
    # Ответим в чат (если у callback есть сообщение)
    if callback.message:
        await callback.message.answer("Вы выбрали Premium за 500⭐")
    else:
        # На всякий случай, если нет callback.message
        await callback.bot.send_message(callback.from_user.id, "Вы выбрали Premium за 500⭐")


# ---------- Запуск / остановка ----------
async def main():
    logger.info("Запуск бота...")
    try:
        # start_polling блокирует текущую задачу, пока не будет отменено
        await dp.start_polling(bot)
    finally:
        logger.info("Остановка бота. Закрываем сессии...")
        # Закрываем клиентскую сессию aiohttp у Bot
        try:
            await bot.session.close()
        except Exception as e:
            logger.exception("Ошибка при закрытии bot.session: %s", e)
        # Закрываем сам объект бота на всякий случай
        try:
            await bot.close()
        except Exception:
            # bot.close может быть уже выполнен выше, игнорируем
            pass
        logger.info("Завершено.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Завершение по сигналу остановки.")
