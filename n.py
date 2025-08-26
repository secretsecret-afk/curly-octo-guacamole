import os
import json
import asyncio
import random
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Union
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

# ===================== ENV =====================
load_dotenv(".env.prem")
API_TOKEN = os.getenv("BOT_TOKEN2")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
MAIN_ADMIN_ID = int(os.getenv("MAIN_ADMIN_ID", "0"))
ADMINS = [int(x) for x in os.getenv("ADMINS", "").split(",") if x.strip()]

if not API_TOKEN:
    raise RuntimeError("BOT_TOKEN2 не найден в .env.prem")
if ADMIN_CHAT_ID == 0:
    print("[WARN] ADMIN_CHAT_ID=0 — заявки не попадут в админ-чат.\nПроверь .env.prem")

bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Объект для блокировки одновременной обработки заявок от одного пользователя
user_submission_locks = defaultdict(asyncio.Lock)

REQUESTS_FILE = "requests.json"
CONFIG_FILE = "config.json"
WELCOME_IMAGE = "IMG_20250825_170645_742.jpg"
BANNED_FILE = "banned.json"

# Buffers and tasks to collect messages sent by user within a short window
submission_buffers: Dict[str, List[Message]] = defaultdict(list)
collecting_tasks: Dict[str, asyncio.Task] = {}

# mapping admin chat message_id -> user_id (для reply из админ-чата)
admin_message_to_user: Dict[int, int] = {}

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
    except (json.JSONDecodeError, IOError):
        print(f"[WARN] {REQUESTS_FILE} поврежден или не читается, создаем новый.")
        data = {}
    now = _now()
    changed = False
    for uid, rec in list(data.items()):
        if rec.get("started_at"):
            started = rec.get("started_at")
            try:
                if started and now - datetime.fromisoformat(started) > timedelta(days=3):
                    del data[uid]
                    changed = True
            except (ValueError, TypeError):
                del data[uid]
                changed = True
    if changed:
        save_requests(data)
    return data


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
            return json.load(f)
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


def is_banned(uid: int) -> bool:
    return uid in load_banned()


def update_user_lang(user_id: str, lang: str) -> List[str]:
    """
    Добавляет язык в список языков пользователя (если ещё нет) и сохраняет запись.
    """
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


def can_start_new_request(user_id: str) -> bool:
    if is_banned(int(user_id)):
        return False
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
        if message.chat.id != ADMIN_CHAT_ID:
            try:
                await bot.leave_chat(message.chat.id)
                print(f"[LOG] Вышел из чата {message.chat.id}")
            except Exception as e:
                print(f"[ERROR] Не удалось выйти из чата {message.chat.id}: {e}")
        return False
    return True


# ===================== HANDLERS =====================


@dp.message(Command("start"))
async def send_welcome(message: Message):
    # логируем язык при команде start
    update_user_lang(str(message.from_user.id), message.from_user.language_code or "unknown")

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
    # логируем язык админа, если нужен
    update_user_lang(str(message.from_user.id), message.from_user.language_code or "unknown")

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


@dp.callback_query(F.data == "premium")
async def process_premium(callback: CallbackQuery):
    # логируем язык на нажатие кнопки Premium
    update_user_lang(str(callback.from_user.id), callback.from_user.language_code or "unknown")

    await callback.answer()
    if callback.message.chat.type != "private":
        return
    # Build payment keyboard with emojis and Home button
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇷🇺 Картой", callback_data="pay_card")],
        [InlineKeyboardButton(text="🌎 Crypto (@send) (0%)", callback_data="pay_crypto")],
        [InlineKeyboardButton(text="⭐ Telegram Stars", callback_data="pay_stars")],
        [InlineKeyboardButton(text="🔒 Заблокировать (только админы)", callback_data="noop")],  # placeholder in user UI
        [InlineKeyboardButton(text="🏠", callback_data="home")],
    ])

    # Instead of sending a new separate message with price, edit the original message/caption
    try:
        if callback.message.photo:
            await callback.message.edit_caption("Вы выбрали Premium", reply_markup=keyboard)
        else:
            await callback.message.edit_text("Вы выбрали Premium", reply_markup=keyboard)
    except Exception:
        # fallback — just send a new message if edit fails
        await callback.message.answer("Вы выбрали Premium", reply_markup=keyboard)


@dp.callback_query(F.data == "home")
async def go_home(callback: CallbackQuery):
    # логируем язык при возврате домой
    update_user_lang(str(callback.from_user.id), callback.from_user.language_code or "unknown")

    await callback.answer()
    if callback.message.chat.type != "private":
        return
    # Recreate the welcome screen (try to edit caption if there is photo)
    price = load_config()["price"]
    caption = (
        "Добро пожаловать! Я платёжный бот Gene's Land!\n\n"
        "Здесь вы можете приобрести Premium-версию Gene Brawl!\n\n"
        "Gene Premium Ultimate выдается навсегда.\n"
        "(Нажмите на товар, чтобы узнать подробности)"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f" Premium - {price}", callback_data="premium")],
        [InlineKeyboardButton(text=" Поддержка", url="https://t.me/genepremiumsupportbot")],
    ])
    try:
        if os.path.exists(WELCOME_IMAGE) and callback.message.photo:
            # If the message already has a photo, just edit caption back
            await callback.message.edit_caption(caption, reply_markup=keyboard)
        else:
            # Try to edit text; if impossible, send a new welcome message and delete old
            try:
                await callback.message.edit_text(caption, reply_markup=keyboard)
            except Exception:
                # delete old and send a fresh welcome (keeps UI clean)
                try:
                    await callback.message.delete()
                except Exception:
                    pass
                if os.path.exists(WELCOME_IMAGE):
                    await bot.send_photo(chat_id=callback.from_user.id, photo=FSInputFile(WELCOME_IMAGE),
                                         caption=caption, reply_markup=keyboard)
                else:
                    await bot.send_message(chat_id=callback.from_user.id, text=caption, reply_markup=keyboard)
    except Exception as e:
        print(f"[WARN] Не удалось вернуть домой: {e}")
        # fallback
        await callback.message.answer(caption, reply_markup=keyboard)


@dp.callback_query(F.data.in_(["pay_card", "pay_crypto", "pay_stars"]))
async def ask_screenshots(callback: CallbackQuery):
    # логируем язык при выборе способа оплаты
    update_user_lang(str(callback.from_user.id), callback.from_user.language_code or "unknown")

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
        # NOTE: removed the '🔔 Жду ваши сообщения' message as requested
        if user_id_str in data:
            data[user_id_str]["has_seen_instructions"] = True
            save_requests(data)
    else:
        await callback.message.answer(instruction)
        # NOTE: removed the '🔔 Жду ваши сообщения' message as requested


@dp.callback_query(F.data.startswith("reject_"))
async def reject_request(callback: CallbackQuery):
    # логируем язык админа
    update_user_lang(str(callback.from_user.id), callback.from_user.language_code or "unknown")

    await callback.answer("Заявка отклонена и удалена ❌")
    if callback.message.chat.id != ADMIN_CHAT_ID:
        return
    if callback.from_user.id not in ADMINS and callback.from_user.id != MAIN_ADMIN_ID:
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
    # логируем язык админа
    update_user_lang(str(callback.from_user.id), callback.from_user.language_code or "unknown")

    await callback.answer("Пользователь заблокирован 🔒")
    if callback.message.chat.id != ADMIN_CHAT_ID:
        return
    if callback.from_user.id not in ADMINS and callback.from_user.id != MAIN_ADMIN_ID:
        return
    user_id = callback.data.split("_", 1)[1]
    try:
        ban_user_by_id(int(user_id))
    except Exception as e:
        print(f"[WARN] Не удалось заблокировать {user_id}: {e}")
    data = load_requests()
    if user_id in data:
        del data[user_id]
        save_requests(data)
    try:
        await bot.send_message(user_id, "🔒 Вы были заблокированы.")
    except Exception as e:
        print(f"[WARN] Не удалось уведомить пользователя {user_id}: {e}")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


# ===================== ПРИЁМ ЗАЯВОК (с копированием) =====================


# Internal function that actually sends collected messages to admin chat
async def handle_submission(messages: Union[Message, List[Message]]):
    # Определяем первое сообщение и проверяем личку
    first_message: Message = messages[0] if isinstance(messages, list) else messages
    if not await ensure_private_and_autoleave(first_message):
        return
    user = first_message.from_user
    user_id_str = str(user.id)

    async with user_submission_locks[user_id_str]:
        if not has_active_request(user_id_str):
            return
        update_user_lang(user_id_str, user.language_code or "unknown")

        # Шапка для админов + клавиатура
        safe_full_name = escape(user.full_name or "(без имени)")
        safe_username = f"@{escape(user.username)}" if user.username else ""
        data = load_requests()
        langs = data.get(user_id_str, {}).get("langs", [user.language_code or "неизвестно"])
        safe_langs = ", ".join([escape(x) for x in langs])
        header = f"{safe_full_name} {safe_username}\nID: {user.id}\nЯзыки: {safe_langs}"
        admin_keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{user.id}"),
                 InlineKeyboardButton(text="🔒 Заблокировать", callback_data=f"ban_{user.id}")],
            ]
        )

        try:
            # ===== МНОГО СООБЩЕНИЙ (включая альбомы) =====
            if isinstance(messages, list):
                # Сортируем по id (порядок прихода не гарантирован)
                album_msgs: List[Message] = sorted(messages, key=lambda m: m.message_id)

                # Если это настоящий альбом — все сообщения имеют одинаковый media_group_id
                media_group_ids = {getattr(m, "media_group_id", None) for m in album_msgs}
                if len(media_group_ids) == 1 and next(iter(media_group_ids)) is not None:
                    # Для альбомов: копируем все media (telegram сохранит порядок),
                    # затем отправляем шапку с кнопкой
                    for m in album_msgs:
                        res = await bot.copy_message(chat_id=ADMIN_CHAT_ID, from_chat_id=m.chat.id, message_id=m.message_id)
                        # связываем скопированное сообщение с user_id
                        admin_message_to_user[res.message_id] = int(user.id)
                    header_msg = await bot.send_message(ADMIN_CHAT_ID, text=header, reply_markup=admin_keyboard)
                    admin_message_to_user[header_msg.message_id] = int(user.id)

                else:
                    # Не альбом — собираем InputMedia и отправляем как media_group когда возможно
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
                            # если встретился тип, не поддерживаемый в альбомах — просто докинем отдельно
                            res = await bot.copy_message(chat_id=ADMIN_CHAT_ID, from_chat_id=m.chat.id, message_id=m.message_id)
                            admin_message_to_user[res.message_id] = int(user.id)
                    if media_group:
                        sent = await bot.send_media_group(chat_id=ADMIN_CHAT_ID, media=media_group)
                        # sent — список сообщений, свяжем все с user_id
                        for s in sent:
                            admin_message_to_user[s.message_id] = int(user.id)
                        header_msg = await bot.send_message(ADMIN_CHAT_ID, text=header, reply_markup=admin_keyboard)
                        admin_message_to_user[header_msg.message_id] = int(user.id)

            # ===== ОДИНОЧНОЕ СООБЩЕНИЕ =====
            else:
                res = await bot.copy_message(
                    chat_id=ADMIN_CHAT_ID, from_chat_id=first_message.chat.id, message_id=first_message.message_id
                )
                admin_message_to_user[res.message_id] = int(user.id)
                header_msg = await bot.send_message(ADMIN_CHAT_ID, text=header, reply_markup=admin_keyboard)
                admin_message_to_user[header_msg.message_id] = int(user.id)

            # уведомляем пользователя и помечаем заявку
            await bot.send_message(chat_id=user.id, text="✅ Ваша заявка отправлена администраторам.\nОжидайте ответа.")
            mark_submitted(user_id_str)

        except TelegramBadRequest as e:
            # частый кейс: неверная комбинация media/caption или слишком длинная подпись
            print(f"[BAD_REQUEST] {e!r}")
            await first_message.answer("⚠️ Не удалось отправить администраторам. Попробуйте ещё раз (или без подписи).")
        except Exception as e:
            print(f"[ERROR] Не удалось отправить в админ-чат: {e}")
            await first_message.answer("⚠️ Не удалось отправить администраторам.\nПопробуйте ещё раз позже.")


# Новый обработчик: собирает сообщения от пользователя в буфер и запускает задачу-коллектор
@dp.message(F.chat.type == "private")
async def collect_user_messages(message: Message):
    # логируем язык при любом личном сообщении
    update_user_lang(str(message.from_user.id), message.from_user.language_code or "unknown")

    if not await ensure_private_and_autoleave(message):
        return
    user = message.from_user
    user_id_str = str(user.id)

    # Если у пользователя нет активной заявки — ничего не делаем
    if not has_active_request(user_id_str) or load_requests().get(user_id_str, {}).get("submitted"):
        return

    # Положим сообщение в буфер
    submission_buffers[user_id_str].append(message)

    # Если уже есть активная задача — ничего не создаём
    existing = collecting_tasks.get(user_id_str)
    if existing and not existing.done():
        return

    # Параллельный коллектор: ждёт 3 секунды и отправляет собранное
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
@dp.message(F.chat.id == ADMIN_CHAT_ID)
async def admin_reply_handler(message: Message):
    """
    Если админ отвечает reply'ом на сообщение в админ-чате, и это сообщение было ранее
    связано с user_id (в admin_message_to_user), то копируем сообщение (reply от админа)
    пользователю.
    """
    # разрешаем только админам
    if message.from_user.id not in ADMINS and message.from_user.id != MAIN_ADMIN_ID:
        return

    if not message.reply_to_message:
        return

    replied = message.reply_to_message
    target_user_id = admin_message_to_user.get(replied.message_id)
    if not target_user_id:
        # Если нет в маппинге — попробуем проверить reply_to_message.forward_from (иногда присутствует)
        ffrom = getattr(replied, "forward_from", None)
        if ffrom and getattr(ffrom, "id", None):
            target_user_id = ffrom.id

    if not target_user_id:
        # не удалось сопоставить
        return

    # логируем язык админа (опционально)
    update_user_lang(str(message.from_user.id), message.from_user.language_code or "unknown")

    try:
        # копируем сообщение из админ-чата в чат пользователя
        await bot.copy_message(chat_id=target_user_id, from_chat_id=message.chat.id, message_id=message.message_id)
        # можно уведомить в админ-чате об успехе (тихо)
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
    asyncio.run(main())
