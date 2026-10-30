"""Send a photo post (or a plain text post) to Telegram via the Bot API."""

import requests

API = "https://api.telegram.org/bot{token}/{method}"


def tradingview_url(symbol):
    tv = symbol.replace("-", "_").replace("&", "_")
    return f"https://www.tradingview.com/chart/?symbol=NSE:{tv}"


def send_photo(token, chat_id, photo_path, caption, filing_url, symbol):
    url = API.format(token=token, method="sendPhoto")
    keyboard = {"inline_keyboard": [[
        {"text": "Result ↗", "url": filing_url},
        {"text": "Chart ↗", "url": tradingview_url(symbol)},
    ]]}
    with open(photo_path, "rb") as fh:
        r = requests.post(url, data={
            "chat_id": chat_id, "caption": caption,
            "reply_markup": __import__("json").dumps(keyboard),
        }, files={"photo": fh}, timeout=60)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram sendPhoto failed: {data}")
    return data


def send_photo_plain(token, chat_id, photo_path, caption):
    """Send a photo with a caption and NO buttons. Digest images
    (morning grid, recap, wrap, FII/DII, actions, IPO cards) have no
    single filing or symbol to link, so attaching Result/Chart
    buttons would be wrong; the facts live in the caption."""
    url = API.format(token=token, method="sendPhoto")
    with open(photo_path, "rb") as fh:
        r = requests.post(url, data={
            "chat_id": chat_id, "caption": caption,
        }, files={"photo": fh}, timeout=60)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram sendPhoto failed: {data}")
    return data


def send_text(token, chat_id, text):
    url = API.format(token=token, method="sendMessage")
    r = requests.post(url, data={"chat_id": chat_id, "text": text},
                      timeout=60)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram sendMessage failed: {data}")
    return data
