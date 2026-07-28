# bot.py
# RichAds reklamali kino bot (Webhook versiya)
# Python 3.10+
#
# O‘rnatish:
#   pip install pyTelegramBotAPI flask
#
# Ishga tushirishdan oldin quyidagilarni almashtiring:
#   BOT_TOKEN
#   ADMIN_IDS
#   RICHADS_POSTBACK_SECRET

import html
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime

import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException
from flask import Flask, request, jsonify

# =========================================================
# SOZLAMALAR
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

ADMIN_IDS = {
    int(value.strip())
    for value in os.getenv("ADMIN_IDS", "").split(",")
    if value.strip().isdigit()
}

DB_NAME = "kino_bot.db"

# RichAds postback sirli kaliti – xavfsizlik uchun.
RICHADS_POSTBACK_SECRET = os.getenv("RICHADS_POSTBACK_SECRET", "change_me_123")

if not BOT_TOKEN or ":" not in BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN noto‘g‘ri. Environment bo‘limiga haqiqiy BotFather tokenini kiriting."
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=True)

# =========================================================
# MA’LUMOTLAR BAZASI
# =========================================================

db_lock = threading.RLock()

def db_connect():
    connection = sqlite3.connect(
        DB_NAME,
        timeout=30,
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    return connection

def execute(query: str, params: tuple = (), fetchone=False, fetchall=False):
    with db_lock:
        conn = db_connect()
        try:
            cursor = conn.execute(query, params)
            conn.commit()

            if fetchone:
                return cursor.fetchone()
            if fetchall:
                return cursor.fetchall()
            return cursor.lastrowid
        finally:
            conn.close()

def init_db():
    execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            full_name TEXT NOT NULL DEFAULT '',
            username TEXT NOT NULL DEFAULT '',
            is_blocked INTEGER NOT NULL DEFAULT 0,
            joined_at TEXT NOT NULL,
            last_active TEXT NOT NULL
        )
    """)

    execute("""
        CREATE TABLE IF NOT EXISTS movies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            caption TEXT NOT NULL,
            file_id TEXT NOT NULL,
            file_type TEXT NOT NULL DEFAULT 'video',
            views INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            added_by INTEGER NOT NULL
        )
    """)

    execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            message_text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            answered INTEGER NOT NULL DEFAULT 0
        )
    """)

    execute("""
        CREATE TABLE IF NOT EXISTS states (
            user_id INTEGER PRIMARY KEY,
            state TEXT NOT NULL,
            data TEXT NOT NULL DEFAULT ''
        )
    """)

    execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    execute("""
        CREATE TABLE IF NOT EXISTS watch_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            movie_id INTEGER NOT NULL,
            watched_at TEXT NOT NULL
        )
    """)

    execute("""
        CREATE TABLE IF NOT EXISTS search_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            code TEXT NOT NULL,
            found INTEGER NOT NULL,
            searched_at TEXT NOT NULL
        )
    """)

    # RichAds reklama jadvali
    execute("""
        CREATE TABLE IF NOT EXISTS ad_views (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            movie_id INTEGER NOT NULL,
            click_id TEXT UNIQUE NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL
        )
    """)

    # Standart reklama sozlamalari
    for key, value in [
        ("richads_campaign_id", "1018576"),
        ("richads_link_template", "https://richads.com/c/{campaign_id}?sub_id={sub_id}"),
        ("required_ads", "1"),
        ("richads_min_floor", "0.01"),
        ("richads_lang", "uz"),
    ]:
        execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
            (key, value),
        )

# =========================================================
# YORDAMCHI FUNKSIYALAR
# =========================================================

def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def safe(value):
    return html.escape(str(value or ""))

def is_admin(user_id: int):
    return user_id in ADMIN_IDS

def get_setting(key: str):
    row = execute(
        "SELECT value FROM settings WHERE key=?",
        (key,),
        fetchone=True,
    )
    return row["value"] if row else ""

def set_setting(key: str, value: str):
    execute("""
        INSERT INTO settings(key, value)
        VALUES(?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """, (key, value))

def set_state(user_id: int, state: str, data: str = ""):
    execute("""
        INSERT INTO states(user_id, state, data)
        VALUES(?, ?, ?)
        ON CONFLICT(user_id)
        DO UPDATE SET state=excluded.state, data=excluded.data
    """, (user_id, state, data))

def get_state(user_id: int):
    row = execute(
        "SELECT state, data FROM states WHERE user_id=?",
        (user_id,),
        fetchone=True,
    )
    if not row:
        return None, ""
    return row["state"], row["data"]

def clear_state(user_id: int):
    execute("DELETE FROM states WHERE user_id=?", (user_id,))

def register_user(user):
    username = f"@{user.username}" if user.username else ""
    full_name = " ".join(
        part for part in [user.first_name, user.last_name] if part
    ).strip()

    execute("""
        INSERT INTO users(
            user_id, full_name, username, joined_at, last_active
        )
        VALUES(?, ?, ?, ?, ?)
        ON CONFLICT(user_id)
        DO UPDATE SET
            full_name=excluded.full_name,
            username=excluded.username,
            last_active=excluded.last_active
    """, (
        user.id,
        full_name,
        username,
        now_text(),
        now_text(),
    ))

def get_user(user_id: int):
    return execute(
        "SELECT * FROM users WHERE user_id=?",
        (user_id,),
        fetchone=True,
    )

# =========================================================
# RichAds REKLAMA TIZIMI
# =========================================================

def get_richads_link(sub_id: str):
    template = get_setting("richads_link_template")
    campaign_id = get_setting("richads_campaign_id")
    return template.format(campaign_id=campaign_id, sub_id=sub_id)

def ad_required_for_movie(user_id: int, movie_id: int):
    required = int(get_setting("required_ads"))
    completed = execute(
        "SELECT COUNT(*) AS cnt FROM ad_views WHERE user_id=? AND movie_id=? AND status='completed'",
        (user_id, movie_id),
        fetchone=True,
    )["cnt"]
    return completed < required

def send_ad_and_track(user_id: int, chat_id: int, movie_id: int):
    required = int(get_setting("required_ads"))
    completed = execute(
        "SELECT COUNT(*) AS cnt FROM ad_views WHERE user_id=? AND movie_id=? AND status='completed'",
        (user_id, movie_id),
        fetchone=True,
    )["cnt"]
    remaining = required - completed

    click_id = str(uuid.uuid4())
    execute(
        "INSERT INTO ad_views(user_id, movie_id, click_id, status, created_at) VALUES(?, ?, ?, 'pending', ?)",
        (user_id, movie_id, click_id, now_text()),
    )

    link = get_richads_link(click_id)
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton("📣 Reklamani ko‘rish", url=link))

    bot.send_message(
        chat_id,
        f"📺 <b>Kinoni tomosha qilish uchun {remaining} ta reklama ko‘rishingiz kerak.</b>\n\n"
        f"Quyidagi tugmani bosing va reklama yakunlangach kino avtomatik yuboriladi.",
        reply_markup=keyboard,
    )

def process_ad_completion(click_id: str):
    ad = execute(
        "SELECT * FROM ad_views WHERE click_id=? AND status='pending'",
        (click_id,),
        fetchone=True,
    )
    if not ad:
        return False

    execute(
        "UPDATE ad_views SET status='completed' WHERE id=?",
        (ad["id"],),
    )

    user_id = ad["user_id"]
    movie_id = ad["movie_id"]
    if not ad_required_for_movie(user_id, movie_id):
        movie = execute("SELECT * FROM movies WHERE id=?", (movie_id,), fetchone=True)
        if movie:
            try:
                send_movie_to_user(user_id, user_id, movie)
            except Exception:
                logging.exception("Kino yuborishda xato")
    return True

# =========================================================
# FLASK ILOVA (Webhook va postback uchun)
# =========================================================

app = Flask(__name__)

WEBHOOK_PATH = f"/webhook/{BOT_TOKEN.split(':', 1)[0]}"
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
# =========================================================
# KLAVIATURALAR VA KINONI YUBORISH
# =========================================================

def main_keyboard(user_id: int):
    keyboard = types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        row_width=2,
    )
    keyboard.add(
        types.KeyboardButton("🔎 Kino kodi"),
        types.KeyboardButton("🔥 Eng mashhur kinolar"),
    )
    keyboard.add(
        types.KeyboardButton("📊 Statistika"),
        types.KeyboardButton("💬 Adminga xabar"),
    )
    keyboard.add(
        types.KeyboardButton("👤 Profil"),
    )
    if is_admin(user_id):
        keyboard.add(types.KeyboardButton("🛠 Admin panel"))
    return keyboard

def admin_keyboard():
    keyboard = types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        row_width=2,
    )
    keyboard.add(
        types.KeyboardButton("🎬 Kinolar"),
        types.KeyboardButton("⚙️ Reklama sozlamalari"),
    )
    keyboard.add(
        types.KeyboardButton("👥 Foydalanuvchilar"),
        types.KeyboardButton("📢 Reklama yuborish"),
    )
    keyboard.add(
        types.KeyboardButton("📊 Statistika"),
        types.KeyboardButton("🏠 Asosiy menyu"),
    )
    return keyboard

def movies_keyboard():
    keyboard = types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        row_width=2,
    )
    keyboard.add(
        types.KeyboardButton("➕ Kino qo‘shish"),
        types.KeyboardButton("🗑 Kino o‘chirish"),
    )
    keyboard.add(
        types.KeyboardButton("📋 Barcha kinolar"),
        types.KeyboardButton("🔙 Admin panelga qaytish"),
    )
    return keyboard

def send_movie_to_user(chat_id: int, user_id: int, movie):
    new_views = movie["views"] + 1
    execute(
        "UPDATE movies SET views=? WHERE id=?",
        (new_views, movie["id"]),
    )
    execute(
        "INSERT INTO watch_log(user_id, movie_id, watched_at) VALUES(?, ?, ?)",
        (user_id, movie["id"], now_text()),
    )

    caption = (
        f"{movie['caption']}\n\n"
        f"🔢 Kod: <code>{safe(movie['code'])}</code>\n"
        f"👁 Ko‘rilgan: <b>{new_views}</b> marta"
    )

    if movie["file_type"] == "video":
        bot.send_video(
            chat_id,
            movie["file_id"],
            caption=caption,
            supports_streaming=True,
        )
    else:
        bot.send_document(
            chat_id,
            movie["file_id"],
            caption=caption,
        )

def ensure_not_blocked(message):
    register_user(message.from_user)
    user = get_user(message.from_user.id)
    if user and user["is_blocked"]:
        bot.send_message(message.chat.id, "🚫 Siz botdan bloklangansiz.")
        return False
    return True

# =========================================================
# START VA ASOSIY MENYU
# =========================================================

@bot.message_handler(commands=["start"])
def start_handler(message):
    if not ensure_not_blocked(message):
        return
    clear_state(message.from_user.id)
    open_main_menu(message.chat.id, message.from_user.id)

def open_main_menu(chat_id: int, user_id: int):
    bot.send_message(
        chat_id,
        "🎬 <b>Kino botga xush kelibsiz!</b>\n\n"
        "Kinoni ko‘rish uchun reklama tomosha qilishingiz kerak bo‘ladi.\n"
        "Quyidagi menyudan foydalaning:",
        reply_markup=main_keyboard(user_id),
    )

@bot.message_handler(commands=["admin"])
def admin_command(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "❌ Siz admin emassiz.")
        return
    clear_state(message.from_user.id)
    bot.send_message(
        message.chat.id,
        "🛠 <b>Admin panel</b>",
        reply_markup=admin_keyboard(),
    )

@bot.message_handler(func=lambda m: m.text == "🏠 Asosiy menyu")
def home_handler(message):
    if not ensure_not_blocked(message):
        return
    clear_state(message.from_user.id)
    open_main_menu(message.chat.id, message.from_user.id)

@bot.message_handler(func=lambda m: m.text == "🛠 Admin panel")
def admin_panel_button(message):
    if not is_admin(message.from_user.id):
        return
    clear_state(message.from_user.id)
    bot.send_message(
        message.chat.id,
        "🛠 <b>Admin panel</b>",
        reply_markup=admin_keyboard(),
    )

# =========================================================
# KINO QIDIRISH VA REYTING
# =========================================================

@bot.message_handler(func=lambda m: m.text == "🔎 Kino kodi")
def ask_movie_code(message):
    if not ensure_not_blocked(message):
        return
    set_state(message.from_user.id, "waiting_movie_code")
    bot.send_message(
        message.chat.id,
        "🔢 <b>Kino kodini yuboring:</b>\nMasalan: <code>145</code>",
    )

@bot.message_handler(func=lambda m: m.text == "🔥 Eng mashhur kinolar")
def popular_movies(message):
    if not ensure_not_blocked(message):
        return
    movies = execute("""
        SELECT code, title, views
        FROM movies
        ORDER BY views DESC, id DESC
        LIMIT 10
    """, fetchall=True)

    if not movies:
        bot.send_message(message.chat.id, "Hozircha kinolar mavjud emas.")
        return

    medals = ["🥇", "🥈", "🥉"]
    lines = ["🔥 <b>ENG MASHHUR KINOLAR</b>\n"]
    for i, m in enumerate(movies):
        icon = medals[i] if i < 3 else f"{i+1}."
        lines.append(
            f"{icon} <b>{safe(m['title'])}</b>\n"
            f"   Kod: <code>{safe(m['code'])}</code> | 👁 {m['views']}"
        )
    bot.send_message(message.chat.id, "\n\n".join(lines))

# =========================================================
# STATISTIKA VA PROFIL
# =========================================================

@bot.message_handler(func=lambda m: m.text == "📊 Statistika")
def public_statistics(message):
    if not ensure_not_blocked(message):
        return
    users = execute("SELECT COUNT(*) AS c FROM users", fetchone=True)["c"]
    movies = execute("SELECT COUNT(*) AS c FROM movies", fetchone=True)["c"]
    views = execute("SELECT COALESCE(SUM(views),0) AS c FROM movies", fetchone=True)["c"]

    bot.send_message(
        message.chat.id,
        "📊 <b>BOT STATISTIKASI</b>\n\n"
        f"🎬 Kinolar: <b>{movies}</b>\n"
        f"👁 Jami ko‘rishlar: <b>{views}</b>\n"
        f"👥 Foydalanuvchilar: <b>{users}</b>",
    )

@bot.message_handler(func=lambda m: m.text == "👤 Profil")
def profile_handler(message):
    register_user(message.from_user)
    user = get_user(message.from_user.id)
    watched = execute(
        "SELECT COUNT(*) AS c FROM watch_log WHERE user_id=?",
        (message.from_user.id,),
        fetchone=True,
    )["c"]

    bot.send_message(
        message.chat.id,
        "👤 <b>PROFIL</b>\n\n"
        f"Ism: <b>{safe(user['full_name'])}</b>\n"
        f"🆔 ID: <code>{user['user_id']}</code>\n"
        f"🔗 Username: {safe(user['username'] or 'Yo‘q')}\n"
        f"🎬 Ko‘rilgan kinolar: <b>{watched}</b>\n"
        f"📆 Qo‘shilgan: <b>{safe(user['joined_at'])}</b>",
    )

# =========================================================
# USERDAN ADMINGA XABAR
# =========================================================

@bot.message_handler(func=lambda m: m.text == "💬 Adminga xabar")
def ask_admin_message(message):
    if not ensure_not_blocked(message):
        return
    set_state(message.from_user.id, "waiting_admin_message")
    bot.send_message(message.chat.id, "💬 Adminga yuboriladigan xabarni yozing:")

# =========================================================
# ADMIN: KINOLAR BO‘LIMI
# =========================================================

@bot.message_handler(func=lambda m: m.text == "🎬 Kinolar")
def movies_section(message):
    if not is_admin(message.from_user.id):
        return
    clear_state(message.from_user.id)
    bot.send_message(message.chat.id, "🎬 Kinolar bo‘limi", reply_markup=movies_keyboard())

@bot.message_handler(func=lambda m: m.text == "➕ Kino qo‘shish")
def add_movie_start(message):
    if not is_admin(message.from_user.id):
        return
    set_state(message.from_user.id, "add_movie_video")
    bot.send_message(
        message.chat.id,
        "🎥 Kino videosini (yoki hujjat) yuboring:",
    )

@bot.message_handler(func=lambda m: m.text == "🗑 Kino o‘chirish")
def delete_movie_start(message):
    if not is_admin(message.from_user.id):
        return
    set_state(message.from_user.id, "delete_movie_code")
    bot.send_message(message.chat.id, "🗑 O‘chiriladigan kino kodini yuboring:")

@bot.message_handler(func=lambda m: m.text == "📋 Barcha kinolar")
def all_movies_handler(message):
    if not is_admin(message.from_user.id):
        return
    movies = execute("""
        SELECT code, title, views FROM movies ORDER BY id DESC LIMIT 100
    """, fetchall=True)
    if not movies:
        bot.send_message(message.chat.id, "Kinolar mavjud emas.")
        return
    text = ["📋 <b>BARCHA KINOLAR</b>\n"]
    for i, m in enumerate(movies, 1):
        text.append(
            f"{i}. <b>{safe(m['title'])}</b>\n"
            f"   Kod: <code>{safe(m['code'])}</code> | 👁 {m['views']}"
        )
    full = "\n\n".join(text)
    for start in range(0, len(full), 3900):
        bot.send_message(message.chat.id, full[start:start+3900])

@bot.message_handler(func=lambda m: m.text == "🔙 Admin panelga qaytish")
def back_to_admin_panel(message):
    if not is_admin(message.from_user.id):
        return
    clear_state(message.from_user.id)
    bot.send_message(
        message.chat.id,
        "🛠 <b>Admin panel</b>",
        reply_markup=admin_keyboard(),
    )

# =========================================================
# ADMIN: REKLAMA SOZLAMALARI
# =========================================================

@bot.message_handler(func=lambda m: m.text == "⚙️ Reklama sozlamalari")
def ad_settings_handler(message):
    if not is_admin(message.from_user.id):
        return
    template = get_setting("richads_link_template")
    campaign = get_setting("richads_campaign_id")
    required = get_setting("required_ads")
    floor = get_setting("richads_min_floor")
    lang = get_setting("richads_lang")

    text = (
        "⚙️ <b>REKLAMA SOZLAMALARI</b>\n\n"
        f"Kampaniya ID: <code>{safe(campaign)}</code>\n"
        f"Link shabloni: <code>{safe(template)}</code>\n"
        f"Har kino uchun reklama soni: <b>{required}</b>\n"
        f"Minimal floor: {floor}\n"
        f"Til: {lang}"
    )
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        types.InlineKeyboardButton("Kampaniya ID o‘zgartirish", callback_data="ad_campaign_id"),
        types.InlineKeyboardButton("Link shablonini o‘zgartirish", callback_data="ad_link_template"),
        types.InlineKeyboardButton("Reklama sonini o‘zgartirish", callback_data="ad_required"),
        types.InlineKeyboardButton("Minimal floor o‘zgartirish", callback_data="ad_floor"),
        types.InlineKeyboardButton("Tilni o‘zgartirish", callback_data="ad_lang"),
    )
    bot.send_message(message.chat.id, text, reply_markup=keyboard)

@bot.callback_query_handler(func=lambda call: call.data in [
    "ad_campaign_id", "ad_link_template", "ad_required", "ad_floor", "ad_lang"
])
def ad_settings_callback(call):
    if not is_admin(call.from_user.id):
        return bot.answer_callback_query(call.id, "Ruxsat yo‘q")
    mapping = {
        "ad_campaign_id": ("change_ad_campaign_id", "RichAds kampaniya ID sini yuboring:"),
        "ad_link_template": ("change_ad_link_template", "Yangi link shablonini yuboring. {campaign_id} va {sub_id} o‘zgaruvchilarni saqlang:"),
        "ad_required": ("change_ad_required", "Bitta kino uchun nechta reklama kerak? (son):"),
        "ad_floor": ("change_ad_floor", "Minimal floor qiymatini yuboring (masalan, 0.01):"),
        "ad_lang": ("change_ad_lang", "Til kodini yuboring (masalan, uz, ru, en):"),
    }
    state, text = mapping[call.data]
    set_state(call.from_user.id, state)
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, text)

# =========================================================
# ADMIN: FOYDALANUVCHILAR
# =========================================================

@bot.message_handler(func=lambda m: m.text == "👥 Foydalanuvchilar")
def user_manage_start(message):
    if not is_admin(message.from_user.id):
        return
    set_state(message.from_user.id, "manage_user_id")
    bot.send_message(message.chat.id, "Boshqariladigan foydalanuvchi ID sini yuboring:")

def user_manage_keyboard(user_id: int):
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton("🚫 Bloklash", callback_data=f"user_block:{user_id}"),
        types.InlineKeyboardButton("✅ Blokdan chiqarish", callback_data=f"user_unblock:{user_id}"),
    )
    keyboard.add(
        types.InlineKeyboardButton("💬 Xabar yuborish", callback_data=f"reply_user:{user_id}"),
    )
    return keyboard

@bot.callback_query_handler(func=lambda call: call.data.startswith("user_block:"))
def user_block_callback(call):
    if not is_admin(call.from_user.id):
        return
    user_id = int(call.data.split(":", 1)[1])
    execute("UPDATE users SET is_blocked=1 WHERE user_id=?", (user_id,))
    bot.answer_callback_query(call.id, "Bloklandi.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("user_unblock:"))
def user_unblock_callback(call):
    if not is_admin(call.from_user.id):
        return
    user_id = int(call.data.split(":", 1)[1])
    execute("UPDATE users SET is_blocked=0 WHERE user_id=?", (user_id,))
    bot.answer_callback_query(call.id, "Blokdan chiqarildi.")

# =========================================================
# ADMIN: REKLAMA YUBORISH
# =========================================================

@bot.message_handler(func=lambda m: m.text == "📢 Reklama yuborish")
def broadcast_start(message):
    if not is_admin(message.from_user.id):
        return
    set_state(message.from_user.id, "broadcast_content")
    bot.send_message(message.chat.id, "Yuboriladigan kontentni yuboring (matn, rasm, video...)")

def broadcast_copy(admin_chat_id: int, source_message_id: int):
    users = execute("SELECT user_id FROM users WHERE is_blocked=0", fetchall=True)
    success = 0
    failed = 0
    status_msg = bot.send_message(admin_chat_id, f"📤 Yuborish boshlandi. Jami: {len(users)}")
    for i, row in enumerate(users, 1):
        try:
            bot.copy_message(row["user_id"], admin_chat_id, source_message_id)
            success += 1
        except Exception:
            failed += 1
        time.sleep(0.05)
        if i % 100 == 0:
            try:
                bot.edit_message_text(
                    f"📤 Jarayon: {i}/{len(users)}\n✅ {success} | ❌ {failed}",
                    admin_chat_id,
                    status_msg.message_id,
                )
            except:
                pass
    bot.edit_message_text(
        f"📊 <b>REKLAMA NATIJASI</b>\n\n✅ Yuborildi: <b>{success}</b>\n❌ Yuborilmadi: <b>{failed}</b>",
        admin_chat_id,
        status_msg.message_id,
    )

@bot.message_handler(content_types=["text", "photo", "video", "document", "animation"],
                     func=lambda m: is_admin(m.from_user.id) and get_state(m.from_user.id)[0] == "broadcast_content")
def broadcast_content_handler(message):
    clear_state(message.from_user.id)
    bot.send_message(message.chat.id, "📢 Reklama qabul qilindi. Yuborilmoqda...")
    broadcast_copy(message.chat.id, message.message_id)

# =========================================================
# ADMIN: STATISTIKA
# =========================================================

@bot.message_handler(func=lambda m: m.text == "📊 Statistika")
def admin_statistics(message):
    if not is_admin(message.from_user.id):
        return
    users = execute("SELECT COUNT(*) AS c FROM users", fetchone=True)["c"]
    blocked = execute("SELECT COUNT(*) AS c FROM users WHERE is_blocked=1", fetchone=True)["c"]
    movies = execute("SELECT COUNT(*) AS c FROM movies", fetchone=True)["c"]
    views = execute("SELECT COALESCE(SUM(views),0) AS c FROM movies", fetchone=True)["c"]
    ad_views = execute("SELECT COUNT(*) AS c FROM ad_views WHERE status='completed'", fetchone=True)["c"]

    bot.send_message(
        message.chat.id,
        "📊 <b>ADMIN STATISTIKA</b>\n\n"
        f"👥 Foydalanuvchilar: <b>{users}</b> (bloklangan: {blocked})\n"
        f"🎬 Kinolar: <b>{movies}</b>\n"
        f"👁 Ko‘rishlar: <b>{views}</b>\n"
        f"📣 Reklama tomoshalari: <b>{ad_views}</b>",
    )

# =========================================================
# UNIVERSAL MATN HANDLER — HOLATLAR
# =========================================================

@bot.message_handler(commands=["cancel"])
def cancel_handler(message):
    clear_state(message.from_user.id)
    keyboard = admin_keyboard() if is_admin(message.from_user.id) else main_keyboard(message.from_user.id)
    bot.send_message(message.chat.id, "❌ Amal bekor qilindi.", reply_markup=keyboard)

@bot.message_handler(content_types=["text"])
def text_state_handler(message):
    register_user(message.from_user)
    user_id = message.from_user.id
    text = (message.text or "").strip()
    state, data = get_state(user_id)

    if not state:
        if not is_admin(user_id):
            bot.send_message(
                message.chat.id,
                "Menyudan kerakli bo‘limni tanlang.",
                reply_markup=main_keyboard(user_id),
            )
        else:
            bot.send_message(
                message.chat.id,
                "Admin panel yoki menyudan foydalaning.",
                reply_markup=admin_keyboard() if is_admin(user_id) else main_keyboard(user_id),
            )
        return

    # --- KINO KODI ---
    if state == "waiting_movie_code":
        code = text
        movie = execute("SELECT * FROM movies WHERE code=?", (code,), fetchone=True)
        execute(
            "INSERT INTO search_log(user_id, code, found, searched_at) VALUES(?,?,?,?)",
            (user_id, code, 1 if movie else 0, now_text()),
        )
        clear_state(user_id)

        if not movie:
            bot.send_message(
                message.chat.id,
                "❌ Bunday kodli kino topilmadi.",
                reply_markup=main_keyboard(user_id),
            )
            return

        # Reklama kerakmi?
        if ad_required_for_movie(user_id, movie["id"]):
            send_ad_and_track(user_id, message.chat.id, movie["id"])
        else:
            send_movie_to_user(message.chat.id, user_id, movie)
        return

    # --- ADMINGA XABAR ---
    if state == "waiting_admin_message":
        msg_id = execute(
            "INSERT INTO messages(user_id, message_text, created_at) VALUES(?,?,?)",
            (user_id, text, now_text()),
        )
        clear_state(user_id)
        bot.send_message(message.chat.id, "✅ Xabaringiz adminga yuborildi.", reply_markup=main_keyboard(user_id))

        user = get_user(user_id)
        for admin_id in ADMIN_IDS:
            try:
                bot.send_message(
                    admin_id,
                    f"📩 <b>Yangi xabar #{msg_id}</b>\n\n"
                    f"👤 {safe(user['full_name'])} (<code>{user_id}</code>)\n"
                    f"💬 {safe(text)}",
                    reply_markup=types.InlineKeyboardMarkup().add(
                        types.InlineKeyboardButton("✍️ Javob yozish", callback_data=f"reply_user:{user_id}")
                    ),
                )
            except:
                pass
        return

    # --- ADMIN: KINO QO‘SHISH (caption) ---
    if state == "add_movie_caption" and is_admin(user_id):
        parts = data.split("|", 2)
        if len(parts) != 3:
            clear_state(user_id)
            return
        file_type, file_id, _ = parts
        title = text.splitlines()[0].strip()
        title = re.sub(r"^[🎬\s]+", "", title).strip() or "Nomsiz kino"
        set_state(user_id, "add_movie_code", f"{file_type}|{file_id}|{title}|{text}")
        bot.send_message(message.chat.id, "🔢 Kino uchun unikal kod yuboring:")
        return

    if state == "add_movie_code" and is_admin(user_id):
        code = text.replace(" ", "")
        if len(code) > 30:
            bot.send_message(message.chat.id, "❌ Kod juda uzun.")
            return
        existing = execute("SELECT title FROM movies WHERE code=?", (code,), fetchone=True)
        if existing:
            bot.send_message(message.chat.id, f"❌ Bu kod band. Kino: <b>{safe(existing['title'])}</b>")
            return
        parts = data.split("|", 3)
        if len(parts) != 4:
            clear_state(user_id)
            return
        file_type, file_id, title, caption = parts
        execute(
            "INSERT INTO movies(code, title, caption, file_id, file_type, created_at, added_by) VALUES(?,?,?,?,?,?,?)",
            (code, title, caption, file_id, file_type, now_text(), user_id),
        )
        clear_state(user_id)
        bot.send_message(message.chat.id, f"✅ <b>{safe(title)}</b> qo‘shildi. Kod: <code>{safe(code)}</code>",
                         reply_markup=admin_keyboard())
        return

    # --- ADMIN: KINO O‘CHIRISH ---
    if state == "delete_movie_code" and is_admin(user_id):
        movie = execute("SELECT * FROM movies WHERE code=?", (text,), fetchone=True)
        clear_state(user_id)
        if not movie:
            bot.send_message(message.chat.id, "❌ Bunday kodli kino topilmadi.", reply_markup=admin_keyboard())
            return
        execute("DELETE FROM movies WHERE id=?", (movie["id"],))
        bot.send_message(message.chat.id, f"✅ <b>{safe(movie['title'])}</b> o‘chirildi.", reply_markup=admin_keyboard())
        return

    # --- ADMIN: REKLAMA SOZLAMALARI ---
    if state.startswith("change_ad_") and is_admin(user_id):
        key_map = {
            "change_ad_campaign_id": "richads_campaign_id",
            "change_ad_link_template": "richads_link_template",
            "change_ad_required": "required_ads",
            "change_ad_floor": "richads_min_floor",
            "change_ad_lang": "richads_lang",
        }
        key = key_map.get(state)
        if key == "required_ads":
            if not text.isdigit() or int(text) < 1:
                bot.send_message(message.chat.id, "❌ Iltimos, musbat son kiriting.")
                return
        set_setting(key, text)
        clear_state(user_id)
        bot.send_message(message.chat.id, f"✅ {key} yangilandi.", reply_markup=admin_keyboard())
        return

    # --- ADMIN: FOYDALANUVCHI BOSHQARUVI ---
    if state == "manage_user_id" and is_admin(user_id):
        try:
            target_id = int(text)
        except ValueError:
            bot.send_message(message.chat.id, "❌ Faqat raqamli ID yuboring.")
            return
        target = get_user(target_id)
        clear_state(user_id)
        if not target:
            bot.send_message(message.chat.id, "❌ Foydalanuvchi topilmadi.")
            return
        status = "Bloklangan" if target["is_blocked"] else "Aktiv"
        bot.send_message(
            message.chat.id,
            f"👤 <b>FOYDALANUVCHI</b>\n\n"
            f"Ism: {safe(target['full_name'])}\n"
            f"ID: <code>{target_id}</code>\n"
            f"Username: {safe(target['username'] or 'Yo‘q')}\n"
            f"Holat: <b>{status}</b>",
            reply_markup=user_manage_keyboard(target_id),
        )
        return

    # --- ADMIN: USERGA JAVOB ---
    if state == "reply_to_user" and is_admin(user_id):
        try:
            target_id = int(data)
        except:
            clear_state(user_id)
            return
        try:
            bot.send_message(target_id, f"💬 <b>Admin javobi:</b>\n\n{safe(text)}")
            bot.send_message(message.chat.id, "✅ Xabar yuborildi.")
        except Exception as e:
            bot.send_message(message.chat.id, f"❌ Yuborilmadi: {safe(e)}")
        clear_state(user_id)
        return

    bot.send_message(message.chat.id, "❌ Noto‘g‘ri ma’lumot. Bekor qilish uchun /cancel")

# =========================================================
# ADMIN: VIDEO/DOCUMENT QABUL QILISH
# =========================================================

@bot.message_handler(content_types=["video", "document"],
                     func=lambda m: is_admin(m.from_user.id) and get_state(m.from_user.id)[0] == "add_movie_video")
def add_movie_video(message):
    state, _ = get_state(message.from_user.id)
    if message.content_type == "video":
        file_id = message.video.file_id
        file_type = "video"
    else:
        file_id = message.document.file_id
        file_type = "document"
    set_state(message.from_user.id, "add_movie_caption", f"{file_type}|{file_id}")
    bot.send_message(message.chat.id,
                     "📝 Kino captionini yuboring (birinchi qatorda nom bo‘lsin).")

# =========================================================
# FLASK: WEBHOOK VA RICHADS POSTBACK
# =========================================================

@app.get("/")
def health():
    return jsonify({"ok": True, "service": "kino-bot"})

@app.get("/health")
def health_check():
    return jsonify({"ok": True}), 200

@app.post(WEBHOOK_PATH)
def telegram_webhook():
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        return jsonify({"ok": False, "error": "invalid content-type"}), 415
    try:
        update_json = request.get_json(force=True)
        update = telebot.types.Update.de_json(update_json)
        bot.process_new_updates([update])
        return jsonify({"ok": True}), 200
    except Exception:
        logging.exception("Webhook update qayta ishlashda xato")
        return jsonify({"ok": False}), 500

@app.route("/richads/callback")
def richads_callback():
    click_id = request.args.get("click_id")
    secret = request.args.get("secret", "")
    if secret != RICHADS_POSTBACK_SECRET:
        return jsonify({"error": "unauthorized"}), 403
    if not click_id:
        return jsonify({"error": "missing click_id"}), 400

    success = process_ad_completion(click_id)
    if success:
        return jsonify({"status": "ok"}), 200
    else:
        return jsonify({"status": "not_found_or_already_completed"}), 200

# =========================================================
# WEBHOOK SOZLASH VA ISHGA TUSHIRISH
# =========================================================

def configure_webhook():
    init_db()

    if not RENDER_EXTERNAL_URL:
        logging.warning(
            "RENDER_EXTERNAL_URL topilmadi. Render Web Service deploy bo‘lganda avtomatik beriladi."
        )
        return

    webhook_url = f"{RENDER_EXTERNAL_URL}{WEBHOOK_PATH}"

    try:
        bot.remove_webhook()
        time.sleep(0.5)
        result = bot.set_webhook(
            url=webhook_url,
            allowed_updates=[
                "message",
                "callback_query",
                "my_chat_member",
                "chat_member",
            ],
            drop_pending_updates=True,
        )
        logging.info("Webhook o‘rnatildi: %s | result=%s", webhook_url, result)
    except Exception:
        logging.exception("Webhook o‘rnatishda xato")

configure_webhook()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
