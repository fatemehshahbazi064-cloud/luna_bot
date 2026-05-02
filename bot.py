import os
import asyncio
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)

logging.basicConfig(level=logging.INFO)

API_TOKEN    = os.environ["BOT_TOKEN"]
ADMIN_ID     = int(os.environ["ADMIN_ID"])
CARD_NUMBER  = os.environ.get("CARD_NUMBER", "")
CARD_HOLDER  = os.environ.get("CARD_HOLDER", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")   # اگه ست باشه از PostgreSQL استفاده میکنه
DB_PATH      = os.environ.get("DB_PATH", "luna.db") # برای SQLite

PRICE_PER_GB         = 590
CARD_MSG_TTL         = 300
REFERRAL_BONUS_TOMAN = 590
CHARGE_PRESETS       = [5000, 10000, 20000, 50000, 100000]
ADMIN_USERS_PAGE     = 8

# اگه DATABASE_URL ست باشه psycopg2 لود میشه
if DATABASE_URL:
    import psycopg2
    import psycopg2.extras
    PH = "%s"
else:
    PH = "?"

bot = Bot(token=API_TOKEN)
dp  = Dispatcher(bot)

user_state:  dict = {}
admin_state: dict = {}


# ─────────────────────────── DATABASE LAYER ──────────────────────

@contextmanager
def db_conn():
    if DATABASE_URL:
        conn = psycopg2.connect(DATABASE_URL)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def _q(sql: str) -> str:
    """تبدیل placeholder برای هر دیتابیس"""
    return sql if DATABASE_URL else sql.replace("%s", "?")


def _exec(conn, sql: str, params=()):
    """اجرای query با cursor مناسب"""
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        return cur
    else:
        return conn.execute(_q(sql), params)


def _fetchall(conn, sql: str, params=()):
    return _exec(conn, sql, params).fetchall()


def _fetchone(conn, sql: str, params=()):
    return _exec(conn, sql, params).fetchone()


def _insert(conn, sql: str, params=()) -> int:
    """INSERT و برگشت lastrowid"""
    if DATABASE_URL:
        cur = conn.cursor()
        cur.execute(sql + " RETURNING id", params)
        return cur.fetchone()[0]
    else:
        cur = conn.execute(_q(sql), params)
        return cur.lastrowid


# ── Schema ────────────────────────────────────────────────────────

def _init_sqlite(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id             INTEGER PRIMARY KEY,
            username            TEXT,
            first_name          TEXT,
            joined_at           TEXT NOT NULL,
            referrer_id         INTEGER,
            balance             INTEGER NOT NULL DEFAULT 0,
            first_purchase_done INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS orders (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id        INTEGER NOT NULL,
            plan_gb        INTEGER NOT NULL,
            price          INTEGER NOT NULL,
            status         TEXT NOT NULL DEFAULT 'pending',
            payment_method TEXT NOT NULL DEFAULT 'card',
            created_at     TEXT NOT NULL,
            updated_at     TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS charges (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            amount     INTEGER NOT NULL,
            status     TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_orders_user   ON orders(user_id);
        CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
        CREATE INDEX IF NOT EXISTS idx_charges_user  ON charges(user_id);
    """)
    # migrations
    for col, decl in [
        ("referrer_id",        "INTEGER"),
        ("balance",            "INTEGER NOT NULL DEFAULT 0"),
        ("first_purchase_done","INTEGER NOT NULL DEFAULT 0"),
    ]:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
        if col not in cols:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} {decl}")


def _init_postgres(conn):
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id             BIGINT PRIMARY KEY,
            username            TEXT,
            first_name          TEXT,
            joined_at           TEXT NOT NULL,
            referrer_id         BIGINT,
            balance             INTEGER NOT NULL DEFAULT 0,
            first_purchase_done INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS orders (
            id             SERIAL PRIMARY KEY,
            user_id        BIGINT NOT NULL,
            plan_gb        INTEGER NOT NULL,
            price          INTEGER NOT NULL,
            status         TEXT NOT NULL DEFAULT 'pending',
            payment_method TEXT NOT NULL DEFAULT 'card',
            created_at     TEXT NOT NULL,
            updated_at     TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS charges (
            id         SERIAL PRIMARY KEY,
            user_id    BIGINT NOT NULL,
            amount     INTEGER NOT NULL,
            status     TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_user   ON orders(user_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_charges_user  ON charges(user_id);")


def init_db():
    with db_conn() as conn:
        if DATABASE_URL:
            _init_postgres(conn)
        else:
            _init_sqlite(conn)


# ── CRUD ──────────────────────────────────────────────────────────

def upsert_user(user: types.User, referrer_id=None):
    now = datetime.utcnow().isoformat()
    with db_conn() as conn:
        existing = _fetchone(conn, "SELECT user_id FROM users WHERE user_id=%s", (user.id,))
        if existing is None:
            ref = referrer_id if referrer_id and referrer_id != user.id else None
            _exec(conn,
                "INSERT INTO users (user_id,username,first_name,joined_at,referrer_id) VALUES (%s,%s,%s,%s,%s)",
                (user.id, user.username or "", user.first_name or "", now, ref),
            )
        else:
            _exec(conn,
                "UPDATE users SET username=%s, first_name=%s WHERE user_id=%s",
                (user.username or "", user.first_name or "", user.id),
            )


def get_user(user_id):
    with db_conn() as conn:
        return _fetchone(conn, "SELECT * FROM users WHERE user_id=%s", (user_id,))


def get_balance(user_id) -> int:
    row = get_user(user_id)
    return int(row["balance"]) if row else 0


def adjust_balance(user_id, delta: int):
    with db_conn() as conn:
        _exec(conn, "UPDATE users SET balance=balance+%s WHERE user_id=%s", (delta, user_id))


def create_order(user_id, plan_gb, price, status, method) -> int:
    now = datetime.utcnow().isoformat()
    with db_conn() as conn:
        return _insert(conn,
            "INSERT INTO orders (user_id,plan_gb,price,status,payment_method,created_at,updated_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (user_id, plan_gb, price, status, method, now, now),
        )


def get_order(order_id):
    with db_conn() as conn:
        return _fetchone(conn, "SELECT * FROM orders WHERE id=%s", (order_id,))


def update_order_status(order_id, status):
    now = datetime.utcnow().isoformat()
    with db_conn() as conn:
        _exec(conn, "UPDATE orders SET status=%s, updated_at=%s WHERE id=%s", (status, now, order_id))


def create_charge(user_id, amount) -> int:
    now = datetime.utcnow().isoformat()
    with db_conn() as conn:
        return _insert(conn,
            "INSERT INTO charges (user_id,amount,status,created_at,updated_at)"
            " VALUES (%s,%s,'pending',%s,%s)",
            (user_id, amount, now, now),
        )


def get_charge(charge_id):
    with db_conn() as conn:
        return _fetchone(conn, "SELECT * FROM charges WHERE id=%s", (charge_id,))


def update_charge_status(charge_id, status):
    now = datetime.utcnow().isoformat()
    with db_conn() as conn:
        _exec(conn, "UPDATE charges SET status=%s, updated_at=%s WHERE id=%s", (status, now, charge_id))


def user_stats(user_id):
    with db_conn() as conn:
        return _fetchone(conn, """
            SELECT
                COALESCE(SUM(CASE WHEN status='confirmed' THEN 1 ELSE 0 END),0)      AS confirmed_count,
                COALESCE(SUM(CASE WHEN status='confirmed' THEN price ELSE 0 END),0)   AS total_spent,
                COALESCE(SUM(CASE WHEN status='confirmed' THEN plan_gb ELSE 0 END),0) AS total_gb,
                COALESCE(SUM(CASE WHEN status IN('pending','paid_wallet') THEN 1 ELSE 0 END),0) AS pending_count
            FROM orders WHERE user_id=%s
        """, (user_id,))


def user_recent_orders(user_id, limit=5):
    with db_conn() as conn:
        return _fetchall(conn,
            "SELECT * FROM orders WHERE user_id=%s ORDER BY id DESC LIMIT %s", (user_id, limit)
        )


def referral_count(user_id) -> int:
    with db_conn() as conn:
        return _fetchone(conn,
            "SELECT COUNT(*) AS c FROM users WHERE referrer_id=%s", (user_id,)
        )["c"]


def admin_stats():
    with db_conn() as conn:
        ut = _fetchone(conn, "SELECT COUNT(*) AS c FROM users")["c"]
        agg = _fetchone(conn, """
            SELECT
                COALESCE(SUM(CASE WHEN status='confirmed' THEN 1 ELSE 0 END),0)      AS confirmed_count,
                COALESCE(SUM(CASE WHEN status='confirmed' THEN price ELSE 0 END),0)   AS revenue,
                COALESCE(SUM(CASE WHEN status='confirmed' THEN plan_gb ELSE 0 END),0) AS total_gb,
                COALESCE(SUM(CASE WHEN status IN('pending','paid_wallet') THEN 1 ELSE 0 END),0) AS pending_count,
                COALESCE(SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END),0)        AS rejected_count
            FROM orders
        """)
        wt = _fetchone(conn, "SELECT COALESCE(SUM(balance),0) AS s FROM users")["s"]
        return ut, agg, wt


def list_pending_orders(limit=10):
    with db_conn() as conn:
        return _fetchall(conn,
            "SELECT * FROM orders WHERE status IN('pending','paid_wallet') ORDER BY id DESC LIMIT %s",
            (limit,)
        )


def list_pending_charges(limit=10):
    with db_conn() as conn:
        return _fetchall(conn,
            "SELECT * FROM charges WHERE status='pending' ORDER BY id DESC LIMIT %s", (limit,)
        )


def list_users_page(offset=0, limit=ADMIN_USERS_PAGE):
    with db_conn() as conn:
        rows  = _fetchall(conn,
            "SELECT * FROM users ORDER BY user_id DESC LIMIT %s OFFSET %s", (limit, offset)
        )
        total = _fetchone(conn, "SELECT COUNT(*) AS c FROM users")["c"]
        return rows, total


def all_user_ids():
    with db_conn() as conn:
        return [r["user_id"] for r in _fetchall(conn, "SELECT user_id FROM users")]


# ─────────────────────────── HELPERS ─────────────────────────────

STATUS_FA = {
    "pending":     "⏳ در انتظار تایید",
    "confirmed":   "✅ تایید شده",
    "rejected":    "❌ رد شده",
    "paid_wallet": "💼 کیف پول، در انتظار کانفیگ",
}
PAY_FA = {"card": "کارت", "wallet": "کیف پول"}


def fp(n) -> str:
    return f"{int(n):,}"


async def delete_after(chat_id, msg_id, delay):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id, msg_id)
    except Exception:
        pass


async def send_card_info(chat_id, amount, purpose):
    text = (
        f"💳 پرداخت {purpose}\n\n"
        f"💰 مبلغ: {fp(amount)} تومان\n\n"
        f"🏦 شماره کارت:\n`{CARD_NUMBER}`\n"
        f"👤 به نام: {CARD_HOLDER}\n\n"
        f"⚠️ این پیام بعد از ۵ دقیقه خودکار پاک میشه.\n"
        f"بعد از واریز، تصویر رسید رو همین‌جا بفرستید."
    )
    sent = await bot.send_message(chat_id, text, parse_mode="Markdown")
    asyncio.create_task(delete_after(sent.chat.id, sent.message_id, CARD_MSG_TTL))


async def maybe_pay_referral_bonus(user_id):
    u = get_user(user_id)
    if not u or u["first_purchase_done"]:
        return
    with db_conn() as conn:
        _exec(conn, "UPDATE users SET first_purchase_done=1 WHERE user_id=%s", (user_id,))
    if u["referrer_id"]:
        adjust_balance(u["referrer_id"], REFERRAL_BONUS_TOMAN)
        try:
            await bot.send_message(
                u["referrer_id"],
                f"🎁 یکی از دوستانی که دعوت کرده بودید اولین خرید موفقش رو انجام داد!\n"
                f"💰 {fp(REFERRAL_BONUS_TOMAN)} تومان به کیف پول شما اضافه شد.",
            )
        except Exception:
            pass


# ─────────────────────────── KEYBOARDS ───────────────────────────

def reply_kb():
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(KeyboardButton("🏠 منوی اصلی"), KeyboardButton("👤 حساب کاربری"))
    kb.add(KeyboardButton("💰 کیف پول"),   KeyboardButton("🚀 شروع"))
    return kb


def main_menu():
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("🎟 اشتراک VIP",       callback_data="vip"),
        InlineKeyboardButton("💰 کیف پول",           callback_data="wallet"),
        InlineKeyboardButton("👤 حساب کاربری",       callback_data="account"),
        InlineKeyboardButton("👥 دعوت از دوستان",    callback_data="invite"),
        InlineKeyboardButton("📞 پشتیبانی",          callback_data="support"),
    )
    return kb


def vip_menu():
    kb = InlineKeyboardMarkup(row_width=2)
    for g in range(1, 11):
        price = g * PRICE_PER_GB
        star  = " ⭐" if g == 5 else (" 🔥" if g == 10 else "")
        kb.insert(InlineKeyboardButton(f"{g} گیگ — {fp(price)}{star}", callback_data=f"plan_{g}"))
    kb.add(InlineKeyboardButton("🔙 برگشت", callback_data="back"))
    return kb


def back_only():
    return InlineKeyboardMarkup().add(InlineKeyboardButton("🔙 برگشت", callback_data="back"))


def wallet_menu():
    return InlineKeyboardMarkup(row_width=1).add(
        InlineKeyboardButton("➕ شارژ کیف پول", callback_data="charge"),
        InlineKeyboardButton("🔙 برگشت",         callback_data="back"),
    )


def charge_menu():
    kb = InlineKeyboardMarkup(row_width=2)
    for amt in CHARGE_PRESETS:
        kb.insert(InlineKeyboardButton(f"{fp(amt)} ت", callback_data=f"charge_{amt}"))
    kb.add(InlineKeyboardButton("✏️ مبلغ دلخواه", callback_data="charge_custom"))
    kb.add(InlineKeyboardButton("🔙 برگشت",        callback_data="wallet"))
    return kb


def admin_menu_kb():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("📊 آمار کلی",            callback_data="adm_stats"),
        InlineKeyboardButton("⏳ سفارش‌های در انتظار", callback_data="adm_orders"),
    )
    kb.add(
        InlineKeyboardButton("💰 شارژ‌های در انتظار", callback_data="adm_charges"),
        InlineKeyboardButton("👥 لیست کاربران",        callback_data="adm_users:0"),
    )
    kb.add(
        InlineKeyboardButton("🔍 جستجوی کاربر", callback_data="adm_search"),
        InlineKeyboardButton("📢 پیام همگانی",   callback_data="adm_bcast"),
    )
    kb.add(InlineKeyboardButton("❌ بستن پنل", callback_data="adm_close"))
    return kb


def adm_back():
    return InlineKeyboardMarkup().add(
        InlineKeyboardButton("🔙 برگشت به پنل", callback_data="adm_menu")
    )


def user_actions_kb(uid):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("➕ افزایش موجودی", callback_data=f"adm_baladd:{uid}"),
        InlineKeyboardButton("➖ کاهش موجودی",   callback_data=f"adm_balsub:{uid}"),
    )
    kb.add(InlineKeyboardButton("🔙 برگشت به پنل", callback_data="adm_menu"))
    return kb


# ─────────────────────────── TEXTS ───────────────────────────────

def account_text(user: types.User) -> str:
    s    = user_stats(user.id)
    u    = get_user(user.id)
    bal  = int(u["balance"]) if u else 0
    refs = referral_count(user.id)
    lines = [
        f"👤 حساب کاربری: {user.first_name or 'کاربر'}",
        f"🆔 آیدی عددی: `{user.id}`", "",
        f"💰 موجودی کیف پول: {fp(bal)} تومان",
        f"🛒 خریدهای موفق: {s['confirmed_count']}",
        f"📦 مجموع گیگ: {s['total_gb']} گیگ",
        f"💸 مجموع پرداختی: {fp(s['total_spent'])} تومان",
        f"⏳ در انتظار: {s['pending_count']}",
        f"👥 دوستان دعوت‌شده: {refs}",
    ]
    recent = user_recent_orders(user.id, 5)
    if recent:
        lines += ["", "🧾 آخرین سفارش‌ها:"]
        for o in recent:
            lines.append(
                f"#{o['id']} • {o['plan_gb']}G • {fp(o['price'])} ت • "
                f"{PAY_FA.get(o['payment_method'],'')} • {STATUS_FA.get(o['status'], o['status'])}"
            )
    return "\n".join(lines)


def user_card_text(u, s) -> str:
    uname = f"@{u['username']}" if u["username"] else "—"
    return (
        f"👤 {u['first_name'] or '—'} ({uname})\n"
        f"🆔 آیدی: `{u['user_id']}`\n"
        f"💰 موجودی: {fp(int(u['balance']))} تومان\n"
        f"🛒 خریدهای موفق: {s['confirmed_count']} • 📦 {s['total_gb']}G • 💸 {fp(s['total_spent'])} ت\n"
        f"⏳ در انتظار: {s['pending_count']}\n"
        f"📅 عضویت: {(u['joined_at'] or '')[:10]}"
    )


# ═══════════════════════════ HANDLERS ════════════════════════════

# ── /start ────────────────────────────────────────────────────────

@dp.message_handler(commands=["start"])
async def cmd_start(message: types.Message):
    args = message.get_args() or ""
    ref = None
    if args.startswith("ref"):
        try: ref = int(args[3:])
        except ValueError: pass
    upsert_user(message.from_user, referrer_id=ref)
    await message.answer(
        "🌙 به LUNA SERVER خوش اومدید ✨\n\n"
        "⚡️ اینترنت پرسرعت و پایدار\n"
        "🔒 امنیت بالا و بدون قطعی\n\n"
        "💎 سرویس‌های VIP با کیفیت ویژه",
        reply_markup=reply_kb(),
    )
    await message.answer("👇 منوی اصلی:", reply_markup=main_menu())


@dp.message_handler(lambda m: m.text == "🚀 شروع")
async def text_start(msg: types.Message):
    await cmd_start(msg)


@dp.message_handler(lambda m: m.text == "🏠 منوی اصلی")
async def text_home(msg: types.Message):
    upsert_user(msg.from_user)
    user_state.pop(msg.from_user.id, None)
    await msg.answer("🌙 LUNA SERVER\n\n👇 انتخاب کنید:", reply_markup=main_menu())


@dp.message_handler(lambda m: m.text == "👤 حساب کاربری")
async def text_account(msg: types.Message):
    upsert_user(msg.from_user)
    await msg.answer(account_text(msg.from_user), parse_mode="Markdown")


@dp.message_handler(lambda m: m.text == "💰 کیف پول")
async def text_wallet(msg: types.Message):
    upsert_user(msg.from_user)
    await msg.answer(
        f"💰 کیف پول شما\n\nموجودی فعلی: {fp(get_balance(msg.from_user.id))} تومان",
        reply_markup=wallet_menu(),
    )


# ── /stats /admin ─────────────────────────────────────────────────

@dp.message_handler(commands=["stats"])
async def cmd_stats(msg: types.Message):
    if msg.from_user.id != ADMIN_ID: return
    u, agg, wt = admin_stats()
    await msg.answer(
        f"📊 آمار کلی\n\n👥 کاربرها: {u}\n✅ تایید‌شده: {agg['confirmed_count']}\n"
        f"⏳ در انتظار: {agg['pending_count']}\n❌ رد‌شده: {agg['rejected_count']}\n"
        f"📦 کل گیگ: {agg['total_gb']}G\n💰 کل درآمد: {fp(agg['revenue'])} ت\n"
        f"💼 جمع کیف پول‌ها: {fp(wt)} ت"
    )


@dp.message_handler(commands=["admin"])
async def cmd_admin(msg: types.Message):
    if msg.from_user.id != ADMIN_ID: return
    admin_state.clear()
    await msg.answer("🛠 پنل مدیریت LUNA SERVER\n\nیک گزینه را انتخاب کنید:", reply_markup=admin_menu_kb())


# ── Admin state: user lookup ──────────────────────────────────────

@dp.message_handler(
    lambda m: m.from_user.id == ADMIN_ID and admin_state.get("awaiting_user_lookup"),
    content_types=["text"],
)
async def admin_user_lookup(msg: types.Message):
    admin_state.pop("awaiting_user_lookup", None)
    q = (msg.text or "").strip().lstrip("@")
    uid = None
    if q.isdigit():
        uid = int(q)
    else:
        with db_conn() as conn:
            row = _fetchone(conn,
                "SELECT user_id FROM users WHERE LOWER(username)=LOWER(%s)", (q,)
            )
            if row: uid = int(row["user_id"])
    if uid is None:
        await msg.answer("⚠️ کاربری پیدا نشد.", reply_markup=adm_back()); return
    u = get_user(uid)
    if not u:
        await msg.answer(f"⚠️ آیدی {uid} در دیتابیس نیست.", reply_markup=adm_back()); return
    await msg.answer(user_card_text(u, user_stats(uid)), parse_mode="Markdown", reply_markup=user_actions_kb(uid))


# ── Admin state: balance adjust ───────────────────────────────────

@dp.message_handler(
    lambda m: m.from_user.id == ADMIN_ID and bool(admin_state.get("awaiting_balance")),
    content_types=["text"],
)
async def admin_balance_input(msg: types.Message):
    info = admin_state.pop("awaiting_balance", None)
    if not info: return
    uid, sign = info["user_id"], info["sign"]
    raw = (msg.text or "").strip().replace(",", "").replace("،", "")
    if not raw.isdigit():
        await msg.answer("⚠️ فقط عدد بفرستید. لغو شد.", reply_markup=adm_back()); return
    amount = int(raw)
    if amount <= 0:
        await msg.answer("⚠️ مبلغ باید مثبت باشه.", reply_markup=adm_back()); return
    delta = sign * amount
    if delta < 0 and get_balance(uid) + delta < 0:
        await msg.answer(
            f"⚠️ موجودی کاربر ({fp(get_balance(uid))} ت) کمتر از مبلغ کسر هست.",
            reply_markup=adm_back()
        ); return
    adjust_balance(uid, delta)
    new_bal = get_balance(uid)
    label = "افزایش" if sign > 0 else "کاهش"
    await msg.answer(
        f"✅ موجودی کاربر {uid} با {fp(amount)} تومان {label} یافت.\n💼 موجودی جدید: {fp(new_bal)} تومان",
        reply_markup=adm_back(),
    )
    try:
        note = f"🎁 ادمین {fp(amount)} تومان به کیف پول شما اضافه کرد.\n" if sign > 0 \
               else f"⚠️ ادمین {fp(amount)} تومان از کیف پول شما کسر کرد.\n"
        await bot.send_message(uid, note + f"💼 موجودی جدید: {fp(new_bal)} تومان")
    except Exception:
        pass


# ── Admin state: broadcast ────────────────────────────────────────

@dp.message_handler(
    lambda m: m.from_user.id == ADMIN_ID and admin_state.get("awaiting_broadcast"),
    content_types=["text", "photo", "document", "video"],
)
async def admin_broadcast_send(msg: types.Message):
    admin_state.pop("awaiting_broadcast", None)
    ids = all_user_ids()
    await msg.answer(f"📢 در حال ارسال به {len(ids)} کاربر...")
    sent = failed = 0
    for uid in ids:
        try:
            await bot.copy_message(uid, msg.chat.id, msg.message_id)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    await msg.answer(
        f"✅ پیام همگانی ارسال شد.\n📨 موفق: {sent}\n⚠️ ناموفق: {failed}",
        reply_markup=adm_back(),
    )


# ── Admin state: send config ──────────────────────────────────────

@dp.message_handler(
    lambda m: m.from_user.id == ADMIN_ID and admin_state.get("awaiting_config_for") is not None,
    content_types=["text", "photo", "document", "video"],
)
async def admin_send_config(msg: types.Message):
    order_id = admin_state.pop("awaiting_config_for")
    order = get_order(order_id)
    if not order:
        await msg.answer("⚠️ سفارش پیدا نشد"); return
    uid = order["user_id"]
    try:
        await bot.send_message(uid, f"📦 کانفیگ سفارش #{order_id} ({order['plan_gb']} گیگ):")
        await bot.copy_message(uid, msg.chat.id, msg.message_id)
    except Exception as e:
        await msg.answer(f"⚠️ ارسال به کاربر ناموفق بود: {e}"); return
    if order["status"] != "confirmed":
        update_order_status(order_id, "confirmed")
        await maybe_pay_referral_bonus(uid)
    await msg.answer(f"✅ کانفیگ سفارش #{order_id} ارسال شد.")


# ── Receipt photo ─────────────────────────────────────────────────

@dp.message_handler(content_types=["photo"])
async def receipt(msg: types.Message):
    upsert_user(msg.from_user)
    state    = user_state.get(msg.from_user.id, {})
    awaiting = state.get("awaiting")
    uname = f"@{msg.from_user.username}" if msg.from_user.username else "—"

    if awaiting == "receipt_order":
        plan_gb  = state["plan"]
        price    = state["price"]
        order_id = create_order(msg.from_user.id, plan_gb, price, "pending", "card")
        user_state.pop(msg.from_user.id, None)
        kb = InlineKeyboardMarkup().add(
            InlineKeyboardButton("✅ تایید", callback_data=f"confirm_order_{order_id}"),
            InlineKeyboardButton("❌ رد",    callback_data=f"reject_order_{order_id}"),
        )
        await bot.forward_message(ADMIN_ID, msg.chat.id, msg.message_id)
        await bot.send_message(
            ADMIN_ID,
            f"📥 رسید سفارش جدید\n🧾 #{order_id}\n"
            f"👤 {msg.from_user.first_name or ''} ({uname})\n"
            f"🆔 {msg.from_user.id}\n📦 {plan_gb} گیگ • {fp(price)} ت",
            reply_markup=kb,
        )
        await msg.answer("📨 رسیدت ارسال شد، منتظر تایید باش.")
        return

    if awaiting == "receipt_charge":
        amount    = state["amount"]
        charge_id = create_charge(msg.from_user.id, amount)
        user_state.pop(msg.from_user.id, None)
        kb = InlineKeyboardMarkup().add(
            InlineKeyboardButton("✅ تایید شارژ", callback_data=f"confirm_charge_{charge_id}"),
            InlineKeyboardButton("❌ رد",          callback_data=f"reject_charge_{charge_id}"),
        )
        await bot.forward_message(ADMIN_ID, msg.chat.id, msg.message_id)
        await bot.send_message(
            ADMIN_ID,
            f"💰 رسید شارژ کیف پول\n🧾 #{charge_id}\n"
            f"👤 {msg.from_user.first_name or ''} ({uname})\n"
            f"🆔 {msg.from_user.id}\n➕ {fp(amount)} تومان",
            reply_markup=kb,
        )
        await msg.answer("📨 رسید شارژ ارسال شد، منتظر تایید باش.")
        return

    await msg.answer("⚠️ اول از منو یه پلن انتخاب کن یا شارژ کیف پول رو بزن، بعد رسید بفرست.")


# ── Custom charge amount ──────────────────────────────────────────

@dp.message_handler(lambda m: user_state.get(m.from_user.id, {}).get("awaiting") == "custom_charge")
async def custom_charge_amount(msg: types.Message):
    raw = (msg.text or "").strip().replace(",", "").replace("،", "")
    if not raw.isdigit():
        await msg.answer("⚠️ فقط عدد بفرستید. مثلاً: 15000"); return
    amount = int(raw)
    if amount < 1000:
        await msg.answer("⚠️ حداقل مبلغ شارژ ۱۰۰۰ تومانه."); return
    await _start_charge_payment(msg.from_user.id, msg.chat.id, amount)


# ── VIP ───────────────────────────────────────────────────────────

@dp.callback_query_handler(lambda c: c.data == "vip")
async def cb_vip(call: types.CallbackQuery):
    await call.message.edit_text(
        "💎 اشتراک‌های VIP\n\n📦 هر گیگ: ۵۹۰ تومان\n\n👇 پلن مورد نظر را انتخاب کنید:",
        reply_markup=vip_menu(),
    )


@dp.callback_query_handler(lambda c: c.data.startswith("plan_"))
async def cb_plan(call: types.CallbackQuery):
    size  = int(call.data.split("_")[1])
    price = size * PRICE_PER_GB
    bal   = get_balance(call.from_user.id)
    user_state[call.from_user.id] = {"plan": size, "price": price}
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("💳 پرداخت با کارت", callback_data="pay_card"))
    if bal >= price:
        kb.add(InlineKeyboardButton(
            f"💼 پرداخت از کیف پول ({fp(bal)} ت)", callback_data="pay_wallet"
        ))
    else:
        kb.add(InlineKeyboardButton(
            f"💼 کیف پول: {fp(bal)} ت — ناکافی، شارژ", callback_data="charge"
        ))
    kb.add(InlineKeyboardButton("🔙 برگشت", callback_data="vip"))
    await call.message.edit_text(
        f"💳 پلن VIP {size} گیگ\n\n💰 مبلغ: {fp(price)} تومان\n\n👇 روش پرداخت رو انتخاب کنید:",
        reply_markup=kb,
    )


@dp.callback_query_handler(lambda c: c.data == "pay_card")
async def cb_pay_card(call: types.CallbackQuery):
    state = user_state.get(call.from_user.id, {})
    size  = state.get("plan")
    if not size:
        await call.answer("اول پلن رو انتخاب کنید", show_alert=True); return
    price = size * PRICE_PER_GB
    user_state[call.from_user.id] = {"awaiting": "receipt_order", "plan": size, "price": price}
    await call.message.edit_text(
        f"📩 پرداخت با کارت — پلن {size} گیگ\n💰 مبلغ: {fp(price)} تومان\n\n"
        f"شماره کارت در پیام بعدی فرستاده میشه. بعد از واریز رسید بفرستید.",
        reply_markup=back_only(),
    )
    await send_card_info(call.message.chat.id, price, f"{size} گیگ")


@dp.callback_query_handler(lambda c: c.data == "pay_wallet")
async def cb_pay_wallet(call: types.CallbackQuery):
    state = user_state.get(call.from_user.id, {})
    size  = state.get("plan")
    if not size:
        await call.answer("اول پلن رو انتخاب کنید", show_alert=True); return
    price = size * PRICE_PER_GB
    if get_balance(call.from_user.id) < price:
        await call.answer("موجودی کافی نیست", show_alert=True); return
    adjust_balance(call.from_user.id, -price)
    order_id = create_order(call.from_user.id, size, price, "paid_wallet", "wallet")
    user_state.pop(call.from_user.id, None)
    uname = f"@{call.from_user.username}" if call.from_user.username else "—"
    await call.message.edit_text(
        f"✅ پرداخت از کیف پول\n🧾 سفارش #{order_id} • {size} گیگ\n"
        f"💰 کسر: {fp(price)} ت — موجودی جدید: {fp(get_balance(call.from_user.id))} ت\n\n"
        f"⏳ کانفیگ به‌زودی ارسال میشه.",
        reply_markup=back_only(),
    )
    await bot.send_message(
        ADMIN_ID,
        f"💼 سفارش از کیف پول\n🧾 #{order_id}\n"
        f"👤 {call.from_user.first_name or ''} ({uname})\n🆔 {call.from_user.id}\n"
        f"📦 {size} گیگ • {fp(price)} ت",
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("📤 ارسال کانفیگ", callback_data=f"sendcfg_{order_id}")
        ),
    )


# ── Wallet ────────────────────────────────────────────────────────

@dp.callback_query_handler(lambda c: c.data == "wallet")
async def cb_wallet(call: types.CallbackQuery):
    await call.message.edit_text(
        f"💰 کیف پول شما\n\nموجودی فعلی: {fp(get_balance(call.from_user.id))} تومان",
        reply_markup=wallet_menu(),
    )


@dp.callback_query_handler(lambda c: c.data == "charge")
async def cb_charge(call: types.CallbackQuery):
    await call.message.edit_text("➕ شارژ کیف پول\n\nمبلغ شارژ رو انتخاب کنید:", reply_markup=charge_menu())


@dp.callback_query_handler(lambda c: c.data.startswith("charge_"))
async def cb_charge_amount(call: types.CallbackQuery):
    payload = call.data.split("_", 1)[1]
    if payload == "custom":
        user_state[call.from_user.id] = {"awaiting": "custom_charge"}
        await call.message.edit_text(
            "✏️ مبلغ دلخواه رو به تومان بفرستید (فقط عدد، حداقل ۱۰۰۰):", reply_markup=back_only()
        ); return
    try: amount = int(payload)
    except ValueError:
        await call.answer(); return
    await _start_charge_payment(call.from_user.id, call.message.chat.id, amount, edit_msg=call.message)


async def _start_charge_payment(user_id, chat_id, amount, edit_msg=None):
    user_state[user_id] = {"awaiting": "receipt_charge", "amount": amount}
    text = f"➕ شارژ کیف پول — {fp(amount)} تومان\n\nشماره کارت در پیام بعدی فرستاده میشه. بعد از واریز رسید بفرستید."
    if edit_msg:
        await edit_msg.edit_text(text, reply_markup=back_only())
    else:
        await bot.send_message(chat_id, text, reply_markup=back_only())
    await send_card_info(chat_id, amount, "شارژ کیف پول")


# ── Admin confirm/reject orders ───────────────────────────────────

@dp.callback_query_handler(lambda c: c.data.startswith("confirm_order_"))
async def cb_confirm_order(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔️", show_alert=True); return
    order_id = int(call.data.rsplit("_", 1)[1])
    if not get_order(order_id):
        await call.answer("سفارش پیدا نشد"); return
    admin_state["awaiting_config_for"] = order_id
    try: await call.message.edit_reply_markup(reply_markup=None)
    except Exception: pass
    await bot.send_message(ADMIN_ID, f"📤 سفارش #{order_id} تایید شد.\nحالا کانفیگ رو همینجا بفرستید.")
    await call.answer("منتظر کانفیگ هستم")


@dp.callback_query_handler(lambda c: c.data.startswith("reject_order_"))
async def cb_reject_order(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔️", show_alert=True); return
    order_id = int(call.data.rsplit("_", 1)[1])
    order = get_order(order_id)
    if not order:
        await call.answer("سفارش پیدا نشد"); return
    update_order_status(order_id, "rejected")
    await bot.send_message(order["user_id"], f"❌ پرداخت سفارش #{order_id} تایید نشد.")
    try: await call.message.edit_reply_markup(reply_markup=None)
    except Exception: pass
    await call.answer("رد شد")


@dp.callback_query_handler(lambda c: c.data.startswith("sendcfg_"))
async def cb_sendcfg(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔️", show_alert=True); return
    order_id = int(call.data.split("_")[1])
    if not get_order(order_id):
        await call.answer("سفارش پیدا نشد"); return
    admin_state["awaiting_config_for"] = order_id
    await bot.send_message(ADMIN_ID, f"📤 سفارش #{order_id}: کانفیگ رو بفرستید.")
    await call.answer("منتظر کانفیگ هستم")


# ── Admin confirm/reject charges ──────────────────────────────────

@dp.callback_query_handler(lambda c: c.data.startswith("confirm_charge_"))
async def cb_confirm_charge(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔️", show_alert=True); return
    charge_id = int(call.data.rsplit("_", 1)[1])
    ch = get_charge(charge_id)
    if not ch:
        await call.answer("شارژ پیدا نشد"); return
    if ch["status"] == "confirmed":
        await call.answer("قبلاً تایید شده"); return
    update_charge_status(charge_id, "confirmed")
    adjust_balance(ch["user_id"], int(ch["amount"]))
    try: await call.message.edit_reply_markup(reply_markup=None)
    except Exception: pass
    await bot.send_message(
        ch["user_id"],
        f"✅ شارژ #{charge_id} — {fp(ch['amount'])} تومان تایید شد.\n"
        f"💼 موجودی جدید: {fp(get_balance(ch['user_id']))} تومان"
    )
    await call.answer("شارژ تایید شد")


@dp.callback_query_handler(lambda c: c.data.startswith("reject_charge_"))
async def cb_reject_charge(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔️", show_alert=True); return
    charge_id = int(call.data.rsplit("_", 1)[1])
    ch = get_charge(charge_id)
    if not ch:
        await call.answer("شارژ پیدا نشد"); return
    update_charge_status(charge_id, "rejected")
    try: await call.message.edit_reply_markup(reply_markup=None)
    except Exception: pass
    await bot.send_message(ch["user_id"], f"❌ شارژ #{charge_id} تایید نشد.")
    await call.answer("رد شد")


# ── Admin panel callbacks ─────────────────────────────────────────

@dp.callback_query_handler(lambda c: c.data == "adm_menu")
async def cb_adm_menu(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔️", show_alert=True); return
    admin_state.clear()
    try:
        await call.message.edit_text(
            "🛠 پنل مدیریت LUNA SERVER\n\nیک گزینه را انتخاب کنید:", reply_markup=admin_menu_kb()
        )
    except Exception:
        await bot.send_message(
            ADMIN_ID, "🛠 پنل مدیریت LUNA SERVER\n\nیک گزینه را انتخاب کنید:", reply_markup=admin_menu_kb()
        )
    await call.answer()


@dp.callback_query_handler(lambda c: c.data == "adm_close")
async def cb_adm_close(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔️", show_alert=True); return
    admin_state.clear()
    try: await call.message.delete()
    except Exception: pass
    await call.answer("بسته شد")


@dp.callback_query_handler(lambda c: c.data == "adm_stats")
async def cb_adm_stats(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔️", show_alert=True); return
    u, agg, wt = admin_stats()
    await call.message.edit_text(
        f"📊 آمار کلی\n\n👥 کاربرها: {u}\n✅ تایید‌شده: {agg['confirmed_count']}\n"
        f"⏳ در انتظار: {agg['pending_count']}\n❌ رد‌شده: {agg['rejected_count']}\n"
        f"📦 کل گیگ: {agg['total_gb']}G\n💰 کل درآمد: {fp(agg['revenue'])} ت\n"
        f"💼 جمع کیف پول‌ها: {fp(wt)} ت",
        reply_markup=adm_back(),
    )
    await call.answer()


@dp.callback_query_handler(lambda c: c.data == "adm_orders")
async def cb_adm_orders(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔️", show_alert=True); return
    orders = list_pending_orders()
    if not orders:
        await call.message.edit_text("✅ هیچ سفارش در انتظاری نیست.", reply_markup=adm_back())
        await call.answer(); return
    lines = ["⏳ سفارش‌های در انتظار:\n"]
    kb    = InlineKeyboardMarkup(row_width=2)
    for o in orders:
        lines.append(
            f"#{o['id']} • {o['plan_gb']}G • {fp(o['price'])} ت • "
            f"{PAY_FA.get(o['payment_method'],'')} • 🆔{o['user_id']} • {STATUS_FA.get(o['status'],'')}"
        )
        if o["status"] == "paid_wallet":
            kb.add(InlineKeyboardButton(f"📤 کانفیگ #{o['id']}", callback_data=f"sendcfg_{o['id']}"))
        else:
            kb.row(
                InlineKeyboardButton(f"✅ تایید #{o['id']}", callback_data=f"confirm_order_{o['id']}"),
                InlineKeyboardButton(f"❌ رد #{o['id']}",    callback_data=f"reject_order_{o['id']}"),
            )
    kb.add(InlineKeyboardButton("🔙 برگشت", callback_data="adm_menu"))
    await call.message.edit_text("\n".join(lines), reply_markup=kb)
    await call.answer()


@dp.callback_query_handler(lambda c: c.data == "adm_charges")
async def cb_adm_charges(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔️", show_alert=True); return
    charges = list_pending_charges()
    if not charges:
        await call.message.edit_text("✅ هیچ شارژ در انتظاری نیست.", reply_markup=adm_back())
        await call.answer(); return
    lines = ["💰 شارژ‌های در انتظار:\n"]
    kb    = InlineKeyboardMarkup(row_width=2)
    for ch in charges:
        lines.append(f"#{ch['id']} • {fp(ch['amount'])} ت • 🆔{ch['user_id']}")
        kb.row(
            InlineKeyboardButton(f"✅ تایید #{ch['id']}", callback_data=f"confirm_charge_{ch['id']}"),
            InlineKeyboardButton(f"❌ رد #{ch['id']}",    callback_data=f"reject_charge_{ch['id']}"),
        )
    kb.add(InlineKeyboardButton("🔙 برگشت", callback_data="adm_menu"))
    await call.message.edit_text("\n".join(lines), reply_markup=kb)
    await call.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("adm_users:"))
async def cb_adm_users(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔️", show_alert=True); return
    offset = int(call.data.split(":")[1])
    rows, total = list_users_page(offset=offset)
    if not rows:
        await call.message.edit_text("هیچ کاربری نیست.", reply_markup=adm_back())
        await call.answer(); return
    lines = [f"👥 کاربران ({offset+1}–{offset+len(rows)} از {total}):\n"]
    for u in rows:
        uname = f"@{u['username']}" if u["username"] else "—"
        lines.append(f"🆔 `{u['user_id']}` • {u['first_name'] or '—'} ({uname}) • 💰 {fp(int(u['balance']))} ت")
    kb  = InlineKeyboardMarkup(row_width=2)
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton("⬅️ قبلی", callback_data=f"adm_users:{max(0,offset-ADMIN_USERS_PAGE)}"))
    if offset + len(rows) < total:
        nav.append(InlineKeyboardButton("بعدی ➡️", callback_data=f"adm_users:{offset+ADMIN_USERS_PAGE}"))
    if nav: kb.row(*nav)
    kb.add(InlineKeyboardButton("🔙 برگشت", callback_data="adm_menu"))
    await call.message.edit_text("\n".join(lines), parse_mode="Markdown", reply_markup=kb)
    await call.answer()


@dp.callback_query_handler(lambda c: c.data == "adm_search")
async def cb_adm_search(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔️", show_alert=True); return
    admin_state["awaiting_user_lookup"] = True
    await call.message.edit_text("🔍 آیدی عددی یا نام کاربری رو بفرستید:", reply_markup=adm_back())
    await call.answer()


@dp.callback_query_handler(
    lambda c: c.data.startswith("adm_baladd:") or c.data.startswith("adm_balsub:")
)
async def cb_adm_balance(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔️", show_alert=True); return
    action, uid = call.data.split(":")
    uid  = int(uid)
    sign = 1 if action == "adm_baladd" else -1
    admin_state["awaiting_balance"] = {"user_id": uid, "sign": sign}
    label = "افزایش" if sign > 0 else "کاهش"
    await call.message.edit_text(
        f"✏️ {label} موجودی کاربر `{uid}`\n\nمبلغ (تومان) رو بفرستید:",
        parse_mode="Markdown", reply_markup=adm_back(),
    )
    await call.answer()


@dp.callback_query_handler(lambda c: c.data == "adm_bcast")
async def cb_adm_bcast(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔️", show_alert=True); return
    admin_state["awaiting_broadcast"] = True
    await call.message.edit_text(
        f"📢 پیام همگانی\n\nپیام رو بفرستید — برای {len(all_user_ids())} کاربر ارسال میشه.\n"
        f"⚠️ قابل لغو نیست. برای لغو برگشت بزنید.",
        parse_mode="Markdown", reply_markup=adm_back(),
    )
    await call.answer()


# ── General nav ───────────────────────────────────────────────────

@dp.callback_query_handler(lambda c: c.data == "account")
async def cb_account(call: types.CallbackQuery):
    upsert_user(call.from_user)
    await call.message.edit_text(account_text(call.from_user), parse_mode="Markdown", reply_markup=back_only())


@dp.callback_query_handler(lambda c: c.data == "invite")
async def cb_invite(call: types.CallbackQuery):
    me   = (await bot.get_me()).username
    link = f"https://t.me/{me}?start=ref{call.from_user.id}"
    refs = referral_count(call.from_user.id)
    await call.message.edit_text(
        f"👥 سیستم دعوت از دوستان\n\n"
        f"🔗 لینک اختصاصی شما:\n`{link}`\n\n"
        f"🎁 با اولین خرید موفق هر دوست، {fp(REFERRAL_BONUS_TOMAN)} تومان به کیف پول شما اضافه میشه.\n\n"
        f"👥 دوستان دعوت‌شده تا الان: {refs}",
        parse_mode="Markdown", reply_markup=back_only(),
    )


@dp.callback_query_handler(lambda c: c.data == "support")
async def cb_support(call: types.CallbackQuery):
    await call.message.edit_text(
        "📞 پشتیبانی",
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("📩 پیام به پشتیبانی", url="https://t.me/Luna_1_support"),
            InlineKeyboardButton("🔙 برگشت", callback_data="back"),
        ),
    )


@dp.callback_query_handler(lambda c: c.data == "back")
async def cb_back(call: types.CallbackQuery):
    user_state.pop(call.from_user.id, None)
    await call.message.edit_text("🌙 LUNA SERVER\n\n👇 انتخاب کنید:", reply_markup=main_menu())


# ══════════════════════════════ MAIN ═════════════════════════════

if __name__ == "__main__":
    init_db()
    executor.start_polling(dp, skip_updates=True)
