"""Optional Discord notifications so you always know what the bot did."""

import requests
from config import DISCORD_WEBHOOK_URL


def notify(message: str):
    print(message)  # always log to stdout (shows up in Render logs)
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        # Discord caps content at 2000 chars
        requests.post(DISCORD_WEBHOOK_URL, json={"content": message[:1990]}, timeout=10)
    except requests.RequestException as e:
        print(f"[notify] Discord post failed (non-fatal): {e}")
