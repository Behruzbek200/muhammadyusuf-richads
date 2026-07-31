# ===================================================================
#         TARJIMON BOT – FAQAT TUGMALAR, ADSGRAM INTEGRATSIYASI
#                      TO‘LIQ ISHLAYDI
# ===================================================================

import os
import logging
import sqlite3
import datetime
import threading
import time
import json
from io import BytesIO
from flask import Flask, request
import telebot
from telebot import types
from deep_translator import GoogleTranslator

# ---------------------- KONFIGURATSIYA ----------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "123456789"))
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")  # https://your-app.onrender.com/webhook
ADSGRAM_AD_UNIT_ID = os.environ.get("ADSGRAM_AD_UNIT_ID", "YOUR_AD_UNIT_ID")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is required!")
if not WEBHOOK_URL:
    raise ValueError("WEBHOOK_URL environment variable is required!")

# ---------------------- LOGGER ----------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ---------------------- BOT, FLASK, TRANSLATOR ----------------------
bot = telebot.TeleBot(BOT_TOKEN, threaded=False)
translator = GoogleTranslator()
app = Flask(__name__)

# ---------------------- SQLITE ----------------------
DB_NAME = 'translator_bot.db'

def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        lang TEXT DEFAULT 'uz',
        limit_count INTEGER DEFAULT 10,
        used_count INTEGER DEFAULT 0,
        total_ads_watched INTEGER DEFAULT 0,
        registered_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS stats (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        total_users INTEGER DEFAULT 0,
        total_translations INTEGER DEFAULT 0,
        total_ads_watched INTEGER DEFAULT 0,
        daily_ads_count INTEGER DEFAULT 0,
        last_ad_reset TEXT DEFAULT CURRENT_DATE
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('daily_ad_limit', '5')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('ad_reward_limit', '5')")
    c.execute("INSERT OR IGNORE INTO stats (id, total_users, total_translations, total_ads_watched, daily_ads_count, last_ad_reset) VALUES (1, 0, 0, 0, 0, date('now'))")
    conn.commit()
    conn.close()
    logger.info("Ma'lumotlar bazasi tayyorlandi.")

# ---------------------- DB YORDAMCHILARI ----------------------
def get_user(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT user_id, lang, limit_count, used_count, total_ads_watched FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {
            'user_id': row[0],
            'lang': row[1],
            'limit': row[2],
            'used': row[3],
            'total_ads_watched': row[4]
        }
    return None

def create_or_update_user(user_id, lang='uz'):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, lang) VALUES (?, ?)", (user_id, lang))
    c.execute("UPDATE users SET lang = ? WHERE user_id = ?", (lang, user_id))
    conn.commit()
    conn.close()
    update_stats_total_users()

def update_user_limit(user_id, new_limit):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE users SET limit_count = ? WHERE user_id = ?", (new_limit, user_id))
    conn.commit()
    conn.close()

def update_user_used(user_id, new_used):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE users SET used_count = ? WHERE user_id = ?", (new_used, user_id))
    conn.commit()
    conn.close()
    update_stats_translations()

def update_user_ads(user_id, new_ads):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE users SET total_ads_watched = ? WHERE user_id = ?", (new_ads, user_id))
    conn.commit()
    conn.close()
    update_stats_ads()

def get_user_lang_db(user_id):
    user = get_user(user_id)
    return user['lang'] if user else 'uz'

def get_stats():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT total_users, total_translations, total_ads_watched, daily_ads_count, last_ad_reset FROM stats WHERE id = 1")
    row = c.fetchone()
    conn.close()
    if row:
        return {
            'total_users': row[0],
            'total_translations': row[1],
            'total_ads_watched': row[2],
            'daily_ads_count': row[3],
            'last_ad_reset': row[4]
        }
    return None

def update_stats_total_users():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    count = c.fetchone()[0]
    c.execute("UPDATE stats SET total_users = ? WHERE id = 1", (count,))
    conn.commit()
    conn.close()

def update_stats_translations():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT SUM(used_count) FROM users")
    total = c.fetchone()[0] or 0
    c.execute("UPDATE stats SET total_translations = ? WHERE id = 1", (total,))
    conn.commit()
    conn.close()

def update_stats_ads():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT SUM(total_ads_watched) FROM users")
    total = c.fetchone()[0] or 0
    c.execute("UPDATE stats SET total_ads_watched = ? WHERE id = 1", (total,))
    conn.commit()
    conn.close()

def get_setting(key):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def set_setting(key, value):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

def reset_daily_ads_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE stats SET daily_ads_count = 0, last_ad_reset = date('now') WHERE id = 1")
    conn.commit()
    conn.close()

def increment_daily_ads():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE stats SET daily_ads_count = daily_ads_count + 1 WHERE id = 1")
    conn.commit()
    conn.close()

def check_daily_reset_db():
    stats = get_stats()
    if stats:
        today = datetime.datetime.now().date().isoformat()
        if stats['last_ad_reset'] != today:
            reset_daily_ads_db()
            logger.info("Kunlik reklama hisobi tiklandi (DB).")
    threading.Timer(3600, check_daily_reset_db).start()

# ---------------------- VAQTINCHALIK MA'LUMOTLAR ----------------------
temp_data = {}

# ---------------------- FAYL YARATISH ----------------------
def create_translation_file(text, format_type='txt'):
    if format_type == 'txt':
        file_io = BytesIO()
        file_io.write(text.encode('utf-8'))
        file_io.seek(0)
        return file_io, 'txt'
    elif format_type == 'docx':
        try:
            from docx import Document
            doc = Document()
            doc.add_paragraph(text)
            file_io = BytesIO()
            doc.save(file_io)
            file_io.seek(0)
            return file_io, 'docx'
        except ImportError:
            return create_translation_file(text, 'txt')
    elif format_type == 'pdf':
        try:
            from reportlab.lib.pagesizes import letter
            from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
            from reportlab.lib.styles import getSampleStyleSheet
            file_io = BytesIO()
            doc = SimpleDocTemplate(file_io, pagesize=letter)
            styles = getSampleStyleSheet()
            story = []
            for line in text.split('\n'):
                story.append(Paragraph(line, styles['Normal']))
                story.append(Spacer(1, 12))
            doc.build(story)
            file_io.seek(0)
            return file_io, 'pdf'
        except ImportError:
            return create_translation_file(text, 'txt')
    else:
        return create_translation_file(text, 'txt')

# ---------------------- KEYBOARDLAR ----------------------
def get_main_menu_keyboard(lang='uz', user_id=None):
    texts = {
        'uz': {
            'translate': "📝 Tarjima",
            'tariffs': "📊 Tariflar",
            'limit': "💰 Limit",
            'ad': "📢 Reklama",
            'language': "🌐 Til",
            'account': "👤 Hisob",
            'admin': "🔐 Admin"
        },
        'ru': {
            'translate': "📝 Перевод",
            'tariffs': "📊 Тарифы",
            'limit': "💰 Лимит",
            'ad': "📢 Реклама",
            'language': "🌐 Язык",
            'account': "👤 Аккаунт",
            'admin': "🔐 Админ"
        },
        'en': {
            'translate': "📝 Translate",
            'tariffs': "📊 Tariffs",
            'limit': "💰 Limit",
            'ad': "📢 Ad",
            'language': "🌐 Language",
            'account': "👤 Account",
            'admin': "🔐 Admin"
        }
    }
    t = texts.get(lang, texts['uz'])
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    btns = [
        types.InlineKeyboardButton(t['translate'], callback_data="translate"),
        types.InlineKeyboardButton(t['tariffs'], callback_data="tariffs"),
        types.InlineKeyboardButton(t['limit'], callback_data="limit"),
        types.InlineKeyboardButton(t['ad'], callback_data="ad"),
        types.InlineKeyboardButton(t['language'], callback_data="language"),
        types.InlineKeyboardButton(t['account'], callback_data="account")
    ]
    if user_id == ADMIN_ID:
        btns.append(types.InlineKeyboardButton(t['admin'], callback_data="admin_panel"))
    keyboard.add(*btns)
    return keyboard

def get_language_keyboard():
    keyboard = types.InlineKeyboardMarkup(row_width=3)
    btn_uz = types.InlineKeyboardButton("🇺🇿 O'zbek", callback_data="lang_uz")
    btn_ru = types.InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru")
    btn_en = types.InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")
    keyboard.add(btn_uz, btn_ru, btn_en)
    return keyboard

def get_translation_languages_keyboard():
    keyboard = types.InlineKeyboardMarkup(row_width=3)
    buttons = [
        ("🇬🇧 Ingliz", "trans_en"),
        ("🇷🇺 Rus", "trans_ru"),
        ("🇺🇿 O'zbek", "trans_uz"),
        ("🇹🇷 Turk", "trans_tr"),
        ("🇫🇷 Fransuz", "trans_fr"),
        ("🇩🇪 Nemis", "trans_de"),
        ("🇪🇸 Ispan", "trans_es"),
        ("🇨🇳 Xitoy", "trans_zh-cn"),
        ("🇦🇪 Arab", "trans_ar")
    ]
    for label, callback in buttons:
        keyboard.add(types.InlineKeyboardButton(label, callback_data=callback))
    return keyboard

def get_result_format_keyboard(lang='uz'):
    texts = {
        'uz': {"text": "📝 Matn", "file": "📄 Fayl"},
        'ru': {"text": "📝 Текст", "file": "📄 Файл"},
        'en': {"text": "📝 Text", "file": "📄 File"}
    }
    t = texts.get(lang, texts['uz'])
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton(t['text'], callback_data="result_text"),
        types.InlineKeyboardButton(t['file'], callback_data="result_file")
    )
    return keyboard

def get_ad_watch_keyboard(lang='uz'):
    texts = {
        'uz': "🎥 Reklama ko'rish (+5 limit)",
        'ru': "🎥 Посмотреть рекламу (+5 лимит)",
        'en': "🎥 Watch ad (+5 limit)"
    }
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton(texts.get(lang, texts['uz']), callback_data="watch_ad"))
    return keyboard

def get_admin_keyboard(lang='uz'):
    texts = {
        'uz': {
            'add_limit': "➕ Limit qo'shish",
            'remove_limit': "➖ Limit o'chirish",
            'stats': "📊 Statistika",
            'ad_settings': "📢 Reklama sozlamalari",
            'send_ad': "📨 Reklama yuborish",
            'reset_daily': "🔄 Kunlik reklamani tiklash"
        },
        'ru': {
            'add_limit': "➕ Добавить лимит",
            'remove_limit': "➖ Удалить лимит",
            'stats': "📊 Статистика",
            'ad_settings': "📢 Настройки рекламы",
            'send_ad': "📨 Отправить рекламу",
            'reset_daily': "🔄 Сбросить дневную рекламу"
        },
        'en': {
            'add_limit': "➕ Add limit",
            'remove_limit': "➖ Remove limit",
            'stats': "📊 Statistics",
            'ad_settings': "📢 Ad settings",
            'send_ad': "📨 Send ad",
            'reset_daily': "🔄 Reset daily ads"
        }
    }
    t = texts.get(lang, texts['uz'])
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    btn1 = types.InlineKeyboardButton(t['add_limit'], callback_data="admin_add_limit")
    btn2 = types.InlineKeyboardButton(t['remove_limit'], callback_data="admin_remove_limit")
    btn3 = types.InlineKeyboardButton(t['stats'], callback_data="admin_stats")
    btn4 = types.InlineKeyboardButton(t['ad_settings'], callback_data="admin_ad_settings")
    btn5 = types.InlineKeyboardButton(t['send_ad'], callback_data="admin_send_ad")
    btn6 = types.InlineKeyboardButton(t['reset_daily'], callback_data="admin_reset_daily")
    keyboard.add(btn1, btn2, btn3, btn4, btn5, btn6)
    return keyboard

# ---------------------- BOT HANDLERLAR ----------------------
@bot.message_handler(commands=['start'])
def start_command(message):
    user_id = message.from_user.id
    user = get_user(user_id)
    if not user:
        create_or_update_user(user_id, 'uz')
        lang = 'uz'
    else:
        lang = user['lang']
    bot.send_message(
        message.chat.id,
        "Assalomu alaykum! Men tarjimon botman. Quyidagi menyudan foydalaning:",
        reply_markup=get_main_menu_keyboard(lang, user_id)
    )
    logger.info(f"User {user_id} started bot")

@bot.message_handler(commands=['admin'])
def admin_command(message):
    user_id = message.from_user.id
    if user_id != ADMIN_ID:
        bot.reply_to(message, "⛔ Siz admin emassiz!")
        return
    lang = get_user_lang_db(user_id)
    bot.send_message(
        message.chat.id,
        "🔐 Admin paneliga xush kelibsiz!",
        reply_markup=get_admin_keyboard(lang)
    )

@bot.message_handler(func=lambda message: True, content_types=['text'])
def handle_text(message):
    user_id = message.from_user.id
    text = message.text
    # Buyruqlarni e'tiborsiz qoldiramiz (ular maxsus handlerlar tomonidan ishlov oladi)
    if text.startswith('/'):
        return

    user = get_user(user_id)
    if not user:
        create_or_update_user(user_id, 'uz')
        lang = 'uz'
        bot.send_message(
            message.chat.id,
            "Assalomu alaykum! Men tarjimon botman. Quyidagi menyudan foydalaning:",
            reply_markup=get_main_menu_keyboard(lang, user_id)
        )
        return

    lang = user['lang']
    if user_id not in temp_data:
        temp_data[user_id] = {}
    temp_data[user_id]['temp_text'] = text
    temp_data[user_id]['is_file'] = False

    prompt = {
        'uz': "📝 Quyidagi matn tarjima qilinadi:\n\n",
        'ru': "📝 Следующий текст будет переведён:\n\n",
        'en': "📝 The following text will be translated:\n\n"
    }.get(lang, "📝 Quyidagi matn tarjima qilinadi:\n\n")

    bot.reply_to(
        message,
        prompt + text[:500] + ("..." if len(text) > 500 else ""),
        reply_markup=get_translation_languages_keyboard()
    )

@bot.message_handler(content_types=['document'])
def handle_document(message):
    user_id = message.from_user.id
    lang = get_user_lang_db(user_id)
    file_info = bot.get_file(message.document.file_id)
    file_name = message.document.file_name

    allowed = ('.txt', '.docx', '.pdf')
    if not file_name.lower().endswith(allowed):
        bot.reply_to(message, "❌ Faqat .txt, .docx va .pdf formatdagi fayllarni qabul qilaman.")
        return

    try:
        data = bot.download_file(file_info.file_path)
        if file_name.endswith('.txt'):
            content = data.decode('utf-8')
        elif file_name.endswith('.docx'):
            try:
                from docx import Document
                doc = Document(BytesIO(data))
                content = '\n'.join([p.text for p in doc.paragraphs])
            except ImportError:
                bot.reply_to(message, "❌ .docx o'qish uchun python-docx o'rnatilmagan.")
                return
        elif file_name.endswith('.pdf'):
            try:
                import PyPDF2
                reader = PyPDF2.PdfReader(BytesIO(data))
                content = ''
                for page in reader.pages:
                    content += page.extract_text() or ''
            except ImportError:
                bot.reply_to(message, "❌ .pdf o'qish uchun PyPDF2 o'rnatilmagan.")
                return
        else:
            bot.reply_to(message, "❌ Qo'llab-quvvatlanmaydigan format.")
            return
    except Exception as e:
        logger.error(f"Faylni o'qish xatosi: {e}")
        bot.reply_to(message, "❌ Faylni o'qib bo'lmadi.")
        return

    if user_id not in temp_data:
        temp_data[user_id] = {}
    temp_data[user_id]['temp_text'] = content
    temp_data[user_id]['is_file'] = True
    temp_data[user_id]['file_name'] = file_name

    prompt = {
        'uz': f"📄 Fayl qabul qilindi: {file_name}\n({len(content)} belgi) tarjima qilinadi:",
        'ru': f"📄 Файл получен: {file_name}\n({len(content)} символов) будет переведён:",
        'en': f"📄 File received: {file_name}\n({len(content)} characters) will be translated:"
    }.get(lang, f"📄 Fayl qabul qilindi: {file_name}\n({len(content)} belgi) tarjima qilinadi:")

    bot.reply_to(
        message,
        prompt + "\n\n" + content[:300] + ("..." if len(content) > 300 else ""),
        reply_markup=get_translation_languages_keyboard()
    )

# ---------------------- CALLBACK HANDLER ----------------------
@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    user_id = call.from_user.id
    lang = get_user_lang_db(user_id)
    data = call.data

    # Til o'zgartirish
    if data.startswith("lang_"):
        new_lang = data.split("_")[1]
        create_or_update_user(user_id, new_lang)
        bot.answer_callback_query(call.id, text=f"Til o'zgartirildi: {new_lang.upper()}")
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="Til o'zgartirildi. Quyidagi menyudan foydalaning:",
            reply_markup=get_main_menu_keyboard(new_lang, user_id)
        )
        return

    # Asosiy menyu
    if data == "limit":
        user = get_user(user_id)
        if user:
            bot.send_message(call.message.chat.id, f"💰 Limitingiz: {user['used']}/{user['limit']} ta ishlatilgan.")
        else:
            bot.send_message(call.message.chat.id, "❌ Foydalanuvchi topilmadi.")
        bot.answer_callback_query(call.id)
        return

    if data == "account":
        user = get_user(user_id)
        if user:
            text = f"👤 Hisobingiz:\nLimit: {user['limit']}\nIshlatilgan: {user['used']}\nKo'rilgan reklamalar: {user['total_ads_watched']}"
            bot.send_message(call.message.chat.id, text)
        else:
            bot.send_message(call.message.chat.id, "❌ Foydalanuvchi topilmadi.")
        bot.answer_callback_query(call.id)
        return

    if data == "translate":
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, "📝 Tarjima qilish uchun matn yoki fayl yuboring.")
        return

    if data == "tariffs":
        bot.answer_callback_query(call.id)
        user = get_user(user_id)
        if user:
            text = f"📊 Tariflaringiz:\nBepul limit: {user['limit']} ta\nIshlatilgan: {user['used']} ta\nQolgan: {user['limit'] - user['used']} ta"
            bot.send_message(call.message.chat.id, text)
        return

    if data == "ad":
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "📢 Reklama ko'rib limit oshiring:",
            reply_markup=get_ad_watch_keyboard(lang)
        )
        return

    if data == "language":
        bot.answer_callback_query(call.id)
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="🌐 Tilni tanlang:",
            reply_markup=get_language_keyboard()
        )
        return

    # Reklama ko'rish (adsgram)
    if data == "watch_ad":
        stats = get_stats()
        if not stats:
            bot.answer_callback_query(call.id, text="❌ Statistika xatosi.")
            return
        daily_limit = int(get_setting('daily_ad_limit') or 5)
        if stats['daily_ads_count'] >= daily_limit:
            bot.answer_callback_query(call.id, text="❌ Bugungi reklama limiti tugagan. Ertaga urinib ko'ring.")
            return

        ad_link = f"https://t.me/adsgram_bot?start=reward_{ADSGRAM_AD_UNIT_ID}_{user_id}"
        bot.send_message(
            call.message.chat.id,
            f"🎥 Reklamani ko'rish uchun quyidagi tugmani bosing:\n\n👉 {ad_link}",
            reply_markup=types.InlineKeyboardMarkup().add(
                types.InlineKeyboardButton("📺 Reklamani ko'rish", url=ad_link)
            )
        )
        if user_id not in temp_data:
            temp_data[user_id] = {}
        temp_data[user_id]['waiting_ad'] = True
        bot.answer_callback_query(call.id, text="Reklama ko'rish uchun havola yuborildi.")
        return

    # Tarjima tillari
    if data.startswith('trans_'):
        target_lang = data.split('_')[1]
        user_temp = temp_data.get(user_id, {})
        text_to_translate = user_temp.get('temp_text')
        if not text_to_translate:
            bot.answer_callback_query(call.id, text="❌ Tarjima uchun matn topilmadi.")
            return

        user = get_user(user_id)
        if not user:
            bot.answer_callback_query(call.id, text="❌ Foydalanuvchi topilmadi.")
            return
        if user['used'] >= user['limit']:
            bot.answer_callback_query(call.id, text="❌ Limitingiz tugagan. Reklama ko'ring.")
            bot.send_message(
                call.message.chat.id,
                "⚠️ Limitingiz tugadi. Reklama ko'rib limit oshiring:",
                reply_markup=get_ad_watch_keyboard(lang)
            )
            return

        try:
            translated = translator.translate(text_to_translate, target=target_lang)
        except Exception as e:
            logger.error(f"Tarjima xatosi: {e}")
            bot.answer_callback_query(call.id, text="❌ Tarjima xatosi.")
            return

        if user_id not in temp_data:
            temp_data[user_id] = {}
        temp_data[user_id]['translated_text'] = translated
        temp_data[user_id]['target_lang'] = target_lang

        bot.answer_callback_query(call.id, text="✅ Tarjima tayyor! Formatni tanlang.")
        bot.send_message(
            call.message.chat.id,
            "📥 Natija formatini tanlang:",
            reply_markup=get_result_format_keyboard(lang)
        )
        return

    # Natija formati
    if data.startswith('result_'):
        result_type = data.split('_')[1]
        user_temp = temp_data.get(user_id, {})
        translated = user_temp.get('translated_text')
        target_lang = user_temp.get('target_lang', 'en')
        if not translated:
            bot.answer_callback_query(call.id, text="❌ Tarjima matni topilmadi.")
            return

        user = get_user(user_id)
        if not user:
            bot.answer_callback_query(call.id, text="❌ Foydalanuvchi topilmadi.")
            return
        new_used = user['used'] + 1
        update_user_used(user_id, new_used)
        total_limit = user['limit']

        if result_type == 'text':
            bot.send_message(
                call.message.chat.id,
                f"🌐 Tarjima ({target_lang.upper()}):\n\n{translated}",
                reply_markup=get_main_menu_keyboard(lang, user_id)
            )
            bot.answer_callback_query(call.id, text=f"✅ Matn jo'natildi. ({new_used}/{total_limit})")
        else:  # fayl
            file_io, ext = create_translation_file(translated, 'txt')
            try:
                bot.send_document(
                    call.message.chat.id,
                    file_io,
                    caption=f"📄 Tarjima fayli ({target_lang.upper()}).{ext}",
                    reply_markup=get_main_menu_keyboard(lang, user_id)
                )
                bot.answer_callback_query(call.id, text=f"✅ Fayl jo'natildi. ({new_used}/{total_limit})")
            except Exception as e:
                logger.error(f"Fayl jo'natish xatosi: {e}")
                bot.answer_callback_query(call.id, text="❌ Fayl jo'natishda xatolik.")
            finally:
                file_io.close()

        temp_data[user_id].pop('translated_text', None)
        temp_data[user_id].pop('target_lang', None)
        temp_data[user_id].pop('temp_text', None)
        return

    # Admin paneli
    if data == "admin_panel":
        if user_id != ADMIN_ID:
            bot.answer_callback_query(call.id, text="⛔ Ruxsat yo'q!")
            return
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "🔐 Admin paneli:",
            reply_markup=get_admin_keyboard(lang)
        )
        return

    # Admin callback'lar
    if user_id != ADMIN_ID:
        bot.answer_callback_query(call.id, text="⛔ Ruxsat yo'q!")
        return

    if data == "admin_add_limit":
        bot.answer_callback_query(call.id)
        msg = bot.send_message(call.message.chat.id, "👤 ID va limit miqdorini yozing (masalan: `123456 5`):")
        bot.register_next_step_handler(msg, admin_add_limit_step)
    elif data == "admin_remove_limit":
        bot.answer_callback_query(call.id)
        msg = bot.send_message(call.message.chat.id, "👤 ID va o'chiriladigan limit miqdorini yozing (masalan: `123456 3`):")
        bot.register_next_step_handler(msg, admin_remove_limit_step)
    elif data == "admin_stats":
        bot.answer_callback_query(call.id)
        show_statistics(call.message)
    elif data == "admin_ad_settings":
        bot.answer_callback_query(call.id)
        show_ad_settings(call.message)
    elif data == "admin_send_ad":
        bot.answer_callback_query(call.id)
        msg = bot.send_message(call.message.chat.id, "📨 Reklama matnini yozing (barchaga yuboriladi):")
        bot.register_next_step_handler(msg, admin_send_ad_step)
    elif data == "admin_reset_daily":
        reset_daily_ads_db()
        bot.answer_callback_query(call.id, text="✅ Kunlik reklama tiklandi!")

# ---------------------- ADMIN STEP ----------------------
def admin_add_limit_step(message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        parts = message.text.split()
        target_id = int(parts[0])
        amount = int(parts[1])
        user = get_user(target_id)
        if not user:
            create_or_update_user(target_id, 'uz')
            user = get_user(target_id)
        new_limit = user['limit'] + amount
        update_user_limit(target_id, new_limit)
        bot.reply_to(message, f"✅ {target_id} ga {amount} ta limit qo'shildi. Yangi limit: {new_limit}")
    except:
        bot.reply_to(message, "❌ Xato format! `ID limit` deb yozing.")

def admin_remove_limit_step(message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        parts = message.text.split()
        target_id = int(parts[0])
        amount = int(parts[1])
        user = get_user(target_id)
        if not user:
            create_or_update_user(target_id, 'uz')
            user = get_user(target_id)
        new_limit = max(0, user['limit'] - amount)
        update_user_limit(target_id, new_limit)
        bot.reply_to(message, f"✅ {target_id} dan {amount} ta limit olib tashlandi. Yangi limit: {new_limit}")
    except:
        bot.reply_to(message, "❌ Xato format! `ID limit` deb yozing.")

def admin_send_ad_step(message):
    if message.from_user.id != ADMIN_ID:
        return
    ad_text = message.text
    sent = 0
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT user_id FROM users")
    users = c.fetchall()
    conn.close()
    for (uid,) in users:
        try:
            bot.send_message(uid, f"📢 Reklama:\n\n{ad_text}")
            sent += 1
            time.sleep(0.05)
        except:
            pass
    bot.reply_to(message, f"✅ {sent} ta foydalanuvchiga yuborildi.")

def show_statistics(message):
    stats = get_stats()
    if not stats:
        bot.send_message(message.chat.id, "❌ Statistika mavjud emas.")
        return
    daily_limit = get_setting('daily_ad_limit') or 5
    reward = get_setting('ad_reward_limit') or 5
    text = f"""
📊 **Statistika**:
- 👥 Foydalanuvchilar: {stats['total_users']}
- 📝 Tarjimalar: {stats['total_translations']}
- 🎥 Reklama ko'rishlar: {stats['total_ads_watched']}
- 📅 Kunlik reklama limiti: {daily_limit}
- 📢 Bugungi reklamalar: {stats['daily_ads_count']}
- 🎁 Reklama mukofoti: +{reward} limit
- 📆 Oxirgi tiklash: {stats['last_ad_reset']}
    """
    bot.send_message(message.chat.id, text, parse_mode='Markdown')

def show_ad_settings(message):
    daily_limit = get_setting('daily_ad_limit') or 5
    reward = get_setting('ad_reward_limit') or 5
    stats = get_stats()
    daily_ads = stats['daily_ads_count'] if stats else 0
    last_reset = stats['last_ad_reset'] if stats else "Noma'lum"
    text = f"""
📢 **Reklama sozlamalari**:
- Kunlik limit: {daily_limit}
- Har bir reklama mukofoti: {reward} ta limit
- Bugungi reklamalar: {daily_ads}
- Oxirgi tiklash: {last_reset}

💡 O'zgartirish uchun admin tugmalardan foydalaning.
    """
    bot.send_message(message.chat.id, text, parse_mode='Markdown')

# ---------------------- ADSGRAM CALLBACK ----------------------
@app.route('/adsgram_callback', methods=['POST'])
def adsgram_callback():
    try:
        data = request.get_json()
        if not data:
            return "Invalid JSON", 400
        user_id = data.get('user_id')
        status = data.get('status')
        reward = data.get('reward', 5)
        if not user_id:
            return "Missing user_id", 400
        logger.info(f"Adsgram callback: user={user_id}, status={status}, reward={reward}")
        if status == 'completed':
            user = get_user(user_id)
            if user:
                new_limit = user['limit'] + reward
                new_ads = user['total_ads_watched'] + 1
                update_user_limit(user_id, new_limit)
                update_user_ads(user_id, new_ads)
                increment_daily_ads()
                try:
                    lang = get_user_lang_db(user_id)
                    bot.send_message(
                        user_id,
                        f"🎉 Reklama muvaffaqiyatli ko'rildi! +{reward} ta limit qo'shildi.\nJoriy limitingiz: {new_limit}",
                        reply_markup=get_main_menu_keyboard(lang, user_id)
                    )
                except Exception as e:
                    logger.error(f"Foydalanuvchiga xabar yuborishda xatolik: {e}")
            else:
                logger.warning(f"Adsgram callback: user {user_id} topilmadi.")
        else:
            try:
                bot.send_message(
                    user_id,
                    "❌ Reklama ko'rish bekor qilindi yoki xatolik yuz berdi. Qayta urinib ko'ring."
                )
            except:
                pass
        return "OK", 200
    except Exception as e:
        logger.error(f"Adsgram callback xatosi: {e}")
        return "Error", 500

# ---------------------- WEBHOOK ----------------------
@app.route('/webhook', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return '', 200
    return 'Unsupported content type', 400

@app.route('/')
def index():
    return "Translator bot is running!"

def set_webhook():
    bot.remove_webhook()
    bot.set_webhook(url=WEBHOOK_URL)
    logger.info(f"Webhook set to {WEBHOOK_URL}")

# ---------------------- ISHGA TUSHIRISH ----------------------
if __name__ == '__main__':
    init_db()
    check_daily_reset_db()
    set_webhook()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
