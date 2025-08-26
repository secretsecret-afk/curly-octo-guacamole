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

REQUESTS_FILE = "requests.json"
CONFIG_FILE = "config.json"
WELCOME_IMAGE = "IMG_20250825_170645_742.jpg"

# ===================== STORAGE =====================
def _now() -> datetime:
    return datetime.now()

def load_requests() -> Dict[str, dict]:
    if not os.path.exists(REQUESTS_FILE):
        return {}
    try:
        with open(REQUESTS_FILE, "r", encoding="utf-8") as f:
            txt = f.read().strip()
            data = json.loads(txt) if txt else {}
    except json.JSONDecodeError:
        print(f"[WARN] {REQUESTS_FILE} поврежден, создаем новый.")
        data = {}

    # автоочистка старше 3 дней (по started_at)
    now = _now()
    changed = False
    for uid, rec in list(data.items()):
        # Проверяем, что заявка не "вечная" (т.е. у нее есть дата начала)
        if rec.get("started_at"):
            started = rec.get("started_at")
            try:
                if started and now - datetime.fromisoformat(started) > timedelta(days=3):
                    del data[uid]
                    changed = True
            except (ValueError, TypeError):
                # Удаляем записи с некорректной датой
                del data[uid]
                changed = True
    if changed:
        save_requests(data)
    return data


def save_requests(data: Dict[str, dict]) -> None:
    with open(REQUESTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def update_user_lang(user_id: str, lang: str) -> List[str]:
    data = load_requests()
    rec = data.get(user_id) or {
        "full_name": "",
        "username": "",
        "langs": [],
        "started_at": None,
        "submitted": False,
        "has_seen_instructions": False, # [ИЗМЕНЕНО] Поле для запоминания
    }
    if lang and lang not in rec["langs"]:
        rec["langs"].append(lang)
    data[user_id] = rec
    save_requests(data)
    return rec["langs"]

def start_request(user, langs: List[str]) -> None:
    data = load_requests()
    user_id_str = str(user.id)
    # [ИЗМЕНЕНО] Сохраняем флаг, если он уже был, чтобы не показывать задержку повторно
    existing_record = data.get(user_id_str, {})
    has_seen = existing_record.get("has_seen_instructions", False)

    data[user_id_str] = {
        "full_name": user.full_name,
        "username": user.username or "",
        "langs": langs,
        "started_at": _now().isoformat(),
        "submitted": False,
        "has_seen_instructions": has_seen,
    }
    save_requests(data)

def mark_submitted(user_id: str) -> None:
    data = load_requests()
    if user_id in data:
        data[user_id]["submitted"] = True
        save_requests(data)

def can_start_new_request(user_id: str) -> bool:
    """Можно ли начать новый процесс (клик по способу оплаты)."""
    data = load_requests()
    rec = data.get(user_id)
    if not rec:
        return True
    # Если заявка есть, но она не была отправлена, разрешаем начать заново
    if not rec.get("submitted"):
        return True
    if not rec.get("started_at"):
        return True
    started = datetime.fromisoformat(rec["started_at"])
    return _now() - started > timedelta(days=3)


def has_active_request(user_id: str) -> bool:
    """Можно ли сейчас прислать СВОЁ ЕДИНСТВЕННОЕ сообщение-заявку."""
    data = load_requests()
    rec = data.get(user_id)
    if not rec or not rec.get("started_at"):
        return False
    if rec.get("submitted"):
        return False
    started = datetime.fromisoformat(rec["started_at"])
    return _now() - started <= timedelta(days=3)

# ===================== CONFIG (цена) =====================
def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"price": "9$"}

def save_config(config: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

# ===================== HELPERS =====================
async def ensure_private_and_autoleave(message: Message) -> bool:
    """
    Возвращает True, если это ЛС. Если группа/канал — бот ливает (если это не ADMIN_CHAT_ID) и возвращает False.
    """
    if message.chat.type in ("group", "supergroup", "channel"):
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

# ===================== ALBUM MIDDLEWARE (aiogram 3.x) =====================
class AlbumMiddleware(BaseMiddleware):
    def __init__(self, wait: float = 0.35):
        super().__init__()
        self.wait = wait
        self._buffer: Dict[str, List[Message]] = defaultdict(list)
        self._locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def __call__(
        self,
        handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: Dict[str, Any]
    ) -> Any:
        if not isinstance(event, Message) or not event.media_group_id:
            return await handler(event, data)

        group_id = str(event.media_group_id)
        async with self._locks[group_id]:
            self._buffer[group_id].append(event)
            await asyncio.sleep(self.wait)
            messages = self._buffer.pop(group_id, [])
            if not messages:
                return

            messages.sort(key=lambda m: m.message_id)
            data["album"] = messages
            return await handler(messages[0], data)

dp.message.middleware(AlbumMiddleware())

# ===================== HANDLERS =====================
@dp.message(Command("start"))
async def send_welcome(message: Message):
    if not await ensure_private_and_autoleave(message):
        return

    update_user_lang(str(message.from_user.id), message.from_user.language_code or "unknown")

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

    if os.path.exists(WELCOME_IMAGE):
        try:
            photo = FSInputFile(WELCOME_IMAGE)
            await message.answer_photo(photo=photo, caption=caption, reply_markup=keyboard)
        except Exception as e:
            print(f"[WARN] Не удалось отправить локальную картинку: {e}")
            await message.answer(caption, reply_markup=keyboard)
    else:
        await message.answer(caption, reply_markup=keyboard)

@dp.message(Command("setprice"))
async def set_price(message: Message):
    if message.chat.id != ADMIN_CHAT_ID or message.from_user.id != MAIN_ADMIN_ID:
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        await message.answer("Использование: /setprice 15$")
        return

    new_price = args[1].strip()
    cfg = load_config()
    cfg["price"] = new_price
    save_config(cfg)
    await message.answer(f"✅ Цена изменена на {new_price}")

# ---- Кнопки в ЛС ----
@dp.callback_query(F.data == "premium")
async def process_premium(callback: CallbackQuery):
    if callback.message.chat.type != "private": return
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
    if callback.message.chat.type != "private": return
    await callback.answer()

    user = callback.from_user
    user_id_str = str(user.id)
    
    if not can_start_new_request(user_id_str):
        await callback.message.answer("Вы уже подавали заявку, ожидайте одобрения ✅")
        return

    langs = update_user_lang(user_id_str, user.language_code or "unknown")
    start_request(user, langs) # Создаем или обновляем запись о начале заявки

    instruction = (
        "Наша система сочла ваш аккаунт подозрительным.\n"
        "Для покупки Gene Premium мы обязаны убедиться в вас.\n\n"
        "📸 Отправьте скриншоты ваших первых сообщений в:\n"
        "• Brawl Stars Datamines | Чат\n"
        "• Gene's Land чат\n\n"
        "А также (по желанию) фото прошитого 4G модема.\n\n"
        "⏳ Срок одобрения заявки ~3 дня."
    )

    # [ИЗМЕНЕНО] Проверяем, видел ли пользователь инструкцию раньше
    data = load_requests()
    user_record = data.get(user_id_str, {})
    has_seen = user_record.get("has_seen_instructions", False)

    if not has_seen:
        # Если видит в первый раз - показываем задержку
        preparing_msg = await callback.message.answer("⏳ Подготавливаем для вас оплату...")
        await asyncio.sleep(random.randint(4234, 10110) / 1000)
        await preparing_msg.edit_text(instruction)
        
        # Помечаем, что пользователь увидел инструкцию
        if user_id_str in data:
            data[user_id_str]["has_seen_instructions"] = True
            save_requests(data)
    else:
        # Если уже видел - отправляем мгновенно
        await callback.message.answer(instruction)


# ---- Кнопки в админ-чате ----
@dp.callback_query(F.data.startswith("reject_"))
async def reject_request(callback: CallbackQuery):
    if callback.message.chat.id != ADMIN_CHAT_ID: return
    if callback.from_user.id not in ADMINS and callback.from_user.id != MAIN_ADMIN_ID:
        await callback.answer("❌ Нет прав")
        return

    user_id = callback.data.split("_", 1)[1]
    
    # [ИЗМЕНЕНО] Удаляем заявку из базы, чтобы пользователь мог подать новую
    data = load_requests()
    if user_id in data:
        del data[user_id]
        save_requests(data)

    await callback.answer("Заявка отклонена и удалена ❌")
    try:
        # [ИЗМЕНЕНО] Сообщаем пользователю, что он может попробовать снова
        await bot.send_message(user_id, "❌ Ваша заявка на Gene Premium отклонена. Вы можете попробовать подать её снова, выбрав способ оплаты в /start.")
    except Exception as e:
        print(f"[WARN] Не удалось уведомить пользователя {user_id}: {e}")
    
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

# ===================== ПРИЁМ ЗАЯВОК =====================
@dp.message()
async def handle_submission(message: Message, album: Optional[List[Message]] = None):
    if not await ensure_private_and_autoleave(message): return

    user = message.from_user
    user_id = str(user.id)

    if not has_active_request(user_id):
        return # Молча игнорируем, если нет активного процесса подачи заявки

    langs = update_user_lang(user_id, user.language_code or "unknown")
    header = make_header(user, langs)

    admin_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{user.id}")]]
    )

    try:
        # ===== АЛЬБОМ =====
        if album:
            original_caption = album[0].caption or ""
            full_caption = f"{header}\n\n{original_caption}".strip()
            
            photo_video_builder = MediaGroupBuilder()
            doc_builder = MediaGroupBuilder()

            for m in album:
                if m.photo: photo_video_builder.add_photo(m.photo[-1].file_id)
                elif m.video: photo_video_builder.add_video(m.video.file_id)
                elif m.document: doc_builder.add_document(m.document.file_id)
            
            caption_sent = False
            media_sent = False

            # [ИСПРАВЛЕНО] Проверяем, есть ли что-то в билдере, перед отправкой
            pv_built = photo_video_builder.build()
            if pv_built:
                pv_built[0].caption = full_caption
                await bot.send_media_group(ADMIN_CHAT_ID, media=pv_built)
                caption_sent = True
                media_sent = True

            doc_built = doc_builder.build()
            if doc_built:
                if not caption_sent:
                    doc_built[0].caption = full_caption
                await bot.send_media_group(ADMIN_CHAT_ID, media=doc_built)
                media_sent = True
            
            if media_sent:
                await bot.send_message(chat_id=ADMIN_CHAT_ID, text="Управление заявкой:", reply_markup=admin_keyboard)
                await message.answer("✅ Ваша заявка отправлена администраторам. Ожидайте ответа.")
                mark_submitted(user_id)
            
            return

        # ===== ОДИНОЧНОЕ СООБЩЕНИЕ =====
        cap = f"{header}\n\n{message.caption or ''}".strip()
        if message.photo: await bot.send_photo(ADMIN_CHAT_ID, photo=message.photo[-1].file_id, caption=cap, reply_markup=admin_keyboard)
        elif message.document: await bot.send_document(ADMIN_CHAT_ID, document=message.document.file_id, caption=cap, reply_markup=admin_keyboard)
        elif message.video: await bot.send_video(ADMIN_CHAT_ID, video=message.video.file_id, caption=cap, reply_markup=admin_keyboard)
        elif message.text: await bot.send_message(ADMIN_CHAT_ID, f"{header}\n\n{message.text}", reply_markup=admin_keyboard)
        else:
            await bot.send_message(ADMIN_CHAT_ID, f"{header}\n[Неподдерживаемый тип сообщения]", reply_markup=admin_keyboard)

        await message.answer("✅ Ваша заявка отправлена администраторам. Ожидайте ответа.")
        mark_submitted(user_id)

    except Exception as e:
        print(f"[ERROR] Не удалось отправить в админ-чат: {e}")
        await message.answer("⚠️ Не удалось отправить администраторам. Попробуйте ещё раз позже.")

# ===================== АВТО-ЛИВ ИЗ ЧАТОВ =====================
@dp.chat_member(ChatMemberUpdatedFilter(member_status_changed=MEMBER))
async def on_added(event: ChatMemberUpdated):
    if event.chat.id != ADMIN_CHAT_ID:
        try:
            await bot.leave_chat(event.chat.id)
            print(f"[LOG] Автовыход из чата {event.chat.id}")
        except Exception as e:
            print(f"[ERROR] Не удалось выйти из чата {event.chat.id}: {e}")

@dp.message(F.chat.type.in_(["group", "supergroup", "channel"]))
async def leave_any_group(message: Message):
    if message.chat.id != ADMIN_CHAT_ID:
        try:
            await bot.leave_chat(message.chat.id)
            print(f"[LOG] Вышел из чата по сообщению {message.chat.id}")
        except Exception as e:
            print(f"[ERROR] Не удалось выйти из чата {message.chat.id}: {e}")

# ===================== MAIN =====================
async def main():
    print(f"[BOOT] ADMIN_CHAT_ID={ADMIN_CHAT_ID}, MAIN_ADMIN_ID={MAIN_ADMIN_ID}, ADMINS={ADMINS}")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())```
