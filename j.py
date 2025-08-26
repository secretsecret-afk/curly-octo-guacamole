import os
import json
import asyncio
import random
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, List, Optional

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.types import (
    Message, CallbackQuery, ChatMemberUpdated,
    InlineKeyboardMarkup, InlineKeyboardButton,
    InputMediaPhoto, InputMediaDocument,
    FSInputFile
)
from aiogram.filters import Command, ChatMemberUpdatedFilter, MEMBER
from aiogram.utils.media_group import MediaGroupBuilder

# ===================== ENV =====================
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

# [НОВАЯ ЛОГИКА] Хранилища для отложенной отправки сообщений
user_message_buffers = defaultdict(list)
user_submission_tasks = {}
user_submission_locks = defaultdict(asyncio.Lock)

REQUESTS_FILE = "requests.json"
CONFIG_FILE = "config.json"
WELCOME_IMAGE = "IMG_20250825_170645_742.jpg"

# ===================== STORAGE =====================
def _now() -> datetime:
    return datetime.now()

def load_requests() -> Dict[str, dict]:
    if not os.path.exists(REQUESTS_FILE): return {}
    try:
        with open(REQUESTS_FILE, "r", encoding="utf-8") as f:
            txt = f.read().strip()
            return json.loads(txt) if txt else {}
    except (json.JSONDecodeError, IOError):
        print(f"[WARN] {REQUESTS_FILE} поврежден, создаем новый.")
        return {}

def save_requests(data: Dict[str, dict]) -> None:
    try:
        with open(REQUESTS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except IOError as e:
        print(f"[ERROR] Не удалось сохранить {REQUESTS_FILE}: {e}")

def update_user_lang(user_id: str, lang: str) -> List[str]:
    data = load_requests()
    rec = data.get(user_id) or {
        "full_name": "", "username": "", "langs": [],
        "started_at": None, "submitted": False, "has_seen_instructions": False,
    }
    if lang and lang not in rec["langs"]:
        rec["langs"].append(lang)
    data[user_id] = rec
    save_requests(data)
    return rec["langs"]

def start_request(user, langs: List[str]) -> None:
    data = load_requests()
    user_id_str = str(user.id)
    existing_record = data.get(user_id_str, {})
    has_seen = existing_record.get("has_seen_instructions", False)
    data[user_id_str] = {
        "full_name": user.full_name, "username": user.username or "", "langs": langs,
        "started_at": _now().isoformat(), "submitted": False, "has_seen_instructions": has_seen,
    }
    save_requests(data)

def mark_submitted(user_id: str) -> None:
    data = load_requests()
    if user_id in data:
        data[user_id]["submitted"] = True
        save_requests(data)

def has_active_request(user_id: str) -> bool:
    data = load_requests()
    rec = data.get(user_id)
    return not (not rec or not rec.get("started_at") or rec.get("submitted"))

# ===================== CONFIG, HELPERS и т.д. (без изменений) =====================
def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f: return json.load(f)
        except (json.JSONDecodeError, IOError): pass
    return {"price": "9$"}

def save_config(config: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f: json.dump(config, f, ensure_ascii=False, indent=2)

async def ensure_private_and_autoleave(message: Message) -> bool:
    if message.chat.type != "private":
        if message.chat.id != ADMIN_CHAT_ID:
            try:
                await bot.leave_chat(message.chat.id)
                print(f"[LOG] Вышел из чата {message.chat.id}")
            except Exception as e:
                print(f"[ERROR] Не удалось выйти из чата {message.chat.id}: {e}")
        return False
    return True

def make_header(user: "aiogram.types.User", langs: List[str]) -> str:
    username = f"@{user.username}" if user.username else "—"
    langs_str = ", ".join(langs) or "—"
    return f"{user.full_name} | id {user.id} | {username} | Языки: {langs_str}"

# ===================== HANDLERS (без AlbumMiddleware) =====================
@dp.message(Command("start"))
async def send_welcome(message: Message):
    if not await ensure_private_and_autoleave(message): return
    update_user_lang(str(message.from_user.id), message.from_user.language_code or "unknown")
    price = load_config()["price"]
    caption = (
        "Добро пожаловать! Я платёжный бот Gene's Land!\n\n"
        "Здесь вы можете приобрести Premium-версию Gene Brawl!\n\n"
        "Gene Premium Ultimate выдается навсегда.\n"
        "(Нажмите на товар, чтобы узнать подробности)"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🫣 Premium - {price}", callback_data="premium")],
        [InlineKeyboardButton(text="🩼 Поддержка", url="https://t.me/genepremiumsupportbot")],
    ])
    if os.path.exists(WELCOME_IMAGE):
        try:
            await message.answer_photo(photo=FSInputFile(WELCOME_IMAGE), caption=caption, reply_markup=keyboard)
        except Exception: await message.answer(caption, reply_markup=keyboard)
    else: await message.answer(caption, reply_markup=keyboard)

@dp.message(Command("setprice"))
async def set_price(message: Message):
    if message.chat.id != ADMIN_CHAT_ID or message.from_user.id != MAIN_ADMIN_ID: return
    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        await message.answer("Использование: /setprice 15$")
        return
    new_price = args[1].strip()
    cfg = load_config(); cfg["price"] = new_price; save_config(cfg)
    await message.answer(f"✅ Цена изменена на {new_price}")

@dp.callback_query(F.data == "premium")
async def process_premium(callback: CallbackQuery):
    await callback.answer()
    price = load_config()["price"]
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Описание", url="https://t.me/GenePremium/6")],
        [InlineKeyboardButton(text="🇷🇺 Картой", callback_data="pay_card")],
        [InlineKeyboardButton(text="🌎 Crypto (@send) (0%)", callback_data="pay_crypto")],
        [InlineKeyboardButton(text="⭐ Telegram Stars", callback_data="pay_stars")],
    ])
    await callback.message.answer(f"Вы выбрали Premium за {price}\n\nВыберите способ оплаты:", reply_markup=keyboard)

@dp.callback_query(F.data.in_(["pay_card", "pay_crypto", "pay_stars"]))
async def ask_screenshots(callback: CallbackQuery):
    await callback.answer()
    user, user_id_str = callback.from_user, str(callback.from_user.id)
    
    # Проверяем, есть ли уже отправленная заявка
    data = load_requests()
    if data.get(user_id_str) and data[user_id_str].get("submitted"):
        await callback.message.answer("Вы уже подавали заявку, ожидайте одобрения ✅")
        return

    langs = update_user_lang(user_id_str, user.language_code or "unknown")
    start_request(user, langs)
    instruction = (
        "Наша система сочла ваш аккаунт подозрительным...\n\n"
        "📸 Отправьте скриншоты ваших первых сообщений в:\n"
        "• Brawl Stars Datamines | Чат\n"
        "• Gene's Land чат\n\n"
        "⏳ Срок одобрения заявки ~3 дня."
    )
    user_record = data.get(user_id_str, {})
    if not user_record.get("has_seen_instructions", False):
        preparing_msg = await callback.message.answer("⏳ Подготавливаем для вас оплату...")
        await asyncio.sleep(random.uniform(4.2, 10.1))
        await preparing_msg.edit_text(instruction)
        if user_id_str in data:
            data[user_id_str]["has_seen_instructions"] = True
            save_requests(data)
    else:
        await callback.message.answer(instruction)

@dp.callback_query(F.data.startswith("reject_"))
async def reject_request(callback: CallbackQuery):
    await callback.answer("Заявка отклонена и удалена ❌", show_alert=True)
    if callback.from_user.id not in ADMINS and callback.from_user.id != MAIN_ADMIN_ID: return
    
    user_id = callback.data.split("_", 1)[1]
    data = load_requests()
    if user_id in data:
        del data[user_id]
        save_requests(data)
    
    try: await bot.send_message(user_id, "❌ Ваша заявка отклонена. Можете попробовать подать её снова.")
    except Exception as e: print(f"[WARN] Не удалось уведомить {user_id}: {e}")
    try: await callback.message.edit_reply_markup(reply_markup=None)
    except Exception: pass

# ===================== ПРИЁМ ЗАЯВОК (НОВАЯ, НАДЕЖНАЯ ЛОГИКА) =====================
@dp.message()
async def handle_submission(message: Message):
    if not await ensure_private_and_autoleave(message): return
    
    user_id = str(message.from_user.id)
    
    # Блокируем, чтобы избежать хаоса при одновременной обработке
    async with user_submission_locks[user_id]:
        if not has_active_request(user_id):
            return
        
        # Добавляем текущее сообщение в буфер пользователя
        user_message_buffers[user_id].append(message)
        
        # Если таймер для этого пользователя еще не запущен, запускаем его
        if user_id not in user_submission_tasks:
            task = asyncio.create_task(_process_user_submission(message.from_user))
            user_submission_tasks[user_id] = task

async def _process_user_submission(user: "aiogram.types.User"):
    user_id = str(user.id)
    
    # Ждем 2 секунды, чтобы собрать все сообщения
    await asyncio.sleep(2)
    
    # Снова блокируем, чтобы безопасно забрать сообщения из буфера
    async with user_submission_locks[user_id]:
        messages_to_forward = user_message_buffers.pop(user_id, [])
        user_submission_tasks.pop(user_id, None) # Удаляем задачу
        
        if not messages_to_forward:
            return

        try:
            # 1. Пересылаем все собранные сообщения
            for msg in messages_to_forward:
                await bot.forward_message(
                    chat_id=ADMIN_CHAT_ID,
                    from_chat_id=user_id,
                    message_id=msg.message_id
                )
            
            # 2. Отправляем информацию о пользователе и кнопки управления
            langs = update_user_lang(user_id, user.language_code or "unknown")
            header = make_header(user, langs)
            admin_keyboard = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{user_id}")]]
            )
            await bot.send_message(ADMIN_CHAT_ID, text=header, reply_markup=admin_keyboard)
            
            # 3. Уведомляем пользователя и помечаем заявку
            await messages_to_forward[0].answer("✅ Ваша заявка отправлена администраторам. Ожидайте ответа.")
            mark_submitted(user_id)

        except Exception as e:
            print(f"[ERROR] Не удалось отправить заявку от {user_id} в админ-чат: {e}")
            await messages_to_forward[0].answer("⚠️ Не удалось отправить администраторам. Попробуйте ещё раз позже.")


# ===================== АВТО-ЛИВ ИЗ ЧАТОВ =====================
@dp.chat_member(ChatMemberUpdatedFilter(member_status_changed=MEMBER))
async def on_added(event: ChatMemberUpdated):
    if event.chat.id != ADMIN_CHAT_ID:
        try:
            await bot.leave_chat(event.chat.id)
        except Exception: pass

@dp.message(F.chat.type.in_(["group", "supergroup", "channel"]))
async def leave_any_group(message: Message):
    if message.chat.id != ADMIN_CHAT_ID:
        try:
            await bot.leave_chat(message.chat.id)
        except Exception: pass

# ===================== MAIN =====================
async def main():
    print(f"[BOOT] ADMIN_CHAT_ID={ADMIN_CHAT_ID}, MAIN_ADMIN_ID={MAIN_ADMIN_ID}, ADMINS={ADMINS}")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
