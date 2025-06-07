import os
import json
import requests
from flask import Flask, request
from dotenv import load_dotenv
from typing import Optional, Tuple, Dict, Any


load_dotenv()


app = Flask(__name__)


# === CONFIGURATION ===
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
ADMIN_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL")


# File storage paths
FORWARD_MAP_FILE = "forward_map.json"
REPLIED_MESSAGES_FILE = "replied_messages.json"


# Load data from files
forward_map = {}  # forwarded_msg_id → (orig_chat_id, orig_msg_id)
replied_messages = set()  # Set of message IDs that have been replied to


def load_data():
    """Load forward_map and replied_messages from JSON files."""
    global forward_map, replied_messages
    
    # Load forward_map
    if os.path.exists(FORWARD_MAP_FILE):
        try:
            with open(FORWARD_MAP_FILE, 'r') as f:
                data = json.load(f)
                # Convert string keys back to integers
                forward_map = {int(k): tuple(v) for k, v in data.items()}
            app.logger.info(f"Loaded {len(forward_map)} forward mappings")
        except (json.JSONDecodeError, ValueError) as e:
            app.logger.error(f"Error loading forward map: {e}")
            forward_map = {}
    
    # Load replied_messages
    if os.path.exists(REPLIED_MESSAGES_FILE):
        try:
            with open(REPLIED_MESSAGES_FILE, 'r') as f:
                replied_messages = set(json.load(f))
            app.logger.info(f"Loaded {len(replied_messages)} replied messages")
        except json.JSONDecodeError as e:
            app.logger.error(f"Error loading replied messages: {e}")
            replied_messages = set()


def save_forward_map():
    """Save forward_map to JSON file."""
    try:
        # Convert int keys to strings for JSON serialization
        data = {str(k): list(v) for k, v in forward_map.items()}
        with open(FORWARD_MAP_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        app.logger.error(f"Error saving forward map: {e}")


def save_replied_messages():
    """Save replied_messages to JSON file."""
    try:
        with open(REPLIED_MESSAGES_FILE, 'w') as f:
            json.dump(list(replied_messages), f, indent=2)
    except Exception as e:
        app.logger.error(f"Error saving replied messages: {e}")


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


def get_file_id(msg: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """Extract file_id and type from a message with media."""
    media_types = {
        'photo': lambda m: m['photo'][-1]['file_id'],  # Last item is highest quality
        'video': lambda m: m['video']['file_id'],
        'document': lambda m: m['document']['file_id'],
        'audio': lambda m: m['audio']['file_id'],
        'voice': lambda m: m['voice']['file_id'],
        'sticker': lambda m: m['sticker']['file_id']
    }
    
    for media_type, extractor in media_types.items():
        if media_type in msg:
            try:
                return extractor(msg), media_type
            except (KeyError, IndexError):
                app.logger.error(f"Failed to extract {media_type} file_id")
    
    return None


def forward_media_message(chat_id: int, file_id: str, media_type: str, 
                         caption: Optional[str] = None, 
                         reply_to_message_id: Optional[int] = None) -> dict:
    """Forward a media message using its file_id."""
    method_map = {
        'photo': 'sendPhoto',
        'video': 'sendVideo',
        'document': 'sendDocument',
        'audio': 'sendAudio',
        'voice': 'sendVoice',
        'sticker': 'sendSticker'
    }
    
    method = method_map.get(media_type)
    if not method:
        app.logger.error(f"Unsupported media type: {media_type}")
        return {}
        
    params = {
        'chat_id': chat_id,
        media_type: file_id,
    }
    
    if caption:
        params['caption'] = caption
    if reply_to_message_id:
        params['reply_to_message_id'] = reply_to_message_id
        
    return telegram_api(method, **params)


@app.route("/", methods=["GET"])
def healthcheck() -> tuple[str, int]:
    return "OK", 200


@app.route("/webhook", methods=["GET", "POST"])
def webhook() -> dict:
    if request.method == "GET":
        return ("This endpoint only accepts POST "
                "from Telegram"), 200

    update = request.get_json(force=True)
    app.logger.info("Received update: %s", update)

    if "message" in update:
        msg = update["message"]
        chat_id = msg["chat"]["id"]
        user_id = msg.get("from", {}).get("id")
        
        # Handle messages FROM the admin (your personal account)
        if user_id == ADMIN_ID:
            app.logger.info("Processing admin message")
            
            # Check if this is a reply to a forwarded message
            if "reply_to_message" in msg:
                reply_to_msg = msg["reply_to_message"]
                reply_to_id = reply_to_msg["message_id"]
                
                # Check if the replied message was forwarded from a user
                if reply_to_id in forward_map:
                    orig_chat_id, orig_msg_id = forward_map[reply_to_id]
                    app.logger.info("Found mapping, sending reply to chat %s", orig_chat_id)
                    
                    # Handle media replies
                    media_info = get_file_id(msg)
                    if media_info:
                        file_id, media_type = media_info
                        response = forward_media_message(
                            orig_chat_id, 
                            file_id, 
                            media_type,
                            msg.get('caption'),
                            orig_msg_id
                        )
                    else:
                        # Text reply - send as bot
                        response = telegram_api(
                            "sendMessage",
                            chat_id=orig_chat_id,
                            text=msg.get("text", ""),
                            reply_to_message_id=orig_msg_id,
                        )
                    
                    # Mark as replied and notify admin
                    if response.get('ok'):
                        replied_messages.add(orig_msg_id)
                        save_replied_messages()  # Save to file
                        telegram_api(
                            "sendMessage",
                            chat_id=ADMIN_ID,
                            text="✅ Reply sent successfully",
                            reply_to_message_id=msg["message_id"]
                        )
                    else:
                        telegram_api(
                            "sendMessage",
                            chat_id=ADMIN_ID,
                            text="❌ Failed to send reply",
                            reply_to_message_id=msg["message_id"]
                        )
                else:
                    # This is a reply to a message that wasn't forwarded from a user
                    # Could be a reply to bot's own message or system message
                    app.logger.info("Reply to non-forwarded message, ignoring")
            else:
                # This is a direct message from admin to bot (not a reply)
                # You might want to handle this case differently
                app.logger.info("Direct message from admin to bot")
            
            return {"ok": True}

        # Handle messages TO the bot (from regular users)
        entities = msg.get("entities", [])
        is_private = msg["chat"]["type"] == "private"
        is_mention = any(
            e["type"] in ("mention", "text_mention") for e in entities
        )

        if is_private or is_mention:
            app.logger.info("Forwarding message to admin")
            
            # Add "Replied" status if this is a reply to a message we've already replied to
            reply_status = ""
            if msg.get("reply_to_message", {}).get("message_id") in replied_messages:
                reply_status = "✅ "
            
            # Forward the message to admin
            fwd = telegram_api(
                "forwardMessage",
                chat_id=ADMIN_ID,
                from_chat_id=chat_id,
                message_id=msg["message_id"],
            )
            
            if "result" in fwd and "message_id" in fwd["result"]:
                fwd_id = fwd["result"]["message_id"]
                # Store the mapping: forwarded_message_id → (original_chat_id, original_message_id)
                forward_map[fwd_id] = (chat_id, msg["message_id"])
                save_forward_map()  # Save to file
                
                # Send user info after forwarded message
                user_info = (
                    f"{reply_status}From: {msg['from'].get('first_name', '')} "
                    f"{msg['from'].get('last_name', '')}\n"
                    f"Username: @{msg['from'].get('username', 'N/A')}\n"
                    f"Chat ID: {chat_id}"
                )
                telegram_api(
                    "sendMessage",
                    chat_id=ADMIN_ID,
                    text=user_info,
                    reply_to_message_id=fwd_id
                )
            else:
                app.logger.error("Failed to get forwarded message ID: %s", fwd)
            
            return {"ok": True}

    return {"ok": True}


if __name__ == "__main__":
    load_data()  # Load existing data on startup
    set_webhook()
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        debug=True
    )
    