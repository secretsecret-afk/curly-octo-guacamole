import os
import asyncio
import json
from datetime import datetime, timezone
from typing import Optional, List, Dict

from aiogram import Bot, Dispatcher, F
from aiogram.types import LabeledPrice, PreCheckoutQuery, Message
from aiogram.filters import Command
import aiohttp
from dotenv import load_dotenv

# Load .env.preme
load_dotenv(".env.preme")

BOT_TOKEN = os.getenv("PREME")
if not BOT_TOKEN:
    raise RuntimeError("PREME env var required in .env.preme")

OWNER_ID_ENV = os.getenv("OWNER_ID")  # optional: для ручных возвратов
OWNER_ID = int(OWNER_ID_ENV) if OWNER_ID_ENV and OWNER_ID_ENV.isdigit() else None

DB_FILE = "payments.json"

# ---- JSON storage helpers (atomic write) ----
def _init_db_sync():
    if not os.path.exists(DB_FILE):
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump([], f)

def _read_all_sync() -> List[Dict]:
    if not os.path.exists(DB_FILE):
        return []
    with open(DB_FILE, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception:
            return []

def _write_all_sync(data: List[Dict]):
    tmp = DB_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DB_FILE)

def _save_payment_sync(record: Dict):
    data = _read_all_sync()
    for r in data:
        if r.get("charge_id") == record.get("charge_id"):
            return False
    data.append(record)
    _write_all_sync(data)
    return True

def _mark_refunded_sync(charge_id: str) -> bool:
    data = _read_all_sync()
    changed = False
    for r in data:
        if r.get("charge_id") == charge_id and not r.get("refunded"):
            r["refunded"] = True
            r["refunded_at"] = datetime.now(timezone.utc).isoformat()
            changed = True
    if changed:
        _write_all_sync(data)
    return changed

def _get_payment_by_charge_sync(charge_id: str) -> Optional[Dict]:
    data = _read_all_sync()
    for r in data:
        if r.get("charge_id") == charge_id:
            return r
    return None

async def init_db():
    await asyncio.to_thread(_init_db_sync)

async def save_payment(user_id: int, charge_id: str, payload: str, amount: int, currency: str):
    record = {
        "user_id": user_id,
        "charge_id": charge_id,
        "payload": payload,
        "amount": amount,
        "currency": currency,
        "refunded": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "refunded_at": None,
    }
    return await asyncio.to_thread(_save_payment_sync, record)

async def mark_refunded(charge_id: str) -> bool:
    return await asyncio.to_thread(_mark_refunded_sync, charge_id)

async def get_payment_by_charge(charge_id: str) -> Optional[Dict]:
    return await asyncio.to_thread(_get_payment_by_charge_sync, charge_id)

# ---- Telegram API refund call (логируем тело ответа) ----
async def refund_star_payment(user_id: int, telegram_payment_charge_id: str) -> dict:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/refundStarPayment"
    payload = {"user_id": user_id, "telegram_payment_charge_id": telegram_payment_charge_id}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as resp:
            text = await resp.text()
            try:
                js = json.loads(text)
            except Exception:
                js = {"ok": False, "error": "invalid_json", "text": text}
            print("refundStarPayment response:", js)  # лог в stdout
            return js

# ---- Aiogram v3 setup ----
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ---- /buy command
# usage examples:
#   /buy            -> amount=50
#   /buy 100        -> amount=100
#   /buy auto_refund -> amount=50 + auto-refund flag
#   /buy 100 r      -> amount=100 + auto-refund
@dp.message(Command(commands=["buy"]))
async def cmd_buy(message: Message):
    parts = message.text.split()[1:]  # убираем команду
    amount = 50
    auto_refund = False

    for p in parts:
        p_low = p.lower()
        if p_low in ("auto_refund", "autoref", "r"):
            auto_refund = True
            continue
        try:
            amount = int(p)
            continue
        except Exception:
            # игнорируем непонятные токены
            continue

    if amount <= 0:
        await message.reply("Сумма должна быть положительной.")
        return

    title = "Премиум-доступ"
    description = f"Доступ к премиум на {amount} stars"
    if auto_refund:
        payload = f"auto_refund:order_{message.from_user.id}_{int(datetime.now(timezone.utc).timestamp())}"
    else:
        payload = f"order_{message.from_user.id}_{int(datetime.now(timezone.utc).timestamp())}"

    prices = [LabeledPrice(label=title, amount=amount)]

    try:
        await bot.send_invoice(
            chat_id=message.chat.id,
            title=title,
            description=description,
            payload=payload,
            provider_token="",  # empty for Stars (XTR)
            currency="XTR",
            prices=prices,
        )
    except Exception as e:
        await message.reply(f"Ошибка при отправке инвойса: {e}")

# ---- Pre-checkout query: подтверждаем
@dp.pre_checkout_query()
async def pre_checkout_handler(pre_checkout: PreCheckoutQuery):
    await pre_checkout.answer(ok=True)

# ---- Successful payment handler (F.successful_payment)
@dp.message(F.successful_payment)
async def successful_payment_handler(message: Message):
    sp = message.successful_payment
    user_id = message.from_user.id
    charge_id = sp.telegram_payment_charge_id
    payload = sp.invoice_payload or ""
    total_amount = sp.total_amount
    currency = sp.currency

    saved = await save_payment(user_id=user_id, charge_id=charge_id, payload=payload, amount=total_amount, currency=currency)
    if not saved:
        await message.reply("Транзакция уже существует в базе — пропускаем сохранение.")

    await message.reply("Спасибо за оплату! Проверяем доставку товара...")

    if "auto_refund" in payload:
        await message.reply("Требуется авто-возврат по правилам payload — выполняю возврат...")
        result = await refund_star_payment(user_id, charge_id)
        if result.get("ok"):
            await mark_refunded(charge_id)
            await message.reply("Возврат выполнен успешно.")
        else:
            # покажем тело ответа для дебага
            await message.reply(f"Не удалось выполнить возврат: {result}")
        return

    # логика выдачи товара/доступа
    await message.reply("Доступ выдан — всё готово!")

# ---- Ручной возврат: /refund <charge_id> (доступен только OWNER_ID, если задан)
@dp.message(Command(commands=["refund"]))
async def cmd_refund(message: Message):
    if OWNER_ID is None:
        await message.reply("Ручный возврат отключён: OWNER_ID не задан в .env.preme")
        return

    if message.from_user.id != OWNER_ID:
        await message.reply("У вас нет прав на выполнение этой команды.")
        return

    parts = message.text.split()
    if len(parts) < 2:
        await message.reply("Использование: /refund <telegram_payment_charge_id>")
        return

    charge_id = parts[1].strip()
    rec = await get_payment_by_charge(charge_id)
    if not rec:
        await message.reply("Транзакция не найдена в payments.json")
        return
    if rec.get("refunded"):
        await message.reply("Транзакция уже помечена как возвращённая.")
        return

    user_id = rec.get("user_id")
    await message.reply(f"Инициализация возврата для charge_id={charge_id} user_id={user_id}...")
    result = await refund_star_payment(user_id, charge_id)
    if result.get("ok"):
        await mark_refunded(charge_id)
        await message.reply("Возврат выполнен успешно.")
    else:
        await message.reply(f"Ошибка при возврате: {result}")

# ---- Запуск
async def main():
    await init_db()
    print("Бот запущен (polling)...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Завершение...")
