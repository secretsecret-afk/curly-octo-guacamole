import os
import json
import asyncio
import random
from datetime import datetime, timedelta
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton, ChatMemberUpdated
)
from aiogram.filters import Command, ChatMemberUpdatedFilter, MEMBER

# ---------------------- env ----------------------
load_dotenv(".env.prem")
API_TOKEN = os.getenv("BOT_TOKEN2")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
MAIN_ADMIN_ID = int(os.getenv("MAIN_ADMIN_ID", "0"))
ADMINS = [int(x) for x in os.getenv("ADMINS", "").split(",") if x.strip()]

if not API_TOKEN:
    raise RuntimeError("BOT_TOKEN2 не найден в .env.prem")
if ADMIN_CHAT_ID == 0:
    print("[WARN] ADMIN_CHAT_ID=0 — заявки не попадут в админ-чат. Проверь .env.prem")

bot = Bot(token=API_TOKEN)
dp = Dispatcher()

REQUESTS_FILE = "requests.json"
CONFIG_FILE = "config.json"

# ---------------------- JSON helpers ----------------------
def load_requests():
    if os.path.exists(REQUESTS_FILE):
        with open(REQUESTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {}

    # автоочистка старше 3 дней
    now = datetime.now()
    to_delete = []
    for uid, req in list(data.items()):
        ts = req.get("submitted_at")
        if ts:
            submitted_at = datetime.fromisoformat(ts)
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
    if lang and lang not in data[user_id]["langs"]:
        data[user_id]["langs"].append(lang)
    save_requests(data)
    return data[user_id]["langs"]


def can_start_new_request(user_id: str) -> bool:
    """
    Можно ли НАЧАТЬ НОВУЮ заявку (по кнопке оплаты).
    Да, если нет записи или прошло >3 дней.
    """
    data = load_requests()
    rec = data.get(user_id)
    if not rec or not rec.get("submitted_at"):
        return True
    submitted_at = datetime.fromisoformat(rec["submitted_at"])
    return datetime.now() - submitted_at > timedelta(days=3)


def has_active_request(user_id: str) -> bool:
    """
    Есть ли активная заявка (≤3 дней с момента старта).
    Нужна для пропуска сообщений пользователя в админ-чат.
    """
    data = load_requests()
    rec = data.get(user_id)
    if not rec or not rec.get("submitted_at"):
        return False
    submitted_at = datetime.fromisoformat(rec["submitted_at"])
    return datetime.now() - submitted_at <= timedelta(days=3)


def start_request(user, langs):
    """Стартуем/обновляем заявку и фиксируем время начала окна в 3 дня."""
    data = load_requests()
    data[str(user.id)] = {
        "full_name": user.full_name,
        "username": user.username or "",
        "langs": langs,
        "submitted_at": datetime.now().isoformat(),
    }
    save_requests(data)

# ---------------------- Config (price) ----------------------
def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"price": "9$"}  # дефолт


def save_config(config):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

# ---------------------- Handlers ----------------------
@dp.message(Command("start"))
async def send_welcome(message: Message):
    langs = update_user_lang(str(message.from_user.id), message.from_user.language_code or "unknown")
    print(f"[LOG] Пользователь {message.from_user.id} языки: {','.join(langs)}")

    price = load_config()["price"]
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

    new_price = args[1].strip()
    if not new_price:
        await message.answer("Укажите цену, например: 15$")
        return

    config = load_config()
    config["price"] = new_price
    save_config(config)
    await message.answer(f"✅ Цена изменена на {new_price}")


@dp.callback_query(F.data == "premium")
async def process_premium(callback: CallbackQuery):
    await callback.answer()
    price = load_config()["price"]
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

    # НОВОЕ: проверяем можно ли начать новую заявку
    if not can_start_new_request(str(user.id)):
        await callback.message.answer("Вы уже подавали заявку, ожидайте одобрения ✅")
        return

    # Стартуем заявку (открываем 3-дневное окно и сохраняем профиль)
    start_request(user, langs)

    # Сообщение о подготовке
    preparing_msg = await callback.message.answer("⏳ Подготавливаем для вас оплату...")

    # Случайная задержка 4234–10110 мс
    delay = random.randint(4234, 10110) / 1000
    await asyncio.sleep(delay)

    # Инструкция после задержки
    instruction = (
        "Наша система сочла ваш аккаунт подозрительным.\n"
        "Для покупки Gene Premium мы обязаны убедиться в вас.\n\n"
        "📸 Отправьте скриншоты ваших первых сообщений в:\n"
        "• Brawl Stars Datamines | Чат\n"
        "• Gene's Land чат\n\n"
        "А также (по желанию) фото прошитого 4G модема.\n\n"
        "⏳ Срок одобрения заявки ~3 дня."
    )
    await preparing_msg.edit_text(instruction)


@dp.message()
async def handle_submission(message: Message):
    """
    НОВОЕ: не блокируем пользователя сообщением «уже подавали».
    Если заявка активна — пересылаем всё в админ-чат.
    Если нет — просим начать через меню оплаты.
    """
    user = message.from_user
    update_user_lang(str(user.id), user.language_code or "unknown")

    if not has_active_request(str(user.id)):
        await message.answer("Чтобы подать заявку, выберите способ оплаты в меню /start и следуйте инструкции.")
        return

    username = f"@{user.username}" if user.username else "—"
    langs = ", ".join(load_requests().get(str(user.id), {}).get("langs", [])) or "—"
    header = f"{user.full_name} | id {user.id} | {username} | Языки: {langs}\nСообщение:"

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{user.id}")]]
    )

    # Пересылаем контент в админ-чат
    try:
        if message.text:
            await bot.send_message(ADMIN_CHAT_ID, f"{header}\n{message.text}", reply_markup=keyboard)
        elif message.photo:
            await bot.send_photo(ADMIN_CHAT_ID, photo=message.photo[-1].file_id, caption=header, reply_markup=keyboard)
        elif message.document:
            await bot.send_document(ADMIN_CHAT_ID, document=message.document.file_id, caption=header, reply_markup=keyboard)
        else:
            await bot.send_message(ADMIN_CHAT_ID, f"{header}\n[Неподдерживаемый тип сообщения]", reply_markup=keyboard)
        await message.answer("✅ Отправлено на проверку администраторам.")
    except Exception as e:
        print(f"[ERROR] Не удалось переслать сообщение в админ-чат: {e}")
        await message.answer("⚠️ Не удалось отправить администраторам. Попробуйте ещё раз позже.")


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
    except Exception as e:
        print(f"[WARN] Не удалось уведомить пользователя {user_id} об отклонении: {e}")


# ---- если бота добавили в чат (защита) ----
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


# ---------------------- MAIN ----------------------
async def main():
    print(f"[BOOT] ADMIN_CHAT_ID={ADMIN_CHAT_ID}, MAIN_ADMIN_ID={MAIN_ADMIN_ID}, ADMINS={ADMINS}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

