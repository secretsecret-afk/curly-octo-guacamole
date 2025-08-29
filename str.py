import os
import asyncio
import json
from datetime import datetime
from typing import Optional, List, Dict

from aiogram import Bot, Dispatcher, types
from aiogram.types import LabeledPrice, ContentType, PreCheckoutQuery
from aiogram.filters import Command
import aiohttp
from dotenv import load_dotenv

load_dotenv('.env.preme')

BOT_TOKEN = os.getenv('PREME')
if not BOT_TOKEN:
    raise RuntimeError('PREME env var required in .env.preme')

DB_FILE = 'payments.json'

def _init_db_sync():
    if not os.path.exists(DB_FILE):
        with open(DB_FILE, 'w', encoding='utf-8') as f:
            json.dump([], f)

def _read_all_sync() -> List[Dict]:
    if not os.path.exists(DB_FILE):
        return []
    with open(DB_FILE, 'r', encoding='utf-8') as f:
        try:
            data = json.load(f)
            if isinstance(data, list):
                return data
            return []
        except Exception:
            return []

def _write_all_sync(data: List[Dict]):
    tmp = DB_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DB_FILE)

def _save_payment_sync(record: Dict):
    data = _read_all_sync()
    for r in data:
        if r.get('charge_id') == record.get('charge_id'):
            return False
    data.append(record)
    _write_all_sync(data)
    return True

def _mark_refunded_sync(charge_id: str) -> bool:
    data = _read_all_sync()
    changed = False
    for r in data:
        if r.get('charge_id') == charge_id and not r.get('refunded'):
            r['refunded'] = True
            r['refunded_at'] = datetime.utcnow().isoformat()
            changed = True
    if changed:
        _write_all_sync(data)
    return changed

def _get_payment_by_charge_sync(charge_id: str) -> Optional[Dict]:
    data = _read_all_sync()
    for r in data:
        if r.get('charge_id') == charge_id:
            return r
    return None

async def init_db():
    await asyncio.to_thread(_init_db_sync)

async def save_payment(user_id: int, charge_id: str, payload: str, amount: int, currency: str):
    record = {
        'user_id': user_id,
        'charge_id': charge_id,
        'payload': payload,
        'amount': amount,
        'currency': currency,
        'refunded': False,
        'created_at': datetime.utcnow().isoformat(),
        'refunded_at': None,
    }
    return await asyncio.to_thread(_save_payment_sync, record)

async def mark_refunded(charge_id: str) -> bool:
    return await asyncio.to_thread(_mark_refunded_sync, charge_id)

async def get_payment_by_charge(charge_id: str) -> Optional[Dict]:
    return await asyncio.to_thread(_get_payment_by_charge_sync, charge_id)

async def refund_star_payment(user_id: int, telegram_payment_charge_id: str) -> dict:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/refundStarPayment"
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json={'user_id': user_id, 'telegram_payment_charge_id': telegram_payment_charge_id}) as resp:
            try:
                return await resp.json()
            except Exception:
                text = await resp.text()
                return {'ok': False, 'error': 'invalid_json_response', 'text': text}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

@dp.message(Command(commands=['buy']))
async def cmd_buy(message: types.Message):
    args = message.text.split()
    amount = 50
    if len(args) >= 2:
        try:
            amount = int(args[1])
        except ValueError:
            await message.reply('Неверная сумма, используйте целое число (количество звёзд).')
            return
    if amount <= 0:
        await message.reply('Сумма должна быть положительной.')
        return

    title = 'Премиум-доступ'
    description = f'Доступ к премиум на {amount} stars'
    payload = f'order_{message.from_user.id}_{int(datetime.utcnow().timestamp())}'

    prices = [LabeledPrice(label=title, amount=amount)]

    try:
        await bot.send_invoice(
            chat_id=message.chat.id,
            title=title,
            description=description,
            payload=payload,
            provider_token='',
            currency='XTR',
            prices=prices,
        )
    except Exception as e:
        await message.reply(f'Ошибка при отправке инвойса: {e}')

@dp.pre_checkout_query()
async def pre_checkout(pre_checkout_q: PreCheckoutQuery):
    await pre_checkout_q.answer(ok=True)

@dp.message(content_types=ContentType.SUCCESSFUL_PAYMENT)
async def successful_payment_handler(message: types.Message):
    sp = message.successful_payment
    user_id = message.from_user.id
    charge_id = sp.telegram_payment_charge_id
    payload = sp.invoice_payload
    total_amount = sp.total_amount
    currency = sp.currency

    saved = await save_payment(user_id=user_id, charge_id=charge_id, payload=payload, amount=total_amount, currency=currency)
    if not saved:
        await message.reply('Транзакция уже существует в базе — пропускаем сохранение.')

    await message.reply('Спасибо за оплату! Проверяем доставку товара...')

    if payload.startswith('auto_refund'):
        await message.reply('Требуется авто-возврат по правилам payload — выполняю возврат...')
        result = await refund_star_payment(user_id, charge_id)
        if result.get('ok'):
            await mark_refunded(charge_id)
            await message.reply('Возврат выполнен успешно.')
        else:
            await message.reply(f'Не удалось выполнить возврат: {result}')
        return

    await message.reply('Доступ выдан — всё готово!')

async def main():
    await init_db()
    print('Бот запущен (polling)...')
    await dp.start_polling(bot)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('Завершение...')
