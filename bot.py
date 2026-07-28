import html, logging, os, re, sqlite3, threading, time, uuid, requests
from datetime import datetime
import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException
from flask import Flask, request, jsonify

# ---------- SOZLAMALAR ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
DB_NAME = "kino_bot.db"

# RichAds / AdX1 API sozlamalari
AD_API_URL = os.getenv("AD_API_URL", "http://15068.xml.adx1.com/telegram-mb")
PUBLISHER_ID = os.getenv("PUBLISHER_ID", "792361")
WIDGET_ID = os.getenv("WIDGET_ID", "351352")
BID_FLOOR = float(os.getenv("BID_FLOOR", "0.0001"))
PRODUCTION = os.getenv("PRODUCTION", "true").lower() == "true"

if not BOT_TOKEN or ":" not in BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN noto‘g‘ri")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=True)

# ---------- BAZA ----------
db_lock = threading.RLock()
def db_connect():
    c = sqlite3.connect(DB_NAME, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c
def execute(q, p=(), fetchone=False, fetchall=False):
    with db_lock:
        conn = db_connect()
        try:
            cur = conn.execute(q, p)
            conn.commit()
            if fetchone: return cur.fetchone()
            if fetchall: return cur.fetchall()
            return cur.lastrowid
        finally: conn.close()

def init_db():
    execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, full_name TEXT DEFAULT '', username TEXT DEFAULT '',
        is_blocked INTEGER DEFAULT 0, joined_at TEXT, last_active TEXT,
        free_movies_count INTEGER DEFAULT 0,
        pending_ads_required INTEGER DEFAULT 0,
        pending_ads_completed INTEGER DEFAULT 0,
        pending_movie_id INTEGER
    )""")
    for col in ["free_movies_count", "pending_ads_required", "pending_ads_completed", "pending_movie_id"]:
        try: execute(f"ALTER TABLE users ADD COLUMN {col} INTEGER DEFAULT 0")
        except: pass

    execute("""CREATE TABLE IF NOT EXISTS movies (
        id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE, title TEXT,
        caption TEXT, file_id TEXT, file_type TEXT DEFAULT 'video',
        views INTEGER DEFAULT 0, created_at TEXT, added_by INTEGER
    )""")
    execute("""CREATE TABLE IF NOT EXISTS channels (
        id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT,
        username TEXT UNIQUE, invite_link TEXT, chat_id INTEGER
    )""")
    execute("""CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
        message_text TEXT, created_at TEXT, answered INTEGER DEFAULT 0
    )""")
    execute("""CREATE TABLE IF NOT EXISTS states (
        user_id INTEGER PRIMARY KEY, state TEXT, data TEXT DEFAULT ''
    )""")
    execute("""CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT
    )""")
    execute("""CREATE TABLE IF NOT EXISTS watch_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
        movie_id INTEGER, watched_at TEXT
    )""")
    execute("""CREATE TABLE IF NOT EXISTS search_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
        code TEXT, found INTEGER, searched_at TEXT
    )""")
    execute("""CREATE TABLE IF NOT EXISTS ad_views (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
        movie_id INTEGER, click_id TEXT UNIQUE, link_url TEXT,
        status TEXT DEFAULT 'pending', created_at TEXT, clicked_at TEXT
    )""")
    execute("""CREATE TABLE IF NOT EXISTS join_requests (
        user_id INTEGER, chat_id INTEGER, request_date TEXT,
        PRIMARY KEY (user_id, chat_id)
    )""")

    # standart sozlamalar
    for k,v in [
        ("free_movies_limit", "3"),
        ("ads_after_limit", "2"),
        ("ad_publisher_id", PUBLISHER_ID),
        ("ad_widget_id", WIDGET_ID),
        ("ad_bid_floor", str(BID_FLOOR)),
        ("ad_production", str(PRODUCTION).lower()),
    ]:
        execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k,v))

init_db()

# ---------- YORDAMCHILAR ----------
def now_text(): return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
def safe(v): return html.escape(str(v or ""))
def is_admin(uid): return uid in ADMIN_IDS
def get_setting(key):
    r = execute("SELECT value FROM settings WHERE key=?", (key,), fetchone=True)
    return r["value"] if r else ""
def set_setting(key,val):
    execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key,val))

def set_state(uid,state,data=""):
    execute("INSERT INTO states(user_id,state,data) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET state=excluded.state, data=excluded.data", (uid,state,data))
def get_state(uid):
    r = execute("SELECT state,data FROM states WHERE user_id=?",(uid,), fetchone=True)
    return (None,"") if not r else (r["state"], r["data"])
def clear_state(uid): execute("DELETE FROM states WHERE user_id=?",(uid,))

def register_user(user):
    name = " ".join(filter(None,[user.first_name, user.last_name])).strip()
    uname = f"@{user.username}" if user.username else ""
    execute("""INSERT INTO users(user_id,full_name,username,joined_at,last_active)
               VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
               full_name=excluded.full_name, username=excluded.username,
               last_active=excluded.last_active""",
            (user.id, name, uname, now_text(), now_text()))
def get_user(uid): return execute("SELECT * FROM users WHERE user_id=?",(uid,), fetchone=True)

# ========== MAJBURIY OBUNA ==========
def get_channels():
    return execute("SELECT * FROM channels ORDER BY id", fetchall=True)

def check_subscription(user_id):
    for ch in get_channels():
        try:
            if ch["username"] and ch["username"].startswith("private_"):
                if not ch["chat_id"]: continue
                chat_id = ch["chat_id"]
            elif ch["username"]: chat_id = ch["username"]
            elif ch["chat_id"]:  chat_id = ch["chat_id"]
            else: continue
            member = bot.get_chat_member(chat_id, user_id)
            if member.status not in ("creator","administrator","member"):
                return False
        except ApiTelegramException as e:
            logging.warning(f"Obuna tekshirishda kanal {ch['id']}: {e}")
        except Exception as e:
            logging.error(f"Kutilmagan xato: {e}")
    return True

def subscription_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=1)
    for ch in get_channels():
        title = ch.get("title") or "Kanal"
        if ch.get("username") and not ch["username"].startswith("private_"):
            name = ch["username"].replace("@","").strip()
            if name: kb.add(types.InlineKeyboardButton(f"📢 {title}", url=f"https://t.me/{name}"))
        elif ch.get("invite_link"):
            kb.add(types.InlineKeyboardButton(f"📩 {title} – so‘rov yuborish", url=ch["invite_link"]))
    kb.add(types.InlineKeyboardButton("✅ Obunani tekshirish", callback_data="check_subscription"))
    return kb

def send_subscription_required(chat_id):
    kb = subscription_keyboard()
    bot.send_message(chat_id,
        "🔐 <b>Botdan foydalanish uchun quyidagi kanallarga obuna bo'ling:</b>\n\n"
        "📢 <b>Ochiq kanallar</b> — havola orqali kirib <b>Join</b> tugmasini bosing.\n"
        "📩 <b>Maxfiy kanallar</b> — <b>So'rov yuborish</b> tugmasini bosing.\n\n"
        "So‘ngra <b>✅ Obunani tekshirish</b> tugmasini bosing.",
        reply_markup=kb)

@bot.chat_join_request_handler()
def handle_join_request(req: types.ChatJoinRequest):
    try:
        bot.approve_chat_join_request(req.chat.id, req.from_user.id)
        execute("INSERT OR IGNORE INTO join_requests VALUES(?,?,?)",
                (req.from_user.id, req.chat.id, now_text()))
    except Exception as e:
        logging.error(f"Join request xato: {e}")

@bot.callback_query_handler(func=lambda c: c.data == "check_subscription")
def check_sub_cb(call):
    register_user(call.from_user)
    if check_subscription(call.from_user.id):
        bot.answer_callback_query(call.id, "Obuna tasdiqlandi ✅")
        try: bot.edit_message_text("✅ <b>Obunangiz tasdiqlandi.</b>", call.message.chat.id, call.message.message_id)
        except: pass
        open_main_menu(call.message.chat.id, call.from_user.id)
    else:
        bot.answer_callback_query(call.id, "Hali barcha kanallarga obuna bo‘lmadingiz.", show_alert=True)

# ---------- MENYULAR ----------
def main_keyboard(uid):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add("🔎 Kino kodi", "🔥 Eng mashhur kinolar")
    kb.add("📊 Statistika", "💬 Adminga xabar", "👤 Profil")
    if is_admin(uid): kb.add("🛠 Admin panel")
    return kb

def admin_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add("🎬 Kinolar", "⚙️ Reklama sozlamalari")
    kb.add("👥 Foydalanuvchilar", "📡 Kanallar")
    kb.add("📢 Reklama yuborish", "📊 Statistika")
    kb.add("🏠 Asosiy menyu")
    return kb

def movies_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add("➕ Kino qo‘shish", "🗑 Kino o‘chirish")
    kb.add("📋 Barcha kinolar", "🔙 Admin panelga qaytish")
    return kb

def open_main_menu(chat_id, uid):
    bot.send_message(chat_id, "🎬 <b>Kino botga xush kelibsiz!</b>\nKerakli bo‘limni tanlang:",
                     reply_markup=main_keyboard(uid))

# ---------- KIRISH NAZORATI ----------
def ensure_access(message):
    register_user(message.from_user)
    uid = message.from_user.id
    user = get_user(uid)
    if user and user["is_blocked"]:
        bot.send_message(message.chat.id, "🚫 Siz botdan bloklangansiz.")
        return False
    if not check_subscription(uid):
        send_subscription_required(message.chat.id)
        return False
    return True

# ---------- START / ADMIN ----------
@bot.message_handler(commands=["start"])
def start_handler(message):
    if not ensure_access(message): return
    clear_state(message.from_user.id)
    open_main_menu(message.chat.id, message.from_user.id)

@bot.message_handler(commands=["admin"])
def admin_cmd(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "❌ Siz admin emassiz.")
        return
    clear_state(message.from_user.id)
    bot.send_message(message.chat.id, "🛠 <b>Admin panel</b>", reply_markup=admin_keyboard())

@bot.message_handler(func=lambda m: m.text == "🏠 Asosiy menyu")
def home_btn(message):
    if not ensure_access(message): return
    clear_state(message.from_user.id)
    open_main_menu(message.chat.id, message.from_user.id)

@bot.message_handler(func=lambda m: m.text == "🛠 Admin panel")
def admin_panel_btn(message):
    if not is_admin(message.from_user.id): return
    clear_state(message.from_user.id)
    bot.send_message(message.chat.id, "🛠 <b>Admin panel</b>", reply_markup=admin_keyboard())

# ---------- RichAds REKLAMA FUNKSIYALARI ----------
def fetch_ad_from_adx1(user_id):
    """RichAds API orqali reklama oladi.
    Qaytaradi: (image_url, caption, link_url, button_text, click_id) yoki None."""
    payload = {
        "language_code": "en",
        "publisher_id": get_setting("ad_publisher_id"),
        "widget_id": get_setting("ad_widget_id"),
        "bid_floor": float(get_setting("ad_bid_floor")),
        "telegram_id": str(user_id),
        "production": get_setting("ad_production") == "true"
    }
    try:
        resp = requests.post(AD_API_URL, json=payload, timeout=10)
        if resp.status_code != 200:
            logging.error(f"Reklama API xatosi: {resp.status_code}")
            return None
        data = resp.json()
        if not isinstance(data, list) or len(data) == 0:
            logging.error("Reklama ro‘yxati bo‘sh")
            return None
        ad = data[0]
        image_url = ad.get("image")
        caption = ad.get("message", "")
        link_url = ad.get("link")
        button_text = ad.get("button", "Batafsil")
        if not image_url or not link_url:
            logging.error("Reklama ma'lumoti to‘liq emas")
            return None
        click_id = str(uuid.uuid4())
        return image_url, caption, link_url, button_text, click_id
    except Exception as e:
        logging.exception("Reklama so‘rovda xato")
        return None

def send_ad_message(chat_id, user_id, movie_id):
    """Foydalanuvchiga bitta reklama yuboradi (rasm + inline tugma)."""
    ad_data = fetch_ad_from_adx1(user_id)
    if not ad_data:
        bot.send_message(chat_id, "⚠️ Hozircha reklama topilmadi, keyinroq urinib ko‘ring.")
        return False
    image_url, caption, link_url, button_text, click_id = ad_data

    # Reklama yozuvini kiritamiz
    execute("INSERT INTO ad_views(user_id, movie_id, click_id, link_url, status, created_at) VALUES(?,?,?,?,'pending',?)",
            (user_id, movie_id, click_id, link_url, now_text()))

    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(f"📣 {button_text}", callback_data=f"ad_click:{click_id}"))

    caption = caption if caption else "📺 Kinoni ochish uchun reklamani ko‘ring va tugmani bosing."
    try:
        bot.send_photo(chat_id, image_url, caption=caption, reply_markup=kb)
        return True
    except Exception as e:
        logging.exception("Rasm yuborishda xato")
        execute("DELETE FROM ad_views WHERE click_id=?", (click_id,))
        return False

@bot.callback_query_handler(func=lambda c: c.data.startswith("ad_click:"))
def ad_click_handler(call):
    """Foydalanuvchi reklama tugmasini bosganda ishlaydi."""
    click_id = call.data.split(":", 1)[1]
    ad = execute("SELECT * FROM ad_views WHERE click_id=? AND status='pending'", (click_id,), fetchone=True)
    if not ad:
        bot.answer_callback_query(call.id, "Bu reklama allaqachon ko‘rilgan.")
        return

    execute("UPDATE ad_views SET status='completed', clicked_at=? WHERE id=?", (now_text(), ad["id"]))
    user_id = call.from_user.id
    user = get_user(user_id)
    if not user or not user["pending_ads_required"]:
        bot.answer_callback_query(call.id)
        return

    new_completed = (user["pending_ads_completed"] or 0) + 1
    execute("UPDATE users SET pending_ads_completed=? WHERE user_id=?", (new_completed, user_id))

    # Yetarli reklama ko‘rilgan bo‘lsa, kinoni yuboramiz
    if new_completed >= user["pending_ads_required"]:
        movie = execute("SELECT * FROM movies WHERE id=?", (user["pending_movie_id"],), fetchone=True)
        if movie:
            try:
                send_movie_to_user(user_id, user_id, movie)
            except Exception:
                logging.exception("Kino yuborishda xato")
        # Holatni tozalash
        execute("UPDATE users SET pending_ads_required=0, pending_ads_completed=0, pending_movie_id=NULL WHERE user_id=?", (user_id,))

    # Foydalanuvchini reklama saytiga yo‘naltiramiz
    bot.answer_callback_query(call.id, url=ad["link_url"])
# =========================================================
# 2-QISM: Asosiy handlerlar, admin, universal handler
# =========================================================

def send_movie_to_user(chat_id: int, user_id: int, movie):
    """Kinoni yuboradi. Muvaffaqiyatli yuborilsa ko‘rishlar hisobini yangilaydi."""
    caption = (
        f"{movie['caption']}\n\n"
        f"🔢 Kod: <code>{safe(movie['code'])}</code>\n"
        f"👁 Ko‘rilgan: <b>{movie['views'] + 1}</b> marta"
    )
    try:
        if movie["file_type"] == "video":
            bot.send_video(chat_id, movie["file_id"], caption=caption, supports_streaming=True)
        else:
            bot.send_document(chat_id, movie["file_id"], caption=caption)
    except Exception as e:
        logging.exception(f"Kino yuborishda xato (movie_id={movie['id']}): {e}")
        bot.send_message(chat_id, "❌ Kinoni yuklashda xatolik yuz berdi. Keyinroq urinib ko‘ring.")
        return

    # Yuborish muvaffaqiyatli bo‘lsa, hisoblagichlarni yangilash
    execute("UPDATE movies SET views = views + 1 WHERE id = ?", (movie["id"],))
    execute("INSERT INTO watch_log(user_id, movie_id, watched_at) VALUES(?,?,?)",
            (user_id, movie["id"], now_text()))

    # Bepul kinolar hisobi (reklamasiz yuborilganda)
    user = get_user(user_id)
    if not user.get("pending_ads_required") or user["pending_ads_required"] == 0:
        execute("UPDATE users SET free_movies_count = free_movies_count + 1 WHERE user_id = ?", (user_id,))


# ========== KINO QIDIRISH, MASHHURLAR, STATISTIKA, PROFIL ==========
@bot.message_handler(func=lambda m: m.text == "🔎 Kino kodi")
def ask_movie_code(message):
    if not ensure_access(message): return
    set_state(message.from_user.id, "waiting_movie_code")
    bot.send_message(message.chat.id, "🔢 <b>Kino kodini yuboring:</b>\nMasalan: <code>145</code>")

@bot.message_handler(func=lambda m: m.text == "🔥 Eng mashhur kinolar")
def popular_movies(message):
    if not ensure_access(message): return
    movies = execute("SELECT code, title, views FROM movies ORDER BY views DESC, id DESC LIMIT 10", fetchall=True)
    if not movies:
        bot.send_message(message.chat.id, "Hozircha kinolar mavjud emas.")
        return
    medals = ["🥇","🥈","🥉"]
    lines = ["🔥 <b>ENG MASHHUR KINOLAR</b>\n"]
    for i, m in enumerate(movies):
        icon = medals[i] if i<3 else f"{i+1}."
        lines.append(f"{icon} <b>{safe(m['title'])}</b>\n   Kod: <code>{safe(m['code'])}</code> | 👁 {m['views']}")
    bot.send_message(message.chat.id, "\n\n".join(lines))

@bot.message_handler(func=lambda m: m.text == "📊 Statistika")
def public_statistics(message):
    if not ensure_access(message): return
    users = execute("SELECT COUNT(*) AS c FROM users", fetchone=True)["c"]
    movies = execute("SELECT COUNT(*) AS c FROM movies", fetchone=True)["c"]
    views = execute("SELECT COALESCE(SUM(views),0) AS c FROM movies", fetchone=True)["c"]
    bot.send_message(message.chat.id, f"📊 <b>BOT STATISTIKASI</b>\n\n🎬 Kinolar: <b>{movies}</b>\n👁 Ko‘rishlar: <b>{views}</b>\n👥 Foydalanuvchilar: <b>{users}</b>")

@bot.message_handler(func=lambda m: m.text == "👤 Profil")
def profile_handler(message):
    if not ensure_access(message): return
    register_user(message.from_user)
    user = get_user(message.from_user.id)
    watched = execute("SELECT COUNT(*) AS c FROM watch_log WHERE user_id=?",(message.from_user.id,), fetchone=True)["c"]
    bot.send_message(message.chat.id,
        f"👤 <b>PROFIL</b>\n\n"
        f"Ism: <b>{safe(user['full_name'])}</b>\n"
        f"🆔 ID: <code>{user['user_id']}</code>\n"
        f"🔗 Username: {safe(user['username'] or 'Yo‘q')}\n"
        f"🎬 Ko‘rilgan kinolar: <b>{watched}</b>\n"
        f"📆 Qo‘shilgan: <b>{safe(user['joined_at'])}</b>")

# ========== ADMINGA XABAR ==========
@bot.message_handler(func=lambda m: m.text == "💬 Adminga xabar")
def ask_admin_msg(message):
    if not ensure_access(message): return
    set_state(message.from_user.id, "waiting_admin_message")
    bot.send_message(message.chat.id, "💬 Adminga yuboriladigan xabarni yozing:")

# ========== ADMIN: KINOLAR BO‘LIMI ==========
@bot.message_handler(func=lambda m: m.text == "🎬 Kinolar")
def movies_section(message):
    if not is_admin(message.from_user.id): return
    clear_state(message.from_user.id)
    bot.send_message(message.chat.id, "🎬 Kinolar bo‘limi", reply_markup=movies_keyboard())

@bot.message_handler(func=lambda m: m.text == "➕ Kino qo‘shish")
def add_movie_start(message):
    if not is_admin(message.from_user.id): return
    set_state(message.from_user.id, "add_movie_video")
    bot.send_message(message.chat.id, "🎥 Kino videosini (yoki hujjat) yuboring:")

@bot.message_handler(func=lambda m: m.text == "🗑 Kino o‘chirish")
def delete_movie_start(message):
    if not is_admin(message.from_user.id): return
    set_state(message.from_user.id, "delete_movie_code")
    bot.send_message(message.chat.id, "🗑 O‘chiriladigan kino kodini yuboring:")

@bot.message_handler(func=lambda m: m.text == "📋 Barcha kinolar")
def all_movies_handler(message):
    if not is_admin(message.from_user.id): return
    movies = execute("SELECT code, title, views FROM movies ORDER BY id DESC LIMIT 100", fetchall=True)
    if not movies:
        bot.send_message(message.chat.id, "Kinolar mavjud emas.")
        return
    text = ["📋 <b>BARCHA KINOLAR</b>\n"]
    for i, m in enumerate(movies, 1):
        text.append(f"{i}. <b>{safe(m['title'])}</b>\n   Kod: <code>{safe(m['code'])}</code> | 👁 {m['views']}")
    full = "\n\n".join(text)
    for start in range(0, len(full), 3900):
        bot.send_message(message.chat.id, full[start:start+3900])

@bot.message_handler(func=lambda m: m.text == "🔙 Admin panelga qaytish")
def back_to_admin_panel(message):
    if not is_admin(message.from_user.id): return
    clear_state(message.from_user.id)
    bot.send_message(message.chat.id, "🛠 <b>Admin panel</b>", reply_markup=admin_keyboard())

# ========== ADMIN: KANALLAR ==========
@bot.message_handler(func=lambda m: m.text == "📡 Kanallar")
def channels_admin(message):
    if not is_admin(message.from_user.id): return
    channels = get_channels()
    lines = ["📡 <b>MAJBURIY OBUNA KANALLARI</b>\n"]
    if channels:
        for ch in channels:
            ch_type = "🔒 Maxfiy" if ch["username"] and ch["username"].startswith("private_") else "📢 Ochiq"
            lines.append(f"ID: <code>{ch['id']}</code> | {ch_type}: <b>{safe(ch['title'])}</b> | {safe(ch['username'])}")
    else:
        lines.append("Hozircha kanal qo‘shilmagan.")
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("➕ Kanal qo‘shish", callback_data="channel_add"),
           types.InlineKeyboardButton("🗑 Kanal o‘chirish", callback_data="channel_delete"))
    bot.send_message(message.chat.id, "\n\n".join(lines), reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data == "channel_add")
def channel_add_callback(call):
    if not is_admin(call.from_user.id): return
    set_state(call.from_user.id, "channel_add_username")
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "➕ Kanal username yoki link yuboring:\nMasalan: @kanal yoki https://t.me/kanal")

@bot.callback_query_handler(func=lambda c: c.data == "channel_delete")
def channel_delete_callback(call):
    if not is_admin(call.from_user.id): return
    set_state(call.from_user.id, "channel_delete_value")
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "🗑 O‘chiriladigan kanalning ID raqamini yoki username/linkini yuboring:")

# ========== ADMIN: REKLAMA SOZLAMALARI ==========
@bot.message_handler(func=lambda m: m.text == "⚙️ Reklama sozlamalari")
def ad_settings(message):
    if not is_admin(message.from_user.id): return
    text = (
        "⚙️ <b>REKLAMA SOZLAMALARI</b>\n\n"
        f"Publisher ID: <code>{safe(get_setting('ad_publisher_id'))}</code>\n"
        f"Widget ID: <code>{safe(get_setting('ad_widget_id'))}</code>\n"
        f"Bid Floor: <b>{get_setting('ad_bid_floor')}</b>\n"
        f"Production: <b>{get_setting('ad_production')}</b>\n"
        f"Bepul kinolar soni: <b>{get_setting('free_movies_limit')}</b>\n"
        f"Reklama soni (limitdan keyin): <b>{get_setting('ads_after_limit')}</b>"
    )
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("Publisher ID", callback_data="ad_publisher_id"),
           types.InlineKeyboardButton("Widget ID", callback_data="ad_widget_id"),
           types.InlineKeyboardButton("Bid Floor", callback_data="ad_bid_floor"),
           types.InlineKeyboardButton("Production", callback_data="ad_production"),
           types.InlineKeyboardButton("Bepul kino soni", callback_data="ad_free_limit"),
           types.InlineKeyboardButton("Reklama soni", callback_data="ad_ads_count"))
    bot.send_message(message.chat.id, text, reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data in [
    "ad_publisher_id","ad_widget_id","ad_bid_floor","ad_production","ad_free_limit","ad_ads_count"])
def ad_callback(call):
    if not is_admin(call.from_user.id): return bot.answer_callback_query(call.id, "Ruxsat yo‘q")
    mapping = {
        "ad_publisher_id": ("change_ad_publisher_id", "Yangi Publisher ID yuboring:"),
        "ad_widget_id": ("change_ad_widget_id", "Yangi Widget ID yuboring:"),
        "ad_bid_floor": ("change_ad_bid_floor", "Yangi Bid Floor qiymatini yuboring (masalan 0.01):"),
        "ad_production": ("change_ad_production", "Production rejimini kiriting (true/false):"),
        "ad_free_limit": ("change_ad_free_limit", "Bepul kinolar sonini kiriting (masalan 3):"),
        "ad_ads_count": ("change_ad_ads_count", "Limitdan keyin nechta reklama ko‘rilsin? (son):"),
    }
    state, msg = mapping[call.data]
    set_state(call.from_user.id, state)
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, msg)

# ========== ADMIN: FOYDALANUVCHILAR ==========
@bot.message_handler(func=lambda m: m.text == "👥 Foydalanuvchilar")
def user_manage_start(message):
    if not is_admin(message.from_user.id): return
    set_state(message.from_user.id, "manage_user_id")
    bot.send_message(message.chat.id, "Boshqariladigan foydalanuvchi Telegram ID sini yuboring:")

def user_manage_keyboard(user_id):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("🚫 Bloklash", callback_data=f"user_block:{user_id}"),
           types.InlineKeyboardButton("✅ Blokdan chiqarish", callback_data=f"user_unblock:{user_id}"))
    kb.add(types.InlineKeyboardButton("💬 Xabar yuborish", callback_data=f"reply_user:{user_id}"))
    return kb

@bot.callback_query_handler(func=lambda c: c.data.startswith("user_block:"))
def block_user(call):
    if not is_admin(call.from_user.id): return
    uid = int(call.data.split(":",1)[1])
    execute("UPDATE users SET is_blocked=1 WHERE user_id=?", (uid,))
    bot.answer_callback_query(call.id, "Bloklandi.")

@bot.callback_query_handler(func=lambda c: c.data.startswith("user_unblock:"))
def unblock_user(call):
    if not is_admin(call.from_user.id): return
    uid = int(call.data.split(":",1)[1])
    execute("UPDATE users SET is_blocked=0 WHERE user_id=?", (uid,))
    bot.answer_callback_query(call.id, "Blokdan chiqarildi.")

# ========== ADMIN: REKLAMA YUBORISH ==========
@bot.message_handler(func=lambda m: m.text == "📢 Reklama yuborish")
def broadcast_start(message):
    if not is_admin(message.from_user.id): return
    set_state(message.from_user.id, "broadcast_content")
    bot.send_message(message.chat.id, "Yuboriladigan kontentni yuboring (matn, rasm, video...).")

def broadcast_copy(admin_chat_id, source_msg_id):
    users = execute("SELECT user_id FROM users WHERE is_blocked=0", fetchall=True)
    success, failed = 0, 0
    st = bot.send_message(admin_chat_id, f"📤 Yuborish boshlandi. Jami: {len(users)}")
    for i, row in enumerate(users, 1):
        try:
            bot.copy_message(row["user_id"], admin_chat_id, source_msg_id)
            success += 1
        except:
            failed += 1
        time.sleep(0.05)
        if i % 100 == 0:
            try: bot.edit_message_text(f"📤 {i}/{len(users)}\n✅ {success} | ❌ {failed}", admin_chat_id, st.message_id)
            except: pass
    bot.edit_message_text(f"📊 <b>REKLAMA NATIJASI</b>\n\n✅ Yuborildi: <b>{success}</b>\n❌ Yuborilmadi: <b>{failed}</b>", admin_chat_id, st.message_id)

@bot.message_handler(content_types=["text","photo","video","document","animation"],
                     func=lambda m: is_admin(m.from_user.id) and get_state(m.from_user.id)[0] == "broadcast_content")
def broadcast_content_handler(message):
    clear_state(message.from_user.id)
    bot.send_message(message.chat.id, "📢 Reklama yuborilmoqda...")
    threading.Thread(target=broadcast_copy, args=(message.chat.id, message.message_id), daemon=True).start()

# ========== ADMIN STATISTIKA ==========
@bot.message_handler(func=lambda m: m.text == "📊 Statistika")
def admin_statistics(message):
    if not is_admin(message.from_user.id): return
    users = execute("SELECT COUNT(*) AS c FROM users", fetchone=True)["c"]
    blocked = execute("SELECT COUNT(*) AS c FROM users WHERE is_blocked=1", fetchone=True)["c"]
    movies = execute("SELECT COUNT(*) AS c FROM movies", fetchone=True)["c"]
    views = execute("SELECT COALESCE(SUM(views),0) AS c FROM movies", fetchone=True)["c"]
    ad_views = execute("SELECT COUNT(*) AS c FROM ad_views WHERE status='completed'", fetchone=True)["c"]
    bot.send_message(message.chat.id,
        f"📊 <b>ADMIN STATISTIKA</b>\n\n"
        f"👥 Foydalanuvchilar: <b>{users}</b> (bloklangan: {blocked})\n"
        f"🎬 Kinolar: <b>{movies}</b>\n"
        f"👁 Ko‘rishlar: <b>{views}</b>\n"
        f"📣 Reklama bosilgan: <b>{ad_views}</b>")

# ========== UNIVERSAL MATN HANDLER ==========
@bot.message_handler(commands=["cancel"])
def cancel_handler(message):
    clear_state(message.from_user.id)
    kb = admin_keyboard() if is_admin(message.from_user.id) else main_keyboard(message.from_user.id)
    bot.send_message(message.chat.id, "❌ Amal bekor qilindi.", reply_markup=kb)

@bot.message_handler(content_types=["text"])
def text_state_handler(message):
    register_user(message.from_user)
    user_id = message.from_user.id
    text = message.text.strip()
    state, data = get_state(user_id)

    if not state:
        if not is_admin(user_id):
            bot.send_message(message.chat.id, "Menyudan kerakli bo‘limni tanlang.", reply_markup=main_keyboard(user_id))
        else:
            bot.send_message(message.chat.id, "Admin panel yoki menyudan foydalaning.", reply_markup=admin_keyboard())
        return

    # --- KINO KODI ---
    if state == "waiting_movie_code":
        code = text
        movie = execute("SELECT * FROM movies WHERE code=?", (code,), fetchone=True)
        execute("INSERT INTO search_log(user_id, code, found, searched_at) VALUES(?,?,?,?)", (user_id, code, 1 if movie else 0, now_text()))
        clear_state(user_id)
        if not movie:
            bot.send_message(message.chat.id, "❌ Bunday kodli kino topilmadi.", reply_markup=main_keyboard(user_id))
            return
        user = get_user(user_id)
        free_limit = int(get_setting("free_movies_limit"))
        free_count = user["free_movies_count"] or 0
        if free_count < free_limit:
            send_movie_to_user(message.chat.id, user_id, movie)
        else:
            ads_needed = int(get_setting("ads_after_limit"))
            execute("UPDATE users SET pending_ads_required=?, pending_ads_completed=0, pending_movie_id=? WHERE user_id=?",
                    (ads_needed, movie["id"], user_id))
            send_ad_message(message.chat.id, user_id, movie["id"])
        return

    # --- ADMINGA XABAR ---
    if state == "waiting_admin_message":
        msg_id = execute("INSERT INTO messages(user_id, message_text, created_at) VALUES(?,?,?)", (user_id, text, now_text()))
        clear_state(user_id)
        bot.send_message(message.chat.id, "✅ Xabaringiz adminga yuborildi.", reply_markup=main_keyboard(user_id))
        user = get_user(user_id)
        for aid in ADMIN_IDS:
            try:
                bot.send_message(aid,
                    f"📩 <b>Yangi xabar #{msg_id}</b>\n\n"
                    f"👤 {safe(user['full_name'])} (<code>{user_id}</code>)\n"
                    f"💬 {safe(text)}",
                    reply_markup=types.InlineKeyboardMarkup().add(
                        types.InlineKeyboardButton("✍️ Javob yozish", callback_data=f"reply_user:{user_id}")))
            except: pass
        return

    # --- ADMIN: KINO QO‘SHISH (caption) ---
    if state == "add_movie_caption" and is_admin(user_id):
        parts = data.split("|", 1)   # file_type|file_id
        if len(parts) != 2:
            clear_state(user_id)
            bot.send_message(message.chat.id, "❌ Ma'lumot buzilgan. Qaytadan urinib ko'ring.")
            return
        file_type, file_id = parts
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
        if execute("SELECT id FROM movies WHERE code=?", (code,), fetchone=True):
            bot.send_message(message.chat.id, "❌ Bu kod band. Boshqa kod yuboring.")
            return
        parts = data.split("|", 3)   # file_type|file_id|title|caption
        if len(parts) != 4:
            clear_state(user_id)
            bot.send_message(message.chat.id, "❌ Ma'lumot buzilgan.")
            return
        file_type, file_id, title, caption = parts
        execute("INSERT INTO movies(code,title,caption,file_id,file_type,created_at,added_by) VALUES(?,?,?,?,?,?,?)",
                (code, title, caption, file_id, file_type, now_text(), user_id))
        clear_state(user_id)
        bot.send_message(message.chat.id, f"✅ <b>{safe(title)}</b> qo‘shildi. Kod: <code>{safe(code)}</code>", reply_markup=admin_keyboard())
        return

    # --- ADMIN: KINO O‘CHIRISH ---
    if state == "delete_movie_code" and is_admin(user_id):
        movie = execute("SELECT * FROM movies WHERE code=?", (text,), fetchone=True)
        clear_state(user_id)
        if not movie:
            bot.send_message(message.chat.id, "❌ Bunday kodli kino topilmadi.", reply_markup=admin_keyboard())
            return
        execute("DELETE FROM movies WHERE id=?", (movie["id"],))
        execute("DELETE FROM watch_log WHERE movie_id=?", (movie["id"],))
        bot.send_message(message.chat.id, f"✅ <b>{safe(movie['title'])}</b> o‘chirildi.", reply_markup=admin_keyboard())
        return

    # --- ADMIN: KANAL QO‘SHISH (username) ---
    if state == "channel_add_username" and is_admin(user_id):
        username = text.strip()
        if not (username.startswith("@") or username.startswith("https://")):
            username = normalize_channel_username(username)
        try:
            chat = bot.get_chat(username)
            chat_id = chat.id
            title = chat.title or username
            invite_link = f"https://t.me/{username.lstrip('@')}" if username.startswith("@") else username
        except:
            bot.send_message(message.chat.id, "❌ Kanal topilmadi yoki bot admin emas.")
            return
        set_state(user_id, "channel_add_link", f"{username}|{title}|{chat_id}")
        bot.send_message(message.chat.id, f"📢 Kanal topildi: <b>{safe(title)}</b>\n\nKanalga havolani yuboring (https://t.me/...):")
        return

    if state == "channel_add_link" and is_admin(user_id):
        parts = data.split("|", 2)   # username|title|chat_id
        if len(parts) != 3:
            clear_state(user_id)
            return
        username, title, chat_id = parts
        invite_link = text.strip()
        if not invite_link.startswith("http"):
            bot.send_message(message.chat.id, "❌ To‘g‘ri havola yuboring.")
            return
        if execute("SELECT id FROM channels WHERE username=?", (username,), fetchone=True):
            bot.send_message(message.chat.id, "❌ Bu kanal allaqachon qo‘shilgan.")
            clear_state(user_id)
            return
        execute("INSERT INTO channels(title, username, invite_link, chat_id) VALUES(?,?,?,?)", (title, username, invite_link, int(chat_id)))
        clear_state(user_id)
        bot.send_message(message.chat.id, f"✅ <b>{safe(title)}</b> kanali majburiy obunaga qo‘shildi.", reply_markup=admin_keyboard())
        return

    # --- ADMIN: KANAL O‘CHIRISH ---
    if state == "channel_delete_value" and is_admin(user_id):
        val = text.strip()
        if val.isdigit():
            channel = execute("SELECT * FROM channels WHERE id=?", (int(val),), fetchone=True)
        else:
            val = normalize_channel_username(val)
            channel = execute("SELECT * FROM channels WHERE username=?", (val,), fetchone=True)
        clear_state(user_id)
        if not channel:
            bot.send_message(message.chat.id, "❌ Bunday kanal topilmadi.", reply_markup=admin_keyboard())
            return
        execute("DELETE FROM channels WHERE id=?", (channel["id"],))
        bot.send_message(message.chat.id, f"✅ <b>{safe(channel['title'])}</b> kanali ro‘yxatdan o‘chirildi.", reply_markup=admin_keyboard())
        return

    # --- ADMIN: REKLAMA SOZLAMALARI O‘ZGARTIRISH ---
    if state.startswith("change_ad_") and is_admin(user_id):
        key_map = {
            "change_ad_publisher_id": "ad_publisher_id",
            "change_ad_widget_id": "ad_widget_id",
            "change_ad_bid_floor": "ad_bid_floor",
            "change_ad_production": "ad_production",
            "change_ad_free_limit": "free_movies_limit",
            "change_ad_ads_count": "ads_after_limit",
        }
        key = key_map.get(state)
        if key in ("free_movies_limit", "ads_after_limit", "ad_bid_floor"):
            try:
                val = float(text)
                if val < 0: raise ValueError
            except ValueError:
                bot.send_message(message.chat.id, "❌ Musbat son kiriting.")
                return
        set_setting(key, text)
        clear_state(user_id)
        bot.send_message(message.chat.id, f"✅ {key} yangilandi.", reply_markup=admin_keyboard())
        return

    # --- ADMIN: FOYDALANUVCHI BOSHQARUVI ---
    if state == "manage_user_id" and is_admin(user_id):
        try:
            target_id = int(text)
        except:
            bot.send_message(message.chat.id, "❌ Raqamli ID yuboring.")
            return
        target = get_user(target_id)
        clear_state(user_id)
        if not target:
            bot.send_message(message.chat.id, "❌ Foydalanuvchi topilmadi.")
            return
        status = "Bloklangan" if target["is_blocked"] else "Aktiv"
        bot.send_message(message.chat.id,
            f"👤 <b>FOYDALANUVCHI</b>\n\n"
            f"Ism: {safe(target['full_name'])}\n"
            f"ID: <code>{target_id}</code>\n"
            f"Username: {safe(target['username'] or 'Yo‘q')}\n"
            f"Holat: <b>{status}</b>\n"
            f"Bepul kinolar: {target['free_movies_count']}",
            reply_markup=user_manage_keyboard(target_id))
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

    bot.send_message(message.chat.id, "❌ Noto‘g‘ri ma’lumot. /cancel bilan bekor qiling.")

# ========== ADMIN: VIDEO/DOCUMENT QABUL (kino qo‘shish) ==========
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
    bot.send_message(message.chat.id, "📝 Kino captionini yuboring (birinchi qatorda nom bo‘lsin).")
# =========================================================
# 3-QISM: Flask webhook va ishga tushirish
# =========================================================

app = Flask(__name__)

WEBHOOK_PATH = f"/webhook/{BOT_TOKEN.split(':', 1)[0]}"
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

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

def configure_webhook():
    init_db()

    if not RENDER_EXTERNAL_URL:
        logging.warning("RENDER_EXTERNAL_URL topilmadi. Webhook o‘rnatilmadi.")
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
                "chat_join_request",
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
