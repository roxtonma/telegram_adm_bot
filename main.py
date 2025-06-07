import os
import requests
from flask import Flask, request
from dotenv import load_dotenv


load_dotenv()


app = Flask(__name__)


# === CONFIGURATION ===
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
ADMIN_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL")


# In-memory mapping; replace with Redis/DB in production
forward_map = {}


def telegram_api(method: str, **params) -> dict:
    """Helper to call the Telegram Bot API."""
    resp = requests.post(f"{API_URL}/{method}", data=params)
    if not resp.ok or not resp.json().get("ok"):
        app.logger.error("Telegram API error: %s", resp.text)
    return resp.json()


def set_webhook() -> None:
    """Register your webhook URL with Telegram (run once on startup)."""
    if not WEBHOOK_URL:
        app.logger.warning("WEBHOOK_URL not set; skipping setWebhook")
        return

    url = f"{WEBHOOK_URL}/webhook"
    resp = requests.post(f"{API_URL}/setWebhook", data={"url": url})
    app.logger.info("setWebhook response: %s", resp.text)


@app.route("/", methods=["GET"])
def healthcheck() -> tuple[str, int]:
    return "OK", 200


@app.route("/webhook", methods=["GET", "POST"])
def webhook() -> dict:
    if request.method == "GET":
        return ("This endpoint only accepts POST "
                "from Telegram"), 200

    update = request.get_json(force=True)
    app.logger.info("Received update: %s", update)  # Debug log

    # 1) Forward mentions or private messages to admin
    if "message" in update:
        msg = update["message"]
        chat_id = msg["chat"]["id"]
        user_id = msg.get("from", {}).get("id")
        
        # Skip if message is from admin
        if user_id == ADMIN_ID:
            app.logger.info("Skipping message from admin")
            return {"ok": True}

        entities = msg.get("entities", [])
        is_private = msg["chat"]["type"] == "private"
        is_mention = any(
            e["type"] in ("mention", "text_mention") for e in entities
        )

        if is_private or is_mention:
            app.logger.info("Forwarding message to admin")
            fwd = telegram_api(
                "forwardMessage",
                chat_id=ADMIN_ID,
                from_chat_id=chat_id,
                message_id=msg["message_id"],
            )
            
            if "result" in fwd and "message_id" in fwd["result"]:
                fwd_id = fwd["result"]["message_id"]
                forward_map[fwd_id] = (chat_id, msg["message_id"])
                app.logger.info("Stored mapping: %s -> (%s, %s)", 
                              fwd_id, chat_id, msg["message_id"])
            else:
                app.logger.error("Failed to get forwarded message ID: %s", fwd)
            
            return {"ok": True}

    # 2) Admin replies → send back to original chat
    if "message" in update:
        msg = update["message"]
        chat_id = msg["chat"]["id"]
        
        if chat_id == ADMIN_ID and "reply_to_message" in msg:
            reply_to_msg = msg["reply_to_message"]
            reply_to_id = reply_to_msg["message_id"]
            
            app.logger.info("Admin reply - looking up mapping for message %s", reply_to_id)
            app.logger.info("Current forward_map: %s", forward_map)
            
            if reply_to_id in forward_map:
                orig_chat_id, orig_msg_id = forward_map[reply_to_id]
                app.logger.info("Found mapping, sending reply to chat %s", orig_chat_id)
                
                telegram_api(
                    "sendMessage",
                    chat_id=orig_chat_id,
                    text=msg.get("text", ""),
                    reply_to_message_id=orig_msg_id,
                )
                return {"ok": True}
            else:
                app.logger.warning("No mapping found for reply to message %s", reply_to_id)

    return {"ok": True}


if __name__ == "__main__":
    set_webhook()
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        debug=True  # Enable debug mode
    )
