import os
import telebot
import requests
from flask import Flask, request, abort

TOKEN = "8619014948:AAGuiOhk_UpKaD30PghNmWwqAivEMz-AKHU"
bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

RENDER_URL = "https://muhammadyusuf-richads.onrender.com"
WEBHOOK_URL_PATH = f"/{TOKEN}"
WEBHOOK_URL = f"{RENDER_URL}{WEBHOOK_URL_PATH}"

# RichAds API sozlamalari (Hujjatdagi manzil va parametrlar)
RICHADS_URL = "http://15068.xml.adx1.com/telegram-mb"
PUBLISHER_ID = "1018576"
WIDGET_ID = "402831"

@bot.message_handler(commands=['start', 'reklama'])
def send_ad_to_user(message):
    chat_id = message.chat.id
    
    # RichAds hujjatlariga mos to'liq payload formati
    payload = {
        "language_code": "en",
        "publisher_id": PUBLISHER_ID,
        "widget_id": WIDGET_ID,
        "telegram_id": str(chat_id),
        "production": True  # Sinov uchun false, tayyor bo'lganda true qilasiz
    }
    
    try:
        response = requests.post(RICHADS_URL, json=payload)
        print("RichAds javob kodi:", response.status_code)
        print("RichAds javob matni:", response.text)
        
        if response.status_code == 200:
            ads_data = response.json()
            
            if ads_data and isinstance(ads_data, list):
                ad = ads_data[0]
                photo_url = ad.get("image")
                caption_text = ad.get("message", "Maxsus taklif!")
                button_text = ad.get("button", "O'tish")
                link_url = ad.get("link")
                
                markup = telebot.types.InlineKeyboardMarkup()
                btn = telebot.types.InlineKeyboardButton(text=button_text, url=link_url)
                markup.add(btn)
                
                bot.send_photo(
                    chat_id=chat_id,
                    photo=photo_url,
                    caption=caption_text,
                    reply_markup=markup
                )
            else:
                bot.send_message(chat_id, "Hozircha reklama mavjud emas.")
        else:
            bot.send_message(chat_id, "Reklama serverida xatolik yuz berdi.")
            
    except Exception as e:
        print(f"Xatolik: {e}")
        bot.send_message(chat_id, "Xatolik yuz berdi, keyinroq urinib ko'ring.")

@app.route(WEBHOOK_URL_PATH, methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return '', 200
    else:
        abort(403)

@app.route('/')
def index():
    return "Bot Webhook ishlayapti!", 200

if __name__ == "__main__":
    bot.remove_webhook()
    bot.set_webhook(url=WEBHOOK_URL)
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
