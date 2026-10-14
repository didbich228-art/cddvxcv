import asyncio
import io
import logging
import aiohttp
import aiosqlite
from datetime import date
from zoneinfo import ZoneInfo
from typing import Any, Awaitable, Callable, Dict
from aiogram import Bot, Dispatcher, F, Router, BaseMiddleware
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup,
                            InlineKeyboardButton, TelegramObject, BufferedInputFile)
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

BOT_TOKEN        = "8966602990:AAG0NpXjiwfyHsLm_8WrzgWjJ1NbEVoY2fg"
ADMIN_IDS        = [8734322507]
CRYPTO_BOT_TOKEN = "540061:AADcpP4WKNOudNk0XwMOLDb79lwrSnmXgTo"
CRYPTO_BOT_API   = "https://pay.crypt.bot/api"
DB_PATH          = "bot.db"

# Telegram Premium sticker file_ids used by the bot UI.
STICKER_IDS = {
    "lightning": "5348235470461483629",
    "down": "5231102735817918643",
    "diamond": "5296742257146241213",
    "fire": "5309888005283013801",
    "dollar": "5116648080787112958",
    "add_user": "5427026327679051702",
    "catalog": "4927352014305756514",
    "card": "4924998397997352581",
    "back": "5213343424006100318",
    "profile": "5213043033993421435",
    "attention": "5116275208906343429",
    "send_user": "5427202047676028278",
    "search": "5426981458155707501",
    "minus": "5427193629540128339",
    "wallet": "5426879920833863274",
    "plus": "5429528137539035543",
    "max": "5449407131675558756",
}

def ui_button(text, *, icon=None, **kwargs):
    if icon:
        kwargs["icon_custom_emoji_id"] = STICKER_IDS[icon]
    return InlineKeyboardButton(text=text, **kwargs)

def premium_icon(name, fallback="✦"):
    return f'<tg-emoji emoji-id="{STICKER_IDS[name]}">{fallback}</tg-emoji>'

logging.basicConfig(level=logging.INFO)
bot    = Bot(token=BOT_TOKEN)
router = Router()

import time as _time
import random
from html import escape

bot_enabled         = True
_queue_pick_counter = 0
_presence_tracker: dict[int, dict] = {}
_paused_sellers: set[int] = set()


class BotEnabledMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = None
        if isinstance(event, Message):
            user = event.from_user
        elif isinstance(event, CallbackQuery):
            user = event.from_user
        if user and not is_admin(user.id) and await is_blocked(user.id):
            if isinstance(event, Message):
                await event.answer("Вы заблокированы.")
            return None
        if not bot_enabled:
            if isinstance(event, Message):
                if user and is_admin(user.id):
                    return await handler(event, data)
                if event.text and event.text.strip().startswith("/start"):
                    await event.answer("Конец рабочего дня.")
                return None
            if isinstance(event, CallbackQuery):
                if user and is_admin(user.id):
                    return await handler(event, data)
                return None
        return await handler(event, data)


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id    INTEGER PRIMARY KEY,
                username   TEXT,
                full_name  TEXT,
                balance    REAL    DEFAULT 0,
                total_sold INTEGER DEFAULT 0,
                is_buyer   INTEGER DEFAULT 0,
                priority   INTEGER DEFAULT 0,
                created_at TEXT    DEFAULT (date('now'))
            )""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS queue (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                phone               TEXT UNIQUE,
                seller_id           INTEGER,
                buyer_id            INTEGER,
                status              TEXT DEFAULT 'waiting',
                seller_queue_msg_id INTEGER,
                room                TEXT DEFAULT 'sms',
                source_type         TEXT,
                source_file_id      TEXT,
                source_caption      TEXT,
                added_at            TEXT DEFAULT (datetime('now'))
            )""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id  INTEGER,
                buyer_id   INTEGER,
                phone      TEXT,
                amount     REAL,
                invoice_id TEXT,
                status     TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT (datetime('now'))
            )""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            )""")
        await db.execute("INSERT OR IGNORE INTO settings VALUES ('price_per_number','3')")
        await db.execute("INSERT OR IGNORE INTO settings VALUES ('min_withdraw','3')")
        for room in ("qr", "sms", "token"):
            await db.execute(
                "INSERT OR IGNORE INTO settings (key, value) "
                "SELECT ?, value FROM settings WHERE key='price_per_number'",
                (f"price_{room}",)
            )
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_prices (
                user_id INTEGER PRIMARY KEY,
                buyer_price REAL
            )""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS room_members (
                user_id   INTEGER PRIMARY KEY,
                room      TEXT NOT NULL,
                joined_at TEXT DEFAULT (datetime('now'))
            )""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS buyer_access (
                user_id INTEGER PRIMARY KEY,
                room    TEXT NOT NULL
            )""")
        async with db.execute(
            "SELECT value FROM settings WHERE key='buyer_access_migrated'"
        ) as cursor:
            access_migrated = await cursor.fetchone()
        if not access_migrated:
            await db.execute(
                "INSERT OR IGNORE INTO buyer_access (user_id, room) "
                "SELECT user_id, 'sms' FROM users WHERE is_buyer=1"
            )
            await db.execute(
                "INSERT INTO settings (key, value) VALUES ('buyer_access_migrated', '1')"
            )
        for stmt in [
            "ALTER TABLE transactions ADD COLUMN invoice_id TEXT DEFAULT ''",
            "ALTER TABLE users ADD COLUMN priority INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN blocked INTEGER DEFAULT 0",
            "ALTER TABLE transactions ADD COLUMN buyer_amount REAL DEFAULT 0",
            "ALTER TABLE queue ADD COLUMN room TEXT DEFAULT 'sms'",
            "ALTER TABLE queue ADD COLUMN source_type TEXT",
            "ALTER TABLE queue ADD COLUMN source_file_id TEXT",
            "ALTER TABLE queue ADD COLUMN source_caption TEXT",
        ]:
            try:
                await db.execute(stmt)
            except Exception:
                pass
        await db.commit()

async def join_room(user_id, room):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO room_members (user_id, room) VALUES (?, ?)",
            (user_id, room)
        )
        await db.commit()

async def leave_room(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM room_members WHERE user_id=?", (user_id,))
        await db.commit()

async def create_room_request(buyer_id, room):
    """Create a request for an available QR/token-room worker."""
    if room not in {"qr", "token"}:
        return None
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT rm.user_id AS seller_id
            FROM room_members rm
            WHERE rm.room=? AND rm.user_id != ?
              AND NOT EXISTS (
                  SELECT 1 FROM queue q
                  WHERE q.seller_id=rm.user_id AND q.room=?
                    AND q.status IN ('waiting','in_progress')
              )
            ORDER BY rm.joined_at ASC
            LIMIT 1
        """, (room, buyer_id, room)) as c:
            row = await c.fetchone()
        if not row:
            return None
        marker = f"{room}_request:{row['seller_id']}:{int(_time.time() * 1000)}"
        cur = await db.execute(
            "INSERT INTO queue (phone, seller_id, room) VALUES (?, ?, ?)",
            (marker, row["seller_id"], room)
        )
        await db.commit()
        return {"id": cur.lastrowid, "phone": marker, "seller_id": row["seller_id"], "room": room, "status": "waiting"}


async def get_setting(key):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key=?", (key,)) as c:
            r = await c.fetchone()
            return r[0] if r else None

async def set_setting(key, value):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, value))
        await db.commit()

async def get_room_price(room):
    value = await get_setting(f"price_{room}")
    if value is None:
        value = await get_setting("price_per_number")
    return float(value or 0)

async def get_or_create_user(user_id, username, full_name):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,)) as c:
            r = await c.fetchone()
        if not r:
            await db.execute("INSERT INTO users (user_id,username,full_name) VALUES (?,?,?)",
                             (user_id, username, full_name))
        else:
            await db.execute("UPDATE users SET username=?,full_name=? WHERE user_id=?",
                             (username, full_name, user_id))
        await db.commit()

async def get_user(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)) as c:
            r = await c.fetchone()
            return dict(r) if r else None

async def get_all_users():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users") as c:
            rows = await c.fetchall()
            return [dict(r) for r in rows]

async def get_all_users_sold_today():
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT u.user_id, u.username, u.full_name,
                   COUNT(t.id) AS sold_today,
                   GROUP_CONCAT(t.phone, ', ') AS phones
            FROM users u
            INNER JOIN transactions t ON t.seller_id = u.user_id
                AND t.status='paid' AND date(t.created_at)=?
            GROUP BY u.user_id
            ORDER BY sold_today DESC
        """, (today,)) as c:
            rows = await c.fetchall()
            return [dict(r) for r in rows]

async def get_user_transactions(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM transactions WHERE seller_id=? AND status='paid' ORDER BY created_at DESC",
            (user_id,)
        ) as c:
            rows = await c.fetchall()
            return [dict(r) for r in rows]

async def get_buyer_transactions(buyer_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM transactions WHERE buyer_id=? AND status='paid' ORDER BY created_at DESC",
            (buyer_id,)
        ) as c:
            rows = await c.fetchall()
            return [dict(r) for r in rows]

async def get_all_transactions():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT t.*, u.username, u.full_name
            FROM transactions t
            LEFT JOIN users u ON t.seller_id = u.user_id
            ORDER BY t.created_at DESC
        """) as c:
            rows = await c.fetchall()
            return [dict(r) for r in rows]

async def is_buyer(user_id):
    return bool(await get_buyer_room(user_id))

async def set_buyer(user_id, status):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET is_buyer=? WHERE user_id=?", (int(status), user_id))
        if not status:
            await db.execute("DELETE FROM buyer_access WHERE user_id=?", (user_id,))
        await db.commit()

async def get_buyer_room(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT room FROM buyer_access WHERE user_id=?", (user_id,)) as c:
            row = await c.fetchone()
            return row[0] if row else None

async def set_buyer_room(user_id, room):
    if room not in {"qr", "sms", "token"}:
        raise ValueError("Unknown buyer room")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO buyer_access (user_id, room) VALUES (?, ?)",
            (user_id, room)
        )
        await db.execute("UPDATE users SET is_buyer=1 WHERE user_id=?", (user_id,))
        await db.commit()

async def set_priority(user_id, status):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET priority=? WHERE user_id=?", (int(status), user_id))
        await db.commit()

async def set_user_balance(user_id, amount):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET balance=? WHERE user_id=?", (amount, user_id))
        await db.commit()

async def is_blocked(user_id):
    u = await get_user(user_id)
    return bool(u and u.get("blocked"))

async def set_blocked(user_id, status):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET blocked=? WHERE user_id=?", (int(status), user_id))
        await db.commit()

async def resolve_user_input(text):
    text = text.strip()
    if text.startswith("@"):
        username = text[1:]
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE", (username,)) as c:
                r = await c.fetchone()
                return dict(r) if r else None
    try:
        uid = int(text)
        return await get_user(uid)
    except ValueError:
        return None

async def get_buyer_bought_today(buyer_id):
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM transactions WHERE buyer_id=? AND status='paid' AND date(created_at)=?",
            (buyer_id, today)
        ) as c:
            r = await c.fetchone()
            return r[0] if r else 0

async def get_buyer_bought_total(buyer_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM transactions WHERE buyer_id=? AND status='paid'",
            (buyer_id,)
        ) as c:
            r = await c.fetchone()
            return r[0] if r else 0

async def cancel_queue_item(queue_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE queue SET status='cancelled' WHERE id=?", (queue_id,))
        await db.commit()

async def add_to_queue(phone, seller_id, room="sms"):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id FROM queue WHERE phone=?", (phone,)) as c:
            if await c.fetchone():
                return "duplicate"
        await db.execute("INSERT INTO queue (phone,seller_id,room) VALUES (?,?,?)", (phone, seller_id, room))
        await db.commit()
        return "added"

def extract_media(message):
    """Return a reusable Telegram file id and its media type for a message."""
    if message.photo:
        return "photo", message.photo[-1].file_id
    if message.document:
        return "document", message.document.file_id
    if message.video:
        return "video", message.video.file_id
    if message.animation:
        return "animation", message.animation.file_id
    return None, None

def queue_item_label(item):
    phone = str(item.get("phone") or "")
    room = item.get("room") or "sms"
    if phone.startswith("qr_request:"):
        return "QR-запрос"
    if phone.startswith("token_request:"):
        return "запрос токена"
    if phone.startswith("sms_file:"):
        return "SMS-файл"
    if phone.startswith("token_file:"):
        return "файл токена"
    return phone

async def add_media_to_queue(message, seller_id, room):
    media_type, file_id = extract_media(message)
    if not media_type or room not in {"sms", "token"}:
        return None
    marker = f"{room}_file:{seller_id}:{int(_time.time() * 1000)}:{random.randint(0, 99999)}"
    caption = (message.caption or "").strip()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO queue "
            "(phone, seller_id, room, source_type, source_file_id, source_caption) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (marker, seller_id, room, media_type, file_id, caption)
        )
        await db.commit()
        return {"id": cur.lastrowid, "phone": marker, "room": room,
                "source_type": media_type, "source_file_id": file_id,
                "source_caption": caption}

async def send_media_with_caption(chat_id, media_type, file_id, caption, reply_markup=None):
    """Send stored/incoming media with its description in the same Telegram message."""
    caption = (caption[:1024] if caption else None)
    kwargs = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
    if reply_markup is not None:
        kwargs["reply_markup"] = reply_markup
    method = {
        "photo": bot.send_photo,
        "document": bot.send_document,
        "video": bot.send_video,
        "animation": bot.send_animation,
    }.get(media_type)
    if not method:
        raise ValueError(f"Unsupported media type: {media_type}")
    kwargs[media_type if media_type != "animation" else "animation"] = file_id
    return await method(**kwargs)

async def send_queue_media(item, chat_id, caption=None, reply_markup=None):
    if not item.get("source_file_id") or not item.get("source_type"):
        return False
    base_caption = item.get("source_caption") or ""
    if caption:
        base_caption = f"{caption}\n\n{base_caption}" if base_caption else caption
    await send_media_with_caption(
        chat_id, item["source_type"], item["source_file_id"],
        escape(base_caption), reply_markup=reply_markup
    )
    return True

async def set_queue_msg_id(phone, msg_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE queue SET seller_queue_msg_id=? WHERE phone=?", (msg_id, phone))
        await db.commit()

async def get_next_in_queue(prefer_priority=False, room=None):
    paused = _paused_sellers
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if prefer_priority:
            async with db.execute("""
                SELECT q.* FROM queue q
                JOIN users u ON q.seller_id = u.user_id
                WHERE q.status='waiting' AND u.priority=1
                  AND (? IS NULL OR q.room=?)
            """, (room, room)) as c:
                rows = [dict(r) for r in await c.fetchall()]
                rows = [r for r in rows if r["seller_id"] not in paused]
                if rows:
                    return random.choice(rows)
        async with db.execute(
            "SELECT * FROM queue WHERE status='waiting' AND (? IS NULL OR room=?)",
            (room, room)
        ) as c:
            rows = [dict(r) for r in await c.fetchall()]
            rows = [r for r in rows if r["seller_id"] not in paused]
            return random.choice(rows) if rows else None

async def assign_queue_item(queue_id, buyer_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE queue SET status='in_progress',buyer_id=? WHERE id=?", (buyer_id, queue_id))
        await db.commit()

async def complete_queue_item(queue_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE queue SET status='done' WHERE id=?", (queue_id,))
        await db.commit()

async def get_queue_item_by_id(queue_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM queue WHERE id=?", (queue_id,)) as c:
            r = await c.fetchone()
            return dict(r) if r else None

async def get_queue_item_for_buyer(buyer_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM queue WHERE buyer_id=? AND status='in_progress' ORDER BY id DESC LIMIT 1",
            (buyer_id,)
        ) as c:
            r = await c.fetchone()
            return dict(r) if r else None

async def get_waiting_count():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM queue WHERE status='waiting'") as c:
            r = await c.fetchone()
            return r[0] if r else 0

async def get_room_participants(room):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM room_members WHERE room=?",
            (room,)
        ) as c:
            row = await c.fetchone()
            return row[0] if row else 0

async def get_user_queue_count(seller_id, room=None):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM queue WHERE seller_id=? AND status='waiting' AND (? IS NULL OR room=?)",
            (seller_id, room, room)
        ) as c:
            r = await c.fetchone()
            return r[0] if r else 0

async def get_user_sold_today(seller_id):
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM transactions WHERE seller_id=? AND status='paid' AND date(created_at)=?",
            (seller_id, today)
        ) as c:
            r = await c.fetchone()
            return r[0] if r else 0

async def create_transaction(seller_id, buyer_id, phone, amount, invoice_id="", buyer_amount=0):
    async with aiosqlite.connect(DB_PATH) as db:
        c = await db.execute(
            "INSERT INTO transactions (seller_id,buyer_id,phone,amount,invoice_id,buyer_amount) VALUES (?,?,?,?,?,?)",
            (seller_id, buyer_id, phone, amount, invoice_id, buyer_amount))
        await db.commit()
        return c.lastrowid

async def get_transaction(tx_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)) as c:
            r = await c.fetchone()
            return dict(r) if r else None

async def get_unpaid_transaction_for_buyer(buyer_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM transactions WHERE buyer_id=? AND status='pending' ORDER BY id DESC LIMIT 1",
            (buyer_id,)
        ) as c:
            r = await c.fetchone()
            return dict(r) if r else None

async def get_all_pending_transactions():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM transactions WHERE status='pending' AND invoice_id!='' AND invoice_id IS NOT NULL"
        ) as c:
            rows = await c.fetchall()
            return [dict(r) for r in rows]

async def pay_transaction(tx_id):
    async with aiosqlite.connect(DB_PATH) as db:
        tx = await get_transaction(tx_id)
        if tx:
            await db.execute("UPDATE transactions SET status='paid' WHERE id=?", (tx_id,))
            await db.execute(
                "UPDATE users SET balance=balance+?,total_sold=total_sold+1 WHERE user_id=?",
                (tx["amount"], tx["seller_id"]))
            await db.commit()

async def withdraw_amount(user_id, amount):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET balance=MAX(0,balance-?) WHERE user_id=?", (amount, user_id))
        await db.commit()

async def get_sellers_with_waiting():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT DISTINCT seller_id FROM queue WHERE status='waiting'"
        ) as c:
            rows = await c.fetchall()
            return [r[0] for r in rows]

async def clear_waiting_queue():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM queue WHERE status='waiting'")
        await db.commit()


CB_HEADERS = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}

async def cb_create_invoice(amount, description, payload):
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{CRYPTO_BOT_API}/createInvoice", headers=CB_HEADERS, json={
                "asset": "USDT", "amount": str(round(amount, 2)),
                "description": description, "payload": payload,
                "allow_comments": False, "allow_anonymous": False,
            }) as r:
                res = await r.json()
                return res["result"] if res.get("ok") else None
    except Exception as e:
        logging.error(f"[CryptoBot] createInvoice: {e}")
        return None

async def cb_get_invoice(invoice_id):
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{CRYPTO_BOT_API}/getInvoices",
                             params={"invoice_ids": str(invoice_id)},
                             headers=CB_HEADERS) as r:
                res = await r.json()
                if res.get("ok") and res["result"]["items"]:
                    return res["result"]["items"][0]
                return None
    except Exception as e:
        logging.error(f"[CryptoBot] getInvoices: {e}")
        return None

async def cb_create_check(amount):
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{CRYPTO_BOT_API}/createCheck", headers=CB_HEADERS,
                              json={"asset": "USDT", "amount": str(round(amount, 2))}) as r:
                text_body = await r.text()
                logging.info(f"[CryptoBot] createCheck raw response: {text_body}")
                res = await r.json(content_type=None)
                if res.get("ok"):
                    item = res["result"]
                    item["url"] = (item.get("bot_check_url") or item.get("check_url") or
                                   item.get("url") or "")
                    return item
                err = res.get("error", {})
                logging.error(f"[CryptoBot] createCheck failed: code={err.get('code')} name={err.get('name')}")
                return None
    except Exception as e:
        logging.error(f"[CryptoBot] createCheck exception: {e}", exc_info=True)
        return None


def kb_main(buyer=False):
    room_callback = "menu_buy" if buyer else "menu_sell"
    rows = []
    if buyer:
        rows.append([ui_button("Взять", icon="lightning", callback_data="take", style="success")])
    rows.extend([
        [ui_button("Прием QR/TOKEN/SMS", icon="fire", callback_data=room_callback, style="success")],
        [ui_button("Мои аккаунты", icon="catalog", callback_data="menu_accounts", style="primary"),
         ui_button("Баланс", icon="wallet", callback_data="menu_balance", style="primary")],
        [ui_button("Профиль", icon="profile", callback_data="menu_profile", style="primary"),
         ui_button("Поддержка", icon="attention", callback_data="menu_support", style="primary")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def build_dashboard_text(user_id: int) -> str:
    user = await get_user(user_id)
    if not user:
        return "Твой кабинет • аккаунты и выплаты\n\n👇 Выбери раздел:"
    queue_count = await get_user_queue_count(user_id, "token")
    active = await get_queue_item_for_buyer(user_id)
    balance = float(user.get("balance") or 0)
    qr_price, sms_price, token_price = await asyncio.gather(
        get_room_price("qr"), get_room_price("sms"), get_room_price("token")
    )
    return (
        "Твой кабинет • аккаунты и выплаты\n\n"
        f"┌ Баланс: 🪙 <b>${balance:.2f}</b>\n"
        f"│ / Токенов в очереди: <b>{queue_count}</b> · В работе: ✅ <b>{1 if active else 0}</b>\n"
        f"│ Прайс: QR <b>${qr_price:.2f}</b> · SMS <b>${sms_price:.2f}</b> · Токен <b>${token_price:.2f}</b>\n"
        "│ Приём: ✅ Работа включена (до 23:59 МСК)\n"
        "└ График: 08:00–23:59 МСК\n\n"
        f"🛩 CryptoBot: 🪙 <b>${balance:.2f}</b>\n\n"
        "👇 <b>Выбери раздел:</b>"
    )

def kb_profile(buyer=False):
    if buyer:
        return InlineKeyboardMarkup(inline_keyboard=[
            [ui_button("Статистика", icon="catalog", callback_data="profile_stats", style="primary")],
            [ui_button("Главное меню", icon="back", callback_data="back_main", style="primary")],
        ])
    return InlineKeyboardMarkup(inline_keyboard=[
        [ui_button("Вывод средств", icon="dollar", callback_data="withdraw", style="success"),
         ui_button("Статистика", icon="catalog", callback_data="profile_stats", style="primary")],
        [ui_button("Главное меню", icon="back", callback_data="back_main", style="primary")],
    ])

def kb_user_stats():
    return InlineKeyboardMarkup(inline_keyboard=[
        [ui_button("Скачать .TXT", icon="catalog", callback_data="profile_stats_txt", style="primary")],
        [ui_button("Профиль", icon="profile", callback_data="menu_profile", style="primary")],
    ])

def kb_back():
    return InlineKeyboardMarkup(inline_keyboard=[
        [ui_button("Главное меню", icon="back", callback_data="back_main", style="primary")],
    ])

def kb_withdraw(balance):
    return InlineKeyboardMarkup(inline_keyboard=[
        [ui_button(f"Вывести всё ({balance:.2f}$)", icon="dollar", callback_data="withdraw_all", style="success")],
        [ui_button("Отмена", icon="back", callback_data="withdraw_cancel", style="danger")],
    ])

def kb_admin(enabled=True):
    toggle = "Выключить бота" if enabled else "Включить бота"
    return InlineKeyboardMarkup(inline_keyboard=[
        [ui_button("Цены комнат", icon="dollar", callback_data="admin_room_prices", style="primary"),
         ui_button("Мин. вывод", icon="wallet", callback_data="admin_set_min_wd", style="primary")],
        [ui_button("Статистика", icon="catalog", callback_data="admin_stats", style="primary")],
        [ui_button("Добавить покупателя", icon="add_user", callback_data="admin_add_buyer", style="success"),
         ui_button("Убрать покупателя", icon="minus", callback_data="admin_remove_buyer", style="danger")],
        [ui_button("Изменить баланс", icon="card", callback_data="admin_set_balance", style="primary")],
        [ui_button("Заблокировать", icon="attention", callback_data="admin_block", style="danger"),
         ui_button("Разблокировать", icon="plus", callback_data="admin_unblock", style="success")],
        [ui_button("Рассылка", icon="send_user", callback_data="admin_broadcast", style="primary"),
         ui_button(toggle, icon="lightning", callback_data="admin_toggle_bot",
                   style="danger" if enabled else "success")],
    ])

def kb_admin_back():
    return InlineKeyboardMarkup(inline_keyboard=[
        [ui_button("Назад в панель", icon="back", callback_data="admin_back", style="primary")],
    ])

def kb_admin_rooms(prefix):
    return InlineKeyboardMarkup(inline_keyboard=[
        [ui_button("QR", icon="diamond", callback_data=f"{prefix}:qr", style="primary"),
         ui_button("SMS", icon="card", callback_data=f"{prefix}:sms", style="primary")],
        [ui_button("Токены", icon="dollar", callback_data=f"{prefix}:token", style="success")],
        [ui_button("Назад в панель", icon="back", callback_data="admin_back", style="primary")],
    ])

def kb_admin_stats():
    return InlineKeyboardMarkup(inline_keyboard=[
        [ui_button(".TXT", icon="catalog", callback_data="admin_stats_txt", style="primary")],
        [ui_button("Назад в панель", icon="back", callback_data="admin_back", style="primary")],
    ])

def kb_get_code(queue_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [ui_button("Получить код", icon="lightning", callback_data=f"get_code:{queue_id}", style="success")],
        [ui_button("Отменить номер", icon="minus", callback_data=f"cancel_number:{queue_id}", style="danger")],
    ])

def kb_delivery_received(queue_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [ui_button("Получено / вход выполнен", icon="plus", callback_data=f"login_done:{queue_id}", style="success")],
        [ui_button("Отменить заявку", icon="minus", callback_data=f"cancel_number:{queue_id}", style="danger")],
    ])

def kb_request_delivery(queue_id, room="sms"):
    label = "Запросить токен" if room == "token" else "Получить код"
    return InlineKeyboardMarkup(inline_keyboard=[
        [ui_button(label, icon="lightning", callback_data=f"get_code:{queue_id}", style="success")],
        [ui_button("Отменить заявку", icon="minus", callback_data=f"cancel_number:{queue_id}", style="danger")],
    ])

def kb_code_received(queue_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [ui_button("Получить код", icon="lightning", callback_data=f"get_code:{queue_id}", style="primary")],
        [ui_button("Вход выполнен", icon="plus", callback_data=f"login_done:{queue_id}", style="success")],
        [ui_button("Отменить номер", icon="minus", callback_data=f"cancel_number:{queue_id}", style="danger")],
    ])

def kb_payment(pay_url):
    return InlineKeyboardMarkup(inline_keyboard=[
        [ui_button("Оплатить", icon="wallet", url=pay_url, style="success")],
    ])


class SellStates(StatesGroup):
    choose_room = State()
    waiting_phone = State()
    waiting_batch = State()

class WithdrawStates(StatesGroup):
    enter_amount = State()

class AdminStates(StatesGroup):
    set_min_wd      = State()
    add_buyer       = State()
    remove_buyer    = State()
    add_priority    = State()
    remove_priority = State()
    set_balance_uid = State()
    set_balance_amt = State()
    broadcast       = State()
    block_user      = State()
    unblock_user    = State()
    set_room_price  = State()

class BuyerStates(StatesGroup):
    waiting_qr = State()


pending_code_requests: dict[int, int] = {}

def is_admin(uid):
    return int(uid) in [int(x) for x in ADMIN_IDS]

async def show_main_menu(target, user_id, text="Главное меню"):
    buyer = await is_buyer(user_id)
    if text in ("Главное меню", "Добро пожаловать!"):
        text = await build_dashboard_text(user_id)
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, parse_mode="HTML", reply_markup=kb_main(buyer))
        except Exception:
            await target.message.answer(text, parse_mode="HTML", reply_markup=kb_main(buyer))
        await target.answer()
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=kb_main(buyer))

async def show_admin_panel(target):
    qr_price, sms_price, token_price = await asyncio.gather(
        get_room_price("qr"), get_room_price("sms"), get_room_price("token")
    )
    min_wd       = await get_setting("min_withdraw")
    waiting      = await get_waiting_count()
    status       = "работает" if bot_enabled else "выключен"
    text = (
        f"<b>Панель администратора</b>\n\n"
        f"Прайс QR: <b>{qr_price:.2f}$</b>\n"
        f"Прайс SMS: <b>{sms_price:.2f}$</b>\n"
        f"Прайс токенов: <b>{token_price:.2f}$</b>\n"
        f"Мин. вывод: <b>{min_wd}$</b>\n"
        f"В очереди: <b>{waiting}</b>\n"
        f"Статус бота: <b>{status}</b>"
    )
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, parse_mode="HTML", reply_markup=kb_admin(bot_enabled))
        except Exception:
            await target.message.answer(text, parse_mode="HTML", reply_markup=kb_admin(bot_enabled))
        await target.answer()
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=kb_admin(bot_enabled))

async def do_withdraw(target, amount):
    user_id = target.from_user.id
    check = await cb_create_check(amount)
    if not check:
        msg = (
            "Ошибка создания чека.\n\n"
        )
        if isinstance(target, CallbackQuery):
            await target.message.answer(msg)
            await target.answer("Ошибка чека", show_alert=True)
        else:
            await target.answer(msg)
        return
    url = check.get("url") or check.get("bot_check_url") or check.get("check_url") or ""
    if not url:
        logging.error(f"[withdraw] check created but URL is empty: {check}")
        msg = "Ошибка создания чека. Обратитесь в поддержку."
        if isinstance(target, CallbackQuery):
            await target.message.answer(msg)
            await target.answer()
        else:
            await target.answer(msg)
        return
    await withdraw_amount(user_id, amount)
    text = f"Чек на <b>{amount:.2f}$</b> создан!\n\n{url}"
    if isinstance(target, CallbackQuery):
        await target.message.answer(text, parse_mode="HTML")
        await target.answer("Чек отправлен")
    else:
        await target.answer(text, parse_mode="HTML")

async def confirm_payment(tx):
    try:
        item     = await get_queue_item_for_buyer(tx["buyer_id"])
        queue_id = item["id"] if item else None
        await pay_transaction(tx["id"])
        if queue_id:
            await complete_queue_item(queue_id)
        price   = tx["amount"]
        seller  = await get_user(tx["seller_id"])
        new_bal = seller["balance"] if seller else 0
        try:
            await bot.send_message(
                tx["buyer_id"],
                "Оплата подтверждена!\n\nСпасибо за покупку. Можете взять следующий номер.",
                reply_markup=kb_main(True))
        except Exception:
            pass
        try:
            await bot.send_message(
                tx["seller_id"],
                f"Оплата получена!\n\nЗачислено: <b>{price}$</b>\nНомер: <b>{tx['phone']}</b>\nБаланс: <b>{new_bal:.2f}$</b>",
                parse_mode="HTML")
        except Exception:
            pass
    except Exception as e:
        logging.error(f"[confirm_payment] {e}")

async def payment_polling_task():
    await asyncio.sleep(5)
    while True:
        try:
            for tx in await get_all_pending_transactions():
                invoice = await cb_get_invoice(int(tx["invoice_id"]))
                if invoice and invoice.get("status") == "paid":
                    await confirm_payment(tx)
        except Exception as e:
            logging.error(f"[polling] {e}")
        await asyncio.sleep(10)

async def midnight_queue_reset_task():
    msk = ZoneInfo("Europe/Moscow")
    while True:
        try:
            import datetime as dt
            now_dt   = dt.datetime.now(msk)
            tomorrow = dt.datetime.combine(now_dt.date(), dt.time(0, 0, 0), tzinfo=msk) + dt.timedelta(days=1)
            wait_sec = (tomorrow - now_dt).total_seconds()
            await asyncio.sleep(wait_sec)
            await clear_waiting_queue()
            _presence_tracker.clear()
            _paused_sellers.clear()
            logging.info("[midnight_reset] Queue cleared at MSK midnight")
        except Exception as e:
            logging.error(f"[midnight_reset] {e}")
            await asyncio.sleep(60)

PRESENCE_SCHEDULE = [60, 120, 320, 320, 320, 320, 320, 320]

async def presence_ping_task():
    await asyncio.sleep(10)
    while True:
        try:
            sellers = await get_sellers_with_waiting()
            for sid in sellers:
                if sid in _paused_sellers:
                    continue
                if sid not in _presence_tracker:
                    _presence_tracker[sid] = {"step": 0, "last_ping": 0}
                tr = _presence_tracker[sid]
                step = tr["step"]
                if step >= len(PRESENCE_SCHEDULE):
                    continue
                delay = PRESENCE_SCHEDULE[step]
                now = _time.time()
                if tr["last_ping"] == 0:
                    tr["last_ping"] = now
                    continue
                if now - tr["last_ping"] >= delay:
                    _paused_sellers.add(sid)
                    tr["last_ping"] = now
                    tr["step"] = step + 1
                    try:
                        kb = InlineKeyboardMarkup(inline_keyboard=[
                            [ui_button("Я на месте", icon="lightning", callback_data=f"im_here:{sid}", style="success")]
                        ])
                        await bot.send_message(
                            sid,
                            "Ваши номера приостановлены.\nНажмите кнопку чтобы вернуть их в очередь.",
                            reply_markup=kb)
                    except Exception:
                        pass
        except Exception as e:
            logging.error(f"[presence_ping] {e}")
        await asyncio.sleep(15)


def build_user_stats_text(user, txs, is_buyer=False) -> str:
    total = sum(t["amount"] for t in txs)
    if is_buyer:
        lines = [
            f"Статистика покупателя",
            f"Имя: {user['full_name']}",
            f"ID: {user['user_id']}",
            f"",
            f"Всего куплено номеров: {len(txs)}",
            f"Оплата с покупателя не требуется",
            f"",
            f"Купленные номера:",
        ]
        if txs:
            for t in txs:
                lines.append(f"  {t['phone']}  |  {t['created_at']}")
        else:
            lines.append("  Нет покупок")
    else:
        lines = [
            f"Статистика продавца",
            f"Имя: {user['full_name']}",
            f"ID: {user['user_id']}",
            f"",
            f"Всего продано номеров: {len(txs)}",
            f"Всего заработано: {total:.2f}$",
            f"Текущий баланс: {user['balance']:.2f}$",
            f"",
            f"Проданные номера:",
        ]
        if txs:
            for t in txs:
                lines.append(f"  {t['phone']}  |  {t['amount']:.2f}$  |  {t['created_at']}")
        else:
            lines.append("  Нет продаж")
    return "\n".join(lines)

def build_admin_stats_text(users, txs) -> str:
    total_paid   = [t for t in txs if t["status"] == "paid"]
    total_earned = sum(t["amount"] for t in total_paid)
    lines = [
        f"Полная статистика",
        f"Дата: {date.today().isoformat()}",
        f"",
        f"Всего транзакций (оплачено): {len(total_paid)}",
        f"Общая сумма: {total_earned:.2f}$",
        f"Всего пользователей: {len(users)}",
        f"",
        f"--- Пользователи ---",
    ]
    for u in users:
        uname = f"@{u['username']}" if u['username'] else u['full_name']
        lines.append(f"{uname} (ID: {u['user_id']}) | баланс: {u['balance']:.2f}$ | продано: {u['total_sold']}")
    lines += ["", "--- Все транзакции ---"]
    for t in txs:
        uname = f"@{t['username']}" if t.get('username') else t.get('full_name', '?')
        status = t["status"]
        lines.append(f"{t['created_at']}  |  {t['phone']}  |  {t['amount']:.2f}$  |  {status}  |  продавец: {uname} (ID: {t['seller_id']})")
    return "\n".join(lines)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await get_or_create_user(message.from_user.id, message.from_user.username or "", message.from_user.full_name)
    await show_main_menu(message, message.from_user.id)

@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    await show_admin_panel(message)

@router.callback_query(F.data == "admin_back")
async def cb_admin_back(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_admin_panel(callback)

@router.callback_query(F.data == "admin_toggle_bot")
async def cb_admin_toggle_bot(callback: CallbackQuery):
    global bot_enabled
    bot_enabled = not bot_enabled
    status = "включён" if bot_enabled else "выключен"
    await callback.answer(f"Бот {status}!", show_alert=True)
    await show_admin_panel(callback)

@router.callback_query(F.data == "admin_room_prices")
async def cb_admin_room_prices(callback: CallbackQuery):
    await callback.message.edit_text(
        "Выберите комнату, для которой нужно изменить прайс:",
        reply_markup=kb_admin_rooms("admin_price_room")
    )
    await callback.answer()

@router.callback_query(F.data.startswith("admin_price_room:"))
async def cb_admin_price_room(callback: CallbackQuery, state: FSMContext):
    room = callback.data.split(":", 1)[1]
    if room not in ROOM_NAMES:
        await callback.answer("Неизвестная комната.", show_alert=True)
        return
    await state.update_data(price_room=room)
    await state.set_state(AdminStates.set_room_price)
    await callback.message.edit_text(
        f"Введите новый прайс для комнаты <b>{ROOM_NAMES[room]}</b> ($):\n"
        "Пример: <b>3</b> или <b>2.5</b>",
        parse_mode="HTML", reply_markup=kb_admin_back()
    )
    await callback.answer()

@router.message(AdminStates.set_room_price)
async def process_room_price(message: Message, state: FSMContext):
    try:
        price = float((message.text or "").strip().replace(",", "."))
        if price < 0:
            raise ValueError
    except ValueError:
        await message.answer("Введите неотрицательное число, например: 3 или 2.5")
        return
    data = await state.get_data()
    room = data.get("price_room")
    if room not in ROOM_NAMES:
        await state.clear()
        await message.answer("Комната не выбрана.")
        await show_admin_panel(message)
        return
    await set_setting(f"price_{room}", str(price))
    await state.clear()
    await message.answer(
        f"Прайс комнаты <b>{ROOM_NAMES[room]}</b>: <b>{price:.2f}$</b>",
        parse_mode="HTML"
    )
    await show_admin_panel(message)

@router.callback_query(F.data == "admin_set_min_wd")
async def cb_admin_set_min_wd(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.set_min_wd)
    await callback.message.edit_text("Введите минимальную сумму вывода ($):\nПример: <b>5</b>",
                                     parse_mode="HTML", reply_markup=kb_admin_back())
    await callback.answer()

@router.message(AdminStates.set_min_wd)
async def process_set_min_wd(message: Message, state: FSMContext):
    try:
        amount = float(message.text.strip().replace(",", "."))
        await set_setting("min_withdraw", str(amount))
        await state.clear()
        await message.answer(f"Мин. сумма вывода: <b>{amount}$</b>", parse_mode="HTML")
        await show_admin_panel(message)
    except ValueError:
        await message.answer("Введите число, например: 5")

@router.callback_query(F.data == "admin_add_buyer")
async def cb_admin_add_buyer(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "Для какой комнаты назначить покупателя?\n\n"
        "У покупателя будет доступ только к выбранному типу заявок.",
        reply_markup=kb_admin_rooms("admin_buyer_room"))
    await callback.answer()

@router.callback_query(F.data.startswith("admin_buyer_room:"))
async def cb_admin_buyer_room(callback: CallbackQuery, state: FSMContext):
    room = callback.data.split(":", 1)[1]
    if room not in ROOM_NAMES:
        await callback.answer("Неизвестная комната.", show_alert=True)
        return
    await state.update_data(buyer_room=room)
    await state.set_state(AdminStates.add_buyer)
    await callback.message.edit_text(
        f"Введите ID или @username покупателя для комнаты <b>{ROOM_NAMES[room]}</b>:",
        parse_mode="HTML", reply_markup=kb_admin_back())
    await callback.answer()

@router.message(AdminStates.add_buyer)
async def process_add_buyer(message: Message, state: FSMContext):
    user = await resolve_user_input(message.text)
    if not user:
        await message.answer("Пользователь не найден. Введите ID или @username.")
        return
    uid = user["user_id"]
    data = await state.get_data()
    room = data.get("buyer_room")
    if room not in ROOM_NAMES:
        await state.clear()
        await message.answer("Комната не выбрана. Начните назначение заново.")
        await show_admin_panel(message)
        return
    await set_buyer_room(uid, room)
    await state.clear()
    await message.answer(
        f"Покупатель <b>{uid}</b> назначен на комнату <b>{ROOM_NAMES[room]}</b>.",
        parse_mode="HTML")
    try:
        await bot.send_message(
            uid,
            f"Вам открыт доступ к заявкам комнаты <b>{ROOM_NAMES[room]}</b>!",
            parse_mode="HTML", reply_markup=kb_main(True))
    except Exception:
        pass
    await show_admin_panel(message)

@router.callback_query(F.data == "admin_remove_buyer")
async def cb_admin_remove_buyer(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.remove_buyer)
    await callback.message.edit_text("Введите ID или @username пользователя у которого забрать доступ:",
                                     reply_markup=kb_admin_back())
    await callback.answer()

@router.message(AdminStates.remove_buyer)
async def process_remove_buyer(message: Message, state: FSMContext):
    user = await resolve_user_input(message.text)
    if not user:
        await message.answer("Пользователь не найден. Введите ID или @username.")
        return
    uid = user["user_id"]
    await set_buyer(uid, False)
    await state.clear()
    await message.answer(f"Пользователь <b>{uid}</b> лишён доступа.", parse_mode="HTML")
    await show_admin_panel(message)

@router.callback_query(F.data == "admin_add_priority")
async def cb_admin_add_priority(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.add_priority)
    await callback.message.edit_text(
        "Введите ID или @username пользователя для выдачи приоритета:",
        reply_markup=kb_admin_back())
    await callback.answer()

@router.message(AdminStates.add_priority)
async def process_add_priority(message: Message, state: FSMContext):
    user = await resolve_user_input(message.text)
    if not user:
        await message.answer("Пользователь не найден. Введите ID или @username.")
        return
    await set_priority(user["user_id"], True)
    await state.clear()
    await message.answer(f"Пользователь <b>{user['user_id']}</b> получил приоритет в очереди.", parse_mode="HTML")
    await show_admin_panel(message)

@router.callback_query(F.data == "admin_remove_priority")
async def cb_admin_remove_priority(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.remove_priority)
    await callback.message.edit_text(
        "Введите ID или @username пользователя у которого убрать приоритет:",
        reply_markup=kb_admin_back())
    await callback.answer()

@router.message(AdminStates.remove_priority)
async def process_remove_priority(message: Message, state: FSMContext):
    user = await resolve_user_input(message.text)
    if not user:
        await message.answer("Пользователь не найден. Введите ID или @username.")
        return
    await set_priority(user["user_id"], False)
    await state.clear()
    await message.answer(f"Приоритет у пользователя <b>{user['user_id']}</b> убран.", parse_mode="HTML")
    await show_admin_panel(message)

@router.callback_query(F.data == "admin_set_balance")
async def cb_admin_set_balance(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.set_balance_uid)
    await callback.message.edit_text(
        "Введите ID или @username пользователя, которому хотите изменить баланс:",
        reply_markup=kb_admin_back())
    await callback.answer()

@router.message(AdminStates.set_balance_uid)
async def process_set_balance_uid(message: Message, state: FSMContext):
    user = await resolve_user_input(message.text)
    if not user:
        await message.answer("Пользователь не найден. Введите ID или @username.")
        return
    uid = user["user_id"]
    await state.update_data(target_uid=uid)
    await state.set_state(AdminStates.set_balance_amt)
    await message.answer(
        f"Пользователь: <b>{user['full_name']}</b> (ID: {uid})\n"
        f"Текущий баланс: <b>{user['balance']:.2f}$</b>\n\n"
        f"Введите новый баланс:",
        parse_mode="HTML")

@router.message(AdminStates.set_balance_amt)
async def process_set_balance_amt(message: Message, state: FSMContext):
    try:
        amount = float(message.text.strip().replace(",", "."))
        data   = await state.get_data()
        uid    = data["target_uid"]
        await set_user_balance(uid, amount)
        await state.clear()
        await message.answer(
            f"Баланс пользователя <b>{uid}</b> установлен: <b>{amount:.2f}$</b>", parse_mode="HTML")
        await show_admin_panel(message)
    except ValueError:
        await message.answer("Введите число, например: 10 или 5.5")

@router.callback_query(F.data == "admin_stats")
async def cb_admin_stats(callback: CallbackQuery):
    waiting     = await get_waiting_count()
    qr_price, sms_price, token_price = await asyncio.gather(
        get_room_price("qr"), get_room_price("sms"), get_room_price("token")
    )
    min_wd      = await get_setting("min_withdraw")
    users_today = await get_all_users_sold_today()

    lines = [
        "<b>Статистика</b>\n",
        f"В очереди: <b>{waiting}</b>",
        f"Прайс QR: <b>{qr_price:.2f}$</b>",
        f"Прайс SMS: <b>{sms_price:.2f}$</b>",
        f"Прайс токенов: <b>{token_price:.2f}$</b>",
        f"Мин. вывод: <b>{min_wd}$</b>\n",
        "<b>Продажи сегодня:</b>",
    ]
    if users_today:
        for u in users_today:
            uname = f"@{u['username']}" if u['username'] else u['full_name']
            lines.append(f"• {uname} (ID: {u['user_id']}) — {u['sold_today']} шт.")
    else:
        lines.append("Нет продаж сегодня")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n..."

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb_admin_stats())
    await callback.answer()

@router.callback_query(F.data == "admin_stats_txt")
async def cb_admin_stats_txt(callback: CallbackQuery):
    await callback.answer("Генерирую файл...")
    users = await get_all_users()
    txs   = await get_all_transactions()
    content = build_admin_stats_text(users, txs)
    filename = f"stats_{date.today().isoformat()}.txt"
    file = BufferedInputFile(content.encode("utf-8"), filename=filename)
    await callback.message.answer_document(file, caption="Полная статистика")

@router.callback_query(F.data == "admin_block")
async def cb_admin_block(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.block_user)
    await callback.message.edit_text(
        "Введите ID или @username пользователя для блокировки:",
        reply_markup=kb_admin_back())
    await callback.answer()

@router.message(AdminStates.block_user)
async def process_block_user(message: Message, state: FSMContext):
    user = await resolve_user_input(message.text)
    if not user:
        await message.answer("Пользователь не найден. Введите ID или @username.")
        return
    await set_blocked(user["user_id"], True)
    await state.clear()
    await message.answer(f"Пользователь <b>{user['user_id']}</b> заблокирован.", parse_mode="HTML")
    await show_admin_panel(message)

@router.callback_query(F.data == "admin_unblock")
async def cb_admin_unblock(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.unblock_user)
    await callback.message.edit_text(
        "Введите ID или @username пользователя для разблокировки:",
        reply_markup=kb_admin_back())
    await callback.answer()

@router.message(AdminStates.unblock_user)
async def process_unblock_user(message: Message, state: FSMContext):
    user = await resolve_user_input(message.text)
    if not user:
        await message.answer("Пользователь не найден. Введите ID или @username.")
        return
    await set_blocked(user["user_id"], False)
    await state.clear()
    await message.answer(f"Пользователь <b>{user['user_id']}</b> разблокирован.", parse_mode="HTML")
    await show_admin_panel(message)

@router.callback_query(F.data == "admin_broadcast")
async def cb_admin_broadcast(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.broadcast)
    await callback.message.edit_text(
        "Введите текст рассылки:",
        reply_markup=kb_admin_back())
    await callback.answer()

@router.message(AdminStates.broadcast)
async def process_broadcast(message: Message, state: FSMContext):
    await state.clear()
    users  = await get_all_users()
    text   = message.text
    sent   = 0
    failed = 0
    for u in users:
        if u.get("is_buyer"):
            continue
        try:
            await bot.send_message(u["user_id"], text)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    await message.answer(
        f"Рассылка завершена.\nОтправлено: <b>{sent}</b>\nОшибок: <b>{failed}</b>",
        parse_mode="HTML")
    await show_admin_panel(message)


@router.callback_query(F.data.startswith("im_here:"))
async def cb_im_here(callback: CallbackQuery):
    sid = int(callback.data.split(":")[1])
    if callback.from_user.id != sid:
        await callback.answer("Это не ваша кнопка.", show_alert=True)
        return
    _paused_sellers.discard(sid)
    if sid in _presence_tracker:
        _presence_tracker[sid]["last_ping"] = _time.time()
    await callback.message.edit_text("Вы снова в очереди!")
    await callback.answer("Номера возвращены в очередь.")

@router.callback_query(F.data == "back_main")
async def cb_back_main(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_main_menu(callback, callback.from_user.id, "Главное меню")

@router.callback_query(F.data == "menu_instruction")
async def cb_instruction(callback: CallbackQuery):
    buyer = await is_buyer(callback.from_user.id)
    if buyer:
        text = (
            "<b>Инструкция</b>\n\n"
            "<b>Для покупателей:</b>\n"
            "1. Нажмите <b>Купить номер</b>\n"
            "2. Нажмите <b>Получить код</b>\n"
            "3. После входа нажмите <b>Вход выполнен</b>\n"
            "4. Оплатите счёт — подтверждение придёт автоматически\n"
            "5. Если вы сделали 5 отмен за 10 минут - вы не сможете делать отмены в течении 30 минут"
        )
    else:
        text = (
            "<b>Инструкция</b>\n\n"
            "<b>Для продавцов:</b>\n"
            "1. Нажмите <b>Продать номер</b>\n"
            "2. Введите номер телефона 79XXXXXXXXX\n"
            "3. Номер встанет в очередь\n"
            "4. Когда покупатель запросит код — вам придёт сообщение\n"
            "5. <b>Ответьте</b> на него кодом из СМС\n"
            "6. После подтверждения входа деньги придут на баланс автоматически\n\n"
            "<b>Вывод:</b> Профиль — Вывод средств — выберите сумму — чек в CryptoBot"
        )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb_back())
    await callback.answer()

@router.callback_query(F.data == "menu_support")
async def cb_support(callback: CallbackQuery):
    await callback.message.edit_text(
        "<b>Поддержка</b>\n\nПо всем вопросам: @RoslerX\n",
        parse_mode="HTML", reply_markup=kb_back())
    await callback.answer()

@router.callback_query(F.data == "menu_accounts")
async def cb_accounts(callback: CallbackQuery):
    uid = callback.from_user.id
    active = await get_queue_item_for_buyer(uid)
    transactions = await get_buyer_transactions(uid) if await is_buyer(uid) else await get_user_transactions(uid)
    lines = ["📑 <b>Мои аккаунты</b>", ""]
    if active:
        label = "Токен" if active.get("room") == "token" else "Номер"
        lines.append(f"В работе: {label} <b>{escape(queue_item_label(active))}</b>")
    else:
        lines.append("В работе: <b>нет активной заявки</b>")
    lines.append(f"Завершено: <b>{len(transactions)}</b>")
    await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=kb_back())
    await callback.answer()

@router.callback_query(F.data == "menu_balance")
async def cb_balance(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    balance = float(user.get("balance") or 0) if user else 0
    text = (
        "🪙 <b>Баланс</b>\n\n"
        f"Доступно: <b>${balance:.2f}</b>\n"
        f"🛩 CryptoBot: <b>${balance:.2f}</b>"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb_profile(False))
    await callback.answer()

@router.callback_query(F.data == "menu_profile")
async def cb_profile(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    if not user:
        await show_main_menu(callback, callback.from_user.id)
        return
    uid        = callback.from_user.id
    buyer_flag = await is_buyer(uid)
    if buyer_flag:
        buyer_room   = await get_buyer_room(uid)
        bought_today = await get_buyer_bought_today(uid)
        bought_total = await get_buyer_bought_total(uid)
        await callback.message.edit_text(
            f"<b>{user['full_name']}</b>\n\n"
            f"Комната покупателя: <b>{ROOM_NAMES.get(buyer_room, buyer_room)}</b>\n"
            f"Куплено за сегодня: <b>{bought_today}</b>\n"
            f"Куплено всего: <b>{bought_total}</b>\n"
            "Оплата с покупателя не требуется.",
            parse_mode="HTML", reply_markup=kb_profile(buyer=True))
    else:
        price      = await get_setting("price_per_number")
        sold_today = await get_user_sold_today(uid)
        in_queue   = await get_user_queue_count(uid)
        await callback.message.edit_text(
            f"<b>{user['full_name']}</b>\n\n"
            f"Баланс: <b>{user['balance']:.2f}$</b>\n"
            f"Цена за 1 номер: <b>{price}$</b>\n"
            f"Всего продано: <b>{user['total_sold']}</b>\n"
            f"Продано сегодня: <b>{sold_today}</b>\n"
            f"В очереди: <b>{in_queue}</b>",
            parse_mode="HTML", reply_markup=kb_profile(buyer=False))
    await callback.answer()

@router.callback_query(F.data == "profile_stats")
async def cb_profile_stats(callback: CallbackQuery):
    uid        = callback.from_user.id
    user       = await get_user(uid)
    buyer_flag = await is_buyer(uid)
    if buyer_flag:
        txs   = await get_buyer_transactions(uid)
        lines = [
            "<b>Ваша статистика</b>\n",
            f"Всего куплено: <b>{len(txs)}</b> номеров",
            "Оплата с покупателя не требуется.\n",
            "<b>Купленные номера:</b>",
        ]
        if txs:
            for t in txs[:30]:
                lines.append(f"• {t['phone']} — {t['created_at'][:10]}")
            if len(txs) > 30:
                lines.append(f"... и ещё {len(txs) - 30} (скачайте .TXT)")
        else:
            lines.append("Нет покупок")
    else:
        txs   = await get_user_transactions(uid)
        total = sum(t["amount"] for t in txs)
        lines = [
            "<b>Ваша статистика</b>\n",
            f"Всего продано: <b>{len(txs)}</b> номеров",
            f"Всего заработано: <b>{total:.2f}$</b>",
            f"Текущий баланс: <b>{user['balance']:.2f}$</b>\n",
            "<b>Проданные номера:</b>",
        ]
        if txs:
            for t in txs[:30]:
                lines.append(f"• {t['phone']} — {t['amount']:.2f}$ — {t['created_at'][:10]}")
            if len(txs) > 30:
                lines.append(f"... и ещё {len(txs) - 30} (скачайте .TXT)")
        else:
            lines.append("Нет продаж")
    text = "\n".join(lines)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb_user_stats())
    await callback.answer()

@router.callback_query(F.data == "profile_stats_txt")
async def cb_profile_stats_txt(callback: CallbackQuery):
    await callback.answer("Генерирую файл...")
    uid        = callback.from_user.id
    user       = await get_user(uid)
    buyer_flag = await is_buyer(uid)
    if buyer_flag:
        txs     = await get_buyer_transactions(uid)
        content = build_user_stats_text(user, txs, is_buyer=True)
    else:
        txs     = await get_user_transactions(uid)
        content = build_user_stats_text(user, txs, is_buyer=False)
    filename = f"my_stats_{date.today().isoformat()}.txt"
    file = BufferedInputFile(content.encode("utf-8"), filename=filename)
    await callback.message.answer_document(file, caption="Ваша статистика")


@router.callback_query(F.data == "withdraw")
async def cb_withdraw(callback: CallbackQuery, state: FSMContext):
    user = await get_user(callback.from_user.id)
    if not user:
        await callback.answer("Профиль не найден", show_alert=True)
        return
    min_wd  = float(await get_setting("min_withdraw"))
    balance = user["balance"]
    if balance < min_wd:
        await callback.answer(f"Минимум для вывода: {min_wd}$\nВаш баланс: {balance:.2f}$", show_alert=True)
        return
    await state.set_state(WithdrawStates.enter_amount)
    await state.update_data(balance=balance, min_wd=min_wd)
    await callback.message.edit_text(
        f"<b>Вывод средств</b>\n\n"
        f"Ваш баланс: <b>{balance:.2f}$</b>\n"
        f"Минимум: <b>{min_wd}$</b>\n\n"
        f"Введите сумму или нажмите <b>Вывести всё</b>:",
        parse_mode="HTML", reply_markup=kb_withdraw(balance))
    await callback.answer()

@router.callback_query(F.data == "withdraw_all")
async def cb_withdraw_all(callback: CallbackQuery, state: FSMContext):
    data    = await state.get_data()
    balance = data.get("balance", 0)
    await state.clear()
    await do_withdraw(callback, balance)

@router.callback_query(F.data == "withdraw_cancel")
async def cb_withdraw_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_main_menu(callback, callback.from_user.id, "Главное меню")

@router.message(WithdrawStates.enter_amount)
async def process_withdraw_amount(message: Message, state: FSMContext):
    data    = await state.get_data()
    balance = data.get("balance", 0)
    min_wd  = data.get("min_wd", 0)
    try:
        amount = float(message.text.strip().replace(",", "."))
    except ValueError:
        await message.answer("Введите число, например: 5 или 2.5")
        return
    if amount < min_wd:
        await message.answer(f"Минимальная сумма: <b>{min_wd}$</b>", parse_mode="HTML")
        return
    if amount > balance:
        await message.answer(f"Недостаточно средств. Баланс: <b>{balance:.2f}$</b>", parse_mode="HTML")
        return
    await state.clear()
    await do_withdraw(message, amount)


def kb_sell():
    return InlineKeyboardMarkup(inline_keyboard=[
        [ui_button("MAX QR", icon="max", callback_data="sell_room:qr", style="primary")],
        [ui_button("MAX SMS", icon="max", callback_data="sell_room:sms", style="primary")],
        [ui_button("Сдача токенов", icon="dollar", callback_data="sell_room:token", style="success")],
        [ui_button("Главное меню", icon="back", callback_data="back_main", style="primary")],
    ])

def kb_buy_rooms(room=None):
    rooms = [room] if room in ROOM_NAMES else ["qr", "sms", "token"]
    labels = {"qr": "MAX QR", "sms": "MAX SMS", "token": "Сдача токенов"}
    icons = {"qr": "max", "sms": "max", "token": "dollar"}
    styles = {"qr": "primary", "sms": "primary", "token": "success"}
    rows = [[ui_button(labels[r], icon=icons[r], callback_data=f"buy_room:{r}", style=styles[r])] for r in rooms]
    rows.append([ui_button("Главное меню", icon="back", callback_data="back_main", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_room(room, queue_id=None):
    rows = [[ui_button("Обновить", icon="lightning", callback_data=f"room_refresh:{room}:{queue_id or 0}", style="primary")]]
    if queue_id:
        rows.append([ui_button("Выйти из комнаты", icon="back", callback_data=f"cancel_number:{queue_id}", style="danger")])
    else:
        rows.append([ui_button("Выйти из комнаты", icon="back", callback_data="room_exit", style="danger")])
    rows.append([ui_button("Комнаты и токены", icon="catalog", callback_data="rooms_menu", style="success")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

ROOM_NAMES = {"qr": "MAX QR", "sms": "MAX SMS", "token": "токенов"}

async def build_room_text(room):
    participants = await get_room_participants(room)
    name = ROOM_NAMES.get(room, room.upper())
    return (
        f"🎉 <b>Комната {name}</b>\n"
        "Ты в комнате\n\n"
        f"╰ Участников: <b>{participants}</b>"
    )

ROOMS_TEXT = (
    "🎉 <b>Комнаты по сдаче</b>\n\n"
    f"┌ <b>MAX {premium_icon('max')} QR</b> — сдача по QR\n"
    f"│ <b>MAX {premium_icon('max')} SMS</b> — сдача по SMS\n"
    "│ <b>Токены</b> — сдача Android/WEB-токена\n\n"
    "Выбери комнату или сдачу токена:"
)

@router.callback_query(F.data == "menu_sell")
async def cb_sell(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        ROOMS_TEXT,
        parse_mode="HTML",
        reply_markup=kb_sell())
    await state.set_state(SellStates.choose_room)
    await callback.answer()

@router.callback_query(F.data.startswith("sell_room:"))
async def cb_sell_room(callback: CallbackQuery, state: FSMContext):
    room = callback.data.split(":", 1)[1]
    if room == "qr":
        await join_room(callback.from_user.id, room)
        await state.clear()
        await callback.message.edit_text(
            await build_room_text(room),
            parse_mode="HTML",
            reply_markup=kb_room(room))
        await callback.answer(f"Вы вошли в комнату {ROOM_NAMES[room]}")
        return
    if room == "token":
        await join_room(callback.from_user.id, room)
        await state.update_data(room=room)
        await state.set_state(SellStates.waiting_phone)
        await callback.message.edit_text(
            (await build_room_text(room)) + "\n\nПришлите токен файлом",
            parse_mode="HTML", reply_markup=kb_room(room))
        await callback.answer(f"Вы вошли в комнату {ROOM_NAMES[room]}")
        return
    await leave_room(callback.from_user.id)
    await state.update_data(room=room)
    await state.set_state(SellStates.waiting_phone)
    prompt = (
        "Введите номер телефона для продажи текстом или пришлите файл с SMS.\n"
        "Формат: <b>79XXXXXXXXX</b>"
    )
    await callback.message.edit_text(prompt, parse_mode="HTML", reply_markup=kb_back())
    await callback.answer()

@router.callback_query(F.data == "sell_single")
async def cb_sell_single(callback: CallbackQuery, state: FSMContext):
    msg = await callback.message.edit_text(
        "Введите номер телефона для продажи\nФормат: <b>79XXXXXXXXX</b> или <b>+79XXXXXXXXX</b> или <b>89XXXXXXXXX</b>",
        parse_mode="HTML", reply_markup=kb_back())
    await state.set_state(SellStates.waiting_phone)
    await state.update_data(prompt_msg_id=msg.message_id)
    await callback.answer()

@router.callback_query(F.data == "sell_batch")
async def cb_sell_batch(callback: CallbackQuery, state: FSMContext):
    msg = await callback.message.edit_text(
        "Введите номера телефонов, каждый с новой строки:\n\n<b>7XXXXXXXXXX\n7XXXXXXXXXX\n7XXXXXXXXXX\n7XXXXXXXXXX</b>",
        parse_mode="HTML", reply_markup=kb_back())
    await state.set_state(SellStates.waiting_batch)
    await state.update_data(prompt_msg_id=msg.message_id)
    await callback.answer()

@router.message(SellStates.waiting_phone)
async def process_phone(message: Message, state: FSMContext):
    data      = await state.get_data()
    room      = data.get("room", "sms")
    prompt_id = data.get("prompt_msg_id")
    if prompt_id:
        try:
            await bot.delete_message(message.chat.id, prompt_id)
        except Exception:
            pass
    try:
        await message.delete()
    except Exception:
        pass

    media_type, file_id = extract_media(message)
    if media_type and room in {"sms", "token"}:
        item = await add_media_to_queue(message, message.from_user.id, room)
        await state.clear()
        if item:
            label = "SMS-файл" if room == "sms" else "файл токена"
            await message.answer(
                f"{label.capitalize()} добавлен в очередь. Ожидайте покупателя.",
                reply_markup=kb_main(await is_buyer(message.from_user.id)))
        else:
            await message.answer("Не удалось принять файл. Пришлите фото или документ.")
        try:
            await message.delete()
        except Exception:
            pass
        return

    raw_text = (message.text or "").strip()
    if not raw_text:
        await state.clear()
        await message.answer("Отправьте данные текстовым сообщением.", reply_markup=kb_main(await is_buyer(message.from_user.id)))
        return
    if room == "token":
        result = await add_to_queue(raw_text, message.from_user.id, "token")
        await state.clear()
        text = "Токен уже есть в очереди." if result == "duplicate" else "Токен добавлен в очередь. Ожидайте покупателя."
        await message.answer(text, reply_markup=kb_main(await is_buyer(message.from_user.id)))
        return
    if room != "sms":
        await state.clear()
        await message.answer(
            "Для этой комнаты вводить номер или токен не нужно. Выберите комнату заново.",
            reply_markup=kb_main(await is_buyer(message.from_user.id)))
        return
    phone = raw_text.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")

    if phone.startswith("+7"):
        digits = phone[2:]
    elif phone.startswith("8"):
        digits = phone[1:]
    elif phone.startswith("7") and len(phone) == 11:
        digits = phone[1:]
    else:
        buyer = await is_buyer(message.from_user.id)
        await message.answer(
            "Неверный формат. Принимаются номера начинающиеся на 7, +7 или 8.\nПример: 79161234567",
            reply_markup=kb_main(buyer))
        await state.clear()
        return

    if len(digits) != 10 or not digits.isdigit():
        buyer = await is_buyer(message.from_user.id)
        await message.answer(
            "Номер должен содержать 10 цифр после кода страны.\nПример: 79161234567",
            reply_markup=kb_main(buyer))
        await state.clear()
        return

    phone = "7" + digits
    result = await add_to_queue(phone, message.from_user.id, room)
    await state.clear()
    buyer = await is_buyer(message.from_user.id)

    if result == "duplicate":
        await message.answer(f"Номер <b>{phone}</b> уже в очереди.", parse_mode="HTML", reply_markup=kb_main(buyer))
        return

    price = await get_room_price(room)
    await message.answer("Главное меню:", reply_markup=kb_main(buyer))
    queue_msg = await message.answer(
        f"Номер <b>{phone}</b> добавлен в очередь.\nВы получите <b>{price}$</b> после продажи.\n\nОжидайте — когда покупатель запросит код, вы получите уведомление.",
        parse_mode="HTML"
    )
    await set_queue_msg_id(phone, queue_msg.message_id)

def _parse_phone(raw: str):
    phone = raw.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if phone.startswith("+7"):
        digits = phone[2:]
    elif phone.startswith("8"):
        digits = phone[1:]
    elif phone.startswith("7") and len(phone) == 11:
        digits = phone[1:]
    else:
        return None
    if len(digits) != 10 or not digits.isdigit():
        return None
    return "7" + digits

@router.message(SellStates.waiting_batch)
async def process_batch(message: Message, state: FSMContext):
    data      = await state.get_data()
    prompt_id = data.get("prompt_msg_id")
    if prompt_id:
        try:
            await bot.delete_message(message.chat.id, prompt_id)
        except Exception:
            pass
    try:
        await message.delete()
    except Exception:
        pass

    lines   = message.text.strip().split("\n")
    added   = []
    dupes   = []
    invalid = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        phone = _parse_phone(line)
        if not phone:
            invalid.append(line)
            continue
        result = await add_to_queue(phone, message.from_user.id, data.get("room", "sms"))
        if result == "duplicate":
            dupes.append(phone)
        else:
            added.append(phone)

    await state.clear()
    buyer = await is_buyer(message.from_user.id)
    price = await get_room_price(data.get("room", "sms"))
    parts = []
    if added:
        parts.append(f"Добавлено: <b>{len(added)}</b> номеров ({price}$ за каждый)")
    if dupes:
        parts.append(f"Дубликаты: <b>{len(dupes)}</b>")
    if invalid:
        parts.append(f"Неверный формат: <b>{len(invalid)}</b>")
    text = "\n".join(parts) if parts else "Ни один номер не добавлен."
    await message.answer(text, parse_mode="HTML", reply_markup=kb_main(buyer))


@router.callback_query(F.data == "menu_buy")
async def cb_buy(callback: CallbackQuery):
    global _queue_pick_counter
    if not await is_buyer(callback.from_user.id):
        await callback.answer("У вас нет доступа к покупке.", show_alert=True)
        return
    await callback.message.edit_text(
        ROOMS_TEXT + f"\n\nТебе доступна комната: <b>{ROOM_NAMES[await get_buyer_room(callback.from_user.id)]}</b>",
        parse_mode="HTML", reply_markup=kb_buy_rooms(await get_buyer_room(callback.from_user.id)))
    await callback.answer()

@router.callback_query(F.data == "take")
async def cb_take(callback: CallbackQuery, state: FSMContext):
    """Take a request from the single room assigned to this buyer."""
    global _queue_pick_counter
    room = await get_buyer_room(callback.from_user.id)
    if not room:
        await callback.answer("У вас нет доступа к покупке.", show_alert=True)
        return
    active = await get_queue_item_for_buyer(callback.from_user.id)
    if active:
        await callback.answer(f"У вас уже есть активная заявка: {queue_item_label(active)}", show_alert=True)
        return
    _queue_pick_counter += 1
    item = await get_next_in_queue(_queue_pick_counter % 3 == 1, room)
    if not item and room in {"qr", "token"}:
        item = await create_room_request(callback.from_user.id, room)
    if not item:
        await callback.answer("Очередь пуста.", show_alert=True)
        return
    room = item.get("room", "sms")
    await assign_queue_item(item["id"], callback.from_user.id)
    if room in {"sms", "token"} and item.get("source_file_id"):
        label = "SMS-файл" if room == "sms" else "Файл токена"
        await send_queue_media(
            item, callback.from_user.id,
            caption=f"{label} получен. После проверки нажмите кнопку ниже.",
            reply_markup=kb_delivery_received(item["id"])
        )
        await callback.message.edit_text(
            await build_room_text(room), parse_mode="HTML",
            reply_markup=kb_room(room, item["id"]))
    elif room == "qr":
        await state.set_state(BuyerStates.waiting_qr)
        await state.update_data(qr_queue_id=item["id"])
        await callback.message.edit_text(
            "Заявка взята. Отправьте сюда QR-код фотографией или файлом — "
            "он будет передан рабочему.",
            parse_mode="HTML", reply_markup=kb_room(room, item["id"]))
    elif room == "token":
        try:
            sent = await bot.send_message(
                item["seller_id"],
                "Покупатель взял заявку в комнате токенов. Ответьте на это сообщение текстом токена.",
                parse_mode="HTML")
            pending_code_requests[item["id"]] = sent.message_id
        except Exception:
            pass
        await callback.message.edit_text(
            await build_room_text(room), parse_mode="HTML",
            reply_markup=kb_room(room, item["id"]))
    else:
        await callback.message.edit_text(
            f"Ваш номер: <b>{item['phone']}</b>\n\nНажмите кнопку, чтобы запросить код.",
            parse_mode="HTML", reply_markup=kb_get_code(item["id"]))
    await callback.answer("Заявка взята")

@router.message(BuyerStates.waiting_qr)
async def process_buyer_qr(message: Message, state: FSMContext):
    data = await state.get_data()
    queue_id = data.get("qr_queue_id")
    item = await get_queue_item_by_id(queue_id) if queue_id else None
    if not item or item.get("buyer_id") != message.from_user.id or item.get("room") != "qr":
        await state.clear()
        await message.answer("QR-заявка больше недоступна.", reply_markup=kb_main(True))
        return
    if not (message.photo or message.document or message.text or message.caption):
        await message.answer("Отправьте QR-код фотографией, файлом или текстом.")
        return
    try:
        if message.photo or message.document:
            media_type, file_id = extract_media(message)
            await send_media_with_caption(
                item["seller_id"], media_type, file_id,
                "QR от покупателя для заявки <b>QR-запрос</b>.\n" +
                escape((message.caption or "").strip()) +
                "\n\nQR-код получен. Выполните вход и сообщите покупателю результат.",
            )
        else:
            await bot.send_message(
                item["seller_id"],
                "QR от покупателя для заявки <b>QR-запрос</b>:\n\n"
                f"<blockquote>{escape((message.text or message.caption or '').strip())}</blockquote>\n\n"
                "QR-код получен. Выполните вход и сообщите покупателю результат.",
                parse_mode="HTML")
        await message.answer(
            "QR-код отправлен рабочему. После успешного входа нажмите кнопку ниже.",
            reply_markup=kb_delivery_received(queue_id))
        await state.clear()
    except Exception as exc:
        logging.error("[buyer_qr] %s", exc)
        await message.answer("Не удалось передать QR-код. Попробуйте отправить ещё раз.")

@router.callback_query(F.data == "rooms_menu")
async def cb_rooms_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    buyer_room = await get_buyer_room(callback.from_user.id)
    keyboard = kb_buy_rooms(buyer_room) if buyer_room else kb_sell()
    await callback.message.edit_text(ROOMS_TEXT, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()

@router.callback_query(F.data == "room_exit")
async def cb_room_exit(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await leave_room(callback.from_user.id)
    await show_main_menu(callback, callback.from_user.id)

@router.callback_query(F.data.startswith("room_refresh:"))
async def cb_room_refresh(callback: CallbackQuery):
    _, room, queue_id_raw = callback.data.split(":", 2)
    queue_id = int(queue_id_raw)
    item = await get_queue_item_by_id(queue_id) if queue_id else None
    if item and item.get("buyer_id") != callback.from_user.id:
        await callback.answer("Это не ваша заявка.", show_alert=True)
        return
    await callback.message.edit_text(
        await build_room_text(room),
        parse_mode="HTML",
        reply_markup=kb_room(room, queue_id if item and item.get("status") == "in_progress" else None))
    await callback.answer("Комната обновлена")

@router.callback_query(F.data.startswith("buy_room:"))
async def cb_buy_room(callback: CallbackQuery, state: FSMContext):
    room = callback.data.split(":", 1)[1]
    assigned_room = await get_buyer_room(callback.from_user.id)
    if not assigned_room or assigned_room != room:
        await callback.answer("У вас нет доступа к покупке.", show_alert=True); return
    active = await get_queue_item_for_buyer(callback.from_user.id)
    if active:
        await callback.answer(f"У вас уже есть активная заявка: {queue_item_label(active)}", show_alert=True); return
    _queue_pick_counter += 1
    prefer_priority = (_queue_pick_counter % 3 == 1)
    item = await get_next_in_queue(prefer_priority, room)
    if not item and room in {"qr", "token"}:
        item = await create_room_request(callback.from_user.id, room)
    if not item:
        await callback.answer("Очередь пуста.", show_alert=True)
        return
    await assign_queue_item(item["id"], callback.from_user.id)
    if room in {"sms", "token"} and item.get("source_file_id"):
        label = "SMS-файл" if room == "sms" else "Файл токена"
        await send_queue_media(
            item, callback.from_user.id,
            caption=f"{label} получен. После проверки нажмите кнопку ниже.",
            reply_markup=kb_delivery_received(item["id"])
        )
        await callback.message.edit_text(
            await build_room_text(room), parse_mode="HTML",
            reply_markup=kb_room(room, item["id"]))
    elif room == "token":
        try:
            sent = await bot.send_message(
                item["seller_id"],
                "Покупатель взял заявку в комнате токенов. Ответьте на это сообщение текстом токена.",
                parse_mode="HTML")
            pending_code_requests[item["id"]] = sent.message_id
        except Exception:
            pass
        await callback.message.edit_text(await build_room_text(room), parse_mode="HTML", reply_markup=kb_room(room, item["id"]))
    elif room == "qr":
        await state.set_state(BuyerStates.waiting_qr)
        await state.update_data(qr_queue_id=item["id"])
        await callback.message.edit_text(
            "Заявка взята. Отправьте сюда QR-код фотографией или файлом — он будет передан рабочему.",
            parse_mode="HTML", reply_markup=kb_room(room, item["id"]))
    else:
        await callback.message.edit_text(f"Ваш номер: <b>{item['phone']}</b>\n\nНажмите кнопку чтобы запросить код:", parse_mode="HTML", reply_markup=kb_get_code(item["id"]))
    await callback.answer()


@router.callback_query(F.data.startswith("get_code:"))
async def cb_get_code(callback: CallbackQuery):
    queue_id = int(callback.data.split(":")[1])
    item     = await get_queue_item_by_id(queue_id)
    if not item:
        await callback.answer("Номер не найден.", show_alert=True)
        return
    if item["buyer_id"] != callback.from_user.id:
        await callback.answer("Это не ваш номер.", show_alert=True)
        return
    if item.get("room") == "qr":
        await callback.answer("QR отправляет покупатель отдельным сообщением.", show_alert=True)
        return
    if item.get("room") == "token" and queue_id not in pending_code_requests:
        await callback.answer("Запрос токена уже отправлен рабочему.", show_alert=True)
        return
    if queue_id in pending_code_requests:
        await callback.answer("Запрос уже отправлен. Ожидайте ответ.", show_alert=True)
        return
    if item.get("seller_queue_msg_id"):
        try:
            await bot.delete_message(item["seller_id"], item["seller_queue_msg_id"])
        except Exception:
            pass
    try:
        room = item.get("room", "sms")
        if room == "qr":
            instruction = "QR-кодом (фото или изображением)"
        elif room == "token":
            instruction = "текстом токена"
        else:
            instruction = "кодом из СМС"
        sent = await bot.send_message(
            item["seller_id"],
            f"Запрос покупателя!\n\nЗаявка: <b>{item['phone']}</b>\n\n<b>Ответьте на это сообщение</b> {instruction}.",
            parse_mode="HTML")
        pending_code_requests[queue_id] = sent.message_id
        await callback.answer("Запрос отправлен продавцу!")
    except Exception as e:
        await callback.answer(f"Не удалось уведомить продавца: {e}", show_alert=True)

@router.message(F.reply_to_message)
async def handle_code_reply(message: Message):
    reply_id        = message.reply_to_message.message_id
    target_queue_id = next((q for q, m in pending_code_requests.items() if m == reply_id), None)
    if target_queue_id is None:
        return
    item = await get_queue_item_by_id(target_queue_id)
    if not item or item["seller_id"] != message.from_user.id:
        return
    code = (message.text or message.caption or "").strip()
    media_type, file_id = extract_media(message)
    if not code and not media_type:
        await message.answer("Ответ должен содержать текст, токен или QR-фото.")
        return
    del pending_code_requests[target_queue_id]
    try:
        item_label = queue_item_label(item)
        if media_type:
            await send_media_with_caption(
                item["buyer_id"], media_type, file_id,
                f"Данные получены!\n\nЗаявка: <b>{escape(str(item_label))}</b>\n" +
                escape((message.caption or "").strip()) +
                "\n\nПосле успешного входа нажмите кнопку ниже.",
                reply_markup=kb_delivery_received(target_queue_id)
            )
        else:
            await bot.send_message(
                item["buyer_id"],
                f"Данные получены!\n\nЗаявка: <b>{escape(str(item_label))}</b>\n\n<blockquote>{escape(code)}</blockquote>\n\nПосле успешного входа нажмите кнопку ниже.",
                parse_mode="HTML", reply_markup=kb_delivery_received(target_queue_id))
        await message.answer("Код отправлен покупателю.")
    except Exception as e:
        await message.answer(f"Ошибка: {e}")


@router.callback_query(F.data.startswith("cancel_number:"))
async def cb_cancel_number(callback: CallbackQuery, state: FSMContext):
    queue_id = int(callback.data.split(":")[1])
    item     = await get_queue_item_by_id(queue_id)
    if not item:
        await callback.answer("Номер не найден.", show_alert=True)
        return
    if item["buyer_id"] != callback.from_user.id:
        await callback.answer("Это не ваш номер.", show_alert=True)
        return
    if item["status"] != "in_progress":
        await callback.answer("Этот номер уже нельзя отменить.", show_alert=True)
        return
    if item.get("room") == "qr":
        current = await state.get_data()
        if current.get("qr_queue_id") == queue_id:
            await state.clear()
    await cancel_queue_item(queue_id)
    item_label = queue_item_label(item)
    try:
        await bot.send_message(
            item["seller_id"],
            f"Заявка <b>{escape(str(item_label))}</b> была отменена из-за неработоспособности.",
            parse_mode="HTML")
    except Exception:
        pass
    await callback.message.edit_text(
        f"Заявка <b>{escape(str(item_label))}</b> отменена.",
        parse_mode="HTML", reply_markup=kb_main(True))
    await callback.answer("Номер отменён.")


@router.callback_query(F.data.startswith("login_done:"))
async def cb_login_done(callback: CallbackQuery):
    queue_id = int(callback.data.split(":")[1])
    item     = await get_queue_item_by_id(queue_id)
    if not item:
        await callback.answer("Сессия не найдена.", show_alert=True)
        return
    if item["buyer_id"] != callback.from_user.id:
        await callback.answer("Это не ваша сессия.", show_alert=True)
        return
    if item.get("status") != "in_progress":
        await callback.answer("Эта заявка уже завершена или отменена.", show_alert=True)
        return
    # Покупатель не оплачивает номер: продажа фиксируется и начисляется продавцу автоматически.
    try:
        seller_price = await get_room_price(item.get("room", "sms"))
        tx_id = await create_transaction(item["seller_id"], callback.from_user.id, item["phone"], seller_price, "", 0)
        await pay_transaction(tx_id)
    except Exception as exc:
        logging.error("[free_purchase] %s", exc)
    await complete_queue_item(queue_id)
    try:
        item_label = queue_item_label(item)
        await bot.send_message(
            item["seller_id"],
            f"Покупатель подтвердил вход по заявке <b>{escape(str(item_label))}</b>.",
            parse_mode="HTML")
    except Exception:
        pass
    room_label = ROOM_NAMES.get(item.get("room", "sms"), "SMS")
    await callback.message.edit_text(
        f"Вход через комнату <b>{room_label}</b> подтверждён. Оплата с покупателя не требуется.",
        parse_mode="HTML", reply_markup=kb_main(True))
    await callback.answer("Готово")

@router.callback_query(F.data.startswith("check_pay:"))
async def cb_check_pay(callback: CallbackQuery):
    queue_id = int(callback.data.split(":")[1])
    unpaid   = await get_unpaid_transaction_for_buyer(callback.from_user.id)
    if not unpaid:
        await callback.answer("Оплата уже подтверждена!", show_alert=True)
        return
    invoice = await cb_get_invoice(int(unpaid["invoice_id"]))
    if not invoice or invoice.get("status") != "paid":
        await callback.answer("Оплата ещё не поступила. Попробуйте через несколько секунд.", show_alert=True)
        return
    await confirm_payment(unpaid)
    await callback.message.edit_text(
        callback.message.text + "\n\nОплата подтверждена!",
        parse_mode="HTML", reply_markup=None)
    await callback.answer("Оплата принята!")


async def main():
    await init_db()
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.middleware(BotEnabledMiddleware())
    dp.callback_query.middleware(BotEnabledMiddleware())
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(midnight_queue_reset_task())
    asyncio.create_task(presence_ping_task())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
