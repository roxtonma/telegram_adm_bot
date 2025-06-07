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


# Support multiple admins
ADMIN_IDS_STR = os.getenv("ADMIN_CHAT_IDS", os.getenv("ADMIN_CHAT_ID", "0"))
ADMIN_IDS = [int(id.strip()) for id in ADMIN_IDS_STR.split(",") if id.strip()]


WEBHOOK_URL = os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL")


# GitHub Gist configuration
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")  # Your GitHub personal access token
GIST_ID = os.getenv("GIST_ID")  # Your Gist ID (created once)


# Data storage
forward_map = {}  # forwarded_msg_id → (orig_chat_id, orig_msg_id)
replied_messages = set()  # Set of message IDs that have been replied to


def load_data_from_gist():
    """Load data from GitHub Gist."""
    global forward_map, replied_messages
    
    if not GITHUB_TOKEN or not GIST_ID:
        app.logger.warning("GitHub token or Gist ID not configured, using memory storage")
        return
    
    try:
        headers = {
            'Authorization': f'token {GITHUB_TOKEN}',
            'Accept': 'application/vnd.github.v3+json'
        }
        
        response = requests.get(f'https://api.github.com/gists/{GIST_ID}', headers=headers)
        
        if response.status_code == 200:
            gist_data = response.json()
            
            # Load forward_map
            if 'forward_map.json' in gist_data['files']:
                content = gist_data['files']['forward_map.json']['content']
                data = json.loads(content)
                forward_map = {int(k): tuple(v) for k, v in data.items()}
                app.logger.info(f"Loaded {len(forward_map)} forward mappings from Gist")
            
            # Load replied_messages
            if 'replied_messages.json' in gist_data['files']:
                content = gist_data['files']['replied_messages.json']['content']
                replied_messages = set(json.loads(content))
                app.logger.info(f"Loaded {len(replied_messages)} replied messages from Gist")
        else:
            app.logger.error(f"Failed to load from Gist: {response.status_code}")
            
    except Exception as e:
        app.logger.error(f"Error loading from Gist: {e}")


def save_data_to_gist():
    """Save data to GitHub Gist."""
    if not GITHUB_TOKEN or not GIST_ID:
        return
    
    try:
        headers = {
            'Authorization': f'token {GITHUB_TOKEN}',
            'Accept': 'application/vnd.github.v3+json'
        }
        
        # Prepare data
        forward_map_data = {str(k): list(v) for k, v in forward_map.items()}
        replied_messages_data = list(replied_messages)
        
        # Update Gist
        gist_data = {
            'files': {
                'forward_map.json': {
                    'content': json.dumps(forward_map_data, indent=2)
                },
                'replied_messages.json': {
                    'content': json.dumps(replied_messages_data, indent=2)
                }
            }
        }
        
        response = requests.patch(f'https://api.github.com/gists/{GIST_ID}', 
                                headers=headers, 
                                json=gist_data)
        
        if response.status_code == 200:
            app.logger.info("Data saved to Gist successfully")
        else:
            app.logger.error(f"Failed to save to Gist: {response.status_code}")
            
    except Exception as e:
        app.logger.error(f"Error saving to Gist: {e}")


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
        
        # Handle messages FROM the admins
        if user_id in ADMIN_IDS:
            app.logger.info("Processing admin message from user %s", user_id)
            
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
                        save_data_to_gist()  # Save to Gist
                        
                        # Notify all admins about successful reply
                        admin_name = msg['from'].get('first_name', 'Admin')
                        for admin_id in ADMIN_IDS:
                            telegram_api(
                                "sendMessage",
                                chat_id=admin_id,
                                text=f"✅ Reply sent successfully by {admin_name}",
                                reply_to_message_id=msg["message_id"] if admin_id == user_id else None
                            )
                    else:
                        telegram_api(
                            "sendMessage",
                            chat_id=user_id,
                            text="❌ Failed to send reply",
                            reply_to_message_id=msg["message_id"]
                        )
                else:
                    # This is a reply to a message that wasn't forwarded from a user
                    app.logger.info("Reply to non-forwarded message, ignoring")
            else:
                # This is a direct message from admin to bot (not a reply)
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
                save_data_to_gist()  # Save to Gist
                
                # Send user info to all admins
                user_info = (
                    f"{reply_status}From: {msg['from'].get('first_name', '')} "
                    f"{msg['from'].get('last_name', '')}\n"
                    f"Username: @{msg['from'].get('username', 'N/A')}\n"
                    f"Chat ID: {chat_id}"
                )
                
                for admin_id in ADMIN_IDS:
                    # Forward message to each admin
                    admin_fwd = telegram_api(
                        "forwardMessage",
                        chat_id=admin_id,
                        from_chat_id=chat_id,
                        message_id=msg["message_id"],
                    )
                    
                    if "result" in admin_fwd and "message_id" in admin_fwd["result"]:
                        admin_fwd_id = admin_fwd["result"]["message_id"]
                        # Store mapping for each admin's forwarded message
                        forward_map[admin_fwd_id] = (chat_id, msg["message_id"])
                        
                        # Send user info after forwarded message
                        telegram_api(
                            "sendMessage",
                            chat_id=admin_id,
                            text=user_info,
                            reply_to_message_id=admin_fwd_id
                        )
                
                # Update the mapping save after all forwards
                save_data_to_gist()
            else:
                app.logger.error("Failed to get forwarded message ID: %s", fwd)
            
            return {"ok": True}

    return {"ok": True}


if __name__ == "__main__":
    load_data_from_gist()  # Load existing data on startup
    set_webhook()
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        debug=True
    )