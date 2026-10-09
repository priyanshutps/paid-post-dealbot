"""Run this ONCE on your own PC to create SESSION_STRING for Railway.
   pip install telethon
   python gen_session.py
Telegram will send a login code to your Telegram app. Never share the printed string with anyone."""
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

api_id = int(input("API_ID (from my.telegram.org): ").strip())
api_hash = input("API_HASH: ").strip()
with TelegramClient(StringSession(), api_id, api_hash) as c:
    print("\n\nYour SESSION_STRING (paste into Railway → Variables):\n")
    print(c.session.save())
    c.send_message("me", "✅ Session created for the deal bot. Keep the string secret.")
