import os
from flask import Flask, request, abort
import requests
from dotenv import load_dotenv

app = Flask(__name__)

# === CONFIGURATION ===
load_dotenv()
BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
API_URL   = f"https://api.telegram.org/bot{BOT_TOKEN}"
ADMIN_ID  = int(os.getenv('ADMIN_CHAT_ID'))  # your Telegram user ID

# In-memory mapping: forwarded_message_id → (orig_chat_id, orig_message_id)
# In production, persist this (e.g. Redis or a database).
forward_map = {}

def telegram_api(method, **params):
    """Helper to call Telegram Bot API."""
    resp = requests.post(f"{API_URL}/{method}", data=params)
    if not resp.ok or not resp.json().get("ok"):
        app.logger.error(f"Telegram API error: {resp.text}")
    return resp.json()

@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(force=True)

    # --- 1) Handle incoming messages & mentions ---
    if "message" in update:
        msg = update["message"]
        chat_id = msg["chat"]["id"]
        user_id = msg["from"]["id"]
        text = msg.get("text", "")

        is_private = (msg["chat"]["type"] == "private")
        is_mention = ("entities" in msg and
                      any(e["type"] == "mention" or e["type"] == "text_mention"
                          for e in msg["entities"]))

        if is_private or is_mention:
            # Forward to admin
            fwd = telegram_api("forwardMessage",
                                chat_id=ADMIN_ID,
                                from_chat_id=chat_id,
                                message_id=msg["message_id"]) 
            # Record mapping
            fwd_id = fwd["result"]["message_id"]
            forward_map[fwd_id] = (chat_id, msg["message_id"])
            return {"ok": True}

    # --- 2) Handle admin replies ---
    if "message" in update and update["message"]["chat"]["id"] == ADMIN_ID:
        msg = update["message"]
        if msg.get("reply_to_message"):
            reply_to = msg["reply_to_message"]["message_id"]
            if reply_to in forward_map:
                orig_chat_id, orig_msg_id = forward_map[reply_to]
                # Send admin's reply text back to the original chat
                telegram_api("sendMessage",
                             chat_id=orig_chat_id,
                             text=msg.get("text", ""),
                             reply_to_message_id=orig_msg_id)
                return {"ok": True}

    return {"ok": True}

def set_webhook():
    webhook_url = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("WEBHOOK_URL")
    if not webhook_url:
        app.logger.warning("WEBHOOK_URL not set; skipping setWebhook")
        return

    resp = requests.post(
        f"{API_URL}/setWebhook",
        data={"url": f"{webhook_url}/webhook"}
    )
    app.logger.info("setWebhook response: %s", resp.text)

if __name__ == "__main__":
    set_webhook()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))