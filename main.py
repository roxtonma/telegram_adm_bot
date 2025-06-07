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

    # 1) Forward mentions or private messages to admin
    if "message" in update:
        msg = update["message"]
        chat_id = msg["chat"]["id"]
        entities = msg.get("entities", [])

        is_private = msg["chat"]["type"] == "private"
        is_mention = any(
            e["type"] in ("mention", "text_mention") for e in entities
        )

        if is_private or is_mention:
            fwd = telegram_api(
                "forwardMessage",
                chat_id=ADMIN_ID,
                from_chat_id=chat_id,
                message_id=msg["message_id"],
            )
            fwd_id = fwd.get("result", {}).get("message_id")
            if fwd_id:
                forward_map[fwd_id] = (chat_id, msg["message_id"])
            return {"ok": True}

    # 2) Admin replies → send back to original chat
    if (
        "message" in update
        and update["message"]["chat"]["id"] == ADMIN_ID
    ):
        msg = update["message"]
        reply_to = msg.get("reply_to_message", {}).get("message_id")
        if reply_to in forward_map:
            orig_chat_id, orig_msg_id = forward_map[reply_to]
            telegram_api(
                "sendMessage",
                chat_id=orig_chat_id,
                text=msg.get("text", ""),
                reply_to_message_id=orig_msg_id,
            )
            return {"ok": True}

    return {"ok": True}


if __name__ == "__main__":
    set_webhook()
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
    )
