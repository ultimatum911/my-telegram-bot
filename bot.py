import os
import time
import asyncio
import threading
import logging
from typing import Optional, Tuple

import requests
from telegram import Bot
from telegram.constants import ParseMode
from flask import Flask

# ---------------- CONFIG ----------------
INTERVAL_SECONDS = 30
THRESHOLD_PERCENT = 0.2  # 0.2% move within 30 seconds

NOBITEX_URL = "https://apiv2.nobitex.ir/market/stats"
NOBITEX_PARAMS = {"srcCurrency": "usdt", "dstCurrency": "rls"}
USER_AGENT = "TraderBot/1.0"

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME")  # e.g. @yourchannel OR -1001234567890

# -------------- LOGGING ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("nobitex-bot")

# -------------- OPTIONAL HTTP SERVER (for Render Web Service) --------------
app = Flask(__name__)

@app.get("/")
def home():
    return "Bot is running!"

def run_http_server_if_needed():
    """
    Render Web Services expect you to bind to $PORT.
    Background Workers usually don't set PORT.
    """
    port = os.getenv("PORT")
    if not port:
        return
    try:
        p = int(port)
    except ValueError:
        p = 8080

    log.info("Starting HTTP server on port %s", p)
    # use_reloader=False prevents double-start
    app.run(host="0.0.0.0", port=p, use_reloader=False)

# ---------------- NOBITEX FETCH ----------------
def fetch_price_sync() -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """
    Returns: (latest, best_buy, best_sell) as ints, or (None, None, None) on failure
    """
    try:
        r = requests.get(
            NOBITEX_URL,
            params=NOBITEX_PARAMS,
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()

        stats = data.get("stats", {})
        pair = stats.get("usdt-rls")
        if not pair:
            raise ValueError("Missing stats['usdt-rls']")

        latest = int(pair["latest"])
        best_buy = int(pair["bestBuy"])
        best_sell = int(pair["bestSell"])
        return latest, best_buy, best_sell

    except Exception as e:
        log.warning("Fetch error: %s", e)
        return None, None, None

# ---------------- TELEGRAM ----------------
async def send_message(bot: Bot, text: str) -> None:
    try:
        await bot.send_message(
            chat_id=CHANNEL_USERNAME,
            text=text,
            parse_mode=ParseMode.HTML,  # safe formatting
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
        raise RuntimeError(
            "Missing env vars. Set BOT_TOKEN and CHANNEL_USERNAME in Render Environment."
        )

    bot = Bot(token=BOT_TOKEN)

    prev_latest: Optional[int] = None

    while True:
        latest, best_buy, best_sell = await asyncio.to_thread(fetch_price_sync)

        if latest is not None:
            if prev_latest is not None and prev_latest != 0:
                pct_change = (latest - prev_latest) / prev_latest * 100.0

                if abs(pct_change) >= THRESHOLD_PERCENT:
                    msg = format_message(latest, best_buy, best_sell, pct_change)
                    await send_message(bot, msg)
                    log.info("Sent alert: %+.3f%% (%s -> %s)", pct_change, prev_latest, latest)
                else:
                    log.info("No alert: %+.4f%% (threshold %.3f%%)", pct_change, THRESHOLD_PERCENT)

            prev_latest = latest

        await asyncio.sleep(INTERVAL_SECONDS)

if __name__ == "__main__":
    # Start tiny HTTP server if PORT is present (Render Web Service case)
    threading.Thread(target=run_http_server_if_needed, daemon=True).start()
    asyncio.run(main())
