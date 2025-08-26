#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import asyncio
import random
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

# ===================== ENV (robust parsing for multiple IDs) =====================
load_dotenv(".env.prem")


def _parse_int_list(env_name: str) -> List[int]:
    """Распарсить список целых из переменной окружения.
    Разделители: запятая, точка с запятой или пробел.
    Нечисловые символы в части будут удалены, если останутся цифры — то будут использованы."""
    raw = os.getenv(env_name, "")
    if raw is None:
        return []
    s = str(raw).strip()
    if not s:
        return []
    parts = []
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
ADMIN_CHAT_IDS = _parse_int_list("ADMIN_CHAT_ID")  # можно указать несколько chat id
ADMIN_CHAT_ID = ADMIN_CHAT_IDS[0] if ADMIN_CHAT_IDS else 0
MAIN_ADMIN_IDS = _parse_int_list("MAIN_ADMIN_ID")
ADMINS = _parse_int_list("ADMINS")

# Combined admin sets for permission checks
ALL_ADMINS_SET = set(ADMINS) | set(MAIN_ADMIN_IDS)

print(f"[ENV] ADMIN_CHAT_ID={ADMIN_CHAT_ID}, ADMIN_CHAT_IDS={ADMIN_CHAT_IDS}, MAIN_ADMIN_IDS={MAIN_ADMIN_IDS}, ADMINS={ADMINS}")

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
ADMIN_MAP_FILE = "admin_map.json"  # сохраняет маппинг admin_message_id -> user_id

# Buffers and tasks to collect messages sent by user within a short window
submission_buffers: Dict[str, List[Message]] = defaultdict(list)
collecting_tasks: Dict[str, asyncio.Task] = {}

# mapping admin chat message_id -> user_id (для reply из админ-чата)
admin_message_to_user: Dict[int, int] = {}

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


def load_admin_map() -> Dict[int, int]:
    if not os.path.exists(ADMIN_MAP_FILE):
        return {}
    try:
        with open(ADMIN_MAP_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
            return {int(k): int(v) for k, v in (raw.items() if isinstance(raw, dict) else {})}
    except Exception:
        return {}


def save_admin_map(m: Dict[int, int]) -> None:
    try:
        raw = {str(k): v for k, v in m.items()}
        with open(ADMIN_MAP_FILE, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[WARN] Не удалось сохранить {ADMIN_MAP_FILE}: {e}")


def set_admin_map(msg_id: int, user_id: int) -> None:
    admin_message_to_user[msg_id] = user_id
    save_admin_map(admin_message_to_user)


def remove_admin_map(msg_id: int) -> None:
    if msg_id in admin_message_to_user:
        del admin_message_to_user[msg_id]
        save_admin_map(admin_message_to_user)


# загрузим маппинг при старте
try:
    admin_message_to_user = load_admin_map()
except Exception:
    admin_message_to_user = {}

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
    return {"price": "9$"}


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


# ===================== EDIT ORIGINAL HELPERS =====================

async def _try_edit_original_message(chat_id: int, message_id: int, text: str, reply_markup, prefer_caption: bool) -> bool:
    """
    Пытается отредактировать существующее сообщение (chat_id/message_id).
    Возвращает True если одно из редактирований прошло.
    prefer_caption = True -> сначала пробуем edit_message_caption, затем edit_message_text.
    """
    # 1) Попытка 1 — предпочитаем caption (когда сообщение медиа)
    if prefer_caption:
        try:
            await bot.edit_message_caption(chat_id=chat_id, message_id=message_id, caption=text, reply_markup=reply_markup)
            return True
        except Exception:
            pass

    # 2) Попытка 2 — edit text
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=reply_markup)
        return True
    except Exception:
        pass

    # 3) Попытка 3 — если не пробовали caption в начале, попробуем сейчас
    try:
        await bot.edit_message_caption(chat_id=chat_id, message_id=message_id, caption=text, reply_markup=reply_markup)
        return True
    except Exception:
        pass

    # 4) Всё упало
    return False


# ===================== HANDLERS =====================


@dp.message(Command("start"))
async def send_welcome(message: Message):
    update_user_lang(str(message.from_user.id), message.from_user.language_code or "unknown")

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
        [InlineKeyboardButton(text=f" 🥳 Premium - {price}", callback_data="premium")],
        [InlineKeyboardButton(text=" 🪄 Поддержка", url="https://t.me/genepremiumsupportbot")],
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

    # разрешено только мейн-админам
    if message.from_user.id not in MAIN_ADMIN_IDS:
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


@dp.callback_query(F.data == "premium")
async def process_premium(callback: CallbackQuery):
    # логируем язык на нажатие кнопки Premium
    update_user_lang(str(callback.from_user.id), callback.from_user.language_code or "unknown")

    # если забанен — не продолжать
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

    # Собираем параметры для редактирования оригинала
    orig_chat_id = callback.message.chat.id
    orig_msg_id = callback.message.message_id

    # Определяем, предпочтителен ли caption (если исходное сообщение содержит медиа)
    content_type = getattr(callback.message, "content_type", None)
    prefer_caption = content_type in ("photo", "video", "document", "animation") or bool(getattr(callback.message, "photo", None))

    # Текст, который хотим поместить
    new_text = "Вы выбрали Premium"

    # Пытаемся отредактировать оригинал (несколько попыток внутри функции)
    ok = await _try_edit_original_message(orig_chat_id, orig_msg_id, new_text, keyboard, prefer_caption)

    if ok:
        # Успешно отредактировали оригинал — подтверждаем взаимодействие (тихо)
        await callback.answer()
        return

    # Если редактирование не удалось — сообщаем пользователю (не создаём новый message по вашей просьбе)
    await callback.answer("⚠️ Не удалось обновить сообщение. Попробуйте ещё раз.", show_alert=True)


@dp.callback_query(F.data == "home")
async def go_home(callback: CallbackQuery):
    # логируем язык при возврате домой
    update_user_lang(str(callback.from_user.id), callback.from_user.language_code or "unknown")

    # если забанен — не продолжать
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
        [InlineKeyboardButton(text=f" 🥳 Premium - {price}", callback_data="premium")],
        [InlineKeyboardButton(text=" 🪄 Поддержка", url="https://t.me/genepremiumsupportbot")],
    ])

    orig_chat_id = callback.message.chat.id
    orig_msg_id = callback.message.message_id

    content_type = getattr(callback.message, "content_type", None)
    prefer_caption = content_type in ("photo", "video", "document", "animation") or bool(getattr(callback.message, "photo", None))

    ok = await _try_edit_original_message(orig_chat_id, orig_msg_id, caption, keyboard, prefer_caption)

    if ok:
        await callback.answer()
        return

    # Если редактирование не удалось — уведомляем адекватно
    await callback.answer("⚠️ Не удалось обновить приветствие. Попробуйте ещё раз.", show_alert=True)


@dp.callback_query(F.data.in_(["pay_card", "pay_crypto", "pay_stars"]))
async def ask_screenshots(callback: CallbackQuery):
    update_user_lang(str(callback.from_user.id), callback.from_user.language_code or "unknown")

    if is_banned(callback.from_user.id):
        await callback.answer("🔒 Вы заблокированы.", show_alert=True)
        return

    await callback.answer()
    if callback.message.chat.type != "private":
        return
    user, user_id_str = callback.from_user, str(callback.from_user.id)
    if not can_start_new_request(user_id_str):
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

    await callback.answer("Заявка отклонена и удалена ❌")
    if callback.message.chat.id not in ADMIN_CHAT_IDS:
        return
    if callback.from_user.id not in ALL_ADMINS_SET:
        return
    user_id = callback.data.split("_", 1)[1]
    data = load_requests()
    if user_id in data:
        del data[user_id]
        save_requests(data)
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
        await bot.send_message(uid, "🔒 Вы были заблокированы.")
    except Exception as e:
        print(f"[WARN] Не удалось уведомить пользователя {uid}: {e}")

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


# ------------------ UNBAN: команда и callback ------------------

@dp.message(Command("unban"))
async def cmd_unban(message: Message):
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
            replied_id = message.reply_to_message.message_id
            target_id = admin_message_to_user.get(replied_id)
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
    if message.from_user.id not in ALL_ADMINS_SET:
        return
    banned = load_banned()
    if not banned:
        await message.reply("Список заблокированных пуст.")
        return
    text = "Заблокированные пользователи:\n" + "\n".join([str(x) for x in banned])
    await message.reply(text)


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
        admin_keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{user.id}"),
                 InlineKeyboardButton(text="🔒 Заблокировать", callback_data=f"ban_{user.id}")],
            ]
        )

        try:
            if isinstance(messages, list):
                album_msgs: List[Message] = sorted(messages, key=lambda m: m.message_id)
                media_group_ids = {getattr(m, "media_group_id", None) for m in album_msgs}
                if len(media_group_ids) == 1 and next(iter(media_group_ids)) is not None:
                    for m in album_msgs:
                        res = await bot.copy_message(chat_id=ADMIN_CHAT_ID, from_chat_id=m.chat.id, message_id=m.message_id)
                        set_admin_map(res.message_id, int(user.id))
                    header_msg = await bot.send_message(ADMIN_CHAT_ID, text=header, reply_markup=admin_keyboard)
                    set_admin_map(header_msg.message_id, int(user.id))
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
                            res = await bot.copy_message(chat_id=ADMIN_CHAT_ID, from_chat_id=m.chat.id, message_id=m.message_id)
                            set_admin_map(res.message_id, int(user.id))
                    if media_group:
                        sent = await bot.send_media_group(chat_id=ADMIN_CHAT_ID, media=media_group)
                        for s in sent:
                            set_admin_map(s.message_id, int(user.id))
                        header_msg = await bot.send_message(ADMIN_CHAT_ID, text=header, reply_markup=admin_keyboard)
                        set_admin_map(header_msg.message_id, int(user.id))
            else:
                res = await bot.copy_message(
                    chat_id=ADMIN_CHAT_ID, from_chat_id=first_message.chat.id, message_id=first_message.message_id
                )
                set_admin_map(res.message_id, int(user.id))
                header_msg = await bot.send_message(ADMIN_CHAT_ID, text=header, reply_markup=admin_keyboard)
                set_admin_map(header_msg.message_id, int(user.id))

            await bot.send_message(chat_id=user.id, text="✅ Ваша заявка отправлена администраторам.\nОжидайте ответа.")
            mark_submitted(user_id_str)

        except TelegramBadRequest as e:
            print(f"[BAD_REQUEST] {e!r}")
            await first_message.answer("⚠️ Не удалось отправить администраторам. Попробуйте ещё раз (или без подписи).")
        except Exception as e:
            print(f"[ERROR] Не удалось отправить в админ-чат: {e}")
            await first_message.answer("⚠️ Не удалось отправить администраторам.\nПопробуйте ещё раз позже.")


# Новый обработчик: собирает сообщения от пользователя в буфер и запускает задачу-коллектор
@dp.message(F.chat.type == "private")
async def collect_user_messages(message: Message):
    update_user_lang(str(message.from_user.id), message.from_user.language_code or "unknown")

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

    if not has_active_request(user_id_str) or load_requests().get(user_id_str, {}).get("submitted"):
        return

    submission_buffers[user_id_str].append(message)

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


# ===================== АДМИН: ответ reply -> пользователю =====================
@dp.message(F.chat.id.in_(ADMIN_CHAT_IDS) if ADMIN_CHAT_IDS else F.chat.id == ADMIN_CHAT_ID)
async def admin_reply_handler(message: Message):
    # разрешаем только админам
    if message.from_user.id not in ALL_ADMINS_SET:
        return

    if not message.reply_to_message:
        return

    replied = message.reply_to_message
    target_user_id = admin_message_to_user.get(replied.message_id)
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
            print(f"[LOG] Вышел из чата по сообщению {message.chat.id}")
        except Exception as e:
            print(f"[ERROR] Не удалось выйти из чата {message.chat.id}: {e}")


# ===================== MAIN =====================
async def main():
    print(f"[BOOT] ADMIN_CHAT_ID={ADMIN_CHAT_ID}, ADMIN_CHAT_IDS={ADMIN_CHAT_IDS}, MAIN_ADMIN_IDS={MAIN_ADMIN_IDS}, ADMINS={ADMINS}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
