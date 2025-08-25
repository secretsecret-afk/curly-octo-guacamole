import os
import json
import asyncio
from datetime import datetime, timedelta
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton, ChatMemberUpdated
)
from aiogram.filters import Command, ChatMemberUpdatedFilter, MEMBER

load_dotenv(".env.prem")
API_TOKEN = os.getenv("BOT_TOKEN2")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))

MAIN_ADMIN_ID = int(os.getenv("MAIN_ADMIN_ID", "0"))
ADMINS = [int(x) for x in os.getenv("ADMINS", "").split(",") if x.strip()]

bot = Bot(token=API_TOKEN)
dp = Dispatcher()

REQUESTS_FILE = "requests.json"
CONFIG_FILE = "config.json"


def load_requests():
    if os.path.exists(REQUESTS_FILE):
        with open(REQUESTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {}

    now = datetime.now()
    to_delete = []
    for uid, req in data.items():
        if req.get("submitted_at"):
            submitted_at = datetime.fromisoformat(req["submitted_at"])
            if now - submitted_at > timedelta(days=3):
                to_delete.append(uid)

    for uid in to_delete:
        del data[uid]

    if to_delete:
        save_requests(data)

    return data


def save_requests(data):
    with open(REQUESTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def update_user_lang(user_id: str, lang: str):
    data = load_requests()
    if user_id not in data:
        data[user_id] = {
            "full_name": "",
            "username": "",
            "langs": [],
            "submitted_at": None,
        }
    if lang not in data[user_id]["langs"]:
        data[user_id]["langs"].append(lang)
    save_requests(data)
    return data[user_id]["langs"]


def can_submit(user_id: str):
    data = load_requests()
    if user_id not in data or not data[user_id]["submitted_at"]:
        return True
    submitted_at = datetime.fromisoformat(data[user_id]["submitted_at"])
    return datetime.now() - submitted_at > timedelta(days=3)


def save_request(user, langs):
    data = load_requests()
    data[str(user.id)] = {
        "full_name": user.full_name,
        "username": user.username or "",
        "langs": langs,
        "submitted_at": datetime.now().isoformat(),
    }
    save_requests(data)


def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"price": "9$"}


def save_config(config):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


@dp.message(Command("start"))
async def send_welcome(message: Message):
    langs = update_user_lang(str(message.from_user.id), message.from_user.language_code or "unknown")
    print(f"[LOG] Пользователь {message.from_user.id} языки: {','.join(langs)}")

    config = load_config()
    price = config["price"]

    caption = (
        "Добро пожаловать! Я платёжный бот Gene's Land!\n\n"
        "Здесь вы можете приобрести Premium-версию Gene Brawl!\n\n"
        "Gene Premium Ultimate выдается навсегда.\n"
        "(Нажмите на товар, чтобы узнать подробности)"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"🫣 Premium - {price}", callback_data="premium")],
            [InlineKeyboardButton(text="🩼 Поддержка", url="https://t.me/genepremiumsupportbot")],
        ]
    )
    await message.answer(caption, reply_markup=keyboard)


@dp.message(Command("setprice"))
async def set_price(message: Message):
    if message.from_user.id != MAIN_ADMIN_ID:
        await message.answer("❌ У вас нет прав для изменения цены")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Использование: /setprice 15$")
        return

    new_price = args[1]
    config = load_config()
    config["price"] = new_price
    save_config(config)
    await message.answer(f"✅ Цена изменена на {new_price}")


@dp.callback_query(F.data == "premium")
async def process_premium(callback: CallbackQuery):
    await callback.answer()

    config = load_config()
    price = config["price"]

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Описание", url="https://t.me/GenePremium/6")],
            [InlineKeyboardButton(text="🇷🇺 Картой", callback_data="pay_card")],
            [InlineKeyboardButton(text="🌎 Crypto (@send) (0%)", callback_data="pay_crypto")],
            [InlineKeyboardButton(text="⭐ Telegram Stars", callback_data="pay_stars")],
        ]
    )
    await callback.message.answer(f"Вы выбрали Premium за {price}\n\nВыберите способ оплаты:", reply_markup=keyboard)


@dp.callback_query(F.data.in_(["pay_card", "pay_crypto", "pay_stars"]))
async def ask_screenshots(callback: CallbackQuery):
    user = callback.from_user
    langs = update_user_lang(str(user.id), user.language_code or "unknown")
    print(f"[LOG] Пользователь {user.id} языки: {','.join(langs)}")

    if not can_submit(str(user.id)):
        await callback.message.answer("Вы уже подавали заявку, ожидайте одобрения ✅")
        return

    save_request(user, langs)

    instruction = (
        "Привет! Наша система сочла ваш аккаунт подозрительным. "
        "Для покупки Gene Premium, мы обязаны убедиться в вас.\n"
        "📸 Отправьте скриншоты ваших первых сообщений в:\n"
        "• Brawl Stars Datamines | Чат\n"
        "• Gene's Land чат\n\n"
        "А также (по желанию) фото прошитого 4G модема.\n\n"
        "⏳ Срок одобрения заявки ~3 дня."
    )
    await callback.message.answer(instruction)


@dp.message()
async def handle_submission(message: Message):
    user = message.from_user
    langs = update_user_lang(str(user.id), user.language_code or "unknown")

    if not can_submit(str(user.id)):
        await message.answer("Вы уже подавали заявку, ожидайте одобрения ✅")
        return

    save_request(user, langs)

    username = f"@{user.username}" if user.username else "—"
    header = f"{user.full_name} | id {user.id} | {username} | Языки: {','.join(langs)}\nСообщение:"

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{user.id}")]]
    )

    if message.text:
        await bot.send_message(ADMIN_CHAT_ID, f"{header}\n{message.text}", reply_markup=keyboard)
    elif message.photo:
        await bot.send_photo(ADMIN_CHAT_ID, photo=message.photo[-1].file_id, caption=header, reply_markup=keyboard)
    elif message.document:
        await bot.send_document(ADMIN_CHAT_ID, document=message.document.file_id, caption=header, reply_markup=keyboard)


@dp.callback_query(F.data.startswith("reject_"))
async def reject_request(callback: CallbackQuery):
    if callback.from_user.id not in ADMINS and callback.from_user.id != MAIN_ADMIN_ID:
        await callback.answer("❌ У вас нет прав для отклонения")
        return

    user_id = callback.data.split("_")[1]
    await callback.answer("Заявка отклонена ❌")
    await callback.message.edit_reply_markup(reply_markup=None)
    try:
        await bot.send_message(user_id, "❌ Ваша заявка на Gene Premium отклонена.")
    except:
        pass


@dp.chat_member(ChatMemberUpdatedFilter(member_status_changed=MEMBER))
async def on_added(event: ChatMemberUpdated):
    chat = event.chat
    if chat.id != ADMIN_CHAT_ID:
        try:
            await bot.send_message(chat.id, "❌ Этот бот работает только в личных сообщениях и админ-чате.")
            await bot.leave_chat(chat.id)
            print(f"[LOG] Бот автоматически вышел из чата {chat.id}")
        except Exception as e:
            print(f"[ERROR] Не удалось выйти из чата {chat.id}: {e}")


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
