import os
import json
import logging
import tempfile
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# Загружаем переменные окружения из .env.sup
load_dotenv(dotenv_path=".env.sup")

BOT_TOKEN = os.getenv("BOT_TOKEN3")
ADMIN_GROUP_ID = int(os.getenv("ADMIN_GROUP_ID"))
DB_PATH = os.getenv("DB_PATH", "mappings.json")
THREAD_ID = os.getenv("THREAD_ID")
THREAD_ID = int(THREAD_ID) if THREAD_ID else None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ------------------ Работа с JSON DB ------------------
def load_db():
    if not os.path.exists(DB_PATH):
        return {}
    try:
        with open(DB_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_db(data):
    tmp = DB_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DB_PATH)


# ------------------ Хендлеры ------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Напишите сюда свой вопрос по Gene Premium Ultimate.")


async def forward_to_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    user_id = user.id
    user_message_id = update.message.message_id

    # Пересылаем сообщение в админ-группу
    fwd = await context.bot.forward_message(
        chat_id=ADMIN_GROUP_ID,
        from_chat_id=update.message.chat_id,
        message_id=user_message_id,
        message_thread_id=THREAD_ID if THREAD_ID else None
    )

    # Сохраняем соответствие
    db = load_db()
    db[str(fwd.message_id)] = {
        "user_id": user_id,
        "user_message_id": user_message_id,
        "created_ts": update.message.date.isoformat(),
    }
    save_db(db)


async def reply_from_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        return

    # Проверяем, что сообщение — ответ на пересланное
    db = load_db()
    key = str(update.message.reply_to_message.message_id)
    if key not in db:
        return

    user_id = db[key]["user_id"]

    # Отправляем ответ пользователю
    if update.message.text:
        await context.bot.send_message(chat_id=user_id, text=update.message.text)
    elif update.message.sticker:
        await context.bot.send_sticker(chat_id=user_id, sticker=update.message.sticker.file_id)
    elif update.message.photo:
        await context.bot.send_photo(chat_id=user_id, photo=update.message.photo[-1].file_id, caption=update.message.caption)
    elif update.message.document:
        await context.bot.send_document(chat_id=user_id, document=update.message.document.file_id, caption=update.message.caption)
    elif update.message.voice:
        await context.bot.send_voice(chat_id=user_id, voice=update.message.voice.file_id, caption=update.message.caption)
    elif update.message.video:
        await context.bot.send_video(chat_id=user_id, video=update.message.video.file_id, caption=update.message.caption)
    elif update.message.audio:
        await context.bot.send_audio(chat_id=user_id, audio=update.message.audio.file_id, caption=update.message.caption)
    elif update.message.contact:
        await context.bot.send_contact(chat_id=user_id, phone_number=update.message.contact.phone_number, first_name=update.message.contact.first_name)
    elif update.message.location:
        await context.bot.send_location(chat_id=user_id, latitude=update.message.location.latitude, longitude=update.message.location.longitude)
    else:
        # На всякий случай пересылаем
        await context.bot.forward_message(chat_id=user_id, from_chat_id=update.message.chat_id, message_id=update.message.message_id)


async def get_group_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    await update.message.reply_text(f"chat_id = {chat.id}, thread_id = {update.message.message_thread_id}")


# ------------------ main ------------------
def main():
    if not BOT_TOKEN or not ADMIN_GROUP_ID:
        raise RuntimeError("BOT_TOKEN3 и ADMIN_GROUP_ID должны быть заданы в .env.sup")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("get_group_id", get_group_id))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, forward_to_group))
    app.add_handler(MessageHandler(filters.ChatType.SUPERGROUP & ~filters.COMMAND, reply_from_admin))

    logger.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
