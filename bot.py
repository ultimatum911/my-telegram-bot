import os
import time
import asyncio
import threading
import logging
from typing import Optional, Tuple
from decimal import Decimal, InvalidOperation

import requests
from flask import Flask
from telegram import Bot
from telegram.constants import ParseMode

# ---------------- CONFIG ----------------
INTERVAL_SECONDS = 60
THRESHOLD_PERCENT = 0.01  # post if moved +/-0.1% within 60 seconds

NOBITEX_URL = "https://apiv2.nobitex.ir/market/stats"
NOBITEX_PARAMS = {"srcCurrency": "usdt", "dstCurrency": "rls"}
PAIR_KEY = "usdt-rls"
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
    if not value:
        return value
    v = value.strip()
    if v.startswith("@"):
        return v
    try:
        return int(v)
    except ValueError:
        return v

def to_int_price(x) -> int:
    if x is None:
        raise ValueError("price is None")
    try:
        d = Decimal(str(x))
    except InvalidOperation:
        raise ValueError(f"invalid price: {x}")
    return int(d.to_integral_value(rounding="ROUND_DOWN"))

# ---------------- NOBITEX FETCH ----------------
def fetch_price_sync() -> Tuple[Optional[int], Optional[int], Optional[int], int]:
    """
    Returns: (latest, best_buy, best_sell, backoff_seconds)
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    try:
        # cache buster to reduce stale responses
        params = dict(NOBITEX_PARAMS)
        params["_ts"] = str(int(time.time() * 1000))

        r = requests.get(NOBITEX_URL, params=params, headers=headers, timeout=10)

        if r.status_code == 429:
            # rate-limit handling (if the API returns JSON with backOff)
            backoff = 60
            try:
                data = r.json()
                backoff = int(data.get("backOff", 60))
            except Exception:
                pass
            log.warning("Rate-limited (429). backOff=%ss", backoff)
            return None, None, None, backoff

        r.raise_for_status()
        data = r.json()

        # handle non-ok responses if present
        if isinstance(data, dict) and data.get("status") and data.get("status") != "ok":
            backoff = int(data.get("backOff", 0) or 0)
            log.warning(
                "API returned status=%s code=%s message=%s backOff=%s",
                data.get("status"), data.get("code"), data.get("message"), backoff
            )
            return None, None, None, backoff

        stats = data.get("stats", {}) if isinstance(data, dict) else {}
        pair = stats.get(PAIR_KEY)
        if not pair:
            raise ValueError(f"Missing stats['{PAIR_KEY}']")

        latest = to_int_price(pair.get("latest"))
        best_buy = to_int_price(pair.get("bestBuy"))
        best_sell = to_int_price(pair.get("bestSell"))
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
    arrow = "🟢▲" if pct > 0 else "🔻"
    return (
        f"{arrow}\n"
        f"Latest: {latest}\n"
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
            log.info("Backing off for %ss", backoff)
            await asyncio.sleep(backoff)
            continue

        if latest is not None:
            if prev_latest is not None and prev_latest != 0:
                pct_change = (latest - prev_latest) / prev_latest * 100.0
                log.info(
                    "Tick: latest=%s prev=%s bestBuy=%s bestSell=%s change=%+.4f%%",
                    latest, prev_latest, best_buy, best_sell, pct_change
                )

                if abs(pct_change) >= THRESHOLD_PERCENT:
                    msg = format_message(latest, best_buy, best_sell, pct_change)
                    await send_message(bot, chat_id, msg)
                    log.info("Sent alert: %+.4f%%", pct_change)
                else:
                    log.info("No alert (threshold %.3f%%).", THRESHOLD_PERCENT)
            else:
                log.info("Baseline set: latest=%s bestBuy=%s bestSell=%s", latest, best_buy, best_sell)

            prev_latest = latest

        await asyncio.sleep(INTERVAL_SECONDS)

if __name__ == "__main__":
    threading.Thread(target=run_http_server_if_needed, daemon=True).start()
    asyncio.run(main())
