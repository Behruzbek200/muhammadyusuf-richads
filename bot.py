import html, logging, os, re, sqlite3, threading, time, uuid, requests, csv, io, traceback
from datetime import datetime, timedelta
import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException
from flask import Flask, request, jsonify

# ---------- SOZLAMALAR ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
DB_NAME = "kino_bot.db"

ADSGRAM_API_URL = os.getenv("ADSGRAM_API_URL", "https://api.adsgram.ai/v1/ad")
ADSGRAM_PLACEMENT_ID = os.getenv("ADSGRAM_PLACEMENT_ID", "YOUR_PLACEMENT_ID")
ADSGRAM_API_KEY = os.getenv("ADSGRAM_API_KEY", "YOUR_API_KEY")

AD_API_URL = os.getenv("AD_API_URL", "")
PUBLISHER_ID = os.getenv("PUBLISHER_ID", "")
WIDGET_ID = os.getenv("WIDGET_ID", "")
BID_FLOOR = float(os.getenv("BID_FLOOR", "0.0001"))
PRODUCTION = os.getenv("PRODUCTION", "true").lower() == "true"

if not BOT_TOKEN or ":" not in BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN noto‘g‘ri")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=True)

# ---------- 8-QISM: XATOLAR LOGI ----------
def notify_admins_about_error(error_msg, exc_info=None):
    """Kritik xatoliklarni adminlarga yuboradi."""
    for admin_id in ADMIN_IDS:
        try:
            text = f"🚨 <b>Xatolik yuz berdi!</b>\n\n{error_msg}"
            if exc_info:
                text += f"\n\n<code>{traceback.format_exc()}</code>"
            bot.send_message(admin_id, text[:4000])
        except:
            pass

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
        except Exception as e:
            logging.error(f"DB xatosi: {e}")
            notify_admins_about_error(f"DB xatosi: {e}\nSo'rov: {q}")
            raise
        finally: conn.close()

def init_db():
    # Users
    execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, full_name TEXT DEFAULT '', username TEXT DEFAULT '',
        is_blocked INTEGER DEFAULT 0, joined_at TEXT, last_active TEXT,
        free_movies_count INTEGER DEFAULT 0,
        pending_ads_required INTEGER DEFAULT 0,
        pending_ads_completed INTEGER DEFAULT 0,
        pending_movie_id INTEGER,
        language TEXT DEFAULT 'uz',
        bonus_points INTEGER DEFAULT 0,
        free_movies_earned INTEGER DEFAULT 0
    )""")
    for col in ["free_movies_count", "pending_ads_required", "pending_ads_completed", "pending_movie_id", "language", "bonus_points", "free_movies_earned"]:
        try: execute(f"ALTER TABLE users ADD COLUMN {col} INTEGER DEFAULT 0")
        except: pass

    # Movies
    execute("""CREATE TABLE IF NOT EXISTS movies (
        id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE, title TEXT,
        caption TEXT, file_id TEXT, file_type TEXT DEFAULT 'video',
        views INTEGER DEFAULT 0, created_at TEXT, added_by INTEGER,
        year TEXT DEFAULT '', genre TEXT DEFAULT '', description TEXT DEFAULT '',
        rating_sum INTEGER DEFAULT 0, rating_count INTEGER DEFAULT 0
    )""")
    for col in ["year", "genre", "description", "rating_sum", "rating_count"]:
        try: execute(f"ALTER TABLE movies ADD COLUMN {col} INTEGER DEFAULT 0")
        except: pass

    # Channels
    execute("""CREATE TABLE IF NOT EXISTS channels (
        id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT,
        username TEXT UNIQUE, invite_link TEXT, chat_id INTEGER,
        channel_type TEXT DEFAULT 'public'
    )""")
    try: execute("ALTER TABLE channels ADD COLUMN channel_type TEXT DEFAULT 'public'")
    except: pass

    # Categories
    execute("""CREATE TABLE IF NOT EXISTS categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, created_at TEXT
    )""")
    execute("""CREATE TABLE IF NOT EXISTS movie_categories (
        movie_id INTEGER, category_id INTEGER,
        PRIMARY KEY (movie_id, category_id)
    )""")

    # Translations
    execute("""CREATE TABLE IF NOT EXISTS translations (
        key TEXT, lang TEXT, value TEXT,
        PRIMARY KEY (key, lang)
    )""")

    # Reviews
    execute("""CREATE TABLE IF NOT EXISTS reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, movie_id INTEGER,
        rating INTEGER, review_text TEXT,
        created_at TEXT,
        UNIQUE(user_id, movie_id)
    )""")

    # Messages, states, settings, watch_log, search_log, ad_views, join_requests
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

    # Default settings
    for k,v in [
        ("free_movies_limit", "3"),
        ("ads_after_limit", "2"),
        ("adsgram_api_url", ADSGRAM_API_URL),
        ("adsgram_placement_id", ADSGRAM_PLACEMENT_ID),
        ("adsgram_api_key", ADSGRAM_API_KEY),
        ("ad_publisher_id", PUBLISHER_ID),
        ("ad_widget_id", WIDGET_ID),
        ("ad_bid_floor", str(BID_FLOOR)),
        ("ad_production", str(PRODUCTION).lower()),
        ("ad_timeout_seconds", "60"),
        ("daily_ad_limit", "10"),
        ("hourly_ad_limit", "3"),
        ("notification_channel", ""),
        ("bonus_threshold", "5"),
    ]:
        execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k,v))

    # Default translations
    default_trans = {
        ("welcome", "uz"): "🎬 <b>Kino botga xush kelibsiz!</b>\nKerakli bo‘limni tanlang:",
        ("welcome", "ru"): "🎬 <b>Добро пожаловать в кинобот!</b>\nВыберите раздел:",
        ("welcome", "en"): "🎬 <b>Welcome to the movie bot!</b>\nChoose a section:",
        ("movie_code_prompt", "uz"): "🔢 <b>Kino kodini yuboring:</b>\nMasalan: <code>145</code> yoki <code>MOV-001</code>",
        ("movie_code_prompt", "ru"): "🔢 <b>Отправьте код фильма:</b>\nНапример: <code>145</code> или <code>MOV-001</code>",
        ("movie_code_prompt", "en"): "🔢 <b>Send movie code:</b>\nExample: <code>145</code> or <code>MOV-001</code>",
        ("no_movie", "uz"): "❌ Bunday kodli kino topilmadi.",
        ("no_movie", "ru"): "❌ Фильм с таким кодом не найден.",
        ("no_movie", "en"): "❌ Movie with this code not found.",
        ("ad_loading", "uz"): "⏳ Reklama yuklanmoqda, iltimos kuting...",
        ("ad_loading", "ru"): "⏳ Загружается реклама, подождите...",
        ("ad_loading", "en"): "⏳ Loading ad, please wait...",
        ("ad_not_found", "uz"): "⚠️ Hozircha reklama topilmadi, keyinroq urinib ko‘ring.",
        ("ad_not_found", "ru"): "⚠️ Реклама не найдена, попробуйте позже.",
        ("ad_not_found", "en"): "⚠️ No ad found, try again later.",
        ("ad_error", "uz"): "❌ Reklama yuborishda xatolik yuz berdi.",
        ("ad_error", "ru"): "❌ Ошибка при отправке рекламы.",
        ("ad_error", "en"): "❌ Error sending ad.",
        ("subscription_required", "uz"): "🔐 <b>Botdan foydalanish uchun quyidagi kanallarga obuna bo'ling yoki so‘rov yuboring:</b>\n\n📢 <b>Ochiq kanallar</b> — havola orqali kirib <b>Join</b> tugmasini bosing.\n🔒 <b>Maxfiy kanallar</b> — <b>So‘rov yuborish</b> tugmasini bosing.\n\nSo‘ngra <b>✅ Obunani tekshirish</b> tugmasini bosing.",
        ("subscription_required", "ru"): "🔐 <b>Для использования бота подпишитесь на каналы или отправьте запрос:</b>\n\n📢 <b>Открытые каналы</b> — перейдите по ссылке и нажмите <b>Join</b>.\n🔒 <b>Приватные каналы</b> — нажмите <b>Отправить запрос</b>.\n\nЗатем нажмите <b>✅ Проверить подписку</b>.",
        ("subscription_required", "en"): "🔐 <b>To use the bot, subscribe to the channels or send a request:</b>\n\n📢 <b>Public channels</b> — click the link and press <b>Join</b>.\n🔒 <b>Private channels</b> — press <b>Send request</b>.\n\nThen press <b>✅ Check subscription</b>.",
        ("subscription_ok", "uz"): "✅ <b>Obunangiz tasdiqlandi.</b>",
        ("subscription_ok", "ru"): "✅ <b>Подписка подтверждена.</b>",
        ("subscription_ok", "en"): "✅ <b>Subscription confirmed.</b>",
        ("profile", "uz"): "👤 <b>PROFIL</b>\n\nIsm: {name}\n🆔 ID: {id}\n🔗 Username: {username}\n🎬 Ko‘rilgan kinolar: {watched}\n📣 Ko‘rilgan reklamalar: {ads}\n🎁 Bonuslar: {bonus}\n🌐 Til: {lang}\n📆 Qo‘shilgan: {joined}",
        ("profile", "ru"): "👤 <b>ПРОФИЛЬ</b>\n\nИмя: {name}\n🆔 ID: {id}\n🔗 Username: {username}\n🎬 Просмотрено фильмов: {watched}\n📣 Просмотрено реклам: {ads}\n🎁 Бонусов: {bonus}\n🌐 Язык: {lang}\n📆 Дата регистрации: {joined}",
        ("profile", "en"): "👤 <b>PROFILE</b>\n\nName: {name}\n🆔 ID: {id}\n🔗 Username: {username}\n🎬 Movies watched: {watched}\n📣 Ads viewed: {ads}\n🎁 Bonuses: {bonus}\n🌐 Language: {lang}\n📆 Joined: {joined}",
        ("statistics", "uz"): "📊 <b>BOT STATISTIKASI</b>\n\n🎬 Kinolar: {movies}\n👁 Ko‘rishlar: {views}\n👥 Foydalanuvchilar: {users}",
        ("statistics", "ru"): "📊 <b>СТАТИСТИКА БОТА</b>\n\n🎬 Фильмов: {movies}\n👁 Просмотров: {views}\n👥 Пользователей: {users}",
        ("statistics", "en"): "📊 <b>BOT STATISTICS</b>\n\n🎬 Movies: {movies}\n👁 Views: {views}\n👥 Users: {users}",
        ("admin_panel", "uz"): "🛠 <b>Admin panel</b>",
        ("admin_panel", "ru"): "🛠 <b>Панель администратора</b>",
        ("admin_panel", "en"): "🛠 <b>Admin panel</b>",
        ("message_sent", "uz"): "✅ Xabaringiz adminga yuborildi.",
        ("message_sent", "ru"): "✅ Ваше сообщение отправлено администратору.",
        ("message_sent", "en"): "✅ Your message was sent to admin.",
        ("ad_limit_reached", "uz"): "⚠️ Siz kunlik reklama limitiga yetdingiz. Ertaga urinib ko‘ring.",
        ("ad_limit_reached", "ru"): "⚠️ Вы достигли дневного лимита рекламы. Попробуйте завтра.",
        ("ad_limit_reached", "en"): "⚠️ You have reached the daily ad limit. Try again tomorrow.",
        ("rate_movie", "uz"): "⭐ <b>Kinoni baholang</b>\n\nYulduzcha tanlang:",
        ("rate_movie", "ru"): "⭐ <b>Оцените фильм</b>\n\nВыберите звезду:",
        ("rate_movie", "en"): "⭐ <b>Rate the movie</b>\n\nChoose a star:",
        ("review_prompt", "uz"): "✍️ <b>Izoh qoldiring</b>\n\nMaksimal 500 belgi. Bekor qilish uchun /cancel",
        ("review_prompt", "ru"): "✍️ <b>Оставьте отзыв</b>\n\nМаксимум 500 символов. Отмена /cancel",
        ("review_prompt", "en"): "✍️ <b>Leave a review</b>\n\nMax 500 characters. Cancel /cancel",
        ("review_saved", "uz"): "✅ Izoh saqlandi. Rahmat!",
        ("review_saved", "ru"): "✅ Отзыв сохранен. Спасибо!",
        ("review_saved", "en"): "✅ Review saved. Thanks!",
        ("rating_saved", "uz"): "✅ Baholash saqlandi.",
        ("rating_saved", "ru"): "✅ Оценка сохранена.",
        ("rating_saved", "en"): "✅ Rating saved.",
        ("already_reviewed", "uz"): "Siz bu kinoni allaqachon baholagansiz.",
        ("already_reviewed", "ru"): "Вы уже оценили этот фильм.",
        ("already_reviewed", "en"): "You have already rated this movie.",
        ("no_rating", "uz"): "Bu kino hali baholanmagan.",
        ("no_rating", "ru"): "Этот фильм еще не оценен.",
        ("no_rating", "en"): "This movie is not rated yet.",
        ("recommendations", "uz"): "🎯 <b>Sizga tavsiya etilgan kinolar</b>\n\n{list}",
        ("recommendations", "ru"): "🎯 <b>Рекомендуемые фильмы</b>\n\n{list}",
        ("recommendations", "en"): "🎯 <b>Recommended movies</b>\n\n{list}",
        ("no_recommendations", "uz"): "Hozircha sizga tavsiya qiladigan kino yo‘q. Ko‘proq kinolar ko‘ring!",
        ("no_recommendations", "ru"): "Пока нет фильмов для рекомендации. Смотрите больше!",
        ("no_recommendations", "en"): "No recommendations yet. Watch more movies!",
        ("bonus_earned", "uz"): "🎉 <b>Tabriklaymiz!</b> Siz {threshold} ta reklama ko‘rdingiz va <b>1 ta bepul kino</b> sovg‘a qildingiz! Uni ko‘rish uchun istalgan kodni yuboring.",
        ("bonus_earned", "ru"): "🎉 <b>Поздравляем!</b> Вы посмотрели {threshold} реклам и получили <b>1 бесплатный фильм</b>! Отправьте любой код, чтобы посмотреть.",
        ("bonus_earned", "en"): "🎉 <b>Congratulations!</b> You watched {threshold} ads and earned <b>1 free movie</b>! Send any code to watch.",
        ("bonus_used", "uz"): "🎁 Siz bonus kinoni ko‘rdingiz. Qolgan bonuslar: {remaining}",
        ("bonus_used", "ru"): "🎁 Вы посмотрели бонусный фильм. Осталось бонусов: {remaining}",
        ("bonus_used", "en"): "🎁 You watched a bonus movie. Remaining bonuses: {remaining}",
    }
    for (key, lang), value in default_trans.items():
        execute("INSERT OR IGNORE INTO translations(key,lang,value) VALUES(?,?,?)", (key, lang, value))

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
    execute("""INSERT INTO users(user_id,full_name,username,joined_at,last_active,language,bonus_points)
               VALUES(?,?,?,?,?,COALESCE((SELECT language FROM users WHERE user_id=?), 'uz'),
                      COALESCE((SELECT bonus_points FROM users WHERE user_id=?), 0))
               ON CONFLICT(user_id) DO UPDATE SET
               full_name=excluded.full_name, username=excluded.username,
               last_active=excluded.last_active""",
            (user.id, name, uname, now_text(), now_text(), user.id, user.id))
def get_user(uid): return execute("SELECT * FROM users WHERE user_id=?",(uid,), fetchone=True)

def get_translation(key, lang="uz"):
    r = execute("SELECT value FROM translations WHERE key=? AND lang=?", (key, lang), fetchone=True)
    if r:
        return r["value"]
    r = execute("SELECT value FROM translations WHERE key=? AND lang='uz'", (key,), fetchone=True)
    return r["value"] if r else key

def normalize_channel_username(value: str) -> str:
    """Foydalanuvchi kiritgan satrdan @username yoki linkni tozalaydi."""
    value = value.strip()
    if value.startswith("https://t.me/"):
        return "@" + value.split("https://t.me/")[-1].split("/")[0].split("?")[0]
    if value.startswith("t.me/"):
        return "@" + value.split("t.me/")[-1].split("/")[0].split("?")[0]
    if value.startswith("@"):
        return value
    if value.startswith("https://"):
        return value
    return f"@{value}"

# ========== AVTOMATIK KOD GENERATSIYASI ==========
def generate_movie_code():
    max_id = execute("SELECT MAX(id) AS max_id FROM movies", fetchone=True)["max_id"] or 0
    new_id = max_id + 1
    return f"MOV-{new_id:05d}"

# ========== REKLAMA VAQTI VA CHEKLOVLAR ==========
def expire_old_ads():
    timeout = int(get_setting("ad_timeout_seconds"))
    expire_time = (datetime.now() - timedelta(seconds=timeout)).strftime("%Y-%m-%d %H:%M:%S")
    execute("UPDATE ad_views SET status='expired' WHERE status='pending' AND created_at < ?", (expire_time,))

def check_ad_limit(user_id):
    daily_limit = int(get_setting("daily_ad_limit"))
    hourly_limit = int(get_setting("hourly_ad_limit"))
    today = datetime.now().strftime("%Y-%m-%d")
    hour = datetime.now().strftime("%Y-%m-%d %H:00:00")
    daily = execute("SELECT COUNT(*) AS c FROM ad_views WHERE user_id=? AND status='completed' AND date(clicked_at)=?", (user_id, today), fetchone=True)["c"]
    if daily >= daily_limit:
        return False
    hourly = execute("SELECT COUNT(*) AS c FROM ad_views WHERE user_id=? AND status='completed' AND clicked_at >= ?", (user_id, hour), fetchone=True)["c"]
    if hourly >= hourly_limit:
        return False
    return True

# ========== 8-QISM: BONUS TIZIMI ==========
def check_and_give_bonus(user_id):
    threshold = int(get_setting("bonus_threshold"))
    user = get_user(user_id)
    if not user:
        return
    total_ads = execute("SELECT COUNT(*) AS c FROM ad_views WHERE user_id=? AND status='completed'", (user_id,), fetchone=True)["c"]
    earned = user["free_movies_earned"] or 0
    new_earned = total_ads // threshold
    if new_earned > earned:
        bonus_count = new_earned - earned
        execute("UPDATE users SET bonus_points = bonus_points + ?, free_movies_earned = ? WHERE user_id = ?", 
                (bonus_count, new_earned, user_id))
        lang = user["language"] or "uz"
        try:
            bot.send_message(user_id, get_translation("bonus_earned", lang).format(threshold=threshold))
        except Exception as e:
            logging.error(f"Bonus xabari yuborishda xato: {e}")

# ========== AdsGram REKLAMA ==========
def fetch_ad_from_adsgram(user_id, retry=3):
    api_url = get_setting("adsgram_api_url")
    placement_id = get_setting("adsgram_placement_id")
    api_key = get_setting("adsgram_api_key")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"placement_id": placement_id, "user_id": str(user_id), "telegram_id": str(user_id)}
    for attempt in range(retry):
        try:
            resp = requests.post(api_url, json=payload, headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                ad = data.get("ad")
                if ad:
                    image_url = ad.get("image")
                    caption = ad.get("message", "")
                    link_url = ad.get("link")
                    button_text = ad.get("button", "Batafsil")
                    if image_url and link_url:
                        click_id = str(uuid.uuid4())
                        return image_url, caption, link_url, button_text, click_id
            logging.warning(f"AdsGram urinish {attempt+1} xato: {resp.status_code}")
        except Exception as e:
            logging.exception(f"AdsGram urinish {attempt+1} xato")
            notify_admins_about_error(f"AdsGram xatosi: {e}")
        time.sleep(1)
    return test_ad()

def test_ad():
    image_url = "https://via.placeholder.com/300x200.png?text=AdsGram+Test"
    caption = "📢 <b>Test reklamasi</b>\n\nKinoni ko‘rish uchun tugmani bosing."
    link_url = "https://example.com"
    button_text = "Batafsil"
    click_id = str(uuid.uuid4())
    return image_url, caption, link_url, button_text, click_id

def send_ad_message(chat_id, user_id, movie_id):
    expire_old_ads()
    lang = get_user(user_id)["language"] or "uz"
    if not check_ad_limit(user_id):
        bot.send_message(chat_id, get_translation("ad_limit_reached", lang))
        return False
    status_msg = bot.send_message(chat_id, get_translation("ad_loading", lang))
    ad_data = fetch_ad_from_adsgram(user_id)
    if not ad_data:
        bot.edit_message_text(get_translation("ad_not_found", lang), chat_id, status_msg.message_id)
        return False
    image_url, caption, link_url, button_text, click_id = ad_data
    execute("INSERT INTO ad_views(user_id, movie_id, click_id, link_url, status, created_at) VALUES(?,?,?,?,'pending',?)",
            (user_id, movie_id, click_id, link_url, now_text()))
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(f"📣 {button_text}", callback_data=f"ad_click:{click_id}"))
    caption = caption if caption else "📺 Kinoni ochish uchun reklamani ko‘ring va tugmani bosing."
    try:
        bot.delete_message(chat_id, status_msg.message_id)
        bot.send_photo(chat_id, image_url, caption=caption, reply_markup=kb)
        return True
    except Exception as e:
        logging.exception("Rasm yuborishda xato")
        bot.edit_message_text(get_translation("ad_error", lang), chat_id, status_msg.message_id)
        execute("DELETE FROM ad_views WHERE click_id=?", (click_id,))
        return False

@bot.callback_query_handler(func=lambda c: c.data.startswith("ad_click:"))
def ad_click_handler(call):
    expire_old_ads()
    click_id = call.data.split(":", 1)[1]
    ad = execute("SELECT * FROM ad_views WHERE click_id=? AND status='pending'", (click_id,), fetchone=True)
    if not ad:
        bot.answer_callback_query(call.id, "Bu reklama allaqachon ko‘rilgan yoki vaqti o‘tgan.")
        return
    execute("UPDATE ad_views SET status='completed', clicked_at=? WHERE id=?", (now_text(), ad["id"]))
    user_id = call.from_user.id
    user = get_user(user_id)
    if not user or not user["pending_ads_required"]:
        bot.answer_callback_query(call.id)
        return
    new_completed = (user["pending_ads_completed"] or 0) + 1
    execute("UPDATE users SET pending_ads_completed=? WHERE user_id=?", (new_completed, user_id))
    if new_completed >= user["pending_ads_required"]:
        movie = execute("SELECT * FROM movies WHERE id=?", (user["pending_movie_id"],), fetchone=True)
        if movie:
            try:
                send_movie_to_user(user_id, user_id, movie)
            except Exception:
                logging.exception("Kino yuborishda xato")
                notify_admins_about_error("Kino yuborishda xato", exc_info=True)
        execute("UPDATE users SET pending_ads_required=0, pending_ads_completed=0, pending_movie_id=NULL WHERE user_id=?", (user_id,))
    if ad["link_url"]:
        bot.answer_callback_query(call.id, url=ad["link_url"])
    else:
        bot.answer_callback_query(call.id, "Reklama ko'rildi ✅")
    check_and_give_bonus(user_id)

# ========== MAJBURIY OBUNA (TUZATILGAN) ==========
def get_channels():
    return execute("SELECT * FROM channels ORDER BY id", fetchall=True)

def check_subscription(user_id):
    # Adminlar har doim o‘tkaziladi
    if is_admin(user_id):
        return True

    channels = get_channels()
    if not channels:
        return True

    for ch in channels:
        # Ochiq kanal – username orqali tekshiramiz
        if ch.get("username"):
            try:
                member = bot.get_chat_member(ch["username"], user_id)
                if member.status in ["creator", "administrator", "member"]:
                    continue
                else:
                    return False
            except Exception:
                pass

        # Maxfiy kanal – chat_id orqali tekshiramiz
        if ch.get("chat_id"):
            try:
                member = bot.get_chat_member(ch["chat_id"], user_id)
                if member.status in ["creator", "administrator", "member"]:
                    continue
            except Exception:
                pass
            # Agar a'zo bo'lmasa, join_requests da borligini tekshiramiz
            req = execute(
                "SELECT * FROM join_requests WHERE user_id = ? AND chat_id = ?",
                (user_id, ch["chat_id"]), fetchone=True
            )
            if not req:
                return False

    return True

def subscription_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=1)
    channels = get_channels()
    if not channels:
        return kb

    for ch in channels:
        title = ch.get("title") or "Kanal"
        if ch.get("invite_link"):
            if ch.get("channel_type") == "public":
                kb.add(types.InlineKeyboardButton(f"📢 {title}", url=ch["invite_link"]))
            else:
                kb.add(types.InlineKeyboardButton(f"🔒 {title} – so‘rov yuborish", url=ch["invite_link"]))
        elif ch.get("username"):
            name = ch["username"].replace("@", "")
            if name:
                if ch.get("channel_type") == "public":
                    kb.add(types.InlineKeyboardButton(f"📢 {title}", url=f"https://t.me/{name}"))
                else:
                    kb.add(types.InlineKeyboardButton(f"🔒 {title} – so‘rov yuborish", url=f"https://t.me/{name}"))
        elif ch.get("chat_id"):
            kb.add(types.InlineKeyboardButton(f"🔒 {title} (so‘rov yuboring)", callback_data="noop"))
    kb.add(types.InlineKeyboardButton("✅ Obunani tekshirish", callback_data="check_subscription"))
    return kb

def send_subscription_required(chat_id):
    channels = get_channels()
    if not channels:
        return
    text = "🔔 Botdan foydalanish uchun quyidagi kanallarga obuna bo‘ling:\n\n"
    kb = subscription_keyboard()
    user = get_user(chat_id)
    lang = user["language"] if user else "uz"
    bot.send_message(chat_id, text, reply_markup=kb, disable_web_page_preview=True)

@bot.chat_join_request_handler()
def handle_join_request(req: types.ChatJoinRequest):
    try:
        execute("INSERT OR IGNORE INTO join_requests VALUES(?,?,?)",
                (req.from_user.id, req.chat.id, now_text()))
        logging.info(f"Join request recorded for user {req.from_user.id} in chat {req.chat.id}")
    except Exception as e:
        logging.error(f"Join request xato: {e}")

@bot.callback_query_handler(func=lambda c: c.data == "check_subscription")
def check_sub_cb(call):
    register_user(call.from_user)
    if check_subscription(call.from_user.id):
        bot.answer_callback_query(call.id, "Obuna tasdiqlandi ✅")
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass
        lang = get_user(call.from_user.id)["language"] or "uz"
        bot.send_message(call.message.chat.id, get_translation("subscription_ok", lang), reply_markup=main_keyboard(call.from_user.id))
    else:
        bot.answer_callback_query(call.id, "Hali barcha kanallarga obuna bo‘lmadingiz yoki so‘rov yubormagansiz.", show_alert=True)

def main_keyboard(uid):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add("🔎 Kino kodi", "🔥 Eng mashhur kinolar")
    kb.add("🎯 Tavsiyalar", "📊 Statistika", "💬 Adminga xabar", "👤 Profil")
    kb.add("🌐 Til")
    if is_admin(uid): kb.add("🛠 Admin panel")
    return kb

def admin_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add("🎬 Kinolar", "⚙️ Reklama sozlamalari")
    kb.add("👥 Foydalanuvchilar", "📡 Kanallar")
    kb.add("📢 Reklama yuborish", "📊 Statistika")
    kb.add("📂 Kategoriyalar", "📤 CSV import/export")
    kb.add("📈 Dashboard", "🏠 Asosiy menyu")
    return kb

def movies_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add("➕ Kino qo‘shish", "🗑 Kino o‘chirish")
    kb.add("📋 Barcha kinolar", "🔎 Kino qidirish")
    kb.add("📝 Kino tahrirlash", "🏷 Kategoriya qo‘shish")
    kb.add("🔙 Admin panelga qaytish")
    return kb

def categories_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add("➕ Kategoriya qo‘shish", "🗑 Kategoriya o‘chirish")
    kb.add("📋 Barcha kategoriyalar", "🔙 Admin panelga qaytish")
    return kb

def open_main_menu(chat_id, uid):
    lang = get_user(uid)["language"] if get_user(uid) else "uz"
    bot.send_message(chat_id, get_translation("welcome", lang), reply_markup=main_keyboard(uid))

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

# ========== KINO YUBORISH ==========
def send_movie_to_user(chat_id: int, user_id: int, movie, retry=2, use_bonus=False):
    caption = (
        f"{movie['caption']}\n\n"
        f"🔢 Kod: <code>{safe(movie['code'])}</code>\n"
        f"👁 Ko‘rilgan: <b>{movie['views'] + 1}</b> marta"
    )
    if movie['year']:
        caption += f"\n📅 Yil: {safe(movie['year'])}"
    if movie['genre']:
        caption += f"\n🎭 Janr: {safe(movie['genre'])}"
    if movie['description']:
        caption += f"\n📝 {safe(movie['description'])}"
    avg = round(movie['rating_sum'] / movie['rating_count'], 1) if movie['rating_count'] > 0 else 0
    caption += f"\n⭐ Reyting: {avg if avg else 'Hali baholanmagan'}"

    if use_bonus:
        caption += "\n🎁 <b>Bonus kino</b>"

    for attempt in range(retry):
        try:
            if movie["file_type"] == "video":
                bot.send_video(chat_id, movie["file_id"], caption=caption, supports_streaming=True)
            else:
                bot.send_document(chat_id, movie["file_id"], caption=caption)
            break
        except Exception as e:
            logging.exception(f"Kino yuborishda xato (urinish {attempt+1}): {e}")
            if attempt == retry-1:
                bot.send_message(chat_id, "❌ Kinoni yuklashda xatolik yuz berdi. Keyinroq urinib ko‘ring.")
                return
            time.sleep(1)

    execute("UPDATE movies SET views = views + 1 WHERE id = ?", (movie["id"],))
    execute("INSERT INTO watch_log(user_id, movie_id, watched_at) VALUES(?,?,?)", (user_id, movie["id"], now_text()))

    user = get_user(user_id)
    if use_bonus:
        execute("UPDATE users SET bonus_points = bonus_points - 1 WHERE user_id = ?", (user_id,))
        lang = user["language"] if user else "uz"
        remaining = execute("SELECT bonus_points FROM users WHERE user_id=?", (user_id,), fetchone=True)["bonus_points"] or 0
        bot.send_message(chat_id, get_translation("bonus_used", lang).format(remaining=remaining))
    else:
        if not user.get("pending_ads_required") or user["pending_ads_required"] == 0:
            execute("UPDATE users SET free_movies_count = free_movies_count + 1 WHERE user_id = ?", (user_id,))

    lang = user["language"] if user else "uz"
    existing = execute("SELECT id FROM reviews WHERE user_id=? AND movie_id=?", (user_id, movie["id"]), fetchone=True)
    if existing:
        rating_text = "⭐ Baholadingiz"
    else:
        rating_text = "⭐ Baholash"
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton(rating_text, callback_data=f"rate_movie_{movie['id']}"))
    kb.add(types.InlineKeyboardButton("✍️ Izoh qoldirish", callback_data=f"review_movie_{movie['id']}"))
    bot.send_message(chat_id, "📝 Kinoni baholang yoki izoh qoldiring:", reply_markup=kb)

# ========== 8-QISM: SHAXSIY TAVSIYALAR ==========
@bot.message_handler(func=lambda m: m.text == "🎯 Tavsiyalar")
def recommendations_handler(message):
    if not ensure_access(message): return
    user_id = message.from_user.id
    lang = get_user(user_id)["language"] or "uz"
    watched_movies = execute("SELECT DISTINCT movie_id FROM watch_log WHERE user_id=?", (user_id,), fetchall=True)
    if not watched_movies:
        bot.send_message(message.chat.id, get_translation("no_recommendations", lang))
        return
    movie_ids = [m["movie_id"] for m in watched_movies]
    placeholders = ",".join("?" * len(movie_ids))
    cats = execute(f"SELECT DISTINCT category_id FROM movie_categories WHERE movie_id IN ({placeholders})", movie_ids, fetchall=True)
    if not cats:
        recs = execute("SELECT id, code, title, rating_sum, rating_count FROM movies WHERE rating_count > 0 ORDER BY CAST(rating_sum AS FLOAT)/rating_count DESC LIMIT 5", fetchall=True)
    else:
        cat_ids = [c["category_id"] for c in cats]
        placeholders2 = ",".join("?" * len(cat_ids))
        recs = execute(f"""SELECT m.id, m.code, m.title, m.rating_sum, m.rating_count
                          FROM movies m
                          JOIN movie_categories mc ON m.id = mc.movie_id
                          WHERE mc.category_id IN ({placeholders2})
                          AND m.id NOT IN ({placeholders})
                          AND m.rating_count > 0
                          GROUP BY m.id
                          ORDER BY CAST(m.rating_sum AS FLOAT)/m.rating_count DESC
                          LIMIT 5""", cat_ids + movie_ids, fetchall=True)
        if not recs:
            recs = execute("SELECT id, code, title, rating_sum, rating_count FROM movies WHERE rating_count > 0 ORDER BY CAST(rating_sum AS FLOAT)/rating_count DESC LIMIT 5", fetchall=True)

    if not recs:
        bot.send_message(message.chat.id, get_translation("no_recommendations", lang))
        return

    lines = []
    for i, m in enumerate(recs, 1):
        avg = round(m["rating_sum"] / m["rating_count"], 1) if m["rating_count"] > 0 else 0
        lines.append(f"{i}. <b>{safe(m['title'])}</b> (⭐{avg})\n   Kod: <code>{safe(m['code'])}</code>")
    text = get_translation("recommendations", lang).format(list="\n\n".join(lines))
    bot.send_message(message.chat.id, text)

# ========== 8-QISM: QIDIRUVDA FILTRLASH (admin) ==========
@bot.message_handler(func=lambda m: m.text == "🔎 Kino qidirish")
def search_movie_admin(message):
    if not is_admin(message.from_user.id): return
    set_state(message.from_user.id, "admin_search_filters")
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("📅 Yil", callback_data="search_filter_year"),
           types.InlineKeyboardButton("🎭 Janr", callback_data="search_filter_genre"),
           types.InlineKeyboardButton("⭐ Reyting", callback_data="search_filter_rating"))
    kb.add(types.InlineKeyboardButton("🔍 Qidirish", callback_data="search_filter_go"))
    kb.add(types.InlineKeyboardButton("❌ Bekor qilish", callback_data="search_filter_cancel"))
    bot.send_message(message.chat.id, "🔎 Qidiruv filtrlari:\nYil, janr yoki minimal reytingni tanlang. So‘ng 'Qidirish' tugmasini bosing.", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("search_filter_"))
def search_filter_callback(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Ruxsat yo'q")
        return
    action = call.data.split("_")[2]
    user_id = call.from_user.id
    if action == "cancel":
        clear_state(user_id)
        bot.answer_callback_query(call.id, "Bekor qilindi")
        bot.send_message(call.message.chat.id, "❌ Qidiruv bekor qilindi.", reply_markup=admin_keyboard())
        return
    if action == "go":
        state, data = get_state(user_id)
        if not data:
            bot.answer_callback_query(call.id, "Hech qanday filtr tanlanmagan.")
            return
        parts = data.split("|")
        year = parts[0] if len(parts) > 0 and parts[0] != "0" else None
        genre = parts[1] if len(parts) > 1 and parts[1] != "0" else None
        rating = float(parts[2]) if len(parts) > 2 and parts[2] != "0" else None
        set_state(user_id, "admin_search_result_filters", data)
        send_search_page_with_filters(call.message.chat.id, user_id, 0, year, genre, rating)
        bot.answer_callback_query(call.id, "Qidiruv bajarildi")
        return
    if action == "year":
        set_state(user_id, "search_filter_year_wait", "year")
        bot.answer_callback_query(call.id, "Yilni kiriting (masalan 2024):")
        bot.send_message(call.message.chat.id, "📅 Qidiriladigan yilni yuboring (yoki '0' o'tkazib yuborish):")
    elif action == "genre":
        set_state(user_id, "search_filter_genre_wait", "genre")
        bot.answer_callback_query(call.id, "Janrni kiriting (masalan Drama):")
        bot.send_message(call.message.chat.id, "🎭 Qidiriladigan janrni yuboring (yoki '0' o'tkazib yuborish):")
    elif action == "rating":
        set_state(user_id, "search_filter_rating_wait", "rating")
        bot.answer_callback_query(call.id, "Minimal reytingni kiriting (0.0 - 5.0):")
        bot.send_message(call.message.chat.id, "⭐ Minimal reytingni yuboring (masalan 4.0, yoki '0' o'tkazib yuborish):")

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and get_state(m.from_user.id)[0] in ["search_filter_year_wait", "search_filter_genre_wait", "search_filter_rating_wait"])
def search_filter_value_handler(message):
    user_id = message.from_user.id
    state, data = get_state(user_id)
    value = message.text.strip()
    state_obj = get_state(user_id)
    current_data = state_obj[1]
    parts = current_data.split("|") if current_data else ["0","0","0"]
    if state == "search_filter_year_wait":
        parts[0] = value if value != "0" else "0"
        set_state(user_id, "admin_search_filters", "|".join(parts))
        bot.send_message(message.chat.id, f"✅ Yil: {value if value != '0' else 'o‘tkazib yuborildi'}")
    elif state == "search_filter_genre_wait":
        parts[1] = value if value != "0" else "0"
        set_state(user_id, "admin_search_filters", "|".join(parts))
        bot.send_message(message.chat.id, f"✅ Janr: {value if value != '0' else 'o‘tkazib yuborildi'}")
    elif state == "search_filter_rating_wait":
        try:
            if value != "0":
                val = float(value)
                if val < 0 or val > 5:
                    raise ValueError
                parts[2] = value
            else:
                parts[2] = "0"
        except ValueError:
            bot.send_message(message.chat.id, "❌ Reyting 0.0 dan 5.0 gacha bo'lishi kerak.")
            return
        set_state(user_id, "admin_search_filters", "|".join(parts))
        bot.send_message(message.chat.id, f"✅ Reyting: {value if value != '0' else 'o‘tkazib yuborildi'}")
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("📅 Yil", callback_data="search_filter_year"),
           types.InlineKeyboardButton("🎭 Janr", callback_data="search_filter_genre"),
           types.InlineKeyboardButton("⭐ Reyting", callback_data="search_filter_rating"))
    kb.add(types.InlineKeyboardButton("🔍 Qidirish", callback_data="search_filter_go"))
    kb.add(types.InlineKeyboardButton("❌ Bekor qilish", callback_data="search_filter_cancel"))
    bot.send_message(message.chat.id, "Filtrlarni davom ettiring yoki 'Qidirish' tugmasini bosing.", reply_markup=kb)

def send_search_page_with_filters(chat_id, user_id, page, year=None, genre=None, rating=None):
    per_page = 10
    offset = page * per_page
    query_parts = []
    params = []
    if year:
        query_parts.append("year = ?")
        params.append(year)
    if genre:
        query_parts.append("genre LIKE ?")
        params.append(f"%{genre}%")
    if rating:
        query_parts.append("CAST(rating_sum AS FLOAT) / rating_count >= ?")
        params.append(float(rating))
    where = "WHERE " + " AND ".join(query_parts) if query_parts else ""
    count_query = f"SELECT COUNT(*) AS c FROM movies {where}"
    total = execute(count_query, params, fetchone=True)["c"]
    if rating:
        select_query = f"""SELECT id, code, title, views, year, genre, rating_sum, rating_count,
                           CAST(rating_sum AS FLOAT) / rating_count AS avg_rating
                           FROM movies {where}
                           ORDER BY avg_rating DESC, id DESC LIMIT ? OFFSET ?"""
    else:
        select_query = f"SELECT id, code, title, views, year, genre, rating_sum, rating_count FROM movies {where} ORDER BY id DESC LIMIT ? OFFSET ?"
    movies = execute(select_query, params + [per_page, offset], fetchall=True)
    total_pages = (total + per_page - 1) // per_page if total > 0 else 1
    if not movies:
        bot.send_message(chat_id, "❌ Hech qanday kino topilmadi.", reply_markup=movies_keyboard())
        clear_state(user_id)
        return
    lines = [f"🔎 <b>QIDIRUV NATIJALARI (sahifa {page+1}/{total_pages})</b>\n"]
    for m in movies:
        avg = round(m["rating_sum"] / m["rating_count"], 1) if m["rating_count"] > 0 else 0
        year_info = f" [{m['year']}]" if m['year'] else ""
        genre_info = f" {m['genre']}" if m['genre'] else ""
        rating_info = f" ⭐{avg}" if avg else ""
        lines.append(f"<b>{safe(m['title'])}{year_info}{genre_info}</b>\n   Kod: <code>{safe(m['code'])}</code> | 👁 {m['views']}{rating_info} | ID: {m['id']}")
    full = "\n\n".join(lines)
    kb = types.InlineKeyboardMarkup(row_width=2)
    if page > 0:
        kb.add(types.InlineKeyboardButton("⬅️ Oldingi", callback_data=f"search_page_f_{page-1}_{year or ''}_{genre or ''}_{rating or ''}"))
    if page < total_pages - 1:
        kb.add(types.InlineKeyboardButton("➡️ Keyingi", callback_data=f"search_page_f_{page+1}_{year or ''}_{genre or ''}_{rating or ''}"))
    kb.add(types.InlineKeyboardButton("🔄 Yangilash", callback_data=f"search_page_f_{page}_{year or ''}_{genre or ''}_{rating or ''}"))
    bot.send_message(chat_id, full, reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("search_page_f_"))
def search_page_filtered_callback(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Ruxsat yo'q")
        return
    parts = call.data.split("_")
    page = int(parts[3])
    year = parts[4] if len(parts) > 4 and parts[4] else None
    genre = parts[5] if len(parts) > 5 and parts[5] else None
    rating = parts[6] if len(parts) > 6 and parts[6] else None
    bot.answer_callback_query(call.id)
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        pass
    send_search_page_with_filters(call.message.chat.id, call.from_user.id, page, year, genre, rating)

# ========== KINO QIDIRISH (oddiy) ==========
@bot.message_handler(func=lambda m: m.text == "🔎 Kino kodi")
def ask_movie_code(message):
    if not ensure_access(message): return
    lang = get_user(message.from_user.id)["language"] or "uz"
    set_state(message.from_user.id, "waiting_movie_code")
    bot.send_message(message.chat.id, get_translation("movie_code_prompt", lang))

@bot.message_handler(func=lambda m: m.text == "🔥 Eng mashhur kinolar")
def popular_movies(message):
    if not ensure_access(message): return
    movies = execute("SELECT code, title, views FROM movies ORDER BY views DESC, id DESC LIMIT 10", fetchall=True)
    if not movies:
        lang = get_user(message.from_user.id)["language"] or "uz"
        bot.send_message(message.chat.id, get_translation("no_movie", lang))
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
    lang = get_user(message.from_user.id)["language"] or "uz"
    users = execute("SELECT COUNT(*) AS c FROM users", fetchone=True)["c"]
    movies = execute("SELECT COUNT(*) AS c FROM movies", fetchone=True)["c"]
    views = execute("SELECT COALESCE(SUM(views),0) AS c FROM movies", fetchone=True)["c"]
    text = get_translation("statistics", lang).format(movies=movies, views=views, users=users)
    bot.send_message(message.chat.id, text)

@bot.message_handler(func=lambda m: m.text == "👤 Profil")
def profile_handler(message):
    if not ensure_access(message): return
    register_user(message.from_user)
    user = get_user(message.from_user.id)
    lang = user["language"] or "uz"
    watched = execute("SELECT COUNT(*) AS c FROM watch_log WHERE user_id=?",(message.from_user.id,), fetchone=True)["c"]
    ad_count = execute("SELECT COUNT(*) AS c FROM ad_views WHERE user_id=? AND status='completed'", (message.from_user.id,), fetchone=True)["c"]
    bonus = user["bonus_points"] or 0
    text = get_translation("profile", lang).format(
        name=safe(user['full_name']),
        id=user['user_id'],
        username=safe(user['username'] or 'Yo‘q'),
        watched=watched,
        ads=ad_count,
        bonus=bonus,
        lang=user['language'],
        joined=safe(user['joined_at'])
    )
    bot.send_message(message.chat.id, text)

# ========== TIL SOZLAMALARI ==========
@bot.message_handler(func=lambda m: m.text == "🌐 Til")
def language_menu(message):
    if not ensure_access(message): return
    kb = types.InlineKeyboardMarkup(row_width=3)
    kb.add(types.InlineKeyboardButton("🇺🇿 O'zbekcha", callback_data="lang_uz"),
           types.InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru"),
           types.InlineKeyboardButton("🇬🇧 English", callback_data="lang_en"))
    bot.send_message(message.chat.id, "🌐 Tilni tanlang / Выберите язык / Choose language:", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("lang_"))
def language_callback(call):
    lang = call.data.split("_")[1]
    execute("UPDATE users SET language=? WHERE user_id=?", (lang, call.from_user.id))
    bot.answer_callback_query(call.id, f"Til {lang} ga o'zgartirildi.")
    bot.send_message(call.message.chat.id, f"✅ Til {lang} ga o'zgartirildi.", reply_markup=main_keyboard(call.from_user.id))

# ========== ADMINGA XABAR ==========
@bot.message_handler(func=lambda m: m.text == "💬 Adminga xabar")
def ask_admin_msg(message):
    if not ensure_access(message): return
    set_state(message.from_user.id, "waiting_admin_message")
    bot.send_message(message.chat.id, "💬 Adminga yuboriladigan xabarni yozing:")

# ========== ADMIN: KINOLAR ==========
admin_movie_page = {}

@bot.message_handler(func=lambda m: m.text == "🎬 Kinolar")
def movies_section(message):
    if not is_admin(message.from_user.id): return
    clear_state(message.from_user.id)
    bot.send_message(message.chat.id, "🎬 Kinolar bo‘limi", reply_markup=movies_keyboard())

@bot.message_handler(func=lambda m: m.text == "➕ Kino qo‘shish")
def add_movie_start(message):
    if not is_admin(message.from_user.id): return
    set_state(message.from_user.id, "add_movie_video")
    bot.send_message(message.chat.id, "🎥 Kino videosini (yoki hujjat) yuboring:\n(Kodni keyin o‘zingiz kiritasiz yoki bo‘sh qoldirsangiz avtomatik yaratiladi)")

@bot.message_handler(func=lambda m: m.text == "🗑 Kino o‘chirish")
def delete_movie_start(message):
    if not is_admin(message.from_user.id): return
    set_state(message.from_user.id, "delete_movie_code")
    bot.send_message(message.chat.id, "🗑 O‘chiriladigan kino kodini yoki ID raqamini yuboring:")

@bot.message_handler(func=lambda m: m.text == "📋 Barcha kinolar")
def all_movies_handler(message):
    if not is_admin(message.from_user.id): return
    categories = execute("SELECT id, name FROM categories ORDER BY name", fetchall=True)
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("📋 Hammasi", callback_data="movie_filter_all"))
    for cat in categories:
        kb.add(types.InlineKeyboardButton(cat["name"], callback_data=f"movie_filter_{cat['id']}"))
    kb.add(types.InlineKeyboardButton("❌ Filtrni bekor qilish", callback_data="movie_filter_none"))
    bot.send_message(message.chat.id, "📋 Kategoriya bo'yicha filtrlang yoki hammasini ko'ring:", reply_markup=kb)
    set_state(message.from_user.id, "movie_filter_select")

@bot.callback_query_handler(func=lambda c: c.data.startswith("movie_filter_"))
def movie_filter_callback(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Ruxsat yo'q")
        return
    filter_val = call.data.split("_")[2]
    set_state(call.from_user.id, "movie_filter_active", filter_val)
    bot.answer_callback_query(call.id)
    admin_movie_page[call.from_user.id] = 0
    send_movie_page(call.message.chat.id, call.from_user.id, 0, filter_val)

def send_movie_page(chat_id, user_id, page, filter_cat=None):
    per_page = 10
    offset = page * per_page
    if filter_cat and filter_cat.isdigit():
        movies = execute("""SELECT m.id, m.code, m.title, m.views, m.year, m.genre, m.rating_count, m.rating_sum
                            FROM movies m 
                            JOIN movie_categories mc ON m.id = mc.movie_id 
                            WHERE mc.category_id = ? 
                            ORDER BY m.id DESC LIMIT ? OFFSET ?""", 
                         (int(filter_cat), per_page, offset), fetchall=True)
        total = execute("SELECT COUNT(*) AS c FROM movie_categories WHERE category_id=?", (int(filter_cat),), fetchone=True)["c"]
    else:
        movies = execute("SELECT id, code, title, views, year, genre, rating_count, rating_sum FROM movies ORDER BY id DESC LIMIT ? OFFSET ?", (per_page, offset), fetchall=True)
        total = execute("SELECT COUNT(*) AS c FROM movies", fetchone=True)["c"]
    total_pages = (total + per_page - 1) // per_page if total > 0 else 1
    if not movies:
        bot.send_message(chat_id, "Bu kategoriyada kinolar mavjud emas.", reply_markup=movies_keyboard())
        return
    lines = [f"📋 <b>BARCHA KINOLAR (sahifa {page+1}/{total_pages})</b>\n"]
    for m in movies:
        avg = round(m['rating_sum'] / m['rating_count'], 1) if m['rating_count'] > 0 else 0
        year_info = f" [{m['year']}]" if m['year'] else ""
        genre_info = f" {m['genre']}" if m['genre'] else ""
        rating_info = f" ⭐{avg}" if avg else ""
        lines.append(f"ID: <code>{m['id']}</code> | <b>{safe(m['title'])}{year_info}{genre_info}</b>\n   Kod: <code>{safe(m['code'])}</code> | 👁 {m['views']}{rating_info}")
    full = "\n\n".join(lines)
    kb = types.InlineKeyboardMarkup(row_width=2)
    if page > 0:
        kb.add(types.InlineKeyboardButton("⬅️ Oldingi", callback_data=f"movie_page_{page-1}_{filter_cat or 'all'}"))
    if page < total_pages - 1:
        kb.add(types.InlineKeyboardButton("➡️ Keyingi", callback_data=f"movie_page_{page+1}_{filter_cat or 'all'}"))
    kb.add(types.InlineKeyboardButton("🔄 Yangilash", callback_data=f"movie_page_{page}_{filter_cat or 'all'}"))
    bot.send_message(chat_id, full, reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("movie_page_"))
def movie_page_callback(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Ruxsat yo‘q")
        return
    parts = call.data.split("_")
    page = int(parts[2])
    filter_cat = parts[3] if len(parts) > 3 and parts[3] != 'all' else None
    admin_movie_page[call.from_user.id] = page
    bot.answer_callback_query(call.id)
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        pass
    send_movie_page(call.message.chat.id, call.from_user.id, page, filter_cat)

@bot.message_handler(func=lambda m: m.text == "📝 Kino tahrirlash")
def edit_movie_start(message):
    if not is_admin(message.from_user.id): return
    set_state(message.from_user.id, "edit_movie_select")
    bot.send_message(message.chat.id, "✏️ Tahrirlanadigan kinoning ID raqamini yoki kodini yuboring:")

@bot.message_handler(func=lambda m: m.text == "🔙 Admin panelga qaytish")
def back_to_admin_panel(message):
    if not is_admin(message.from_user.id): return
    clear_state(message.from_user.id)
    bot.send_message(message.chat.id, "🛠 <b>Admin panel</b>", reply_markup=admin_keyboard())

# ========== ADMIN: KATEGORIYALAR ==========
@bot.message_handler(func=lambda m: m.text == "📂 Kategoriyalar")
def categories_admin(message):
    if not is_admin(message.from_user.id): return
    clear_state(message.from_user.id)
    bot.send_message(message.chat.id, "📂 Kategoriyalar bo‘limi", reply_markup=categories_keyboard())

@bot.message_handler(func=lambda m: m.text == "➕ Kategoriya qo‘shish")
def add_category_start(message):
    if not is_admin(message.from_user.id): return
    set_state(message.from_user.id, "add_category_name")
    bot.send_message(message.chat.id, "➕ Yangi kategoriya nomini yuboring:")

@bot.message_handler(func=lambda m: m.text == "🗑 Kategoriya o‘chirish")
def delete_category_start(message):
    if not is_admin(message.from_user.id): return
    categories = execute("SELECT id, name FROM categories ORDER BY name", fetchall=True)
    if not categories:
        bot.send_message(message.chat.id, "Kategoriyalar mavjud emas.", reply_markup=categories_keyboard())
        return
    kb = types.InlineKeyboardMarkup(row_width=2)
    for cat in categories:
        kb.add(types.InlineKeyboardButton(cat["name"], callback_data=f"del_cat_{cat['id']}"))
    kb.add(types.InlineKeyboardButton("❌ Bekor qilish", callback_data="del_cat_cancel"))
    bot.send_message(message.chat.id, "🗑 O‘chiriladigan kategoriyani tanlang:", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("del_cat_"))
def delete_category_callback(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Ruxsat yo'q")
        return
    if call.data == "del_cat_cancel":
        bot.answer_callback_query(call.id, "Bekor qilindi")
        bot.send_message(call.message.chat.id, "❌ Bekor qilindi.", reply_markup=categories_keyboard())
        return
    cat_id = int(call.data.split("_")[2])
    execute("DELETE FROM movie_categories WHERE category_id=?", (cat_id,))
    execute("DELETE FROM categories WHERE id=?", (cat_id,))
    bot.answer_callback_query(call.id, "Kategoriya o'chirildi")
    bot.send_message(call.message.chat.id, "✅ Kategoriya o'chirildi.", reply_markup=categories_keyboard())

@bot.message_handler(func=lambda m: m.text == "📋 Barcha kategoriyalar")
def list_categories(message):
    if not is_admin(message.from_user.id): return
    cats = execute("SELECT id, name, created_at FROM categories ORDER BY name", fetchall=True)
    if not cats:
        bot.send_message(message.chat.id, "Kategoriyalar mavjud emas.", reply_markup=categories_keyboard())
        return
    lines = ["📋 <b>BARCHA KATEGORIYALAR</b>\n"]
    for c in cats:
        count = execute("SELECT COUNT(*) AS cnt FROM movie_categories WHERE category_id=?", (c["id"],), fetchone=True)["cnt"]
        lines.append(f"ID: <code>{c['id']}</code> | <b>{safe(c['name'])}</b> (kinolar: {count})")
    bot.send_message(message.chat.id, "\n\n".join(lines), reply_markup=categories_keyboard())

@bot.message_handler(func=lambda m: m.text == "🏷 Kategoriya qo‘shish")
def assign_category_start(message):
    if not is_admin(message.from_user.id): return
    set_state(message.from_user.id, "assign_category_movie")
    bot.send_message(message.chat.id, "🏷 Kinoning ID raqamini yoki kodini yuboring:")

# ========== ADMIN: KANALLAR ==========
@bot.message_handler(func=lambda m: m.text == "📡 Kanallar")
def channels_admin(message):
    if not is_admin(message.from_user.id): return
    channels = get_channels()
    lines = ["📡 <b>MAJBURIY OBUNA KANALLARI</b>\n"]
    if channels:
        for ch in channels:
            ch_type = "🔒 Maxfiy" if ch["channel_type"] == "private" else "📢 Ochiq"
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
    set_state(call.from_user.id, "channel_add_type")
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("📢 Ochiq", callback_data="ch_type_public"),
           types.InlineKeyboardButton("🔒 Maxfiy", callback_data="ch_type_private"))
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "Kanal turini tanlang:", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data in ["ch_type_public", "ch_type_private"])
def channel_type_callback(call):
    if not is_admin(call.from_user.id): return
    ch_type = call.data.split("_")[2]
    set_state(call.from_user.id, "channel_add_username", ch_type)
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, f"➕ { 'Ochiq' if ch_type=='public' else 'Maxfiy' } kanal uchun username yoki link yuboring:\nMasalan: @kanal yoki https://t.me/kanal")

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
        "--- AdsGram ---\n"
        f"API URL: <code>{safe(get_setting('adsgram_api_url'))}</code>\n"
        f"Placement ID: <code>{safe(get_setting('adsgram_placement_id'))}</code>\n"
        f"API Key: <code>{safe(get_setting('adsgram_api_key'))}</code>\n"
        "--- Bepul kinolar ---\n"
        f"Bepul kinolar soni: <b>{get_setting('free_movies_limit')}</b>\n"
        f"Reklama soni (limitdan keyin): <b>{get_setting('ads_after_limit')}</b>\n"
        f"Reklama vaqti (soniya): <b>{get_setting('ad_timeout_seconds')}</b>\n"
        "--- Reklama cheklovlari ---\n"
        f"Kunlik limit: <b>{get_setting('daily_ad_limit')}</b>\n"
        f"Soatlik limit: <b>{get_setting('hourly_ad_limit')}</b>\n"
        "--- Bonus tizimi ---\n"
        f"Bonus uchun reklama soni: <b>{get_setting('bonus_threshold')}</b>\n"
        "--- Bildirishnoma kanali ---\n"
        f"Kanal: <code>{safe(get_setting('notification_channel'))}</code>"
    )
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("AdsGram API URL", callback_data="ad_adsgram_url"),
           types.InlineKeyboardButton("AdsGram Placement ID", callback_data="ad_adsgram_placement"),
           types.InlineKeyboardButton("AdsGram API Key", callback_data="ad_adsgram_key"),
           types.InlineKeyboardButton("Bepul kino soni", callback_data="ad_free_limit"),
           types.InlineKeyboardButton("Reklama soni", callback_data="ad_ads_count"),
           types.InlineKeyboardButton("Reklama vaqti", callback_data="ad_timeout"),
           types.InlineKeyboardButton("Kunlik reklama limiti", callback_data="ad_daily_limit"),
           types.InlineKeyboardButton("Soatlik reklama limiti", callback_data="ad_hourly_limit"),
           types.InlineKeyboardButton("Bonus uchun reklama soni", callback_data="ad_bonus_threshold"),
           types.InlineKeyboardButton("Bildirishnoma kanali", callback_data="ad_notification_channel"))
    bot.send_message(message.chat.id, text, reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data in [
    "ad_adsgram_url","ad_adsgram_placement","ad_adsgram_key",
    "ad_free_limit","ad_ads_count","ad_timeout",
    "ad_daily_limit","ad_hourly_limit","ad_bonus_threshold","ad_notification_channel"])
def ad_callback(call):
    if not is_admin(call.from_user.id): return bot.answer_callback_query(call.id, "Ruxsat yo‘q")
    mapping = {
        "ad_adsgram_url": ("change_ad_adsgram_url", "Yangi AdsGram API URL yuboring:"),
        "ad_adsgram_placement": ("change_ad_adsgram_placement", "Yangi Placement ID yuboring:"),
        "ad_adsgram_key": ("change_ad_adsgram_key", "Yangi API Key yuboring:"),
        "ad_free_limit": ("change_ad_free_limit", "Bepul kinolar sonini kiriting (masalan 3):"),
        "ad_ads_count": ("change_ad_ads_count", "Limitdan keyin nechta reklama ko‘rilsin? (son):"),
        "ad_timeout": ("change_ad_timeout", "Reklama vaqtini soniyalarda kiriting (masalan 60):"),
        "ad_daily_limit": ("change_ad_daily_limit", "Kunlik reklama limitini kiriting (masalan 10):"),
        "ad_hourly_limit": ("change_ad_hourly_limit", "Soatlik reklama limitini kiriting (masalan 3):"),
        "ad_bonus_threshold": ("change_ad_bonus_threshold", "Bonus uchun nechta reklama ko‘rish kerak? (masalan 5):"),
        "ad_notification_channel": ("change_ad_notification_channel", "Bildirishnoma yuboriladigan kanal username yoki ID sini yuboring (masalan @kanal yoki -100123456):"),
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

@bot.callback_query_handler(func=lambda c: c.data.startswith("reply_user:"))
def reply_user_callback(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Ruxsat yo‘q")
        return
    target_id = int(call.data.split(":",1)[1])
    set_state(call.from_user.id, "reply_to_user", str(target_id))
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, f"✍️ Foydalanuvchiga (<code>{target_id}</code>) yuboriladigan xabarni yozing:")

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
    today = datetime.now().strftime("%Y-%m-%d")
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    ad_today = execute("SELECT COUNT(*) AS c FROM ad_views WHERE status='completed' AND date(clicked_at)=?", (today,), fetchone=True)["c"]
    ad_week = execute("SELECT COUNT(*) AS c FROM ad_views WHERE status='completed' AND date(clicked_at)>=?", (week_ago,), fetchone=True)["c"]
    top_users = execute("""SELECT user_id, COUNT(*) AS cnt, MAX(clicked_at) AS last 
                           FROM ad_views WHERE status='completed' 
                           GROUP BY user_id ORDER BY cnt DESC LIMIT 5""", fetchall=True)
    top_text = "\n🏆 <b>Eng ko‘p reklama ko‘rganlar</b>\n"
    if top_users:
        for i, u in enumerate(top_users, 1):
            user = get_user(u["user_id"])
            name = user["full_name"] if user else str(u["user_id"])
            top_text += f"{i}. {safe(name)} – {u['cnt']} marta (oxirgi: {u['last'][:10]})\n"
    else:
        top_text += "Hali ma'lumot yo'q."
    bot.send_message(message.chat.id,
        f"📊 <b>ADMIN STATISTIKA</b>\n\n"
        f"👥 Foydalanuvchilar: <b>{users}</b> (bloklangan: {blocked})\n"
        f"🎬 Kinolar: <b>{movies}</b>\n"
        f"👁 Ko‘rishlar: <b>{views}</b>\n"
        f"📣 Reklama bosilgan (jami): <b>{ad_views}</b>\n"
        f"📅 Bugun: <b>{ad_today}</b> | Haftalik: <b>{ad_week}</b>\n\n"
        f"{top_text}")

# ========== CSV IMPORT/EXPORT ==========
@bot.message_handler(func=lambda m: m.text == "📤 CSV import/export")
def csv_menu(message):
    if not is_admin(message.from_user.id): return
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("📥 CSV import", callback_data="csv_import"),
           types.InlineKeyboardButton("📤 CSV export", callback_data="csv_export"))
    kb.add(types.InlineKeyboardButton("🔙 Orqaga", callback_data="csv_back"))
    bot.send_message(message.chat.id, "📤 CSV import/export bo‘limi", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data == "csv_export")
def csv_export_callback(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Ruxsat yo'q")
        return
    bot.answer_callback_query(call.id, "CSV yuklanmoqda...")
    movies = execute("SELECT id, code, title, caption, file_id, file_type, views, year, genre, description, created_at FROM movies ORDER BY id", fetchall=True)
    if not movies:
        bot.send_message(call.message.chat.id, "Kinolar mavjud emas.")
        return
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "code", "title", "caption", "file_id", "file_type", "views", "year", "genre", "description", "created_at"])
    for m in movies:
        writer.writerow([m["id"], m["code"], m["title"], m["caption"], m["file_id"], m["file_type"], m["views"], m["year"], m["genre"], m["description"], m["created_at"]])
    output.seek(0)
    bot.send_document(call.message.chat.id, io.BytesIO(output.getvalue().encode('utf-8')), visible_file_name="kinolar.csv")

@bot.callback_query_handler(func=lambda c: c.data == "csv_import")
def csv_import_callback(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Ruxsat yo'q")
        return
    set_state(call.from_user.id, "csv_import_wait")
    bot.answer_callback_query(call.id, "CSV faylni yuboring")
    bot.send_message(call.message.chat.id, "📥 CSV faylni yuboring (format: code,title,caption,file_id,file_type,year,genre,description)")

@bot.message_handler(content_types=["document"],
                     func=lambda m: is_admin(m.from_user.id) and get_state(m.from_user.id)[0] == "csv_import_wait")
def csv_import_handler(message):
    user_id = message.from_user.id
    clear_state(user_id)
    file_info = bot.get_file(message.document.file_id)
    file_content = bot.download_file(file_info.file_path).decode('utf-8')
    reader = csv.reader(io.StringIO(file_content))
    added = 0
    errors = 0
    for row in reader:
        if len(row) < 3:
            continue
        code, title, caption = row[0], row[1], row[2]
        file_id = row[3] if len(row) > 3 else ""
        file_type = row[4] if len(row) > 4 else "video"
        year = row[5] if len(row) > 5 else ""
        genre = row[6] if len(row) > 6 else ""
        description = row[7] if len(row) > 7 else ""
        if not file_id:
            errors += 1
            continue
        if execute("SELECT id FROM movies WHERE code=?", (code,), fetchone=True):
            errors += 1
            continue
        execute("INSERT INTO movies(code,title,caption,file_id,file_type,year,genre,description,created_at,added_by) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (code, title, caption, file_id, file_type, year, genre, description, now_text(), user_id))
        added += 1
    bot.send_message(message.chat.id, f"✅ CSV import yakunlandi.\nQo'shilgan: {added}\nXatoliklar: {errors}")

@bot.callback_query_handler(func=lambda c: c.data == "csv_back")
def csv_back_callback(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Ruxsat yo'q")
        return
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "🛠 <b>Admin panel</b>", reply_markup=admin_keyboard())

# ========== 8-QISM: ADMIN DASHBOARD ==========
@bot.message_handler(func=lambda m: m.text == "📈 Dashboard")
def admin_dashboard(message):
    if not is_admin(message.from_user.id): return
    total_movies = execute("SELECT COUNT(*) AS c FROM movies", fetchone=True)["c"]
    total_views = execute("SELECT COALESCE(SUM(views),0) AS c FROM movies", fetchone=True)["c"]
    total_reviews = execute("SELECT COUNT(*) AS c FROM reviews", fetchone=True)["c"]
    top_rated = execute("""SELECT id, code, title, rating_sum, rating_count, 
                            CAST(rating_sum AS FLOAT) / rating_count AS avg 
                            FROM movies WHERE rating_count > 0 
                            ORDER BY avg DESC LIMIT 5""", fetchall=True)
    most_viewed = execute("SELECT id, code, title, views FROM movies ORDER BY views DESC LIMIT 5", fetchall=True)
    most_reviewed = execute("""SELECT m.id, m.code, m.title, COUNT(r.id) AS cnt 
                               FROM movies m JOIN reviews r ON m.id = r.movie_id 
                               GROUP BY m.id ORDER BY cnt DESC LIMIT 5""", fetchall=True)
    text = "📈 <b>ADMIN DASHBOARD</b>\n\n"
    text += f"🎬 Jami kinolar: <b>{total_movies}</b>\n"
    text += f"👁 Jami ko‘rishlar: <b>{total_views}</b>\n"
    text += f"📝 Jami izohlar: <b>{total_reviews}</b>\n\n"
    text += "⭐ <b>Eng yaxshi reytingli kinolar</b>\n"
    if top_rated:
        for m in top_rated:
            avg = round(m["avg"], 1)
            text += f"  {avg} ⭐ {safe(m['title'])} (Kod: {safe(m['code'])})\n"
    else:
        text += "  Hali baholangan kino yo'q.\n"
    text += "\n👁 <b>Eng ko‘p ko‘rilgan kinolar</b>\n"
    if most_viewed:
        for m in most_viewed:
            text += f"  {m['views']} 👁 {safe(m['title'])} (Kod: {safe(m['code'])})\n"
    else:
        text += "  Hali ko'rilgan kino yo'q.\n"
    text += "\n📝 <b>Eng ko‘p izoh yozilgan kinolar</b>\n"
    if most_reviewed:
        for m in most_reviewed:
            text += f"  {m['cnt']} izoh | {safe(m['title'])} (Kod: {safe(m['code'])})\n"
    else:
        text += "  Hali izoh yo'q.\n"
    bot.send_message(message.chat.id, text)

# ========== KANALGA BILDIRISHNOMA ==========
def notify_channel(movie):
    channel = get_setting("notification_channel")
    if not channel:
        return
    text = (
        f"🎬 <b>Yangi kino qo'shildi!</b>\n\n"
        f"<b>{safe(movie['title'])}</b>\n"
        f"🔢 Kod: <code>{safe(movie['code'])}</code>\n"
    )
    if movie['year']:
        text += f"📅 Yil: {movie['year']}\n"
    if movie['genre']:
        text += f"🎭 Janr: {movie['genre']}\n"
    if movie['description']:
        text += f"📝 {movie['description']}\n"
    text += f"\n👁 Ko‘rish uchun botga kiring: @{bot.get_me().username}"
    try:
        bot.send_message(channel, text)
    except Exception as e:
        logging.error(f"Kanalga xabar yuborishda xato: {e}")
        notify_admins_about_error(f"Kanalga bildirishnoma yuborishda xato: {e}")

# ========== UNIVERSAL MATN HANDLER (TUZATILGAN) ==========
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
    lang = get_user(user_id)["language"] if get_user(user_id) else "uz"

    if not state:
        if not is_admin(user_id):
            bot.send_message(message.chat.id, get_translation("welcome", lang), reply_markup=main_keyboard(user_id))
        else:
            bot.send_message(message.chat.id, get_translation("admin_panel", lang), reply_markup=admin_keyboard())
        return

    # ========== KINO KODI ==========
    if state == "waiting_movie_code":
        code = text
        movie = execute("SELECT * FROM movies WHERE code=?", (code,), fetchone=True)
        execute("INSERT INTO search_log(user_id, code, found, searched_at) VALUES(?,?,?,?)", (user_id, code, 1 if movie else 0, now_text()))
        clear_state(user_id)
        if not movie:
            bot.send_message(message.chat.id, get_translation("no_movie", lang), reply_markup=main_keyboard(user_id))
            return
        user = get_user(user_id)
        bonus = user["bonus_points"] or 0
        free_limit = int(get_setting("free_movies_limit"))
        free_count = user["free_movies_count"] or 0
        if bonus > 0:
            send_movie_to_user(message.chat.id, user_id, movie, use_bonus=True)
        elif free_count < free_limit:
            send_movie_to_user(message.chat.id, user_id, movie)
        else:
            ads_needed = int(get_setting("ads_after_limit"))
            execute("UPDATE users SET pending_ads_required=?, pending_ads_completed=0, pending_movie_id=? WHERE user_id=?",
                    (ads_needed, movie["id"], user_id))
            send_ad_message(message.chat.id, user_id, movie["id"])
        return

    # ========== ADMINGA XABAR ==========
    if state == "waiting_admin_message":
        msg_id = execute("INSERT INTO messages(user_id, message_text, created_at) VALUES(?,?,?)", (user_id, text, now_text()))
        clear_state(user_id)
        bot.send_message(message.chat.id, get_translation("message_sent", lang), reply_markup=main_keyboard(user_id))
        user = get_user(user_id)
        for aid in ADMIN_IDS:
            try:
                bot.send_message(aid,
                    f"📩 <b>Yangi xabar #{msg_id}</b>\n\n"
                    f"👤 {safe(user['full_name'])} (<code>{user_id}</code>)\n"
                    f"💬 {safe(text)}",
                    reply_markup=types.InlineKeyboardMarkup().add(
                        types.InlineKeyboardButton("✍️ Javob yozish", callback_data=f"reply_user:{user_id}")))
            except:
                pass
        return

    # ========== ADMIN: KINO QO'SHISH ==========
    if state == "add_movie_caption" and is_admin(user_id):
        parts = data.split("|", 1)
        if len(parts) != 2:
            clear_state(user_id)
            bot.send_message(message.chat.id, "❌ Ma'lumot buzilgan. Qaytadan urinib ko'ring.")
            return
        file_type, file_id = parts
        title = text.splitlines()[0].strip()
        title = re.sub(r"^[🎬\s]+", "", title).strip() or "Nomsiz kino"
        set_state(user_id, "add_movie_code", f"{file_type}|{file_id}|{title}|{text}")
        bot.send_message(message.chat.id, "🔢 Kino uchun unikal kod yuboring (yoki '0' yozib avtomatik yaratish):")
        return

    if state == "add_movie_code" and is_admin(user_id):
        code = text.replace(" ", "")
        if code == "0":
            code = generate_movie_code()
        if len(code) > 30:
            bot.send_message(message.chat.id, "❌ Kod juda uzun.")
            return
        if execute("SELECT id FROM movies WHERE code=?", (code,), fetchone=True):
            bot.send_message(message.chat.id, f"❌ Bu kod band. Boshqa kod yuboring yoki '0' bilan avtomatik yarating.")
            return
        parts = data.split("|", 3)
        if len(parts) != 4:
            clear_state(user_id)
            bot.send_message(message.chat.id, "❌ Ma'lumot buzilgan.")
            return
        file_type, file_id, title, caption = parts
        set_state(user_id, "add_movie_details", f"{file_type}|{file_id}|{title}|{caption}|{code}")
        bot.send_message(message.chat.id, "📅 Kino yilini yuboring (masalan: 2024, yoki '0' o'tkazib yuborish):")
        return

    if state == "add_movie_details" and is_admin(user_id):
        year = text.strip()
        if year == "0":
            year = ""
        set_state(user_id, "add_movie_genre", f"{data}|{year}")
        bot.send_message(message.chat.id, "🎭 Kino janrini yuboring (masalan: Drama, yoki '0' o'tkazib yuborish):")
        return

    if state == "add_movie_genre" and is_admin(user_id):
        genre = text.strip()
        if genre == "0":
            genre = ""
        set_state(user_id, "add_movie_description", f"{data}|{genre}")
        bot.send_message(message.chat.id, "📝 Kino tavsifini yuboring (yoki '0' o'tkazib yuborish):")
        return

    if state == "add_movie_description" and is_admin(user_id):
        description = text.strip()
        if description == "0":
            description = ""
        parts = data.split("|")
        if len(parts) < 7:
            clear_state(user_id)
            bot.send_message(message.chat.id, "❌ Ma'lumot buzilgan.")
            return
        file_type, file_id, title, caption, code = parts[0], parts[1], parts[2], parts[3], parts[4]
        year = parts[5] if len(parts) > 5 else ""
        genre = parts[6] if len(parts) > 6 else ""
        execute("INSERT INTO movies(code,title,caption,file_id,file_type,year,genre,description,created_at,added_by) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (code, title, caption, file_id, file_type, year, genre, description, now_text(), user_id))
        clear_state(user_id)
        movie = execute("SELECT * FROM movies WHERE code=?", (code,), fetchone=True)
        if movie:
            notify_channel(movie)
        bot.send_message(message.chat.id, f"✅ <b>{safe(title)}</b> qo‘shildi. Kod: <code>{safe(code)}</code>", reply_markup=admin_keyboard())
        return

    # ========== ADMIN: KINO O'CHIRISH ==========
    if state == "delete_movie_code" and is_admin(user_id):
        movie = None
        if text.isdigit():
            movie = execute("SELECT * FROM movies WHERE id=?", (int(text),), fetchone=True)
        if not movie:
            movie = execute("SELECT * FROM movies WHERE code=?", (text,), fetchone=True)
        clear_state(user_id)
        if not movie:
            bot.send_message(message.chat.id, "❌ Bunday kino topilmadi.", reply_markup=admin_keyboard())
            return
        execute("DELETE FROM movies WHERE id=?", (movie["id"],))
        execute("DELETE FROM watch_log WHERE movie_id=?", (movie["id"],))
        execute("DELETE FROM movie_categories WHERE movie_id=?", (movie["id"],))
        execute("DELETE FROM reviews WHERE movie_id=?", (movie["id"],))
        bot.send_message(message.chat.id, f"✅ <b>{safe(movie['title'])}</b> o‘chirildi.", reply_markup=admin_keyboard())
        return

    # ========== ADMIN: KINO TAHRIRLASH ==========
    if state == "edit_movie_select" and is_admin(user_id):
        movie = None
        if text.isdigit():
            movie = execute("SELECT * FROM movies WHERE id=?", (int(text),), fetchone=True)
        if not movie:
            movie = execute("SELECT * FROM movies WHERE code=?", (text,), fetchone=True)
        if not movie:
            bot.send_message(message.chat.id, "❌ Bunday kino topilmadi.", reply_markup=movies_keyboard())
            clear_state(user_id)
            return
        set_state(user_id, "edit_movie_field", str(movie["id"]))
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(types.InlineKeyboardButton("📝 Nom", callback_data="edit_title"),
               types.InlineKeyboardButton("📝 Sarlavha", callback_data="edit_caption"),
               types.InlineKeyboardButton("🔢 Kod", callback_data="edit_code"))
        kb.add(types.InlineKeyboardButton("📅 Yil", callback_data="edit_year"),
               types.InlineKeyboardButton("🎭 Janr", callback_data="edit_genre"),
               types.InlineKeyboardButton("📝 Tavsif", callback_data="edit_description"))
        kb.add(types.InlineKeyboardButton("❌ Bekor qilish", callback_data="edit_cancel"))
        avg = round(movie['rating_sum'] / movie['rating_count'], 1) if movie['rating_count'] > 0 else 0
        bot.send_message(message.chat.id,
            f"✏️ <b>{safe(movie['title'])}</b> (ID: {movie['id']}, Kod: <code>{safe(movie['code'])}</code>)\n"
            f"📅 Yil: {movie['year'] or '—'} | 🎭 Janr: {movie['genre'] or '—'}\n"
            f"⭐ Reyting: {avg} ({movie['rating_count']} ovoz)\n\nQaysi maydonni tahrirlaysiz?",
            reply_markup=kb)
        return

    if state in ["edit_movie_title", "edit_movie_caption", "edit_movie_code", "edit_movie_year", "edit_movie_genre", "edit_movie_description"] and is_admin(user_id):
        movie_id = int(data)
        movie = execute("SELECT * FROM movies WHERE id=?", (movie_id,), fetchone=True)
        if not movie:
            clear_state(user_id)
            bot.send_message(message.chat.id, "❌ Kino topilmadi.", reply_markup=movies_keyboard())
            return
        new_value = text.strip()
        if state == "edit_movie_code":
            if execute("SELECT id FROM movies WHERE code=? AND id!=?", (new_value, movie_id), fetchone=True):
                bot.send_message(message.chat.id, "❌ Bu kod band. Boshqa kod yuboring.")
                return
            execute("UPDATE movies SET code=? WHERE id=?", (new_value, movie_id))
        elif state == "edit_movie_title":
            execute("UPDATE movies SET title=? WHERE id=?", (new_value, movie_id))
        elif state == "edit_movie_caption":
            execute("UPDATE movies SET caption=? WHERE id=?", (new_value, movie_id))
        elif state == "edit_movie_year":
            execute("UPDATE movies SET year=? WHERE id=?", (new_value, movie_id))
        elif state == "edit_movie_genre":
            execute("UPDATE movies SET genre=? WHERE id=?", (new_value, movie_id))
        elif state == "edit_movie_description":
            execute("UPDATE movies SET description=? WHERE id=?", (new_value, movie_id))
        clear_state(user_id)
        bot.send_message(message.chat.id, f"✅ <b>Kino muvaffaqiyatli yangilandi.</b>", reply_markup=movies_keyboard())
        return

    # ========== ADMIN: KINO QIDIRISH ==========
    if state == "admin_search_movie" and is_admin(user_id):
        search_term = f"%{text}%"
        set_state(user_id, "admin_search_result", text)
        send_search_page(message.chat.id, user_id, text, 0)
        return

    # ========== ADMIN: KATEGORIYA QO'SHISH ==========
    if state == "add_category_name" and is_admin(user_id):
        name = text.strip()
        if not name:
            bot.send_message(message.chat.id, "❌ Kategoriya nomi bo'sh bo'lishi mumkin emas.")
            return
        if execute("SELECT id FROM categories WHERE name=?", (name,), fetchone=True):
            bot.send_message(message.chat.id, "❌ Bu nomdagi kategoriya allaqachon mavjud.")
            return
        execute("INSERT INTO categories(name, created_at) VALUES(?,?)", (name, now_text()))
        clear_state(user_id)
        bot.send_message(message.chat.id, f"✅ <b>{safe(name)}</b> kategoriyasi qo'shildi.", reply_markup=categories_keyboard())
        return

    # ========== ADMIN: KINOGA KATEGORIYA BIRIKTIRISH ==========
    if state == "assign_category_movie" and is_admin(user_id):
        movie = None
        if text.isdigit():
            movie = execute("SELECT * FROM movies WHERE id=?", (int(text),), fetchone=True)
        if not movie:
            movie = execute("SELECT * FROM movies WHERE code=?", (text,), fetchone=True)
        if not movie:
            bot.send_message(message.chat.id, "❌ Bunday kino topilmadi.", reply_markup=movies_keyboard())
            clear_state(user_id)
            return
        categories = execute("SELECT id, name FROM categories ORDER BY name", fetchall=True)
        if not categories:
            bot.send_message(message.chat.id, "Hali kategoriyalar mavjud emas. Avval kategoriya qo'shing.", reply_markup=movies_keyboard())
            clear_state(user_id)
            return
        kb = types.InlineKeyboardMarkup(row_width=2)
        for cat in categories:
            exists = execute("SELECT 1 FROM movie_categories WHERE movie_id=? AND category_id=?", (movie["id"], cat["id"]), fetchone=True)
            label = f"✅ {cat['name']}" if exists else cat["name"]
            kb.add(types.InlineKeyboardButton(label, callback_data=f"assign_cat_{movie['id']}_{cat['id']}"))
        kb.add(types.InlineKeyboardButton("❌ Bekor qilish", callback_data="assign_cancel"))
        set_state(user_id, "assign_category_select", str(movie["id"]))
        bot.send_message(message.chat.id, f"🏷 <b>{safe(movie['title'])}</b> uchun kategoriyalarni tanlang (belgilanganlari ✅):", reply_markup=kb)
        return

    if state == "assign_category_select" and is_admin(user_id):
        pass

    # ========== ADMIN: KANAL QO'SHISH (A) ==========
    if state == "channel_add_username" and is_admin(user_id):
        ch_type = data
        username = text.strip()
        if not (username.startswith("@") or username.startswith("https://")):
            username = normalize_channel_username(username)
        try:
            chat = bot.get_chat(username)
            chat_id = chat.id
            title = chat.title or username
            if username.startswith("@"):
                invite_link = f"https://t.me/{username.lstrip('@')}"
            else:
                invite_link = username
            set_state(user_id, "channel_add_link", f"{username}|{title}|{chat_id}|{ch_type}")
            bot.send_message(message.chat.id, f"📢 Kanal topildi: <b>{safe(title)}</b>\n\nKanalga havolani yuboring (https://t.me/...):")
        except ApiTelegramException as e:
            if "bot is not a member" in str(e) or "chat not found" in str(e):
                bot.send_message(message.chat.id,
                    f"❌ Kanal topilmadi yoki bot a'zo emas.\n"
                    f"🛠 Iltimos, botni kanalga <b>admin</b> qilib qo'shing va qaytadan urining.\n"
                    f"Yoki kanalning <b>chat_id</b> sini (masalan -100123456) yuboring.")
                set_state(user_id, "channel_add_chatid", f"{ch_type}|{username}")
            else:
                bot.send_message(message.chat.id, f"❌ Xatolik: {safe(e.description)}")
        except Exception as e:
            bot.send_message(message.chat.id, f"❌ Kutilmagan xato: {safe(e)}")

    # ========== ADMIN: KANAL QO'SHISH (B) – chat_id kiritish ==========
    if state == "channel_add_chatid" and is_admin(user_id):
        parts = data.split("|")
        ch_type = parts[0]
        username = parts[1]
        try:
            chat_id = int(text.strip())
        except ValueError:
            bot.send_message(message.chat.id, "❌ Noto‘g‘ri chat_id. Iltimos raqam yuboring.")
            return
        title = username.lstrip("@") if username.startswith("@") else username
        try:
            chat = bot.get_chat(chat_id)
            if chat.title:
                title = chat.title
        except:
            pass
        invite_link = f"https://t.me/{username.lstrip('@')}" if username.startswith("@") else username
        set_state(user_id, "channel_add_link", f"{username}|{title}|{chat_id}|{ch_type}")
        bot.send_message(message.chat.id, f"📢 Chat ID qabul qilindi: <b>{chat_id}</b>\n\nKanalga havolani yuboring (https://t.me/...):")

    # ========== ADMIN: KANAL QO'SHISH (C) – link kiritish ==========
    if state == "channel_add_link" and is_admin(user_id):
        parts = data.split("|", 3)
        if len(parts) != 4:
            clear_state(user_id)
            bot.send_message(message.chat.id, "❌ Ma'lumot buzilgan.")
            return
        username, title, chat_id, ch_type = parts
        invite_link = text.strip()
        if not invite_link.startswith("http"):
            bot.send_message(message.chat.id, "❌ To‘g‘ri havola yuboring.")
            return
        if execute("SELECT id FROM channels WHERE username=?", (username,), fetchone=True):
            bot.send_message(message.chat.id, "❌ Bu kanal allaqachon qo‘shilgan.")
            clear_state(user_id)
            return
        execute("INSERT INTO channels(title, username, invite_link, chat_id, channel_type) VALUES(?,?,?,?,?)",
                (title, username, invite_link, int(chat_id), ch_type))
        clear_state(user_id)
        bot.send_message(message.chat.id, f"✅ <b>{safe(title)}</b> kanali majburiy obunaga qo‘shildi (tur: {ch_type}).", reply_markup=admin_keyboard())

    # ========== ADMIN: KANAL O'CHIRISH ==========
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

    # ========== ADMIN: REKLAMA SOZLAMALARI O'ZGARTIRISH ==========
    if state.startswith("change_ad_") and is_admin(user_id):
        key_map = {
            "change_ad_adsgram_url": "adsgram_api_url",
            "change_ad_adsgram_placement": "adsgram_placement_id",
            "change_ad_adsgram_key": "adsgram_api_key",
            "change_ad_free_limit": "free_movies_limit",
            "change_ad_ads_count": "ads_after_limit",
            "change_ad_timeout": "ad_timeout_seconds",
            "change_ad_daily_limit": "daily_ad_limit",
            "change_ad_hourly_limit": "hourly_ad_limit",
            "change_ad_bonus_threshold": "bonus_threshold",
            "change_ad_notification_channel": "notification_channel",
        }
        key = key_map.get(state)
        if key in ("free_movies_limit", "ads_after_limit", "ad_timeout_seconds", "daily_ad_limit", "hourly_ad_limit", "bonus_threshold"):
            try:
                val = float(text)
                if val < 0: raise ValueError
            except ValueError:
                bot.send_message(message.chat.id, "❌ Musbat son kiriting.")
                return
        set_setting(key, text)
        clear_state(user_id)
        bot.send_message(message.chat.id, f"✅ {key} yangilandi.", reply_markup=admin_keyboard())

    # ========== ADMIN: FOYDALANUVCHI BOSHQARUVI ==========
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
        ad_count = execute("SELECT COUNT(*) AS c FROM ad_views WHERE user_id=? AND status='completed'", (target_id,), fetchone=True)["c"]
        review_count = execute("SELECT COUNT(*) AS c FROM reviews WHERE user_id=?", (target_id,), fetchone=True)["c"]
        bonus = target["bonus_points"] or 0
        bot.send_message(message.chat.id,
            f"👤 <b>FOYDALANUVCHI</b>\n\n"
            f"Ism: {safe(target['full_name'])}\n"
            f"ID: <code>{target_id}</code>\n"
            f"Username: {safe(target['username'] or 'Yo‘q')}\n"
            f"Holat: <b>{status}</b>\n"
            f"Bepul kinolar: {target['free_movies_count']}\n"
            f"Reklamalar: {ad_count}\n"
            f"Izohlar: {review_count}\n"
            f"Bonuslar: {bonus}\n"
            f"Til: {target['language']}",
            reply_markup=user_manage_keyboard(target_id))
        return

    # ========== ADMIN: USERGA JAVOB ==========
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

    # ========== IZOH QOLDIRISH ==========
    if state == "waiting_review":
        movie_id = int(data)
        if len(text) > 500:
            bot.send_message(message.chat.id, "❌ Izoh 500 belgidan oshmasligi kerak.")
            return
        r = execute("SELECT id FROM reviews WHERE user_id=? AND movie_id=?", (user_id, movie_id), fetchone=True)
        if r:
            execute("UPDATE reviews SET review_text=?, created_at=? WHERE id=?", (text, now_text(), r["id"]))
        else:
            execute("INSERT INTO reviews(user_id, movie_id, review_text, created_at) VALUES(?,?,?,?)", (user_id, movie_id, text, now_text()))
        clear_state(user_id)
        bot.send_message(message.chat.id, get_translation("review_saved", lang))
        return

    # ========== QIDIRUV FILTRI ==========
    if state in ["search_filter_year_wait", "search_filter_genre_wait", "search_filter_rating_wait"] and is_admin(user_id):
        current_data = data
        parts = current_data.split("|") if current_data else ["0","0","0"]
        if state == "search_filter_year_wait":
            parts[0] = text if text != "0" else "0"
            set_state(user_id, "admin_search_filters", "|".join(parts))
            bot.send_message(message.chat.id, f"✅ Yil: {text if text != '0' else 'o‘tkazib yuborildi'}")
        elif state == "search_filter_genre_wait":
            parts[1] = text if text != "0" else "0"
            set_state(user_id, "admin_search_filters", "|".join(parts))
            bot.send_message(message.chat.id, f"✅ Janr: {text if text != '0' else 'o‘tkazib yuborildi'}")
        elif state == "search_filter_rating_wait":
            try:
                if text != "0":
                    val = float(text)
                    if val < 0 or val > 5:
                        raise ValueError
                    parts[2] = text
                else:
                    parts[2] = "0"
            except ValueError:
                bot.send_message(message.chat.id, "❌ Reyting 0.0 dan 5.0 gacha bo'lishi kerak.")
                return
            set_state(user_id, "admin_search_filters", "|".join(parts))
            bot.send_message(message.chat.id, f"✅ Reyting: {text if text != '0' else 'o‘tkazib yuborildi'}")
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(types.InlineKeyboardButton("📅 Yil", callback_data="search_filter_year"),
               types.InlineKeyboardButton("🎭 Janr", callback_data="search_filter_genre"),
               types.InlineKeyboardButton("⭐ Reyting", callback_data="search_filter_rating"))
        kb.add(types.InlineKeyboardButton("🔍 Qidirish", callback_data="search_filter_go"))
        kb.add(types.InlineKeyboardButton("❌ Bekor qilish", callback_data="search_filter_cancel"))
        bot.send_message(message.chat.id, "Filtrlarni davom ettiring yoki 'Qidirish' tugmasini bosing.", reply_markup=kb)
        return

    bot.send_message(message.chat.id, "❌ Noto‘g‘ri ma’lumot. /cancel bilan bekor qiling.")

# ========== CALLBACK HANDLERLAR ==========
@bot.callback_query_handler(func=lambda c: c.data.startswith("edit_"))
def edit_movie_callback(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Ruxsat yo‘q")
        return
    action = call.data.split("_", 1)[1]
    user_id = call.from_user.id
    state, data = get_state(user_id)
    if state != "edit_movie_field":
        bot.answer_callback_query(call.id, "Holat xato")
        return
    movie_id = int(data)
    if action == "cancel":
        clear_state(user_id)
        bot.answer_callback_query(call.id, "Bekor qilindi")
        bot.send_message(call.message.chat.id, "❌ Tahrirlash bekor qilindi.", reply_markup=movies_keyboard())
        return
    field_map = {
        "title": "yangi nom",
        "caption": "yangi sarlavha (caption)",
        "code": "yangi kod",
        "year": "yangi yil",
        "genre": "yangi janr",
        "description": "yangi tavsif"
    }
    if action not in field_map:
        bot.answer_callback_query(call.id, "Noto‘g‘ri maydon")
        return
    set_state(user_id, f"edit_movie_{action}", str(movie_id))
    bot.answer_callback_query(call.id, f"{field_map[action]} ni yuboring")
    bot.send_message(call.message.chat.id, f"✏️ <b>{field_map[action].capitalize()}</b> ni yuboring:")

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and get_state(m.from_user.id)[0] in [
    "edit_movie_title", "edit_movie_caption", "edit_movie_code",
    "edit_movie_year", "edit_movie_genre", "edit_movie_description"
])
def edit_movie_value_handler(message):
    user_id = message.from_user.id
    state, data = get_state(user_id)
    movie_id = int(data)
    movie = execute("SELECT * FROM movies WHERE id=?", (movie_id,), fetchone=True)
    if not movie:
        clear_state(user_id)
        bot.send_message(message.chat.id, "❌ Kino topilmadi.", reply_markup=movies_keyboard())
        return
    new_value = message.text.strip()
    if state == "edit_movie_code":
        if execute("SELECT id FROM movies WHERE code=? AND id!=?", (new_value, movie_id), fetchone=True):
            bot.send_message(message.chat.id, "❌ Bu kod band. Boshqa kod yuboring.")
            return
        execute("UPDATE movies SET code=? WHERE id=?", (new_value, movie_id))
    elif state == "edit_movie_title":
        execute("UPDATE movies SET title=? WHERE id=?", (new_value, movie_id))
    elif state == "edit_movie_caption":
        execute("UPDATE movies SET caption=? WHERE id=?", (new_value, movie_id))
    elif state == "edit_movie_year":
        execute("UPDATE movies SET year=? WHERE id=?", (new_value, movie_id))
    elif state == "edit_movie_genre":
        execute("UPDATE movies SET genre=? WHERE id=?", (new_value, movie_id))
    elif state == "edit_movie_description":
        execute("UPDATE movies SET description=? WHERE id=?", (new_value, movie_id))
    clear_state(user_id)
    bot.send_message(message.chat.id, f"✅ <b>Kino muvaffaqiyatli yangilandi.</b>", reply_markup=movies_keyboard())

@bot.callback_query_handler(func=lambda c: c.data.startswith("assign_cat_"))
def assign_category_callback(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Ruxsat yo'q")
        return
    parts = call.data.split("_")
    movie_id = int(parts[2])
    cat_id = int(parts[3])
    exists = execute("SELECT 1 FROM movie_categories WHERE movie_id=? AND category_id=?", (movie_id, cat_id), fetchone=True)
    if exists:
        execute("DELETE FROM movie_categories WHERE movie_id=? AND category_id=?", (movie_id, cat_id))
        bot.answer_callback_query(call.id, "Kategoriya olib tashlandi")
    else:
        execute("INSERT INTO movie_categories(movie_id, category_id) VALUES(?,?)", (movie_id, cat_id))
        bot.answer_callback_query(call.id, "Kategoriya qo'shildi")
    categories = execute("SELECT id, name FROM categories ORDER BY name", fetchall=True)
    kb = types.InlineKeyboardMarkup(row_width=2)
    for cat in categories:
        exists = execute("SELECT 1 FROM movie_categories WHERE movie_id=? AND category_id=?", (movie_id, cat["id"]), fetchone=True)
        label = f"✅ {cat['name']}" if exists else cat["name"]
        kb.add(types.InlineKeyboardButton(label, callback_data=f"assign_cat_{movie_id}_{cat['id']}"))
    kb.add(types.InlineKeyboardButton("❌ Bekor qilish", callback_data="assign_cancel"))
    try:
        bot.edit_message_text(f"🏷 <b>{safe(execute('SELECT title FROM movies WHERE id=?', (movie_id,), fetchone=True)['title'])}</b> uchun kategoriyalarni tanlang (belgilanganlari ✅):",
                              call.message.chat.id, call.message.message_id, reply_markup=kb)
    except:
        pass

@bot.callback_query_handler(func=lambda c: c.data == "assign_cancel")
def assign_cancel_callback(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Ruxsat yo'q")
        return
    clear_state(call.from_user.id)
    bot.answer_callback_query(call.id, "Bekor qilindi")
    bot.send_message(call.message.chat.id, "❌ Bekor qilindi.", reply_markup=movies_keyboard())

# ========== REYTING VA IZOH CALLBACK ==========
@bot.callback_query_handler(func=lambda c: c.data.startswith("rate_movie_"))
def rate_movie_callback(call):
    user_id = call.from_user.id
    movie_id = int(call.data.split("_")[2])
    lang = get_user(user_id)["language"] or "uz"
    existing = execute("SELECT id FROM reviews WHERE user_id=? AND movie_id=?", (user_id, movie_id), fetchone=True)
    if existing:
        bot.answer_callback_query(call.id, get_translation("already_reviewed", lang), show_alert=True)
        return
    kb = types.InlineKeyboardMarkup(row_width=5)
    buttons = []
    for i in range(1, 6):
        buttons.append(types.InlineKeyboardButton("⭐" * i, callback_data=f"set_rating_{movie_id}_{i}"))
    kb.add(*buttons)
    kb.add(types.InlineKeyboardButton("❌ Bekor qilish", callback_data="cancel_rating"))
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, get_translation("rate_movie", lang), reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("set_rating_"))
def set_rating_callback(call):
    parts = call.data.split("_")
    movie_id = int(parts[2])
    rating = int(parts[3])
    user_id = call.from_user.id
    lang = get_user(user_id)["language"] or "uz"
    existing = execute("SELECT id FROM reviews WHERE user_id=? AND movie_id=?", (user_id, movie_id), fetchone=True)
    if existing:
        bot.answer_callback_query(call.id, get_translation("already_reviewed", lang), show_alert=True)
        return
    execute("INSERT INTO reviews(user_id, movie_id, rating, created_at) VALUES(?,?,?,?)", (user_id, movie_id, rating, now_text()))
    execute("UPDATE movies SET rating_sum = rating_sum + ?, rating_count = rating_count + 1 WHERE id = ?", (rating, movie_id))
    bot.answer_callback_query(call.id, get_translation("rating_saved", lang))
    bot.edit_message_text(get_translation("rating_saved", lang), call.message.chat.id, call.message.message_id)

@bot.callback_query_handler(func=lambda c: c.data == "cancel_rating")
def cancel_rating_callback(call):
    bot.answer_callback_query(call.id, "Bekor qilindi")
    bot.edit_message_text("❌ Bekor qilindi.", call.message.chat.id, call.message.message_id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("review_movie_"))
def review_movie_callback(call):
    user_id = call.from_user.id
    movie_id = int(call.data.split("_")[2])
    lang = get_user(user_id)["language"] or "uz"
    existing = execute("SELECT id FROM reviews WHERE user_id=? AND movie_id=? AND review_text IS NOT NULL", (user_id, movie_id), fetchone=True)
    if existing:
        bot.answer_callback_query(call.id, "Siz bu kinoga allaqachon izoh qoldirgansiz.", show_alert=True)
        return
    set_state(user_id, "waiting_review", str(movie_id))
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, get_translation("review_prompt", lang))

@bot.message_handler(func=lambda m: get_state(m.from_user.id)[0] == "waiting_review")
def review_text_handler(message):
    user_id = message.from_user.id
    state, data = get_state(user_id)
    movie_id = int(data)
    lang = get_user(user_id)["language"] or "uz"
    if len(message.text) > 500:
        bot.send_message(message.chat.id, "❌ Izoh 500 belgidan oshmasligi kerak.")
        return
    r = execute("SELECT id FROM reviews WHERE user_id=? AND movie_id=?", (user_id, movie_id), fetchone=True)
    if r:
        execute("UPDATE reviews SET review_text=?, created_at=? WHERE id=?", (message.text, now_text(), r["id"]))
    else:
        execute("INSERT INTO reviews(user_id, movie_id, review_text, created_at) VALUES(?,?,?,?)", (user_id, movie_id, message.text, now_text()))
    clear_state(user_id)
    bot.send_message(message.chat.id, get_translation("review_saved", lang))

# ========== ADMIN: VIDEO/DOCUMENT QABUL ==========
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
# Flask webhook
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
    except Exception as e:
        logging.exception("Webhook update qayta ishlashda xato")
        notify_admins_about_error(f"Webhook xatosi: {e}", exc_info=True)
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
    except Exception as e:
        logging.exception("Webhook o‘rnatishda xato")
        notify_admins_about_error(f"Webhook o'rnatishda xato: {e}", exc_info=True)

configure_webhook()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
