import os
import json
import asyncio
import random
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Union, Optional
from html import escape

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import (
    Message,
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
)
from aiogram.filters import Command, ChatMemberUpdatedFilter, MEMBER
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaDocument,
    InputMediaAudio,
)
import aiohttp
from aiogram.types import LabeledPrice, PreCheckoutQuery

# ===================== ENV (robust parsing for multiple IDs) =====================
load_dotenv(".env.prem")


def _parse_int_list(env_name: str) -> List[int]:
    raw = os.getenv(env_name, "")
    if raw is None:
        return []
    s = str(raw).strip()
    if not s:
        return []
    parts: List[int] = []
    for part in [p.strip() for p in s.replace(";", ",").replace(" ", ",").split(",")]:
        if not part:
            continue
        try:
            parts.append(int(part))
            continue
        except Exception:
            digits = "".join(ch for ch in part if ch.isdigit())
            if digits:
                try:
                    parts.append(int(digits))
                except Exception:
                    pass
    return parts


# Parse:
ADMIN_CHAT_IDS = _parse_int_list("ADMIN_CHAT_ID")  # можно несколько
ADMIN_CHAT_ID = ADMIN_CHAT_IDS[0] if ADMIN_CHAT_IDS else 0
MAIN_ADMIN_IDS = _parse_int_list("MAIN_ADMIN_ID")
ADMINS = _parse_int_list("ADMINS")
ADMIN_THREAD_IDS = _parse_int_list("ADMIN_THREAD_ID")      # optional topic ids for submissions (per admin chat)
ADMIN_LOG_THREAD_IDS = _parse_int_list("ADMIN_LOG_THREAD_ID")  # optional topic ids for logs (per admin chat)

# ADMIN_THREAD_NAMES: names separated by "||" (double pipe) to allow commas inside names.
ADMIN_THREAD_NAMES_RAW = os.getenv("ADMIN_THREAD_NAMES", "").strip()
ADMIN_THREAD_NAMES: List[str] = []
if ADMIN_THREAD_NAMES_RAW:
    ADMIN_THREAD_NAMES = [p.strip() for p in ADMIN_THREAD_NAMES_RAW.split("||")]

# Combined admin sets for permission checks
ALL_ADMINS_SET = set(ADMINS) | set(MAIN_ADMIN_IDS)

# Provider token for Stars invoices (optional)
PROVIDER_TOKEN_STARS = os.getenv("PROVIDER_TOKEN_STARS", "")

print(f"[ENV] ADMIN_CHAT_IDS={ADMIN_CHAT_IDS}, ADMIN_THREAD_IDS={ADMIN_THREAD_IDS}, ADMIN_THREAD_NAMES={ADMIN_THREAD_NAMES}, ADMIN_LOG_THREAD_IDS={ADMIN_LOG_THREAD_IDS}, MAIN_ADMIN_IDS={MAIN_ADMIN_IDS}, ADMINS={ADMINS}, PROVIDER_TOKEN_STARS_set={'yes' if PROVIDER_TOKEN_STARS else 'no'}")

# ===================== Bot init =====================
API_TOKEN = os.getenv("BOT_TOKEN2")
if not API_TOKEN:
    raise RuntimeError("BOT_TOKEN2 не найден в .env.prem")

bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Объект для блокировки одновременной обработки заявок от одного пользователя
user_submission_locks = defaultdict(asyncio.Lock)

REQUESTS_FILE = "requests.json"
CONFIG_FILE = "config.json"
WELCOME_IMAGE = "IMG_20250825_170645_742.jpg"
BANNED_FILE = "banned.json"
ADMIN_MAP_FILE = "admin_map.json"  # сохраняет маппинг "chat:msg" -> user_id
ADMIN_TOPICS_FILE = "admin_topics.json"  # сохраняет маппинг chat_id -> thread_id (созданные темы)
REJECTED_FILE = "rejected.json"  # сохраняет пользователей, которым отклонили заявку

# NEW: payments file
PAYMENTS_FILE = "payments.json"

# Buffers and tasks to collect messages sent by user within a short window
submission_buffers: Dict[str, List[Message]] = defaultdict(list)
collecting_tasks: Dict[str, asyncio.Task] = {}

# mapping admin chat+message -> user_id (ключ: "chat:msgid")
admin_message_to_user: Dict[str, int] = {}

# in-memory map of created topics (chat_id -> thread_id)
admin_topics_map: Dict[str, int] = {}

# in-memory rejected users set (loaded from REJECTED_FILE)
rejected_users: set = set()

# ===================== STORAGE & MAPS & BANS =====================


def _now() -> datetime:
    return datetime.now()


def load_requests() -> Dict[str, dict]:
    if not os.path.exists(REQUESTS_FILE):
        return {}
    try:
        with open(REQUESTS_FILE, "r", encoding="utf-8") as f:
            txt = f.read().strip()
            return json.loads(txt) if txt else {}
    except (json.JSONDecodeError, IOError):
        print(f"[WARN] {REQUESTS_FILE} поврежден или не читается, создаем новый.")
        return {}


def save_requests(data: Dict[str, dict]) -> None:
    try:
        with open(REQUESTS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except IOError as e:
        print(f"[ERROR] Не удалось сохранить {REQUESTS_FILE}: {e}")


def load_banned() -> List[int]:
    if not os.path.exists(BANNED_FILE):
        return []
    try:
        with open(BANNED_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
            return [int(x) for x in raw if x is not None]
    except Exception:
        return []


def save_banned(b: List[int]) -> None:
    try:
        with open(BANNED_FILE, "w", encoding="utf-8") as f:
            json.dump(b, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[WARN] Не удалось сохранить {BANNED_FILE}: {e}")


def ban_user_by_id(uid: int) -> None:
    b = load_banned()
    if uid not in b:
        b.append(uid)
        save_banned(b)


def unban_user_by_id(uid: int) -> None:
    b = load_banned()
    if uid in b:
        b.remove(uid)
        save_banned(b)


def is_banned(uid: Union[int, str]) -> bool:
    try:
        uid_int = int(uid)
    except Exception:
        return False
    return uid_int in load_banned()


def load_admin_map() -> Dict[str, int]:
    if not os.path.exists(ADMIN_MAP_FILE):
        return {}
    try:
        with open(ADMIN_MAP_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
            return {str(k): int(v) for k, v in (raw.items() if isinstance(raw, dict) else {})}
    except Exception:
        return {}


def save_admin_map(m: Dict[str, int]) -> None:
    try:
        with open(ADMIN_MAP_FILE, "w", encoding="utf-8") as f:
            json.dump(m, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[WARN] Не удалось сохранить {ADMIN_MAP_FILE}: {e}")


def load_admin_topics() -> Dict[str, int]:
    if not os.path.exists(ADMIN_TOPICS_FILE):
        return {}
    try:
        with open(ADMIN_TOPICS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
            return {str(k): int(v) for k, v in (raw.items() if isinstance(raw, dict) else {})}
    except Exception:
        return {}


def save_admin_topics(m: Dict[str, int]) -> None:
    try:
        with open(ADMIN_TOPICS_FILE, "w", encoding="utf-8") as f:
            json.dump(m, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[WARN] Не удалось сохранить {ADMIN_TOPICS_FILE}: {e}")


def load_rejected() -> set:
    if not os.path.exists(REJECTED_FILE):
        return set()
    try:
        with open(REJECTED_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
            return set(int(x) for x in raw if x is not None)
    except Exception:
        return set()


def save_rejected(s: set) -> None:
    try:
        with open(REJECTED_FILE, "w", encoding="utf-8") as f:
            json.dump(list(s), f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[WARN] Не удалось сохранить {REJECTED_FILE}: {e}")


def add_rejected(uid: int) -> None:
    global rejected_users
    rejected_users.add(int(uid))
    save_rejected(rejected_users)


def remove_rejected(uid: int) -> None:
    global rejected_users
    try:
        rejected_users.discard(int(uid))
        save_rejected(rejected_users)
    except Exception:
        pass


def clear_all_rejected() -> None:
    global rejected_users
    rejected_users.clear()
    save_rejected(rejected_users)


def _admin_map_key(chat_id: int, message_id: int) -> str:
    return f"{chat_id}:{message_id}"


def set_admin_map(chat_id: int, msg_id: int, user_id: int) -> None:
    key = _admin_map_key(chat_id, msg_id)
    admin_message_to_user[key] = user_id
    save_admin_map(admin_message_to_user)


def remove_admin_map_by_key(key: str) -> None:
    if key in admin_message_to_user:
        del admin_message_to_user[key]
        save_admin_map(admin_message_to_user)


def remove_admin_map(chat_id: int, msg_id: int) -> None:
    key = _admin_map_key(chat_id, msg_id)
    remove_admin_map_by_key(key)


# загрузим маппинг при старте
try:
    admin_message_to_user = load_admin_map()
except Exception:
    admin_message_to_user = {}

try:
    admin_topics_map = load_admin_topics()
except Exception:
    admin_topics_map = {}

try:
    rejected_users = load_rejected()
except Exception:
    rejected_users = set()

# ===================== REQUESTS / LANGS =====================


def update_user_lang(user_id: str, lang: str) -> List[str]:
    data = load_requests()
    rec = data.get(user_id) or {
        "full_name": "",
        "username": "",
        "langs": [],
        "started_at": None,
        "submitted": False,
        "has_seen_instructions": False,
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


def remove_request(user_id: str) -> None:
    data = load_requests()
    if user_id in data:
        del data[user_id]
        save_requests(data)


def can_start_new_request(user_id: str) -> bool:
    try:
        if is_banned(int(user_id)):
            return False
    except Exception:
        pass
    data = load_requests()
    rec = data.get(user_id)
    return not rec or not rec.get("submitted", False)


def has_active_request(user_id: str) -> bool:
    data = load_requests()
    rec = data.get(user_id)
    if not rec or not rec.get("started_at") or rec.get("submitted"):
        return False
    try:
        started = datetime.fromisoformat(rec["started_at"])
        return _now() - started <= timedelta(days=3)
    except (ValueError, TypeError):
        return False


# ===================== CONFIG (цена) =====================
def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    # default: keep $ sign in default for backward compatibility
    return {"price": "9$", "price_stars": 50}


def save_config(config: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


# ===================== HELPERS =====================


async def ensure_private_and_autoleave(message: Message) -> bool:
    if message.chat.type != "private":
        if message.chat.id not in ADMIN_CHAT_IDS:
            try:
                await bot.leave_chat(message.chat.id)
                print(f"[LOG] Вышел из чата {message.chat.id}")
            except Exception as e:
                print(f"[ERROR] Не удалось выйти из чата {message.chat.id}: {e}")
        return False
    return True


# ===================== PAYMENTS DB HELPERS =====================
def _ensure_payments_file():
    if not os.path.exists(PAYMENTS_FILE):
        try:
            with open(PAYMENTS_FILE, "w", encoding="utf-8") as f:
                json.dump([], f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[WARN] Не удалось создать {PAYMENTS_FILE}: {e}")


def _read_payments_sync():
    _ensure_payments_file()
    try:
        with open(PAYMENTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _write_payments_sync(data):
    tmp = PAYMENTS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, PAYMENTS_FILE)
    except Exception as e:
        print(f"[WARN] Не удалось записать {PAYMENTS_FILE}: {e}")


def _save_payment_sync(record):
    data = _read_payments_sync()
    for r in data:
        if r.get("charge_id") == record.get("charge_id"):
            return False
    data.append(record)
    _write_payments_sync(data)
    return True


def _mark_refunded_sync(charge_id: str):
    data = _read_payments_sync()
    changed = False
    for r in data:
        if r.get("charge_id") == charge_id and not r.get("refunded"):
            r["refunded"] = True
            r["refunded_at"] = datetime.utcnow().isoformat()
            changed = True
    if changed:
        _write_payments_sync(data)
    return changed


def _get_payment_by_charge_sync(charge_id: str):
    data = _read_payments_sync()
    for r in data:
        if r.get("charge_id") == charge_id:
            return r
    return None


# Async wrappers
async def init_payments_db():
    await asyncio.to_thread(_ensure_payments_file)


async def save_payment(user_id: int, charge_id: str, payload: str, amount: int, currency: str):
    record = {
        "user_id": user_id,
        "charge_id": charge_id,
        "payload": payload,
        "amount": amount,
        "currency": currency,
        "refunded": False,
        "created_at": datetime.utcnow().isoformat(),
        "refunded_at": None,
    }
    return await asyncio.to_thread(_save_payment_sync, record)


async def mark_refunded(charge_id: str):
    return await asyncio.to_thread(_mark_refunded_sync, charge_id)


async def get_payment_by_charge(charge_id: str):
    return await asyncio.to_thread(_get_payment_by_charge_sync, charge_id)


# ===================== Telegram API call: refundStarPayment =====================
async def refund_star_payment(user_id: int, telegram_payment_charge_id: str) -> dict:
    """
    Выполняет POST к Telegram Bot API refundStarPayment.
    Возвращает JSON-ответ (распакованный).
    """
    url = f"https://api.telegram.org/bot{API_TOKEN}/refundStarPayment"
    payload = {"user_id": user_id, "telegram_payment_charge_id": telegram_payment_charge_id}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=15) as resp:
                text = await resp.text()
                try:
                    js = json.loads(text)
                except Exception:
                    js = {"ok": False, "error": "invalid_json", "text": text}
                print("refundStarPayment response:", js)
                return js
    except Exception as e:
        return {"ok": False, "error": "exception", "text": str(e)}


# ===================== EDIT ORIGINAL HELPERS =====================

async def _try_edit_original_message(chat_id: int, message_id: int, text: str, reply_markup, prefer_caption: bool) -> bool:
    if prefer_caption:
        try:
            await bot.edit_message_caption(chat_id=chat_id, message_id=message_id, caption=text, reply_markup=reply_markup)
            return True
        except Exception:
            pass
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=reply_markup)
        return True
    except Exception:
        pass
    try:
        await bot.edit_message_caption(chat_id=chat_id, message_id=message_id, caption=text, reply_markup=reply_markup)
        return True
    except Exception:
        pass
    return False


# ===================== HELP: thread selection & creation =====================

def get_thread_for_chat(chat_id: int) -> Optional[int]:
    try:
        idx = ADMIN_CHAT_IDS.index(chat_id)
    except ValueError:
        return None
    if idx < len(ADMIN_THREAD_IDS):
        return ADMIN_THREAD_IDS[idx]
    return None


def get_log_thread_for_chat(chat_id: int) -> Optional[int]:
    try:
        idx = ADMIN_CHAT_IDS.index(chat_id)
    except ValueError:
        return None
    if idx < len(ADMIN_LOG_THREAD_IDS):
        return ADMIN_LOG_THREAD_IDS[idx]
    return None


async def ensure_or_create_topic_for_chat(chat_id: int) -> Optional[int]:
    """
    Если для chat_id есть сохранённый thread_id — вернуть его.
    Иначе, если в ADMIN_THREAD_IDS задано id — вернуть его.
    Иначе, если в ADMIN_THREAD_NAMES задано имя для этого chat (по индексу) — попытаться создать тему.
    Сохранить в admin_topics_map и вернуть id; иначе вернуть None.
    """
    key = str(chat_id)
    if key in admin_topics_map:
        try:
            return int(admin_topics_map[key])
        except Exception:
            pass

    # если в env заранее указан ID — вернуть его
    try:
        idx = ADMIN_CHAT_IDS.index(chat_id)
    except ValueError:
        idx = None

    if idx is not None and idx < len(ADMIN_THREAD_IDS) and ADMIN_THREAD_IDS[idx]:
        return ADMIN_THREAD_IDS[idx]

    # попытка создать тему по имени (если задано имя)
    if idx is not None and idx < len(ADMIN_THREAD_NAMES):
        name = ADMIN_THREAD_NAMES[idx].strip()
        if name:
            try:
                # create_forum_topic возвращает Message с message_thread_id
                res_msg: Message = await bot.create_forum_topic(chat_id=chat_id, name=name)
                thread_id = getattr(res_msg, "message_thread_id", None)
                if not thread_id:
                    # try dict access if aiogram version returns raw
                    try:
                        j = res_msg.json()
                        thread_id = j.get("message_thread_id")
                    except Exception:
                        thread_id = None
                if thread_id:
                    admin_topics_map[key] = int(thread_id)
                    save_admin_topics(admin_topics_map)
                    print(f"[INFO] Создана тема '{name}' в чате {chat_id} -> thread {thread_id}")
                    return int(thread_id)
            except Exception as e:
                print(f"[WARN] Не удалось создать тему '{name}' в чате {chat_id}: {e}")
                return None
    return None


# ===================== LOGGING USER ACTIONS =====================

async def log_user_action(user_obj: Union[Message, CallbackQuery, Message, dict, object], action: str) -> None:
    """
    Логирует событие action для пользователя во все admin_chat'ы, в соответствующие log topics (если заданы).
    """
    if isinstance(user_obj, CallbackQuery):
        user = user_obj.from_user
    elif isinstance(user_obj, Message):
        user = user_obj.from_user
    else:
        user = getattr(user_obj, "from_user", None) or getattr(user_obj, "user", None)

    if not user:
        return

    uid = user.id
    uid_str = str(uid)
    data = load_requests()
    user_rec = data.get(uid_str, {})
    langs = user_rec.get("langs", [])
    if not langs and getattr(user, "language_code", None):
        langs = [user.language_code]

    safe_full_name = escape(user.full_name or "(без имени)")
    safe_username = f"@{escape(user.username)}" if getattr(user, "username", None) else ""
    safe_langs = ", ".join(escape(str(x)) for x in langs) if langs else "неизвестно"
    tm = _now().isoformat(sep=" ", timespec="seconds")
    header = f"{safe_full_name} {safe_username}\nID: {uid}\nЯзыки: {safe_langs}\nВремя: {tm}\n\n"
    text = header + f"Действие: {escape(action)}"

    # Отправляем в каждый admin chat (в thread если задан)
    for admin_chat in ADMIN_CHAT_IDS:
        # пытаемся использовать заранее настроенную log-thread (если есть)
        thread_id = get_log_thread_for_chat(admin_chat)
        try:
            if thread_id is not None:
                await bot.send_message(chat_id=admin_chat, text=text, message_thread_id=thread_id)
            else:
                await bot.send_message(chat_id=admin_chat, text=text)
        except Exception as e:
            # не фатально, логируем на stdout
            print(f"[WARN] Не удалось отправить лог в {admin_chat} (thread {thread_id}): {e}")


# ===================== HELP: forward messages without request to admins =====================
async def forward_no_request_to_admins(message: Message):
    """Если пользователь пишет боту без активной заявки — пересылаем/копируем сообщение в админ-чаты и отправляем лог."""
    user = message.from_user
    uid = user.id
    safe_full_name = escape(user.full_name or "(без имени)")
    safe_username = f"@{escape(user.username)}" if getattr(user, "username", None) else ""
    header = f"Сообщение без заявки от {safe_full_name} {safe_username}\nID: {uid}\n\n"
    preview = ""
    try:
        # попытаемся получить текст / caption
        txt = getattr(message, "text", None) or getattr(message, "caption", None) or ""
        if txt:
            preview = escape(txt if len(txt) < 1500 else txt[:1500] + "...")
    except Exception:
        preview = "(не удалось получить текст)"
    # Основная пересылка: копируем сообщение (для медиа/файлов) или пересылаем text
    for admin_chat in ADMIN_CHAT_IDS:
        thread_id = await ensure_or_create_topic_for_chat(admin_chat)
        try:
            # Сначала копируем само сообщение (чтобы админ мог нажать reply на медиa)
            try:
                copied = await bot.copy_message(chat_id=admin_chat, from_chat_id=message.chat.id, message_id=message.message_id, message_thread_id=thread_id)
                set_admin_map(admin_chat, copied.message_id, uid)
            except Exception:
                # fallback: отправляем текст превью
                copied = None
            # отправим текстовый лог о сообщении
            log_text = header + (f"Текст/Капшн:\n{preview}" if preview else "(нет текста)")
            sent = await bot.send_message(chat_id=admin_chat, text=log_text, message_thread_id=thread_id)
            set_admin_map(admin_chat, sent.message_id, uid)
        except Exception as e:
            print(f"[WARN] Не удалось переслать сообщение без заявки в админ-чат {admin_chat}: {e}")


# ===================== HANDLERS =====================


@dp.message(Command("start"))
async def send_welcome(message: Message):
    update_user_lang(str(message.from_user.id), message.from_user.language_code or "unknown")

    # логируем команду /start
    await log_user_action(message, "/start")

    if is_banned(message.from_user.id):
        await message.answer("🔒 Вы заблокированы.")
        return

    if not await ensure_private_and_autoleave(message):
        return
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
        except Exception as e:
            print(f"[WARN] Не удалось отправить локальную картинку: {e}")
            await message.answer(caption, reply_markup=keyboard)
    else:
        await message.answer(caption, reply_markup=keyboard)


@dp.message(Command("setprice"))
async def set_price(message: Message):
    update_user_lang(str(message.from_user.id), message.from_user.language_code or "unknown")

    # логируем попытку изменить цену (для аудита)
    await log_user_action(message, f"Команда /setprice ({message.text})")

    # разрешено только мейн-админам
    if message.from_user.id not in MAIN_ADMIN_IDS:
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        await message.answer("Использование: /setprice 15  (пример: /setprice 10 -> установит 10$)")
        return
    new_price_raw = args[1].strip()
    # убираем знак $ если пришёл и заменяем запятую на точку
    new_price_clean = new_price_raw.replace("$", "").replace(",", ".").strip()
    try:
        num = float(new_price_clean)
    except Exception:
        await message.answer("Ошибка: значение должно быть числом, например: /setprice 10 или /setprice 9.99")
        return
    # если целое число — показываем без .0
    if num.is_integer():
        price_str = f"{int(num)}$"
    else:
        # сохраняем минимально необходимое количество знаков (например 9.5$)
        price_str = f"{num}$"
    cfg = load_config()
    cfg["price"] = price_str
    save_config(cfg)
    await message.answer(f"✅ Цена изменена на {price_str}")


@dp.message(Command("setprice_stars"))
async def set_price_stars(message: Message):
    update_user_lang(str(message.from_user.id), message.from_user.language_code or "unknown")
    await log_user_action(message, f"Команда /setprice_stars ({message.text})")

    if message.from_user.id not in MAIN_ADMIN_IDS:
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Использование: /setprice_stars 50  (число stars, целое)")
        return
    try:
        val = int(parts[1].strip())
    except Exception:
        await message.answer("Сумма должна быть целым числом (количество stars).")
        return
    cfg = load_config()
    cfg["price_stars"] = val
    save_config(cfg)
    await message.answer(f"✅ Цена в stars установлена: {val} ⭐")


@dp.callback_query(F.data == "premium")
async def process_premium(callback: CallbackQuery):
    update_user_lang(str(callback.from_user.id), callback.from_user.language_code or "unknown")

    # логируем действие пользователя
    await log_user_action(callback, "Нажал кнопку: Premium")

    if is_banned(callback.from_user.id):
        await callback.answer("🔒 Вы заблокированы.", show_alert=True)
        return

    # построим клавиатуру оплаты
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇷🇺 Картой", callback_data="pay_card")],
        [InlineKeyboardButton(text="🌎 Crypto (@send) (0%)", callback_data="pay_crypto")],
        [InlineKeyboardButton(text="⭐ Telegram Stars", callback_data="pay_stars")],
        [InlineKeyboardButton(text="🏠", callback_data="home")],
    ])

    orig_chat_id = callback.message.chat.id
    orig_msg_id = callback.message.message_id
    content_type = getattr(callback.message, "content_type", None)
    prefer_caption = content_type in ("photo", "video", "document", "animation") or bool(getattr(callback.message, "photo", None))
    new_text = "Вы выбрали Premium"

    ok = await _try_edit_original_message(orig_chat_id, orig_msg_id, new_text, keyboard, prefer_caption)
    if ok:
        await callback.answer()
        return

    await callback.answer("⚠️ Не удалось обновить сообщение. Попробуйте ещё раз.", show_alert=True)


@dp.callback_query(F.data == "home")
async def go_home(callback: CallbackQuery):
    update_user_lang(str(callback.from_user.id), callback.from_user.language_code or "unknown")

    # логируем действие пользователя
    await log_user_action(callback, "Нажал кнопку: Домой")

    if is_banned(callback.from_user.id):
        await callback.answer("🔒 Вы заблокированы.", show_alert=True)
        return

    price = load_config()["price"]
    caption = (
        "Добро пожаловать! Я платёжный бот Gene's Land!\n\n"
        "Здесь вы можете приобрести Premium-версию Gene Brawl!\n\n"
        "Gene Premium Ultimate выдается навсегда.\n"
        "(Нажмите на товар, чтобы узнать подробности)"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f" 🫣 Premium - {price}", callback_data="premium")],
        [InlineKeyboardButton(text=" 🩼 Поддержка", url="https://t.me/genepremiumsupportbot")],
    ])

    orig_chat_id = callback.message.chat.id
    orig_msg_id = callback.message.message_id
    content_type = getattr(callback.message, "content_type", None)
    prefer_caption = content_type in ("photo", "video", "document", "animation") or bool(getattr(callback.message, "photo", None))

    ok = await _try_edit_original_message(orig_chat_id, orig_msg_id, caption, keyboard, prefer_caption)
    if ok:
        await callback.answer()
        return

    await callback.answer("⚠️Попробуйте ещё раз.", show_alert=True)


@dp.callback_query(F.data.in_(["pay_card", "pay_crypto", "pay_stars"]))
async def ask_screenshots(callback: CallbackQuery):
    update_user_lang(str(callback.from_user.id), callback.from_user.language_code or "unknown")

    # логируем выбор способа оплаты
    await log_user_action(callback, f"Выбрал способ оплаты: {callback.data}")

    if is_banned(callback.from_user.id):
        await callback.answer("🔒 Вы заблокированы.", show_alert=True)
        return

    await callback.answer()
    if callback.message.chat.type != "private":
        return
    user, user_id_str = callback.from_user, str(callback.from_user.id)
    if not can_start_new_request(user_id_str):
        # логируем и пересылаем сообщение в админ-чаты (пользователь уже подавал заявку)
        await callback.message.answer("Вы уже подавали заявку, ожидайте одобрения ✅")
        return
    langs = update_user_lang(user_id_str, user.language_code or "unknown")
    start_request(user, langs)
    instruction = (
        "Наша система сочла ваш аккаунт подозрительным.\n"
        "Для покупки Gene Premium мы обязаны убедиться в вас.\n\n"
        "Отправьте скриншоты ваших первых сообщений в:\n"
        "• Brawl Stars Datamines | Чат\n"
        "• Gene's Land чат\n\n"
        "А также (по желанию) фото прошитого 4G модема.\n\n"
        "⏳ Срок одобрения заявки ~3 дня."
    )

    data = load_requests()
    user_record = data.get(user_id_str, {})
    # Если пользователь ранее отклонён (в requests.json или в rejected.json) — НЕ показываем "Подготавливаем..." и сразу отправляем инструкцию.
    if user_record.get("rejected", False) or int(user_id_str) in rejected_users:
        await callback.message.answer(instruction)
        # отметим, что он видел инструкции
        if user_id_str in data:
            data[user_id_str]["has_seen_instructions"] = True
            save_requests(data)
        return

    if not user_record.get("has_seen_instructions", False):
        preparing_msg = await callback.message.answer("⏳ Подготавливаем для вас оплату...")
        await asyncio.sleep(random.randint(4234, 10110) / 1000)
        await preparing_msg.edit_text(instruction)
        if user_id_str in data:
            data[user_id_str]["has_seen_instructions"] = True
            save_requests(data)
    else:
        await callback.message.answer(instruction)


@dp.callback_query(F.data.startswith("reject_"))
async def reject_request(callback: CallbackQuery):
    update_user_lang(str(callback.from_user.id), callback.from_user.language_code or "unknown")

    # логируем действие админа (отклонение)
    await log_user_action(callback, f"Админ {callback.from_user.id} отклонил заявку {callback.data}")

    await callback.answer("Заявка отклонена и удалена ❌")
    if callback.message.chat.id not in ADMIN_CHAT_IDS:
        return
    if callback.from_user.id not in ALL_ADMINS_SET:
        return
    user_id = callback.data.split("_", 1)[1]
    data = load_requests()
    # Вместо удаления — помечаем как отклонённую, чтобы при следующем заходе не показывать "Подготавливаем..."
    rec = data.get(user_id, {})
    rec["rejected"] = True
    rec["submitted"] = False
    rec["started_at"] = None
    rec["has_seen_instructions"] = False
    # сохраняем full_name/username если их нет (необязательно)
    rec.setdefault("full_name", rec.get("full_name", ""))
    rec.setdefault("username", rec.get("username", ""))
    rec.setdefault("langs", rec.get("langs", []))
    data[user_id] = rec
    save_requests(data)

    try:
        add_rejected(int(user_id))
    except Exception:
        pass

    try:
        await bot.send_message(user_id, "❌ Ваша заявка отклонена.\nВы можете попробовать подать её снова.")
    except Exception as e:
        print(f"[WARN] Не удалось уведомить пользователя {user_id}: {e}")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@dp.callback_query(F.data.startswith("ban_"))
async def ban_request(callback: CallbackQuery):
    update_user_lang(str(callback.from_user.id), callback.from_user.language_code or "unknown")

    # логируем действие админа (бан)
    await log_user_action(callback, f"Админ {callback.from_user.id} заблокировал пользователя {callback.data}")

    await callback.answer("Пользователь заблокирован 🔒")
    if callback.message.chat.id not in ADMIN_CHAT_IDS:
        return
    if callback.from_user.id not in ALL_ADMINS_SET:
        return

    user_id = callback.data.split("_", 1)[1]
    try:
        uid = int(user_id)
    except Exception:
        await callback.message.answer("Неверный id для блокировки.")
        return

    try:
        ban_user_by_id(uid)
        remove_request(str(uid))
        submission_buffers.pop(str(uid), None)
        task = collecting_tasks.pop(str(uid), None)
        if task and not task.done():
            try:
                task.cancel()
            except Exception:
                pass
    except Exception as e:
        print(f"[WARN] Не удалось полностью заблокировать/очистить данные для {uid}: {e}")

    try:
        add_rejected(uid)
    except Exception:
        pass

    try:
        await bot.send_message(uid, "🔒 Вы были заблокированы.")
    except Exception as e:
        print(f"[WARN] Не удалось уведомить пользователя {uid}: {e}")

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


# ------------------ COMMAND /ban (added) ------------------

@dp.message(Command("ban"))
async def cmd_ban(message: Message):
    update_user_lang(str(message.from_user.id), message.from_user.language_code or "unknown")
    await log_user_action(message, f"Команда /ban ({message.text})")

    if message.from_user.id not in ALL_ADMINS_SET:
        return

    parts = message.text.split(maxsplit=1)
    target_id: Optional[int] = None

    # /ban <id>
    if len(parts) > 1 and parts[1].strip():
        try:
            target_id = int(parts[1].strip())
        except ValueError:
            await message.reply("Неверный id. Использование: /ban <user_id>")
            return
    else:
        # или reply на сообщении бота в админ-чате
        if message.reply_to_message:
            replied_key = _admin_map_key(message.reply_to_message.chat.id, message.reply_to_message.message_id)
            target_id = admin_message_to_user.get(replied_key)
            if not target_id:
                ffrom = getattr(message.reply_to_message, "forward_from", None)
                if ffrom and getattr(ffrom, "id", None):
                    target_id = ffrom.id
        if not target_id:
            await message.reply("Укажите id: /ban <user_id> или сделайте reply на сообщении бота в админ-чате.")
            return

    # Выполняем бан и чистку
    try:
        ban_user_by_id(target_id)
    except Exception as e:
        await message.reply(f"Ошибка при добавлении в бан-лист: {e}")
        return

    # Удаляем/закрываем заявку пользователя, буферы, задачи
    try:
        remove_request(str(target_id))
    except Exception:
        pass
    try:
        submission_buffers.pop(str(target_id), None)
    except Exception:
        pass
    try:
        task = collecting_tasks.pop(str(target_id), None)
        if task and not task.done():
            try:
                task.cancel()
            except Exception:
                pass
    except Exception:
        pass

    # Убираем inline-кнопки и маппинги у всех сообщений админ-чатов, относящихся к этому userid
    try:
        for k, v in list(admin_message_to_user.items()):
            try:
                if int(v) == int(target_id):
                    chat_s, msg_s = k.split(":", 1)
                    chat_id = int(chat_s); msg_id = int(msg_s)
                    try:
                        await bot.edit_message_reply_markup(chat_id=chat_id, message_id=msg_id, reply_markup=None)
                    except Exception:
                        pass
                    remove_admin_map_by_key(k)
            except Exception:
                continue
    except Exception as e:
        print(f"[WARN] Ошибка при очистке админских сообщений для {target_id}: {e}")

    try:
        add_rejected(int(target_id))
    except Exception:
        pass

    # уведомляем админа и пользователя
    await message.reply(f"🔒 Пользователь {target_id} заблокирован и его заявка закрыта.")
    try:
        await bot.send_message(chat_id=target_id, text="🔒 Вы были заблокированы. Связаться с поддержкой нельзя.")
    except Exception:
        pass


# ------------------ UNBAN: команда и callback ------------------

@dp.message(Command("unban"))
async def cmd_unban(message: Message):
    update_user_lang(str(message.from_user.id), message.from_user.language_code or "unknown")

    # логируем действие админа (unban)
    await log_user_action(message, f"Команда /unban ({message.text})")

    if message.from_user.id not in ALL_ADMINS_SET:
        return

    parts = message.text.split(maxsplit=1)
    target_id: Optional[int] = None
    if len(parts) > 1 and parts[1].strip():
        try:
            target_id = int(parts[1].strip())
        except ValueError:
            await message.reply("Неверный id. Использование: /unban <user_id>")
            return
    else:
        if message.reply_to_message:
            replied_key = _admin_map_key(message.reply_to_message.chat.id, message.reply_to_message.message_id)
            target_id = admin_message_to_user.get(replied_key)
        if not target_id:
            await message.reply("Укажите id: /unban <user_id> или выполните команду через reply на сообщении бота в админ-чате.")
            return

    banned = load_banned()
    if target_id not in banned:
        await message.reply(f"Пользователь {target_id} не в списке заблокированных.")
        return

    try:
        unban_user_by_id(target_id)
    except Exception as e:
        await message.reply(f"Ошибка при разблокировке: {e}")
        return

    await message.reply(f"✅ Пользователь {target_id} разблокирован.")
    try:
        await bot.send_message(chat_id=target_id, text="🔓 Вас разблокировали.")
    except Exception:
        pass


@dp.callback_query(F.data.startswith("unban_"))
async def unban_request(callback: CallbackQuery):
    await callback.answer()
    if callback.message.chat.id not in ADMIN_CHAT_IDS:
        return
    if callback.from_user.id not in ALL_ADMINS_SET:
        return

    user_id = callback.data.split("_", 1)[1]
    try:
        uid = int(user_id)
    except Exception:
        await callback.message.answer("Неверный id для разблокировки.")
        return

    banned = load_banned()
    if uid not in banned:
        await callback.message.answer("Пользователь уже не заблокирован.")
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    try:
        unban_user_by_id(uid)
    except Exception as e:
        await callback.message.answer(f"Ошибка при сохранении: {e}")
        return

    await callback.message.answer(f"✅ Пользователь {uid} разблокирован.")
    try:
        await bot.send_message(chat_id=uid, text="🔓 Вас разблокировали.")
    except Exception:
        pass

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@dp.message(Command("banned"))
async def cmd_banned(message: Message):
    update_user_lang(str(message.from_user.id), message.from_user.language_code or "unknown")

    # логирование просмотра списка забаненных
    await log_user_action(message, "Команда /banned")

    if message.from_user.id not in ALL_ADMINS_SET:
        return
    banned = load_banned()
    if not banned:
        await message.reply("Список заблокированных пуст.")
        return
    text = "Заблокированные пользователи:\n" + "\n".join([str(x) for x in banned])
    await message.reply(text)


# ===================== NEW: /clear_rejected command =====================
@dp.message(Command("clear_rejected"))
async def cmd_clear_rejected(message: Message):
    """
    /clear_rejected             -> очистить всех rejected
    /clear_rejected <user_id>   -> удалить одного пользователя из rejected
    Доступно только для админов.
    """
    update_user_lang(str(message.from_user.id), message.from_user.language_code or "unknown")
    await log_user_action(message, f"Команда /clear_rejected ({message.text})")

    if message.from_user.id not in ALL_ADMINS_SET:
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip():
        # очистка конкретного
        try:
            uid = int(parts[1].strip())
        except ValueError:
            await message.reply("Неверный id. Использование: /clear_rejected <user_id> или /clear_rejected")
            return
        remove_rejected(uid)
        # также удаляем флаг rejected из requests.json если он там есть
        data = load_requests()
        rec = data.get(str(uid))
        if rec and rec.get("rejected"):
            rec.pop("rejected", None)
            data[str(uid)] = rec
            save_requests(data)
        await message.reply(f"✅ Пользователь {uid} удалён из списка отклонённых.")
    else:
        # очистка всех
        clear_all_rejected()
        # чистим флаги в requests.json
        data = load_requests()
        changed = False
        for k, rec in list(data.items()):
            if rec.get("rejected"):
                rec.pop("rejected", None)
                data[k] = rec
                changed = True
        if changed:
            save_requests(data)
        await message.reply("✅ Список отклонённых пользователей очищен.")


# ===================== ПРИЁМ ЗАЯВОК (с копированием) =====================


async def handle_submission(messages: Union[Message, List[Message]]):
    first_message: Message = messages[0] if isinstance(messages, list) else messages
    if not await ensure_private_and_autoleave(first_message):
        return
    user = first_message.from_user
    user_id_str = str(user.id)

    if is_banned(user.id):
        remove_request(user_id_str)
        submission_buffers.pop(user_id_str, None)
        task = collecting_tasks.pop(user_id_str, None)
        if task and not task.done():
            try:
                task.cancel()
            except Exception:
                pass
        try:
            await bot.send_message(chat_id=user.id, text="🔒 Вы заблокированы.")
        except Exception:
            pass
        return

    async with user_submission_locks[user_id_str]:
        if not has_active_request(user_id_str):
            return
        update_user_lang(user_id_str, user.language_code or "unknown")

        safe_full_name = escape(user.full_name or "(без имени)")
        safe_username = f"@{escape(user.username)}" if user.username else ""
        data = load_requests()
        langs = data.get(user_id_str, {}).get("langs", [user.language_code or "неизвестно"])
        safe_langs = ", ".join([escape(str(x)) for x in langs])
        header = f"{safe_full_name} {safe_username}\nID: {user.id}\nЯзыки: {safe_langs}"

        # ------------------- NEW: build admin keyboard with issue_pay -------------------
        admin_keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{user.id}"),
                    InlineKeyboardButton(text="🔒 Заблокировать", callback_data=f"ban_{user.id}")
                ],
                [
                    InlineKeyboardButton(text="💳 Выдать доступ к оплате", callback_data=f"issue_pay_{user.id}")
                ]
            ]
        )

        # ------------------- NEW: save combined submitted_text into requests.json -------------------
        try:
            msgs = messages if isinstance(messages, list) else [messages]
            texts = []
            for m in msgs:
                t = getattr(m, "html_text", None) or getattr(m, "caption_html", None) or getattr(m, "text", None) or getattr(m, "caption", None) or ""
                if t:
                    texts.append(escape(t))
            combined = "\n\n".join(texts) if texts else ""
            rec = data.get(user_id_str, {})
            rec["submitted_text"] = combined
            rec.setdefault("full_name", user.full_name or "")
            rec.setdefault("username", user.username or "")
            rec.setdefault("langs", rec.get("langs", []))
            data[user_id_str] = rec
            save_requests(data)
        except Exception as e:
            print(f"[WARN] Не удалось сохранить submitted_text для {user_id_str}: {e}")

        try:
            # Для каждого admin chat отправляем копии и шапку (в topic, если задан или можно создать)
            for admin_chat in ADMIN_CHAT_IDS:
                # NEW: ensure or create thread for submissions (if ADMIN_THREAD_NAMES provided)
                thread_id = await ensure_or_create_topic_for_chat(admin_chat)

                if isinstance(messages, list):
                    album_msgs: List[Message] = sorted(messages, key=lambda m: m.message_id)
                    media_group_ids = {getattr(m, "media_group_id", None) for m in album_msgs}
                    if len(media_group_ids) == 1 and next(iter(media_group_ids)) is not None:
                        for m in album_msgs:
                            res = await bot.copy_message(chat_id=admin_chat, from_chat_id=m.chat.id, message_id=m.message_id, message_thread_id=thread_id)
                            set_admin_map(admin_chat, res.message_id, int(user.id))
                        header_msg = await bot.send_message(admin_chat, text=header, reply_markup=admin_keyboard, message_thread_id=thread_id)
                        set_admin_map(admin_chat, header_msg.message_id, int(user.id))
                    else:
                        media_group = []
                        for i, m in enumerate(album_msgs):
                            caption = getattr(m, "html_text", None) or getattr(m, "caption_html", None) or None
                            cap = caption if i == 0 else None
                            if m.photo:
                                file_id = m.photo[-1].file_id
                                media_group.append(InputMediaPhoto(media=file_id, caption=cap, parse_mode="HTML"))
                            elif m.video:
                                media_group.append(InputMediaVideo(media=m.video.file_id, caption=cap, parse_mode="HTML"))
                            elif getattr(m, "document", None):
                                media_group.append(InputMediaDocument(media=m.document.file_id, caption=cap, parse_mode="HTML"))
                            elif getattr(m, "audio", None):
                                media_group.append(InputMediaAudio(media=m.audio.file_id, caption=cap, parse_mode="HTML"))
                            else:
                                res = await bot.copy_message(chat_id=admin_chat, from_chat_id=m.chat.id, message_id=m.message_id, message_thread_id=thread_id)
                                set_admin_map(admin_chat, res.message_id, int(user.id))
                        if media_group:
                            sent = await bot.send_media_group(chat_id=admin_chat, media=media_group, message_thread_id=thread_id)
                            for s in sent:
                                set_admin_map(admin_chat, s.message_id, int(user.id))
                            header_msg = await bot.send_message(admin_chat, text=header, reply_markup=admin_keyboard, message_thread_id=thread_id)
                            set_admin_map(admin_chat, header_msg.message_id, int(user.id))
                else:
                    res = await bot.copy_message(chat_id=admin_chat, from_chat_id=first_message.chat.id, message_id=first_message.message_id, message_thread_id=thread_id)
                    set_admin_map(admin_chat, res.message_id, int(user.id))
                    header_msg = await bot.send_message(admin_chat, text=header, reply_markup=admin_keyboard, message_thread_id=thread_id)
                    set_admin_map(admin_chat, header_msg.message_id, int(user.id))

            # уведомляем пользователя и помечаем заявку
            await bot.send_message(chat_id=user.id, text="✅ Ваша заявка отправлена администраторам.\nОжидайте ответа.")
            mark_submitted(user_id_str)

        except TelegramBadRequest as e:
            print(f"[BAD_REQUEST] {e!r}")
            await first_message.answer("⚠️ Не удалось отправить заявку. Попробуйте ещё раз .")
        except Exception as e:
            print(f"[ERROR] Не удалось отправить в админ-чат: {e}")
            await first_message.answer("⚠️ Не удалось отправить заявку.\nПопробуйте ещё раз позже.")


# Новый обработчик: собирает сообщения от пользователя в буфер и запускает задачу-коллектор
@dp.message(F.chat.type == "private")
async def collect_user_messages(message: Message):
    update_user_lang(str(message.from_user.id), message.from_user.language_code or "unknown")

    # можно логировать отправку сообщений пользователем (необязательно)
    # await log_user_action(message, "Отправил сообщение в личку")

    if is_banned(message.from_user.id):
        remove_request(str(message.from_user.id))
        submission_buffers.pop(str(message.from_user.id), None)
        task = collecting_tasks.pop(str(message.from_user.id), None)
        if task and not task.done():
            try:
                task.cancel()
            except Exception:
                pass
        try:
            await message.answer("🔒 Вы заблокированы.")
        except Exception:
            pass
        return

    if not await ensure_private_and_autoleave(message):
        return
    user = message.from_user
    user_id_str = str(user.id)

    # Если у пользователя нет активной заявки — логируем/пересылаем его сообщение в админ-чаты
    if not has_active_request(user_id_str) or load_requests().get(user_id_str, {}).get("submitted"):
        # пересылаем в админ-чаты для логирования
        try:
            await forward_no_request_to_admins(message)
        except Exception as e:
            print(f"[WARN] forward_no_request_to_admins failed: {e}")
        # если уже подавал заявку (submitted True) — коротко уведомим
        if load_requests().get(user_id_str, {}).get("submitted"):
            try:
                await message.reply("Вы уже подавали заявку, ожидайте ответа от администраторов.")
            except Exception:
                pass
            return
        # иначе, продолжаем — позволяем пользователю составить заявку (добавляем в буфер)
        # Добавляем сообщение в буфер; ограничиваем до 4 сообщений (чтобы кнопки у заявки не пропадали)
        submission_buffers[user_id_str].append(message)
        if len(submission_buffers[user_id_str]) > 4:
            # оставляем только 4 последних сообщений
            submission_buffers[user_id_str] = submission_buffers[user_id_str][-4:]

        existing = collecting_tasks.get(user_id_str)
        if existing and not existing.done():
            return

        async def _collector(uid: str):
            await asyncio.sleep(3)
            msgs = submission_buffers.pop(uid, [])
            collecting_tasks.pop(uid, None)
            if not msgs:
                return
            if len(msgs) == 1:
                await handle_submission(msgs[0])
            else:
                await handle_submission(msgs)

        task = asyncio.create_task(_collector(user_id_str))
        collecting_tasks[user_id_str] = task
        return

    # Если есть активная заявка — обычный путь (добавляем в буфер и ждём)
    if load_requests().get(user_id_str, {}).get("submitted"):
        return

    # Добавляем сообщение в буфер; ограничиваем до 4 сообщений (чтобы кнопки у заявки не пропадали)
    submission_buffers[user_id_str].append(message)
    if len(submission_buffers[user_id_str]) > 4:
        # оставляем только 4 последних сообщений
        submission_buffers[user_id_str] = submission_buffers[user_id_str][-4:]

    existing = collecting_tasks.get(user_id_str)
    if existing and not existing.done():
        return

    async def _collector_active(uid: str):
        await asyncio.sleep(3)
        msgs = submission_buffers.pop(uid, [])
        collecting_tasks.pop(uid, None)
        if not msgs:
            return
        if len(msgs) == 1:
            await handle_submission(msgs[0])
        else:
            await handle_submission(msgs)

    task = asyncio.create_task(_collector_active(user_id_str))
    collecting_tasks[user_id_str] = task


# ===================== АДМИН: ответ reply -> пользователю =====================
@dp.message(F.chat.id.in_(ADMIN_CHAT_IDS) if ADMIN_CHAT_IDS else F.chat.id == ADMIN_CHAT_ID)
async def admin_reply_handler(message: Message):
    # разрешаем только админам
    if message.from_user.id not in ALL_ADMINS_SET:
        return

    if not message.reply_to_message:
        return

    replied = message.reply_to_message
    key = _admin_map_key(replied.chat.id, replied.message_id)
    target_user_id = admin_message_to_user.get(key)
    if not target_user_id:
        ffrom = getattr(replied, "forward_from", None)
        if ffrom and getattr(ffrom, "id", None):
            target_user_id = ffrom.id

    if not target_user_id:
        return

    update_user_lang(str(message.from_user.id), message.from_user.language_code or "unknown")

    if is_banned(target_user_id):
        await message.reply("⚠️ Этот пользователь заблокирован. Ответ не отправлен.", quote=False)
        return

    try:
        await bot.copy_message(chat_id=target_user_id, from_chat_id=message.chat.id, message_id=message.message_id)
        await message.reply("✅ Ответ отправлен пользователю.", quote=False)
    except TelegramBadRequest as e:
        print(f"[WARN] Не удалось отправить ответ пользователю {target_user_id}: {e}")
        await message.reply("⚠️ Не удалось отправить ответ пользователю.", quote=False)
    except Exception as e:
        print(f"[ERROR] Ошибка при пересылке ответа пользователю: {e}")
        await message.reply("⚠️ Ошибка при пересылке ответа пользователю.", quote=False)


# ===================== АВТО-ЛИВ ИЗ ЧАТОВ =====================
@dp.chat_member(ChatMemberUpdatedFilter(member_status_changed=MEMBER))
async def on_added(event: ChatMemberUpdated):
    if event.chat.id not in ADMIN_CHAT_IDS:
        try:
            await bot.leave_chat(event.chat.id)
            print(f"[LOG] Автовыход из чата {event.chat.id}")
        except Exception as e:
            print(f"[ERROR] Не удалось выйти из чата {event.chat.id}: {e}")


@dp.message(F.chat.type.in_(["group", "supergroup", "channel"]))
async def leave_any_group(message: Message):
    if message.chat.id not in ADMIN_CHAT_IDS:
        try:
            await bot.leave_chat(message.chat.id)
            print(f"[LOG] Вышел из чате по сообщению {message.chat.id}")
        except Exception as e:
            print(f"[ERROR] Не удалось выйти из чата {message.chat.id}: {e}")


# ===================== CALLBACK: issue_pay handler =====================
@dp.callback_query(F.data.startswith("issue_pay_"))
async def issue_payment_callback(callback: CallbackQuery):
    # только админы в admin-chats могут инициировать
    if callback.message.chat.id not in ADMIN_CHAT_IDS:
        await callback.answer("Команда доступна только в админ-чате.", show_alert=True)
        return
    if callback.from_user.id not in ALL_ADMINS_SET and callback.from_user.id not in MAIN_ADMIN_IDS:
        await callback.answer("У вас нет прав.", show_alert=True)
        return

    await callback.answer()  # чтобы убрать "часики"

    parts = callback.data.split("_", 2)
    if len(parts) < 3:
        await callback.message.answer("Не удалось определить user_id.")
        return
    try:
        target_user_id = int(parts[2])
    except Exception:
        await callback.message.answer("Неверный user_id.")
        return

    cfg = load_config()
    price_stars = int(cfg.get("price_stars", 0)) if isinstance(cfg.get("price_stars", None), int) else int(cfg.get("price_stars", 0) if cfg.get("price_stars", 0) else 0)
    if price_stars <= 0:
        await callback.message.answer("Цена в stars не задана. Используйте /setprice_stars <число>.", reply=False)
        return

    title = "Премиум-доступ"
    description = f"Доступ к премиум — {price_stars} stars"
    payload = f"auto_refund:admin_issue_{callback.from_user.id}_to_{target_user_id}_{int(time.time())}"

    prices = [LabeledPrice(label=title, amount=price_stars)]
    try:
        # provider_token может быть пустым, но чаще нужен
        await bot.send_invoice(
            chat_id=target_user_id,
            title=title,
            description=description,
            payload=payload,
            provider_token=PROVIDER_TOKEN_STARS,
            currency="XTR",
            prices=prices,
        )
        await callback.message.answer(f"Инвойс отправлен пользователю {target_user_id}.", reply=False)
    except Exception as e:
        await callback.message.answer(f"Ошибка при отправке инвойса: {e}", reply=False)


# ===================== PAYMENTS: pre_checkout и successful_payment =====================
@dp.pre_checkout_query()
async def pre_checkout_handler(pre_checkout: PreCheckoutQuery):
    await pre_checkout.answer(ok=True)


@dp.message(F.successful_payment)
async def successful_payment_handler(message: Message):
    sp = message.successful_payment
    user_id = message.from_user.id
    charge_id = sp.telegram_payment_charge_id
    payload = sp.invoice_payload or ""
    total_amount = sp.total_amount
    currency = sp.currency

    # сохраняем в локальную базу
    saved = await save_payment(user_id=user_id, charge_id=charge_id, payload=payload, amount=total_amount, currency=currency)
    if not saved:
        try:
            await message.reply("Транзакция уже существует в базе — пропускаем сохранение.")
        except Exception:
            pass
    else:
        try:
            await message.reply("Спасибо за оплату! Проверяем доставку доступа...")
        except Exception:
            pass

    # уведомим админов с текстом заявки (если есть)
    try:
        data = load_requests()
        stext = data.get(str(user_id), {}).get("submitted_text", "(нет текста заявки)")
    except Exception:
        stext = "(ошибка при чтении текста заявки)"

    log_text = (
        f"Платёж: user={message.from_user.full_name} @{getattr(message.from_user,'username', '')} id={user_id}\n"
        f"amount={total_amount} {currency}\ncharge_id={charge_id}\npayload={payload}\n\n"
        f"Текст заявки:\n{stext}"
    )
    for admin_chat in ADMIN_CHAT_IDS:
        try:
            await bot.send_message(chat_id=admin_chat, text=log_text)
        except Exception:
            pass

    # Если payload содержит auto_refund — выполняем автоматический возврат и информируем
    if "auto_refund" in payload:
        try:
            await message.reply("Произошла ошибка в генерации ссылки в чат — выполняю авто-возврат средств...")
        except Exception:
            pass
        result = await refund_star_payment(user_id, charge_id)
        if result.get("ok"):
            await mark_refunded(charge_id)
            try:
                await message.reply("Возврат выполнен успешно.")
            except Exception:
                pass
            # лог в админ-чаты
            for admin_chat in ADMIN_CHAT_IDS:
                try:
                    await bot.send_message(admin_chat, f"Авто-возврат выполнен для charge_id={charge_id} user={user_id}")
                except Exception:
                    pass
        else:
            for admin_chat in ADMIN_CHAT_IDS:
                try:
                    await bot.send_message(admin_chat, f"Не удалось выполнить авто-возврат для charge_id={charge_id}: {result}")
                except Exception:
                    pass


# -------------------- COMMAND: /issuepay (ручная выдача инвойса) --------------------
@dp.message(Command("issuepay"))
async def cmd_issuepay(message: Message):
    update_user_lang(str(message.from_user.id), message.from_user.language_code or "unknown")
    await log_user_action(message, f"Команда /issuepay ({message.text})")

    if message.from_user.id not in ALL_ADMINS_SET and message.from_user.id not in MAIN_ADMIN_IDS:
        await message.reply("У вас нет прав на эту команду.")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 and not message.reply_to_message:
        await message.reply("Использование: /issuepay <user_id> или reply на сообщении бота в админ-чате.")
        return

    target_id = None
    if len(parts) >= 2 and parts[1].strip():
        try:
            target_id = int(parts[1].strip())
        except Exception:
            await message.reply("Неверный user_id.")
            return
    else:
        # reply flow
        replied_key = _admin_map_key(message.reply_to_message.chat.id, message.reply_to_message.message_id)
        target_id = admin_message_to_user.get(replied_key)
        if not target_id:
            ffrom = getattr(message.reply_to_message, "forward_from", None)
            if ffrom and getattr(ffrom, "id", None):
                target_id = ffrom.id
    if not target_id:
        await message.reply("Не удалось определить user_id.")
        return

    cfg = load_config()
    price_stars = int(cfg.get("price_stars", 0)) if isinstance(cfg.get("price_stars", None), int) else int(cfg.get("price_stars", 0) if cfg.get("price_stars", 0) else 0)
    if price_stars <= 0:
        await message.reply("Цена в stars не задана. Используйте /setprice_stars <число>.")
        return

    title = "Премиум-доступ"
    description = f"Доступ к премиум — {price_stars} stars"
    payload = f"admin_issue_manual_{message.from_user.id}_to_{target_id}_{int(time.time())}"
    prices = [LabeledPrice(label=title, amount=price_stars)]
    try:
        await bot.send_invoice(
            chat_id=target_id,
            title=title,
            description=description,
            payload=payload,
            provider_token=PROVIDER_TOKEN_STARS,
            currency="XTR",
            prices=prices,
        )
        await message.reply(f"Инвойс отправлен пользователю {target_id}.")
    except Exception as e:
        await message.reply(f"Ошибка при отправке инвойса: {e}")


# -------------------- COMMAND: /reject (ручное отклонение заявки) --------------------
@dp.message(Command("reject"))
async def cmd_reject(message: Message):
    update_user_lang(str(message.from_user.id), message.from_user.language_code or "unknown")
    await log_user_action(message, f"Команда /reject ({message.text})")

    if message.from_user.id not in ALL_ADMINS_SET and message.from_user.id not in MAIN_ADMIN_IDS:
        await message.reply("У вас нет прав на эту команду.")
        return

    parts = message.text.split(maxsplit=1)
    target_id = None
    if len(parts) >= 2 and parts[1].strip():
        try:
            target_id = int(parts[1].strip())
        except Exception:
            await message.reply("Неверный id. Использование: /reject <user_id> или reply на сообщении бота в админ-чате.")
            return
    else:
        if message.reply_to_message:
            replied_key = _admin_map_key(message.reply_to_message.chat.id, message.reply_to_message.message_id)
            target_id = admin_message_to_user.get(replied_key)
            if not target_id:
                ffrom = getattr(message.reply_to_message, "forward_from", None)
                if ffrom and getattr(ffrom, "id", None):
                    target_id = ffrom.id
        if not target_id:
            await message.reply("Укажите id: /reject <user_id> или сделайте reply на сообщении бота в админ-чате.")
            return

    data = load_requests()
    rec = data.get(str(target_id), {})
    rec["rejected"] = True
    rec["submitted"] = False
    rec["started_at"] = None
    rec["has_seen_instructions"] = False
    data[str(target_id)] = rec
    save_requests(data)
    try:
        add_rejected(int(target_id))
    except Exception:
        pass

    try:
        await bot.send_message(target_id, "❌ Ваша заявка отклонена.\nВы можете попробовать подать её снова.")
    except Exception:
        pass
    # удаляем inline-кнопки у всех связанных сообщений
    try:
        for k, v in list(admin_message_to_user.items()):
            if int(v) == int(target_id):
                chat_s, msg_s = k.split(":", 1)
                try:
                    await bot.edit_message_reply_markup(chat_id=int(chat_s), message_id=int(msg_s), reply_markup=None)
                except Exception:
                    pass
                remove_admin_map_by_key(k)
    except Exception:
        pass

    await message.reply(f"Заявка пользователя {target_id} помечена как отклонённая.")


# ===================== MAIN =====================
async def main():
    print(f"[BOOT] ADMIN_CHAT_IDS={ADMIN_CHAT_IDS}, ADMIN_THREAD_IDS={ADMIN_THREAD_IDS}, ADMIN_THREAD_NAMES={ADMIN_THREAD_NAMES}, ADMIN_LOG_THREAD_IDS={ADMIN_LOG_THREAD_IDS}, MAIN_ADMIN_IDS={MAIN_ADMIN_IDS}, ADMINS={ADMINS}")
    # Попытка заранее создать темы, которые указаны в ADMIN_THREAD_NAMES и ещё не сохранены
    for admin_chat in ADMIN_CHAT_IDS:
        try:
            await ensure_or_create_topic_for_chat(admin_chat)
        except Exception:
            pass
    # Инициализация payments DB
    try:
        await init_payments_db()
    except Exception as e:
        print(f"[WARN] Не удалось инициализировать payments db: {e}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
