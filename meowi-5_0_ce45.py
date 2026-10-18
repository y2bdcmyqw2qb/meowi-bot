#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MEOWI | میویی — ربات بازی و مدیریت برای Termux
این فایل هسته محلی را کامل اجرا می‌کند. اتصال زنده روبیکا به یک روش/کتابخانه
معتبر و سازگار نیاز دارد؛ این نسخه عمداً API یا endpoint ساختگی ندارد.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import shutil
import sqlite3
import sys
import time
import traceback
import types
from datetime import datetime, date
from pathlib import Path
from typing import Optional

APP_NAME = "MEOWI | میویی 🐱"
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("MEOWI_DB", str(BASE_DIR / "meowi.db")))
LOG_PATH = Path(os.getenv("MEOWI_LOG", str(BASE_DIR / "meowi.log")))
BACKUP_DIR = BASE_DIR / "meowi_backups"
CONFIG_PATH = BASE_DIR / "meowi_config.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("meowi")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def today_key() -> str:
    return date.today().isoformat()


def connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_column(db: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    """Add a missing column without deleting existing user data."""
    columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db() -> None:
    with connect_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            user_id TEXT PRIMARY KEY, display_name TEXT NOT NULL DEFAULT '',
            joined_at TEXT NOT NULL, last_seen TEXT NOT NULL,
            blocked INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS admins(
            user_id TEXT PRIMARY KEY, role TEXT NOT NULL DEFAULT 'admin',
            permissions TEXT NOT NULL DEFAULT '[]', added_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS groups(
            chat_id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1, welcome_text TEXT NOT NULL DEFAULT '',
            rules_text TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS required_channels(
            channel_id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '',
            link TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS wallets(
            user_id TEXT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
            balance INTEGER NOT NULL DEFAULT 0 CHECK(balance >= 0)
        );
        CREATE TABLE IF NOT EXISTS transactions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
            amount INTEGER NOT NULL, kind TEXT NOT NULL, reason TEXT NOT NULL,
            created_at TEXT NOT NULL, actor_id TEXT NOT NULL DEFAULT 'system'
        );
        CREATE TABLE IF NOT EXISTS daily_rewards(
            user_id TEXT NOT NULL, reward_date TEXT NOT NULL, amount INTEGER NOT NULL,
            PRIMARY KEY(user_id, reward_date)
        );
        CREATE TABLE IF NOT EXISTS shop_items(
            item_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '', price INTEGER NOT NULL CHECK(price >= 0),
            stock INTEGER NOT NULL DEFAULT -1, enabled INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS purchases(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
            item_id INTEGER NOT NULL, price INTEGER NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS transfers(
            transfer_id TEXT PRIMARY KEY, sender_id TEXT NOT NULL, receiver_id TEXT NOT NULL,
            amount INTEGER NOT NULL CHECK(amount > 0), fee INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS dice_bets(
            game_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, chat_id TEXT NOT NULL,
            stake INTEGER NOT NULL CHECK(stake > 0), roll INTEGER NOT NULL,
            won INTEGER NOT NULL, payout INTEGER NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bot_settings(
            key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS connection_logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT, status TEXT NOT NULL,
            message TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS admin_logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id TEXT NOT NULL,
            action TEXT NOT NULL, details TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reports(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
            category TEXT NOT NULL, details TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS game_sessions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL,
            game_type TEXT NOT NULL, state_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS game_players(
            session_id INTEGER NOT NULL REFERENCES game_sessions(id) ON DELETE CASCADE,
            user_id TEXT NOT NULL, display_name TEXT NOT NULL,
            joined_at TEXT NOT NULL, score INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(session_id, user_id)
        );
        """)
        # Safe migrations for databases created by older MEOWI versions.
        # SQLite CREATE TABLE IF NOT EXISTS does not add new columns to old tables.
        _ensure_column(db, "users", "display_name", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(db, "users", "joined_at", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(db, "users", "last_seen", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(db, "users", "blocked", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(db, "users", "note", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(db, "groups", "title", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(db, "groups", "enabled", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(db, "groups", "welcome_text", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(db, "groups", "rules_text", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(db, "groups", "created_at", "TEXT NOT NULL DEFAULT ''")
        db.execute("CREATE INDEX IF NOT EXISTS idx_transactions_user ON transactions(user_id, id DESC)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_users_last_seen ON users(last_seen)")
        db.execute("UPDATE users SET joined_at=? WHERE joined_at=''", (now_iso(),))
        db.execute("UPDATE users SET last_seen=? WHERE last_seen=''", (now_iso(),))
        db.execute("INSERT OR IGNORE INTO bot_settings(key,value,updated_at) VALUES('bot_name',?,?)", (APP_NAME, now_iso()))
        db.execute("INSERT OR IGNORE INTO bot_settings(key,value,updated_at) VALUES('daily_reward','100',?)", (now_iso(),))
        db.execute("INSERT OR IGNORE INTO bot_settings(key,value,updated_at) VALUES('currency_name','الماس 💎',?)", (now_iso(),))
        for key, value in {
            "transfer_min": "1", "transfer_max": "100000", "transfer_fee_percent": "0",
            "dice_enabled": "1", "dice_min_bet": "1", "dice_max_bet": "10000",
            "dice_win_multiplier": "2", "dice_group_enabled": "1", "dice_private_enabled": "1"
        }.items():
            db.execute("INSERT OR IGNORE INTO bot_settings(key,value,updated_at) VALUES(?,?,?)", (key, value, now_iso()))
        db.execute("INSERT OR IGNORE INTO bot_settings(key,value,updated_at) VALUES('forced_join_enabled','0',?)", (now_iso(),))
        db.execute("INSERT OR IGNORE INTO bot_settings(key,value,updated_at) VALUES('welcome_text','سلام {name} جان! به گروه خوش اومدی 🐱',?)", (now_iso(),))
        db.execute("INSERT OR IGNORE INTO shop_items(name,description,price,stock) VALUES('🎁 جعبه معمولی','یک جعبه سرگرمی',50,-1)")
        db.execute("INSERT OR IGNORE INTO shop_items(name,description,price,stock) VALUES('✨ لقب ویژه','آیتم نمایشی',250,-1)")


def setting(key: str, default: str = "") -> str:
    with connect_db() as db:
        row = db.execute("SELECT value FROM bot_settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with connect_db() as db:
        db.execute("""INSERT INTO bot_settings(key,value,updated_at) VALUES(?,?,?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
                   (key, value, now_iso()))


def ensure_user(user_id: str, name: str = "") -> None:
    user_id = str(user_id).strip()
    if not user_id:
        raise ValueError("شناسه کاربر خالی است")
    with connect_db() as db:
        db.execute("""INSERT INTO users(user_id,display_name,joined_at,last_seen)
            VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
            display_name=CASE WHEN excluded.display_name='' THEN users.display_name ELSE excluded.display_name END,
            last_seen=excluded.last_seen""", (user_id, name[:100], now_iso(), now_iso()))
        db.execute("INSERT OR IGNORE INTO wallets(user_id,balance) VALUES(?,0)", (user_id,))


def is_owner(user_id: str) -> bool:
    owner = os.getenv("MEOWI_OWNER_ID", "").strip()
    return bool(owner) and str(user_id) == owner


def admin_role(user_id: str) -> Optional[str]:
    if is_owner(user_id):
        return "owner"
    with connect_db() as db:
        row = db.execute("SELECT role FROM admins WHERE user_id=?", (str(user_id),)).fetchone()
        return row["role"] if row else None


def is_admin(user_id: str) -> bool:
    return admin_role(user_id) is not None


def admin_log(actor: str, action: str, details: str = "") -> None:
    with connect_db() as db:
        db.execute("INSERT INTO admin_logs(actor_id,action,details,created_at) VALUES(?,?,?,?)",
                   (str(actor), action, details[:1000], now_iso()))


def change_balance(user_id: str, amount: int, kind: str, reason: str, actor: str = "system") -> int:
    ensure_user(user_id)
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT balance FROM wallets WHERE user_id=?", (str(user_id),)).fetchone()
        new_balance = int(row["balance"]) + int(amount)
        if new_balance < 0:
            raise ValueError("موجودی کافی نیست.")
        db.execute("UPDATE wallets SET balance=? WHERE user_id=?", (new_balance, str(user_id)))
        db.execute("INSERT INTO transactions(user_id,amount,kind,reason,created_at,actor_id) VALUES(?,?,?,?,?,?)",
                   (str(user_id), int(amount), kind, reason[:300], now_iso(), str(actor)))
        return new_balance


def balance_of(user_id: str) -> int:
    ensure_user(user_id)
    with connect_db() as db:
        row = db.execute("SELECT balance FROM wallets WHERE user_id=?", (str(user_id),)).fetchone()
        return int(row["balance"])


def daily_reward(user_id: str) -> str:
    ensure_user(user_id)
    amount = max(0, int(setting("daily_reward", "100")))
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("INSERT INTO daily_rewards(user_id,reward_date,amount) VALUES(?,?,?)",
                       (str(user_id), today_key(), amount))
        except sqlite3.IntegrityError:
            return "⏳ امروز جایزه‌ات رو گرفتی! فردا دوباره بیا 🐱"
        row = db.execute("SELECT balance FROM wallets WHERE user_id=?", (str(user_id),)).fetchone()
        new_balance = int(row["balance"]) + amount
        db.execute("UPDATE wallets SET balance=? WHERE user_id=?", (new_balance, str(user_id)))
        db.execute("INSERT INTO transactions(user_id,amount,kind,reason,created_at) VALUES(?,?,?,?,?)",
                   (str(user_id), amount, "daily_reward", "جایزه روزانه", now_iso()))
        return f"🎁 جایزه روزانه: {amount} {setting('currency_name','سکه')}\n💰 موجودی: {new_balance}"


def main_menu() -> str:
    return (
        "🐱 **MEOWI | میویی**\n"
        "━━━━━━━━━━━━━━\n"
        "🎮 بازی‌ها | /games\n"
        "🎁 جایزه روزانه | /daily\n"
        "💰 کیف پول | /wallet\n"
        "🛒 فروشگاه | /shop\n"
        "🎴 کارت‌ها | /cards\n"
        "🏆 رتبه‌بندی | /top\n"
        "👤 حساب کاربری | /profile\n"
        "🎯 مأموریت‌ها | /missions\n"
        "🎉 سرگرمی | /fun\n"
        "📢 اطلاعیه‌ها | /news\n"
        "🆘 راهنما | /help\n"
        "📞 پشتیبانی | /support\n"
        "━━━━━━━━━━━━━━\n"
        "گروه: /mafia /join /leave /players /guess /roll /coin"
    )


def profile(user_id: str, name: str) -> str:
    ensure_user(user_id, name)
    with connect_db() as db:
        u = db.execute("SELECT * FROM users WHERE user_id=?", (str(user_id),)).fetchone()
    return f"👤 پروفایل {u['display_name'] or user_id}\n🗓 عضویت: {u['joined_at'][:10]}\n💰 موجودی: {balance_of(user_id)} {setting('currency_name','سکه')}\n🛡 وضعیت: {'مسدود' if u['blocked'] else 'فعال'}"


def top_users() -> str:
    with connect_db() as db:
        rows = db.execute("""SELECT u.display_name,u.user_id,w.balance FROM wallets w
        JOIN users u ON u.user_id=w.user_id ORDER BY w.balance DESC LIMIT 10""").fetchall()
    if not rows:
        return "🏆 هنوز رتبه‌بندی‌ای وجود نداره."
    return "🏆 رتبه‌بندی MEOWI\n" + "\n".join(
        f"{i}. {r['display_name'] or r['user_id']} — {r['balance']} سکه" for i, r in enumerate(rows, 1)
    )


def shop_text() -> str:
    with connect_db() as db:
        rows = db.execute("SELECT * FROM shop_items WHERE enabled=1 ORDER BY item_id").fetchall()
    if not rows:
        return "🛒 فروشگاه فعلاً خالیه."
    return "🛒 فروشگاه\n" + "\n".join(
        f"{r['item_id']}. {r['name']} — {r['price']} سکه\n{r['description']}" for r in rows
    ) + "\nبرای خرید: /buy شماره_آیتم"


def buy_item(user_id: str, item_id: int) -> str:
    ensure_user(user_id)
    result = ""
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        item = db.execute("SELECT * FROM shop_items WHERE item_id=? AND enabled=1", (int(item_id),)).fetchone()
        if not item:
            return "❌ این آیتم وجود نداره."
        wallet = db.execute("SELECT balance FROM wallets WHERE user_id=?", (str(user_id),)).fetchone()
        if int(wallet["balance"]) < int(item["price"]):
            return "❌ موجودی کافی نیست."
        if int(item["stock"]) == 0:
            return "📦 این آیتم تموم شده."
        db.execute("UPDATE wallets SET balance=balance-? WHERE user_id=?", (item["price"], str(user_id)))
        if int(item["stock"]) > 0:
            db.execute("UPDATE shop_items SET stock=stock-1 WHERE item_id=?", (int(item_id),))
        db.execute("INSERT INTO purchases(user_id,item_id,price,created_at) VALUES(?,?,?,?)",
                   (str(user_id), int(item_id), int(item["price"]), now_iso()))
        db.execute("INSERT INTO transactions(user_id,amount,kind,reason,created_at) VALUES(?,?,?,?,?)",
                   (str(user_id), -int(item["price"]), "purchase", f"خرید {item['name']}", now_iso()))
        new_balance = int(wallet["balance"]) - int(item["price"])
        result = f"✅ خرید انجام شد: {item['name']}\n💰 موجودی جدید: {new_balance}"
    return result



def new_game(chat_id: str, game_type: str, creator_id: str, creator_name: str) -> str:
    with connect_db() as db:
        existing = db.execute("SELECT id FROM game_sessions WHERE chat_id=? AND status='active'", (str(chat_id),)).fetchone()
        if existing:
            return "🎮 در این گروه یک بازی فعال وجود داره. برای دیدن بازیکنان /players رو بزن."
        state = {"phase": "lobby", "secret": None, "turn": 0, "round": 0, "history": []}
        cur = db.execute("""INSERT INTO game_sessions(chat_id,game_type,state_json,status,created_at,updated_at)
            VALUES(?,?,?,'active',?,?)""", (str(chat_id), game_type, json.dumps(state), now_iso(), now_iso()))
        sid = cur.lastrowid
        db.execute("INSERT INTO game_players(session_id,user_id,display_name,joined_at) VALUES(?,?,?,?)",
                   (sid, str(creator_id), creator_name or str(creator_id), now_iso()))
    if game_type == "mafia":
        return "🕵️ **لابی مافیای میویی ساخته شد!**\nبازیکن‌ها با /join وارد بشن.\nحداقل ۵ نفر پیشنهاد می‌شه؛ شروع با /startgame و پایان با /endgame."
    return "🎮 بازی گروهی ساخته شد! با /join وارد بشید."


def active_game(chat_id: str):
    with connect_db() as db:
        return db.execute("SELECT * FROM game_sessions WHERE chat_id=? AND status='active' ORDER BY id DESC LIMIT 1",
                          (str(chat_id),)).fetchone()


def join_game(chat_id: str, user_id: str, name: str) -> str:
    ensure_user(user_id, name)
    game = active_game(chat_id)
    if not game:
        return "❌ بازی فعالی نیست. برای ساخت بازی مافیا /mafia رو بزن."
    with connect_db() as db:
        try:
            db.execute("INSERT INTO game_players(session_id,user_id,display_name,joined_at) VALUES(?,?,?,?)",
                       (game["id"], str(user_id), name or str(user_id), now_iso()))
        except sqlite3.IntegrityError:
            return "🙂 تو از قبل توی این بازی هستی."
        count = db.execute("SELECT COUNT(*) AS n FROM game_players WHERE session_id=?", (game["id"],)).fetchone()["n"]
    return f"✅ وارد بازی شدی، {name or 'بازیکن'}!\n👥 تعداد بازیکنان: {count}"


def players_text(chat_id: str) -> str:
    game = active_game(chat_id)
    if not game:
        return "❌ بازی فعالی وجود نداره."
    with connect_db() as db:
        rows = db.execute("SELECT display_name FROM game_players WHERE session_id=? ORDER BY joined_at",
                          (game["id"],)).fetchall()
    return f"👥 بازیکنان ({len(rows)})\n" + "\n".join(f"• {r['display_name']}" for r in rows)


def leave_game(chat_id: str, user_id: str) -> str:
    game = active_game(chat_id)
    if not game:
        return "❌ بازی فعالی نیست."
    with connect_db() as db:
        cur = db.execute("DELETE FROM game_players WHERE session_id=? AND user_id=?", (game["id"], str(user_id)))
        count = db.execute("SELECT COUNT(*) AS n FROM game_players WHERE session_id=?", (game["id"],)).fetchone()["n"]
    return "🚪 از بازی خارج شدی." if cur.rowcount else "تو داخل این بازی نیستی."


def start_game(chat_id: str) -> str:
    game = active_game(chat_id)
    if not game:
        return "❌ اول /mafia رو بزن."
    with connect_db() as db:
        players = db.execute("SELECT user_id,display_name FROM game_players WHERE session_id=? ORDER BY joined_at",
                             (game["id"],)).fetchall()
        if game["game_type"] == "mafia":
            if len(players) < 5:
                return f"⚠️ برای شروع مافیا حداقل ۵ بازیکن لازمه؛ فعلاً {len(players)} نفر هستید."
            mafia_count = max(1, len(players)//4)
            roles = ["مافیا"] * mafia_count + ["شهروند"] * (len(players)-mafia_count)
            random.shuffle(roles)
            mapping = {p["user_id"]: roles[i] for i, p in enumerate(players)}
            state = json.loads(game["state_json"])
            state.update({"phase": "night", "roles": mapping, "alive": list(mapping.keys()),
                          "round": 1, "votes": {}, "night_target": None})
            db.execute("UPDATE game_sessions SET state_json=?,updated_at=? WHERE id=?",
                       (json.dumps(state, ensure_ascii=False), now_iso(), game["id"]))
            names = "\n".join(f"• {p['display_name']}" for p in players)
            # Role secrecy: in this single-file command interface the bot cannot DM roles without a live connector.
            return ("🌙 **شب اول مافیا شروع شد!**\n" + names +
                    "\n\n🔒 نقش‌ها در دیتابیس ثبت شدند، اما ارسال خصوصی نقش‌ها نیازمند اتصال واقعی روبیکا و پشتیبانی پیام خصوصی است. "
                    "تا زمان فعال‌شدن آن، نقش‌ها را در گروه اعلام نمی‌کنم.")
        return "🎮 بازی شروع شد."


def end_game(chat_id: str) -> str:
    game = active_game(chat_id)
    if not game:
        return "❌ بازی فعالی نیست."
    with connect_db() as db:
        db.execute("UPDATE game_sessions SET status='ended',updated_at=? WHERE id=?", (now_iso(), game["id"]))
    return "🏁 بازی پایان یافت."


def transfer_diamonds(sender_id: str, receiver_id: str, amount: int) -> str:
    """Atomic user-to-user transfer using the shared wallet in group and private chat."""
    sender_id, receiver_id = str(sender_id).strip(), str(receiver_id).strip()
    if not sender_id or not receiver_id or len(sender_id) > 128 or len(receiver_id) > 128:
        return "❌ شناسه فرستنده یا گیرنده معتبر نیست."
    if sender_id == receiver_id:
        return "🐱 نمی‌تونی به خودت الماس انتقال بدی!"
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        return "❌ مبلغ باید عدد صحیح باشد. نمونه: /transfer شناسه_کاربر 100"
    if amount <= 0:
        return "❌ مبلغ انتقال باید بیشتر از صفر باشد."
    minimum = max(1, int(setting("transfer_min", "1")))
    maximum = max(minimum, int(setting("transfer_max", "100000")))
    if not minimum <= amount <= maximum:
        return f"⚠️ مبلغ انتقال باید بین {minimum} و {maximum} الماس باشد."
    fee_percent = max(0, min(50, int(setting("transfer_fee_percent", "0"))))
    fee = amount * fee_percent // 100
    ensure_user(sender_id)
    ensure_user(receiver_id)
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        sender = db.execute("SELECT balance FROM wallets WHERE user_id=?", (sender_id,)).fetchone()
        receiver = db.execute("SELECT balance FROM wallets WHERE user_id=?", (receiver_id,)).fetchone()
        if not sender or not receiver:
            db.rollback()
            return "❌ حساب فرستنده یا گیرنده پیدا نشد."
        total = amount + fee
        if int(sender["balance"]) < total:
            db.rollback()
            return "💎 موجودی کافی نیست؛ مبلغ انتقال و کارمزد را بررسی کن."
        sender_after = int(sender["balance"]) - total
        receiver_after = int(receiver["balance"]) + amount
        transfer_id = os.urandom(12).hex()
        db.execute("UPDATE wallets SET balance=? WHERE user_id=?", (sender_after, sender_id))
        db.execute("UPDATE wallets SET balance=? WHERE user_id=?", (receiver_after, receiver_id))
        db.execute("INSERT INTO transfers(transfer_id,sender_id,receiver_id,amount,fee,created_at) VALUES(?,?,?,?,?,?)",
                   (transfer_id, sender_id, receiver_id, amount, fee, now_iso()))
        db.execute("INSERT INTO transactions(user_id,amount,kind,reason,created_at,actor_id) VALUES(?,?,?,?,?,?)",
                   (sender_id, -total, "transfer_out", f"انتقال به {receiver_id}؛ کارمزد {fee}؛ کد {transfer_id}", now_iso(), sender_id))
        db.execute("INSERT INTO transactions(user_id,amount,kind,reason,created_at,actor_id) VALUES(?,?,?,?,?,?)",
                   (receiver_id, amount, "transfer_in", f"دریافت از {sender_id}؛ کد {transfer_id}", now_iso(), sender_id))
    return (f"✅ انتقال انجام شد!\n👤 گیرنده: {receiver_id}\n💎 مبلغ: {amount}\n"
            f"🧾 کارمزد: {fee}\n💰 موجودی جدید شما: {sender_after}\n🔖 کد پیگیری: {transfer_id}")


def dice_bet(user_id: str, chat_id: str, stake: int) -> str:
    """Single-player virtual-diamond dice bet. A six wins; no real-money payout."""
    if setting("dice_enabled", "1").lower() in ("0", "false", "off", "no"):
        return "🎲 شرط‌بندی تاس فعلاً خاموش است."
    try:
        stake = int(stake)
    except (TypeError, ValueError):
        return "❌ مبلغ شرط باید عدد صحیح باشد. نمونه: /bet 100"
    minimum = max(1, int(setting("dice_min_bet", "1")))
    maximum = max(minimum, int(setting("dice_max_bet", "10000")))
    if not minimum <= stake <= maximum:
        return f"⚠️ مبلغ شرط باید بین {minimum} و {maximum} الماس باشد."
    ensure_user(str(user_id))
    roll = random.SystemRandom().randint(1, 6)
    won = roll == 6
    multiplier = max(1, min(10, int(setting("dice_win_multiplier", "2"))))
    payout = stake * multiplier if won else 0
    game_id = os.urandom(12).hex()
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT wallets.balance,users.blocked FROM wallets JOIN users USING(user_id) WHERE user_id=?", (str(user_id),)).fetchone()
        if not row:
            db.rollback()
            return "❌ حساب پیدا نشد. دوباره /start را بزن."
        if int(row["blocked"]):
            db.rollback()
            return "⛔ حساب شما مسدود است."
        if int(row["balance"]) < stake:
            db.rollback()
            return "💎 موجودی کافی برای این شرط نداری."
        after = int(row["balance"]) - stake + payout
        db.execute("UPDATE wallets SET balance=? WHERE user_id=?", (after, str(user_id)))
        db.execute("INSERT INTO dice_bets(game_id,user_id,chat_id,stake,roll,won,payout,created_at) VALUES(?,?,?,?,?,?,?,?)",
                   (game_id, str(user_id), str(chat_id), stake, roll, int(won), payout, now_iso()))
        db.execute("INSERT INTO transactions(user_id,amount,kind,reason,created_at,actor_id) VALUES(?,?,?,?,?,?)",
                   (str(user_id), -stake, "dice_bet", f"شرط تاس؛ نتیجه {roll}؛ بازی {game_id}", now_iso(), str(user_id)))
        if payout:
            db.execute("INSERT INTO transactions(user_id,amount,kind,reason,created_at,actor_id) VALUES(?,?,?,?,?,?)",
                       (str(user_id), payout, "dice_prize", f"جایزه تاس؛ بازی {game_id}", now_iso(), "system"))
    if won:
        result = f"🎉 عدد ۶ آمد! تو بردی و {payout} الماس جایزه گرفتی!"
    else:
        result = f"😿 تاس {roll} آمد؛ این بار باختی."
    return f"🎲 **MEOWI | شرط‌بندی تاس**\n💎 شرط: {stake}\n{result}\n💰 موجودی فعلی: {after}\n🔖 بازی: {game_id}"


def roll_dice() -> str:
    return f"🎲 نتیجه تاس: {random.SystemRandom().randint(1, 6)}"

def coin_flip() -> str:
    return "🪙 نتیجه: " + random.choice(["شیر", "خط"])


def admin_panel() -> str:
    return ("👑 **پنل مدیریت MEOWI**\n"
            "📊 /stats — آمار\n👥 /admins — مدیران\n"
            "➕ /addadmin شناسه — افزودن مدیر (فقط مالک)\n"
            "➖ /deladmin شناسه — حذف مدیر (فقط مالک)\n"
            "💰 /setbalance شناسه مقدار دلیل — تنظیم موجودی\n"
            "📣 /announce متن — ثبت اعلان محلی\n"
            "🔌 /connection — وضعیت اتصال\n"
            "🛠 /setreward مقدار — جایزه روزانه\n"
            "📦 /additem نام|قیمت|توضیح — افزودن آیتم\n"
            "💾 /backup — پشتیبان\n🧾 /logs — گزارش مدیریتی")


def stats_text() -> str:
    with connect_db() as db:
        users = db.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        active = db.execute("SELECT COUNT(*) n FROM users WHERE last_seen >= ?", (date.today().isoformat(),)).fetchone()["n"]
        admins = db.execute("SELECT COUNT(*) n FROM admins").fetchone()["n"]
        groups = db.execute("SELECT COUNT(*) n FROM groups").fetchone()["n"]
        errors = db.execute("SELECT COUNT(*) n FROM connection_logs WHERE status='error'").fetchone()["n"]
        tx = db.execute("SELECT COUNT(*) n FROM transactions").fetchone()["n"]
    return (f"📊 آمار MEOWI\n👤 کاربران: {users}\n🟢 فعال امروز: {active}\n"
            f"🛡 مدیران ثبت‌شده: {admins}\n👥 گروه‌های ثبت‌شده: {groups}\n"
            f"🧾 تراکنش‌ها: {tx}\n⚠️ خطاهای اتصال ثبت‌شده: {errors}")


def add_admin(actor: str, target: str) -> str:
    if not is_owner(actor):
        return "⛔ فقط مالک اصلی می‌تونه مدیر اضافه کنه."
    if str(target) == str(actor):
        return "ℹ️ مالک از قبل دسترسی کامل داره."
    with connect_db() as db:
        db.execute("INSERT OR REPLACE INTO admins(user_id,role,permissions,added_at) VALUES(?,?,?,?)",
                   (str(target), "admin", json.dumps(["stats", "users"], ensure_ascii=False), now_iso()))
    admin_log(actor, "add_admin", str(target))
    return f"✅ مدیر {target} ثبت شد."


def del_admin(actor: str, target: str) -> str:
    if not is_owner(actor):
        return "⛔ فقط مالک اصلی می‌تونه مدیر حذف کنه."
    if is_owner(target):
        return "⛔ مالک اصلی قابل حذف نیست."
    with connect_db() as db:
        cur = db.execute("DELETE FROM admins WHERE user_id=?", (str(target),))
    admin_log(actor, "del_admin", str(target))
    return "✅ مدیر حذف شد." if cur.rowcount else "ℹ️ این شناسه مدیر نبود."


def set_balance_cmd(actor: str, args: list[str]) -> str:
    if not is_admin(actor) or len(args) < 3:
        return "⛔ دسترسی ندارید یا قالب دستور اشتباه است.\n/setbalance شناسه مقدار دلیل"
    target, raw_amount = args[0], args[1]
    reason = " ".join(args[2:])
    try:
        amount = int(raw_amount)
    except ValueError:
        return "مقدار باید عدد صحیح باشد."
    # Set-to-value, logged as a delta.
    current = balance_of(target)
    delta = amount - current
    if amount < 0:
        return "موجودی نمی‌تونه منفی باشه."
    try:
        new = change_balance(target, delta, "admin_adjustment", reason, actor)
    except ValueError as exc:
        return f"❌ {exc}"
    admin_log(actor, "set_balance", f"{target} => {amount}; reason={reason}")
    return f"✅ موجودی {target} به {new} تغییر کرد."


def add_item(actor: str, raw: str) -> str:
    if not is_owner(actor):
        return "⛔ فقط مالک می‌تونه آیتم اضافه کنه."
    parts = raw.split("|", 2)
    if len(parts) < 2:
        return "قالب: /additem نام|قیمت|توضیح"
    try:
        price = int(parts[1].strip())
        if price < 0:
            raise ValueError()
    except ValueError:
        return "قیمت باید عدد صحیح غیرمنفی باشد."
    name = parts[0].strip()
    desc = parts[2].strip() if len(parts) > 2 else ""
    if not name:
        return "نام آیتم خالی است."
    with connect_db() as db:
        cur = db.execute("INSERT INTO shop_items(name,description,price,stock) VALUES(?,?,?,-1)",
                         (name[:100], desc[:300], price))
    admin_log(actor, "add_item", f"id={cur.lastrowid}; name={name}")
    return f"✅ آیتم با شناسه {cur.lastrowid} اضافه شد."


def backup_db() -> str:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dest = BACKUP_DIR / f"meowi-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    with connect_db() as src, sqlite3.connect(dest) as dst:
        src.backup(dst)
    return f"✅ نسخه پشتیبان ساخته شد: {dest.name}"


def connection_status() -> str:
    return ("🔵 وضعیت اتصال روبیکا: حالت آزمایشی/آفلاین\n"
            "📡 پیام‌رسان زنده: فعال نشده\n"
            "🔐 توکن: در این نسخه دریافت یا ذخیره نشده\n"
            "ℹ️ برای اتصال واقعی باید کتابخانه یا API معتبر و قابل دسترس روبیکا تنظیم شود.\n"
            "منو و دیتابیس محلی فعال هستند؛ این وضعیت اتصال واقعی نیست.")



def mafia_action(chat_id: str, user_id: str, command: str, args: list[str]) -> Optional[str]:
    game = active_game(chat_id)
    if not game or game["game_type"] != "mafia":
        return None
    state = json.loads(game["state_json"])
    roles = state.get("roles", {})
    alive = state.get("alive", list(roles.keys()) if roles else [])
    if not roles:
        return None

    with connect_db() as db:
        players = db.execute("SELECT user_id,display_name FROM game_players WHERE session_id=?",
                             (game["id"],)).fetchall()
    names = {str(p["user_id"]): p["display_name"] for p in players}
    if user_id not in alive and command in ("/kill", "/vote", "/day", "/endvote"):
        return "👻 بازیکن حذف‌شده نمی‌تونه در این مرحله اقدام کنه."

    if command == "/kill":
        if state.get("phase") != "night":
            return "🌞 الان شب نیست؛ اقدام مافیا فقط در مرحله شب انجام می‌شه."
        if roles.get(user_id) != "مافیا":
            return "⛔ فقط مافیا می‌تونه هدف شب انتخاب کنه."
        if not args:
            return "قالب: /kill شناسه_بازیکن\nبرای مشاهده فهرست بازیکنان /players رو بزن."
        target = args[0]
        if target not in alive:
            return "❌ شناسه هدف معتبر نیست یا بازیکن زنده نیست."
        if roles.get(target) == "مافیا":
            return "❌ نمی‌تونی هم‌تیمی مافیات رو هدف بگیری."
        state["night_target"] = target
        with connect_db() as db:
            db.execute("UPDATE game_sessions SET state_json=?,updated_at=? WHERE id=?",
                       (json.dumps(state, ensure_ascii=False), now_iso(), game["id"]))
        return "🌙 انتخاب شب ثبت شد. وقتی مافیا آماده بود، یکی از مدیران بازی /day رو بزنه."

    if command == "/day":
        if not is_admin(user_id):
            return "⛔ فقط مدیر ربات می‌تونه شب رو به روز تبدیل کنه."
        if state.get("phase") != "night":
            return "الان مرحله شب نیست."
        target = state.get("night_target")
        if target and target in alive:
            alive.remove(target)
            state["alive"] = alive
            state["history"] = state.get("history", []) + [f"حذف شب: {target}"]
            announcement = f"🌅 روز شد!\n💀 {names.get(target, target)} در شب از بازی حذف شد."
        else:
            announcement = "🌅 روز شد! دیشب کسی حذف نشد."
        state["phase"] = "day"
        state["night_target"] = None
        state["votes"] = {}
        mafia_alive = sum(1 for pid in alive if roles.get(pid) == "مافیا")
        citizens_alive = len(alive) - mafia_alive
        if mafia_alive == 0:
            state["phase"] = "finished"
            announcement += "\n🏆 شهروندها برنده شدند!"
            status = "ended"
        elif mafia_alive >= citizens_alive:
            state["phase"] = "finished"
            announcement += "\n🏆 مافیا برنده شد!"
            status = "ended"
        else:
            status = "active"
            announcement += "\n🗳 برای رأی‌دادن: /vote شناسه_بازیکن و سپس /endvote"
        with connect_db() as db:
            db.execute("UPDATE game_sessions SET state_json=?,status=?,updated_at=? WHERE id=?",
                       (json.dumps(state, ensure_ascii=False), status, now_iso(), game["id"]))
        return announcement

    if command == "/vote":
        if state.get("phase") != "day":
            return "🗳 رأی‌گیری فقط در مرحله روز انجام می‌شه."
        if not args:
            return "قالب: /vote شناسه_بازیکن"
        target = args[0]
        if target not in alive or target == user_id:
            return "❌ هدف باید شناسه یک بازیکن زنده دیگر باشد."
        votes = state.setdefault("votes", {})
        votes[user_id] = target
        state["votes"] = votes
        with connect_db() as db:
            db.execute("UPDATE game_sessions SET state_json=?,updated_at=? WHERE id=?",
                       (json.dumps(state, ensure_ascii=False), now_iso(), game["id"]))
        return f"🗳 رأی تو برای {names.get(target, target)} ثبت شد. برای اعلام نتیجه مدیر /endvote رو بزنه."

    if command == "/endvote":
        if not is_admin(user_id):
            return "⛔ فقط مدیر ربات می‌تونه نتیجه رأی‌گیری رو اعلام کنه."
        if state.get("phase") != "day":
            return "رأی‌گیری روز فعال نیست."
        votes = state.get("votes", {})
        if not votes:
            return "هنوز رأیی ثبت نشده."
        tally = {}
        for voter, target in votes.items():
            if voter in alive and target in alive:
                tally[target] = tally.get(target, 0) + 1
        if not tally:
            return "رأی معتبر ثبت نشده."
        highest = max(tally.values())
        winners = [pid for pid, count in tally.items() if count == highest]
        if len(winners) > 1:
            state["phase"] = "night"
            state["round"] = int(state.get("round", 1)) + 1
            state["votes"] = {}
            result = "🤝 رأی‌گیری مساوی شد؛ کسی حذف نشد. 🌙 شب بعد شروع شد. مافیا /kill شناسه_بازیکن"
        else:
            target = winners[0]
            alive.remove(target)
            state["alive"] = alive
            result = f"⚖️ با {highest} رأی، {names.get(target, target)} حذف شد. نقش: {roles.get(target, 'نامشخص')}"
            mafia_alive = sum(1 for pid in alive if roles.get(pid) == "مافیا")
            citizens_alive = len(alive) - mafia_alive
            if mafia_alive == 0:
                state["phase"] = "finished"
                result += "\n🏆 شهروندها برنده شدند!"
                status = "ended"
            elif mafia_alive >= citizens_alive:
                state["phase"] = "finished"
                result += "\n🏆 مافیا برنده شد!"
                status = "ended"
            else:
                state["phase"] = "night"
                state["round"] = int(state.get("round", 1)) + 1
                result += "\n🌙 شب بعد شروع شد. مافیا با /kill شناسه_بازیکن هدف انتخاب کنه."
                status = "active"
        state["votes"] = {}
        with connect_db() as db:
            db.execute("UPDATE game_sessions SET state_json=?,status=?,updated_at=? WHERE id=?",
                       (json.dumps(state, ensure_ascii=False), status if 'status' in locals() else "active",
                        now_iso(), game["id"]))
        return result
    return None


def handle_command(command: str, user_id: str = "local-user", name: str = "کاربر",
                   chat_id: str = "local-chat", is_group: bool = False) -> str:
    ensure_user(user_id, name)
    with connect_db() as db:
        u = db.execute("SELECT blocked FROM users WHERE user_id=?", (str(user_id),)).fetchone()
    if u and u["blocked"]:
        return "⛔ دسترسی شما به ربات مسدود شده است."
    parts = command.strip().split()
    if not parts:
        return main_menu()
    cmd, args = parts[0].lower(), parts[1:]
    mafia_result = mafia_action(chat_id, str(user_id), cmd, args)
    if mafia_result is not None:
        return mafia_result
    if cmd in ("/start", "/menu", "شروع", "منو"):
        return main_menu()
    if cmd == "/help":
        return "🆘 راهنما\n/daily /wallet /transfer شناسه مبلغ /bet مبلغ /shop /buy ID /profile /top\nبازی گروهی: /mafia /join /leave /players /startgame /kill شناسه /day /vote شناسه /endvote /endgame /roll /coin"
    if cmd == "/daily":
        return daily_reward(user_id)
    if cmd == "/wallet":
        return f"💰 موجودی شما: {balance_of(user_id)} {setting('currency_name','سکه')}"
    if cmd == "/profile":
        return profile(user_id, name)
    if cmd == "/id":
        return f"🆔 شناسه عددی شما: {user_id}\nاین شناسه را به‌عنوان MEOWI_OWNER_ID در تنظیمات ربات وارد کن تا دسترسی مالک فعال شود."
    if cmd == "/top":
        return top_users()
    if cmd == "/shop":
        return shop_text()
    if cmd == "/buy":
        if not args or not args[0].isdigit():
            return "قالب خرید: /buy شماره_آیتم"
        return buy_item(user_id, int(args[0]))
    if cmd == "/games":
        return "🎮 بازی‌ها\n🕵️ مافیا: /mafia\n🎲 تاس: /roll\n🪙 شیر یا خط: /coin"
    if cmd == "/mafia":
        if not is_group:
            return "🕵️ برای بازی مافیا، این دستور رو داخل گروه بزن."
        return new_game(chat_id, "mafia", user_id, name)
    if cmd == "/join":
        return join_game(chat_id, user_id, name)
    if cmd == "/leave":
        return leave_game(chat_id, user_id)
    if cmd == "/players":
        return players_text(chat_id)
    if cmd == "/startgame":
        return start_game(chat_id)
    if cmd == "/endgame":
        if not is_group:
            return "این دستور برای گروه است."
        if not is_admin(user_id):
            return "⛔ فقط مدیر ربات می‌تونه بازی رو از این نسخه محلی پایان بده."
        return end_game(chat_id)
    if cmd == "/transfer":
        if len(args) < 2:
            return "📨 روش انتقال: /transfer شناسه_کاربر مبلغ\nمثال: /transfer 123456 100"
        return transfer_diamonds(str(user_id), args[0].lstrip("@"), args[1])
    if cmd in ("/bet", "/betdice", "/شرط"):
        if not args:
            return "🎲 روش شرط‌بندی: /bet مبلغ\nمثال: /bet 100\nبا آمدن عدد ۶ برنده می‌شوی؛ این بازی فقط با الماس مجازی است."
        if is_group and setting("dice_group_enabled", "1").lower() in ("0", "false", "off", "no"):
            return "🎲 شرط‌بندی در این گروه غیرفعال است."
        if (not is_group) and setting("dice_private_enabled", "1").lower() in ("0", "false", "off", "no"):
            return "🎲 شرط‌بندی خصوصی غیرفعال است."
        return dice_bet(str(user_id), str(chat_id), args[0])
    if cmd == "/roll":
        return roll_dice()
    if cmd == "/coin":
        return coin_flip()
    if cmd == "/cards":
        return "🎴 بخش کارت‌ها در نسخه فعلی به شکل فروشگاه آیتم‌هاست؛ برای کارت‌های قابل جمع‌آوری می‌توان سیستم مجموعه اضافه کرد."
    if cmd == "/missions":
        return "🎯 مأموریت‌ها: هر روز /daily رو بزن و در بازی‌های گروهی شرکت کن."
    if cmd == "/fun":
        return "🎉 سرگرمی: /roll و /coin و بازی گروهی /mafia"
    if cmd == "/news":
        return "📢 هنوز اطلاعیه‌ای ثبت نشده."
    if cmd == "/support":
        return "📞 برای پشتیبانی، شناسه مدیر ربات را از مالک دریافت کن."
    if cmd == "/admin":
        return admin_panel() if is_admin(user_id) else "⛔ دسترسی مدیریت ندارید."
    if cmd == "/stats":
        return stats_text() if is_admin(user_id) else "⛔ دسترسی مدیریت ندارید."
    if cmd == "/admins":
        if not is_admin(user_id):
            return "⛔ دسترسی مدیریت ندارید."
        with connect_db() as db:
            rows = db.execute("SELECT user_id,role FROM admins ORDER BY added_at").fetchall()
        owner = os.getenv("MEOWI_OWNER_ID", "").strip()
        out = [f"👑 مالک: {owner or 'تنظیم نشده'}"]
        out += [f"🛡 {r['user_id']} — {r['role']}" for r in rows]
        return "\n".join(out)
    if cmd == "/addadmin" and args:
        return add_admin(user_id, args[0])
    if cmd == "/deladmin" and args:
        return del_admin(user_id, args[0])
    if cmd == "/setbalance" and len(args) >= 3:
        return set_balance_cmd(user_id, args)
    if cmd == "/setreward" and args:
        if not is_owner(user_id):
            return "⛔ فقط مالک می‌تونه جایزه رو تغییر بده."
        try:
            value = int(args[0])
            if value < 0: raise ValueError()
            set_setting("daily_reward", str(value))
            admin_log(user_id, "set_reward", str(value))
            return f"✅ جایزه روزانه روی {value} تنظیم شد."
        except ValueError:
            return "مقدار باید عدد صحیح غیرمنفی باشد."
    if cmd == "/additem" and args:
        return add_item(user_id, command.partition(" ")[2])
    if cmd == "/backup":
        if not is_owner(user_id):
            return "⛔ فقط مالک اجازه پشتیبان‌گیری دارد."
        return backup_db()
    if cmd == "/connection":
        return connection_status() if is_admin(user_id) else "⛔ دسترسی مدیریت ندارید."
    if cmd == "/logs":
        if not is_admin(user_id):
            return "⛔ دسترسی مدیریت ندارید."
        with connect_db() as db:
            rows = db.execute("SELECT actor_id,action,details,created_at FROM admin_logs ORDER BY id DESC LIMIT 10").fetchall()
        if not rows:
            return "هنوز گزارشی ثبت نشده."
        return "🧾 آخرین عملیات مدیریت\n" + "\n".join(f"{r['created_at']} | {r['actor_id']} | {r['action']} | {r['details']}" for r in rows)
    if cmd == "/announce":
        if not is_admin(user_id):
            return "⛔ دسترسی مدیریت ندارید."
        msg = command.partition(" ")[2].strip()
        if not msg:
            return "قالب: /announce متن"
        admin_log(user_id, "announce_draft", msg)
        return "📝 اعلان در گزارش محلی ثبت شد. ارسال واقعی به کاربران نیازمند اتصال زنده روبیکا است."
    return "این دستور رو نمی‌شناسم. /help رو بزن 🐱"


def self_test() -> bool:
    init_db()
    test_user = f"test-{int(time.time())}"
    ensure_user(test_user, "آزمایش")
    assert balance_of(test_user) == 0
    recipient = f"test-recipient-{int(time.time())}"
    ensure_user(recipient, "گیرنده آزمایش")
    change_balance(test_user, 500, "test_credit", "اعتبار آزمایشی")
    transfer_message = transfer_diamonds(test_user, recipient, 100)
    assert "انتقال انجام شد" in transfer_message
    assert balance_of(test_user) == 400 and balance_of(recipient) == 100
    assert any(x in transfer_diamonds(test_user, recipient, 999999) for x in ("موجودی کافی", "باید بین"))
    dice_message = dice_bet(test_user, "test-chat", 10)
    assert "موجودی فعلی" in dice_message
    first = daily_reward(test_user)
    assert "جایزه روزانه" in first
    second = daily_reward(test_user)
    assert "فردا" in second
    item_result = buy_item(test_user, 1)
    assert "موجودی کافی" in item_result or "خرید انجام شد" in item_result
    assert "حالت آزمایشی" in connection_status()
    with connect_db() as db:
        db.execute("DELETE FROM users WHERE user_id IN (?,?)", (test_user, recipient))
    print("✅ تست دیتابیس: موفق")
    print("✅ تست کاربر و کیف پول: موفق")
    print("✅ تست جلوگیری از جایزه تکراری: موفق")
    print("✅ تست فروشگاه: موفق")
    print("✅ تست انتقال الماس و جلوگیری از موجودی منفی: موفق")
    print("✅ تست شرط‌بندی تاس با الماس مجازی: موفق")
    print("✅ تست وضعیت اتصال صادقانه: موفق")
    print("ℹ️ تست اتصال زنده روبیکا اجرا نشده؛ اتصال معتبر هنوز تنظیم نشده است.")
    return True



async def run_rubika() -> None:
    """Start live Rubika bot using the third-party rubika-bot-api package."""
    token = os.getenv("RUBIKA_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("متغیر RUBIKA_BOT_TOKEN تنظیم نشده است. توکن واقعی ربات را در محیط Termux تنظیم کن.")
    # Some published builds import `orjson` even though it is not declared as
    # an install dependency. On Termux, a native orjson wheel may be unavailable.
    # Provide a small stdlib-compatible fallback for JSON only when orjson is absent.
    try:
        import orjson  # noqa: F401
    except ModuleNotFoundError as missing:
        if missing.name != "orjson":
            raise
        compat = types.ModuleType("orjson")
        def _compat_dumps(obj, *, default=None, option=None):
            return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=default).encode("utf-8")
        def _compat_loads(data):
            if isinstance(data, (bytes, bytearray, memoryview)):
                data = bytes(data).decode("utf-8")
            return json.loads(data)
        compat.dumps = _compat_dumps
        compat.loads = _compat_loads
        for _name in ("OPT_APPEND_NEWLINE", "OPT_INDENT_2", "OPT_NAIVE_UTC",
                      "OPT_NON_STR_KEYS", "OPT_OMIT_MICROSECONDS", "OPT_PASSTHROUGH_DATACLASS",
                      "OPT_PASSTHROUGH_DATETIME", "OPT_PASSTHROUGH_SUBCLASS",
                      "OPT_SERIALIZE_DATACLASS", "OPT_SERIALIZE_NUMPY",
                      "OPT_SERIALIZE_UUID", "OPT_SORT_KEYS", "OPT_UTC_Z"):
            setattr(compat, _name, 0)
        sys.modules["orjson"] = compat
        log.warning("orjson در Termux نصب نیست؛ از سازگاری JSON استاندارد پایتون استفاده می‌شود.")

    try:
        from rubika_bot_api.api import Robot
    except ModuleNotFoundError as exc:
        if exc.name == "orjson":
            raise RuntimeError("وابستگی orjson بارگذاری نشد؛ دوباره نصب یا سازگاری کتابخانه بررسی شود.") from exc
        raise RuntimeError(
            f"کتابخانه روبیکا کامل نصب نیست؛ ماژول گمشده: {exc.name or 'نامشخص'}. "
            "دستور: python -m pip install -U rubika-bot-api aiohttp aiofiles requests"
        ) from exc
    except ImportError as exc:
        raise RuntimeError(
            "ماژول rubika_bot_api.api بارگذاری نشد. نصب را بررسی کن: "
            "python -m pip install -U rubika-bot-api aiohttp aiofiles requests"
        ) from exc

    bot = Robot(token=token)
    with connect_db() as db:
        db.execute("INSERT INTO connection_logs(status,message,created_at) VALUES(?,?,?)",
                   ("starting", "Starting Rubika bot client", now_iso()))

    @bot.on_message()
    async def on_any_message(bot_instance, message):
        try:
            text_in = str(getattr(message, "text", "") or "").strip()
            if not text_in:
                return
            sender_id = str(getattr(message, "sender_id", "") or "")
            chat_id = str(getattr(message, "chat_id", "") or "")
            chat_type = str(getattr(message, "chat_type", "") or "").lower()
            raw_data = getattr(message, "raw_data", {}) or {}
            if not isinstance(raw_data, dict):
                raw_data = {}
            if not sender_id or not chat_id:
                log.warning("Incoming update did not contain sender_id/chat_id")
                return

            # Some Rubika update versions label group types differently; use explicit group types
            # when present, otherwise treat a chat as a group only when the API says so.
            # Different library versions expose chat_type differently. Fall back to
            # Rubika group/channel chat ID prefixes and raw update metadata.
            raw_chat_type = str(raw_data.get("chat_type", raw_data.get("object_type", "")) or "").lower()
            is_group = (
                any(word in chat_type for word in ("group", "channel", "گروه")) or
                any(word in raw_chat_type for word in ("group", "channel", "گروه")) or
                chat_id.startswith(("g", "c"))
            )
            display_name = ""
            try:
                name_method = getattr(bot_instance, "get_name", None)
                if callable(name_method):
                    maybe = name_method(sender_id)
                    if hasattr(maybe, "__await__"):
                        maybe = await maybe
                    display_name = str(maybe or "")
            except Exception:
                display_name = ""

            ensure_user(sender_id, display_name)
            with connect_db() as db:
                db.execute("INSERT INTO connection_logs(status,message,created_at) VALUES(?,?,?)",
                           ("message_received", f"chat_type={chat_type or 'unknown'}", now_iso()))

            # Only commands and known menu labels trigger responses; no echo-spam.
            command_text = text_in
            if command_text in ("شروع", "منو", "🎮 بازی‌ها", "🎁 جایزه روزانه", "💰 کیف پول",
                                "🛒 فروشگاه", "🏆 رتبه‌بندی", "👤 حساب کاربری"):
                command_text = {
                    "شروع": "/start", "منو": "/start", "🎮 بازی‌ها": "/games",
                    "🎁 جایزه روزانه": "/daily", "💰 کیف پول": "/wallet",
                    "🛒 فروشگاه": "/shop", "🏆 رتبه‌بندی": "/top", "👤 حساب کاربری": "/profile"
                }.get(command_text, command_text)

            response = handle_command(command_text, sender_id, display_name or "کاربر",
                                      chat_id, is_group)
            # Handle game roles privately only after game start; the role message is never posted to group.
            if command_text.lower().startswith("/startgame") and is_group:
                game = active_game(chat_id)
                if game:
                    state = json.loads(game["state_json"])
                    roles = state.get("roles", {})
                    delivered = 0
                    for pid, role in roles.items():
                        try:
                            await bot_instance.send_message(chat_id=pid, text=f"🐱 نقش تو در مافیای MEOWI: **{role}**")
                            delivered += 1
                        except Exception:
                            log.warning("Could not send private role message to one player")
                    if delivered < len(roles):
                        response += (f"\n\n⚠️ نقش خصوصی برای {len(roles)-delivered} نفر ارسال نشد. "
                                     "آن بازیکنان باید ابتدا ربات را در گفت‌وگوی خصوصی Start کنند.")
                    else:
                        response += "\n\n🔐 نقش‌ها به‌صورت خصوصی ارسال شدند."
            await message.reply(response)
        except Exception as exc:
            log.exception("Error handling incoming Rubika message")
            try:
                await message.reply("⚠️ در پردازش پیام خطا رخ داد. لطفاً دوباره تلاش کن.")
            except Exception:
                pass

    @bot.on_started_bot()
    async def on_started(bot_instance, chat_id):
        try:
            await bot_instance.send_message(chat_id=chat_id, text=main_menu())
        except Exception:
            log.exception("Could not send startup message")

    with connect_db() as db:
        db.execute("INSERT INTO connection_logs(status,message,created_at) VALUES(?,?,?)",
                   ("connected_attempt", "Rubika client loop starting; live success depends on API response", now_iso()))
    print("🐱 MEOWI: در حال شروع اتصال روبیکا...")
    print("اگر توکن معتبر باشد، پیام‌ها طبق دسترسی کتابخانه دریافت می‌شوند.")
    await bot.run()


def run_cli() -> None:
    init_db()
    print("🐱 MEOWI | میویی — حالت محلی")
    print("برای خروج: /exit | راهنما: /help")
    print("نکته: این حالت به روبیکا وصل نیست؛ برای آزمایش منو و دیتابیس است.")
    uid = os.getenv("MEOWI_TEST_USER", "local-owner")
    name = "مالک محلی"
    if is_owner(uid):
        print("👑 شناسه مالک از متغیر محیطی خوانده شد.")
    while True:
        try:
            command = input("\nMEOWI> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nخروج امن.")
            break
        if command.lower() in ("/exit", "exit", "quit"):
            break
        try:
            print("\n" + handle_command(command, uid, name, "local-group", True))
        except Exception as exc:
            log.exception("Command failed")
            print(f"⚠️ خطای داخلی: {type(exc).__name__}. جزئیات در فایل لاگ ثبت شد.")


def main() -> None:
    parser = argparse.ArgumentParser(description="MEOWI Rubika bot local core")
    parser.add_argument("--test", action="store_true", help="اجرای تست‌های محلی")
    parser.add_argument("--init", action="store_true", help="ساخت دیتابیس و خروج")
    parser.add_argument("--cli", action="store_true", help="اجرای منوی آزمایشی محلی")
    parser.add_argument("--rubika", action="store_true", help="اجرای ربات متصل به روبیکا")
    args = parser.parse_args()
    init_db()
    if args.test:
        self_test()
    elif args.init:
        print(f"✅ دیتابیس آماده شد: {DB_PATH}")
    elif args.rubika:
        try:
            asyncio.run(run_rubika())
        except KeyboardInterrupt:
            print("\nربات متوقف شد.")
        except Exception as exc:
            log.exception("Rubika startup failed")
            print(f"❌ اتصال اجرا نشد: {type(exc).__name__}: {exc}")
            print("توکن را در چت نفرست؛ در Termux تنظیم کن. اگر کتابخانه کامل است، متن خطای بالا را بررسی می‌کنیم.")
            sys.exit(1)
    else:
        run_cli()


if __name__ == "__main__":
    main()
