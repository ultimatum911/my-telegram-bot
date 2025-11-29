import asyncio
import aiohttp
import os
from telegram import Bot
from flask import Flask
import threading

# === CONFIG ===
BOT_TOKEN = os.environ.get("BOT_TOKEN")  # Set in Fly.io secrets
CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME")  # Set in Fly.io secrets
# How often to poll the Nobitex API for price updates (in seconds)
INTERVAL = 30  # seconds

# Percentage change threshold to trigger a new message.  If the price moves by
# this percentage (up or down) compared to the last recorded price during the
# polling interval, a new message will be sent.  For example, a value of
# 0.2 means a 0.2% change.
PRICE_CHANGE_PERCENT_THRESHOLD = 0.2  # percent

bot = Bot(token=BOT_TOKEN)
last_price = None  # Tracks the last observed price to compute percentage changes

# We no longer use the `alert_sent` flag or fixed Rial change threshold.


# --- Flask server to keep Fly.io happy ---
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running!"

def run_flask():
    app.run(host="0.0.0.0", port=8080)

# --- Bot functions ---
async def fetch_price(session):
    url = "https://apiv2.nobitex.ir/market/stats"
    params = {"srcCurrency": "usdt", "dstCurrency": "rls"}
    headers = {"User-Agent": "TraderBot/1.0"}

    try:
        async with session.get(url, params=params, headers=headers, timeout=10) as resp:
            resp.raise_for_status()
            data = await resp.json()
            stats = data.get("stats", {})
            usdt_rls = stats.get("usdt-rls")
            if usdt_rls is None:
                raise ValueError("Data missing")

            latest = int(usdt_rls.get("latest"))
            bestBuy = int(usdt_rls.get("bestBuy"))
            bestSell = int(usdt_rls.get("bestSell"))

            message = (
                f"💵 USDT/Rial — Latest: `{latest}`\n"
                f"🛒 Buy: `{bestBuy}` | 💰 Sell: `{bestSell}`\n"
                "──────────────"
            )
            return message, latest
    except Exception as e:
        print("Fetch error:", e)
        return f"Error fetching price: {e}", None

async def send_message_safe(text):
    try:
        await bot.send_message(chat_id=CHANNEL_USERNAME, text=text, parse_mode="Markdown")
    except Exception as e:
        print("Send error:", e)

async def main_loop():
    """Main polling loop.

    This function continuously polls the Nobitex API for the USDT/Rial rate.  If the
    price has changed by more than `PRICE_CHANGE_PERCENT_THRESHOLD` percent (either
    up or down) compared to the last recorded price, a message containing the
    latest price information is sent to the Telegram channel.  Otherwise, no
    message is sent.  The first fetched price is always sent to provide a
    baseline for subsequent comparisons.
    """
    global last_price
    async with aiohttp.ClientSession() as session:
        while True:
            # Fetch the latest price data from the API
            message, current_price = await fetch_price(session)

            # Only act if we received a valid current price
            if current_price is not None:
                # If this is the first price fetch, send the message and set baseline
                if last_price is None:
                    await send_message_safe(message)
                    print("Initial price sent:", message)
                    last_price = current_price
                else:
                    # Calculate the absolute percentage change relative to the last recorded price
                    diff = abs(current_price - last_price)
                    percent_change = (diff / last_price) * 100 if last_price != 0 else 0

                    # If the percentage change exceeds the threshold, send a message
                    if percent_change >= PRICE_CHANGE_PERCENT_THRESHOLD:
                        await send_message_safe(message)
                        print(
                            f"Price changed by {percent_change:.3f}% (threshold {PRICE_CHANGE_PERCENT_THRESHOLD}%), message sent: {message}"
                        )
                        # Update last_price to the new price after sending the alert
                        last_price = current_price
                    else:
                        # Price change is below the threshold; do nothing (no message sent)
                        print(
                            f"Price change {percent_change:.3f}% is below threshold {PRICE_CHANGE_PERCENT_THRESHOLD}%, no message sent"
                        )

            # Wait for the configured interval before the next poll
            await asyncio.sleep(INTERVAL)

# --- Run Flask + Bot ---
if __name__ == "__main__":
    threading.Thread(target=run_flask).start()
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main_loop())
