import os
import logging
import json
import asyncio
import tempfile
from datetime import datetime

from dotenv import load_dotenv
from telegram import Update, Message
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    filters,
)

# Загружаем переменные из .env.sup
load_dotenv(".env.sup")

# Конфиг: теперь токен берётся из переменной BOT_TOKEN3
BOT_TOKEN = os.getenv("BOT_TOKEN3")
ADMIN_GROUP_ID = int(os.getenv("ADMIN_GROUP_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "mappings.json")

if not BOT_TOKEN or ADMIN_GROUP_ID == 0:
    raise RuntimeError("Please set BOT_TOKEN3 and ADMIN_GROUP_ID in .env.sup")

# Логирование
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# --- JSON DB helpers (асинхронные обёртки для file IO)
async def db_init():
    def _init():
        if not os.path.exists(DB_PATH):
            with open(DB_PATH, 'w', encoding='utf-8') as f:
                json.dump({}, f, ensure_ascii=False, indent=2)
    await asyncio.to_thread(_init)

async def db_save_mapping(group_message_id: int, user_id: int, user_message_id: int):
    def _save():
        # читаем, обновляем и атомарно записываем обратно
        try:
            if os.path.exists(DB_PATH):
                with open(DB_PATH, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            else:
                data = {}
        except Exception:
            data = {}

        key = str(group_message_id)
        data[key] = {
            "user_id": user_id,
            "user_message_id": user_message_id,
            "created_ts": datetime.utcnow().isoformat(),
        }

        # атомарная запись через временный файл
        dirn = os.path.dirname(DB_PATH) or '.'
        fd, tmppath = tempfile.mkstemp(dir=dirn)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as tmpf:
                json.dump(data, tmpf, ensure_ascii=False, indent=2)
            os.replace(tmppath, DB_PATH)
        finally:
            if os.path.exists(tmppath):
                try:
                    os.remove(tmppath)
                except Exception:
                    pass

    await asyncio.to_thread(_save)

async def db_get_mapping(group_message_id: int):
    def _get():
        try:
            with open(DB_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            return None
        key = str(group_message_id)
        row = data.get(key)
        if not row:
            return None
        return (row.get('user_id'), row.get('user_message_id'))

    return await asyncio.to_thread(_get)

# --- Хендлеры
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Это бот поддержки. Напиши сюда — и твое сообщение будет отправлено в админ-группу."
    )

async def private_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Пришло сообщение от пользователя в приватном чате
    msg: Message = update.message
    user = update.effective_user
    if not msg:
        return
    if user.is_bot:
        return

    logger.info("Новое сообщение от %s (%s): %s", user.full_name, user.id, getattr(msg, 'text', None))

    # Пересылаем сообщение в админ-группу
    try:
        forwarded = await context.bot.forward_message(
            chat_id=ADMIN_GROUP_ID,
            from_chat_id=msg.chat.id,
            message_id=msg.message_id,
        )

        # Сохраняем соответствие
        await db_save_mapping(forwarded.message_id, msg.chat.id, msg.message_id)

        # Уведомим пользователя
        await msg.reply_text("Ваше сообщение отправлено в поддержку — ответ придёт сюда.")

    except Exception as e:
        logger.exception("Ошибка при пересылке в админ-группу: %s", e)
        await msg.reply_text("Не удалось отправить сообщение в поддержку. Попробуйте позже.")


async def group_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Сообщения внутри админ-группы
    msg: Message = update.message
    if not msg or not msg.chat:
        return

    if msg.chat.id != ADMIN_GROUP_ID:
        return

    # Нам важны ответы-reply на пересланные сообщения
    if not msg.reply_to_message:
        # Не обрабатываем обычные сообщения в группе
        return

    replied_id = msg.reply_to_message.message_id
    mapping = await db_get_mapping(replied_id)
    if not mapping:
        # Нет сопоставления —, возможно ответили на другое сообщение
        return

    user_id, user_msg_id = mapping

    # Проверяем, является ли отправитель админом в группе
    try:
        member = await context.bot.get_chat_member(ADMIN_GROUP_ID, update.effective_user.id)
        if member.status not in ("administrator", "creator"):
            await msg.reply_text("Только администраторы могут отвечать пользователям через этого бота.")
            return
    except Exception:
        logger.exception("Не удалось проверить статус члена группы")

    # Формируем подпись ответа (кто ответил)
    sender = update.effective_user
    sender_name = sender.full_name
    sender_info = f"От поддержки — {sender_name}"

    # Теперь пересылаем сообщение обратно пользователю. Поддерживаем разные типы контента.
    try:
        # Текст
        if msg.text or msg.caption:
            text_to_send = ''
            if msg.text:
                text_to_send = msg.text
            else:
                text_to_send = msg.caption or ''

            # Добавим небольшую подпись
            text_with_meta = f"{text_to_send}

— {sender_info}"

            await context.bot.send_message(
                chat_id=user_id,
                text=text_with_meta,
                reply_to_message_id=user_msg_id if user_msg_id else None,
            )
            return

        # Фото
        if msg.photo:
            # Берём наибольшее по размеру фото
            photo = msg.photo[-1]
            await context.bot.send_photo(
                chat_id=user_id,
                photo=photo.file_id,
                caption=( (msg.caption or '') + f"

— {sender_info}" ).strip(),
                reply_to_message_id=user_msg_id if user_msg_id else None,
            )
            return

        # Документы
        if msg.document:
            await context.bot.send_document(
                chat_id=user_id,
                document=msg.document.file_id,
                caption=( (msg.caption or '') + f"

— {sender_info}" ).strip(),
                reply_to_message_id=user_msg_id if user_msg_id else None,
            )
            return

        # Голосовые
        if msg.voice:
            await context.bot.send_voice(
                chat_id=user_id,
                voice=msg.voice.file_id,
                caption=f"— {sender_info}",
                reply_to_message_id=user_msg_id if user_msg_id else None,
            )
            return

        # Видео / аудио
        if msg.video:
            await context.bot.send_video(
                chat_id=user_id,
                video=msg.video.file_id,
                caption=( (msg.caption or '') + f"

— {sender_info}" ).strip(),
                reply_to_message_id=user_msg_id if user_msg_id else None,
            )
            return

        if msg.audio:
            await context.bot.send_audio(
                chat_id=user_id,
                audio=msg.audio.file_id,
                caption=( (msg.caption or '') + f"

— {sender_info}" ).strip(),
                reply_to_message_id=user_msg_id if user_msg_id else None,
            )
            return

        # Стикеры
        if msg.sticker:
            await context.bot.send_sticker(
                chat_id=user_id,
                sticker=msg.sticker.file_id,
            )
            return

        # Контакт / location — пересылаем текстовую репрезентацию
        if msg.contact:
            contact = msg.contact
            text = f"Контакт: {contact.first_name} {contact.last_name or ''}
Телефон: {contact.phone_number}
— {sender_info}"
            await context.bot.send_message(chat_id=user_id, text=text)
            return

        if msg.location:
            await context.bot.send_location(chat_id=user_id, latitude=msg.location.latitude, longitude=msg.location.longitude)
            return

        # По умолчанию: переслать сообщение как есть (forward)
        await context.bot.forward_message(chat_id=user_id, from_chat_id=msg.chat.id, message_id=msg.message_id)

    except Exception as e:
        logger.exception("Ошибка при отправке ответа пользователю: %s", e)
        try:
            await msg.reply_text("Не удалось отправить ответ пользователю: %s" % e)
        except Exception:
            pass


async def get_group_id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Команда, которую можно вызвать в группе чтобы узнать её id
    chat = update.effective_chat
    if chat:
        await update.message.reply_text(f"chat_id = {chat.id}")


async def main():
    await db_init()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("get_group_id", get_group_id_command))

    # Хендлеры: приватные сообщения -> в админ-группу
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, private_message_handler))

    # Хендлер для сообщений в админ-группе (отвечаем пользователям)
    app.add_handler(MessageHandler(filters.Chat(ADMIN_GROUP_ID), group_message_handler))

    logger.info("Bot started")
    await app.run_polling()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")
