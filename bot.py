import os
import asyncio
import threading
import logging
from typing import Optional, Tuple

import requests
from flask import Flask
from telegram import Bot
from telegram.constants import ParseMode

# ---------------- CONFIG ----------------
INTERVAL_SECONDS = 60
THRESHOLD_PERCENT = 0.1  # post if moved +/-0.1% within 60 seconds

NOBITEX_URL = "https://apiv2.nobitex.ir/market/stats"
NOBITEX_PARAMS = {"srcCurrency": "usdt", "dstCurrency": "rls"}
PAIR_KEY = "usdt-rls"  # stats key expected in response for these params
USER_AGENT = "TraderBot/1.0"

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME")  # @channel OR -100xxxxxxxxxx

# -------------- LOGGING ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("nobitex-bot")

# -------------- OPTIONAL HTTP SERVER (Render Web Service) --------------
app = Flask(__name__)

@app.get("/")
def home():
    return "Bot is running!"

def run_http_server_if_needed():
    # If deployed as a Render Web Service, PORT exists and must be bound.
    port = os.getenv("PORT")
    if not port:
        return
    try:
        p = int(port)
    except ValueError:
        p = 8080
    log.info("Starting HTTP server on port %s", p)
    app.run(host="0.0.0.0", port=p, use_reloader=False)

def parse_chat_id(value: str):
    """
    Accepts '@channel' or numeric IDs like '-100123...'.
    python-telegram-bot can take either, but numeric should be int.
    """
    if not value:
        return value
    v = value.strip()
    if v.startswith("@"):
        return v
    # try int cast if it's numeric-ish
    try:
        return int(v)
    except ValueError:
        return v

# ---------------- NOBITEX FETCH ----------------
def fetch_price_sync() -> Tuple[Optional[int], Optional[int], Optional[int], int]:
    """
    Returns: (latest, best_buy, best_sell, backoff_seconds)
    If something goes wrong, returns (None, None, None, backoff) where backoff may be >0.
    """
    try:
        r = requests.get(
            NOBITEX_URL,
            params=NOBITEX_PARAMS,
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )

        # If rate-limited by HTTP status
        if r.status_code == 429:
            try:
                data = r.json()
                backoff = int(data.get("backOff", 60))
                log.warning("Rate-limited (429). backOff=%ss", backoff)
                return None, None, None, backoff
            except Exception:
                return None, None, None, 60

        r.raise_for_status()
        data = r.json()

        # Nobitex can return HTTP 200 with status=failed for some errors
        if data.get("status") != "ok":
            backoff = int(data.get("backOff", 0) or 0)
            log.warning("API returned status=%s code=%s message=%s backOff=%s",
                        data.get("status"), data.get("code"), data.get("message"), backoff)
            return None, None, None, backoff

        stats = data.get("stats", {})
        pair = stats.get(PAIR_KEY)
        if not pair:
            raise ValueError(f"Missing stats['{PAIR_KEY}']")

        latest = int(pair["latest"])
        best_buy = int(pair["bestBuy"])
        best_sell = int(pair["bestSell"])
        return latest, best_buy, best_sell, 0

    except Exception as e:
        log.warning("Fetch error: %s", e)
        return None, None, None, 0

# ---------------- TELEGRAM ----------------
async def send_message(bot: Bot, chat_id, text: str) -> None:
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as e:
        log.warning("Send error: %s", e)

def format_message(latest: int, best_buy: int, best_sell: int, pct: float) -> str:
    arrow = "🔺" if pct > 0 else "🔻"
    return (
        f"{arrow} <b>USDT/Rial moved {pct:+.3f}% in {INTERVAL_SECONDS}s</b>\n"
        f"Latest: <b>{latest}</b>\n"
        f"Buy: {best_buy} | Sell: {best_sell}"
    )

# ---------------- MAIN LOOP ----------------
async def main():
    if not BOT_TOKEN or not CHANNEL_USERNAME:
        raise RuntimeError("Missing env vars. Set BOT_TOKEN and CHANNEL_USERNAME in Render Environment.")

    chat_id = parse_chat_id(CHANNEL_USERNAME)
    bot = Bot(token=BOT_TOKEN)

    prev_latest: Optional[int] = None

    while True:
        latest, best_buy, best_sell, backoff = await asyncio.to_thread(fetch_price_sync)

        if backoff and backoff > 0:
            await asyncio.sleep(backoff)
            continue

        if latest is not None:
            if prev_latest is not None and prev_latest != 0:
                pct_change = (latest - prev_latest) / prev_latest * 100.0
                log.info("Tick: latest=%s prev=%s change=%+.4f%%", latest, prev_latest, pct_change)

                if abs(pct_change) >= THRESHOLD_PERCENT:
                    msg = format_message(latest, best_buy, best_sell, pct_change)
                    await send_message(bot, chat_id, msg)
                    log.info("Sent alert: %+.4f%%", pct_change)
                else:
                    log.info("No alert (threshold %.3f%%).", THRESHOLD_PERCENT)

            prev_latest = latest

        await asyncio.sleep(INTERVAL_SECONDS)

if __name__ == "__main__":
    threading.Thread(target=run_http_server_if_needed, daemon=True).start()
    asyncio.run(main())
