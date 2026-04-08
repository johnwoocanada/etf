import os
import time
import threading
import requests
import gradio as gr
# --- ADD THESE IMPORTS ---
from alpaca.data.live import StockDataStream
from alpaca.data.enums import DataFeed

import exchange_calendars as xcals
from datetime import datetime
import pytz
ET = pytz.timezone("America/New_York")
import aiohttp
import asyncio

# author John

# Load once at module level — not inside the function
NYSE = xcals.get_calendar("XNYS")

def is_market_open() -> bool:
    """Returns True only during NYSE regular trading hours, holidays included."""
    et = pytz.timezone("America/New_York")
    now = datetime.now(et)
    now_utc = datetime.now(pytz.utc)

    # Check if today is a trading day (excludes weekends + all NYSE holidays)
    today_str = now.strftime("%Y-%m-%d")
    if not NYSE.is_session(today_str):
        return False

    # Check if current time is within open/close schedule
    return NYSE.is_open_on_minute(now_utc)

# 1. AUTH & CONFIG (Hugging Face Secrets)
API_KEY = os.environ.get("APCA_API_KEY_ID")
SECRET_KEY = os.environ.get("APCA_API_SECRET_KEY")
FMP_KEY = os.environ.get("FMP_API_KEY")
FRED_KEY = os.environ.get("FRED_API_KEY")

BASE = "https://data.alpaca.markets/v2"
SYMBOLS = ["NUGT", "JDST"]

# Store the last 4 records for each symbol
all_history = {
    "NUGT": {
        "records": [],
        "trade_price": None,     # latest trade price
        "buy": 0,                # cumulative intraday buy volume
        "sell": 0,               # cumulative intraday sell volume
        "obi": None              # Order Book Imbalance
    },
    "JDST": {
        "records": [],
        "trade_price": None,
        "buy": 0,
        "sell": 0,
        "obi": None
    }
}

last_yield_update = 0
yield_history = []

# Fetch from Railway endpoint
async def fetch_yield_from_railway(session) -> float | None:
    try:
        url = "https://playwright-production-da6f.up.railway.app/yield"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as r:
            data = await r.json()
            return float(data.get("yield"))
    except:
        return None


async def yield_updater():
    global yield_history

    async with aiohttp.ClientSession() as session:
        while True:
            val = await fetch_yield_from_railway(session)
            ts = datetime.now(ET).strftime("%H:%M:%S")

            if val is not None:
                yield_history.append({"yield": val, "time": ts})
                yield_history = yield_history[-4:]   # keep last 4 entries

            await asyncio.sleep(5)

last_gld_update = 0
gld_history = []

# Fetch from Railway endpoint
async def fetch_gld_from_railway(session) -> float | None:
    try:
        url = "https://playwright-production-da6f.up.railway.app/gld"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as r:
            data = await r.json()
            return float(data.get("gold"))
    except:
        return None


async def gld_updater():
    global gld_history

    async with aiohttp.ClientSession() as session:
        while True:
            val = await fetch_gld_from_railway(session)
            ts = datetime.now(ET).strftime("%H:%M:%S")

            if val is not None:
                val = f"{val:.2f}"
                gld_history.append({"gld": val, "time": ts})
                gld_history = gld_history[-4:]   # keep last 4 entries

            await asyncio.sleep(2)


# 2. REST FALLBACK (To get the last price when market is closed)
def fmp_poll_loop():

    FMP_KEY = os.environ.get("FMP_API_KEY")
    if not FMP_KEY:
        print("CRITICAL: FMP_API_KEY missing")
        return

    while True:
        if not is_market_open():
            time.sleep(60)  # sleep 1 min when market closed
            continue

        for sym in SYMBOLS:

            # 1. Get bid/ask from aftermarket-quote
            url_trade= "https://financialmodelingprep.com/stable/quote"
            url_quote = "https://financialmodelingprep.com/stable/aftermarket-quote"
            params = {"symbol": sym, "apikey": FMP_KEY}
            timeout=5

            try:
                r = requests.get(url=url_quote, params=params, timeout=5)
                r.raise_for_status()
                quote = r.json()

                if not quote:
                    print(f"No quote data for {sym}")
                    continue

                display_st = ""
                quote = quote[0]

                bid = quote.get("bidPrice")
                ask = quote.get("askPrice")
                bidSize = quote.get("bidSize", 0)
                askSize = quote.get("askSize", 0)

                ts = datetime.now(ET).strftime("%H:%M:%S")

                # 2. Get last trade price from stable/quote
                r2 = requests.get(url=url_trade, params=params, timeout=timeout)
                r2.raise_for_status()
                q2 = r2.json()
                trade_price = q2[0].get("price")

                # 3. Compute current OBI
                if bidSize + askSize > 0:
                    obi = (bidSize - askSize) / (bidSize + askSize)
                else:
                    obi = 0
                obi = float(f"{obi:.2f}")

                # 5. BUY/SELL classification
                prev_trade = all_history[sym]["trade_price"]

                if trade_price is not None and bid is not None and ask is not None:

                    if trade_price >= ask:
                        all_history[sym]["buy"] += 1

                    elif trade_price <= bid:
                        all_history[sym]["sell"] += 1

                    else:
                        # mid-price → tick rule
                        if prev_trade is not None:
                            if trade_price > prev_trade:
                                all_history[sym]["buy"] += 1
                            elif trade_price < prev_trade:
                                all_history[sym]["sell"] += 1

                net_vol = all_history[sym]["buy"] - all_history[sym]["sell"]
                display_st += f"{net_vol} "

                # 6. Format price as "bid - ask"
                if bid is not None and ask is not None:
                    price_str = f"{bid:.2f} - {ask:.2f}"
                elif bid is not None:
                    price_str = f"{bid:.2f} - None"
                elif ask is not None:
                    price_str = f"None - {ask:.2f}"
                else:
                    price_str = "None - None"

                # 7. Append to all_history records
                all_history[sym]["records"].append({
                    "price": price_str,
                    "time": ts,
                    "net_volume": net_vol,
                    "olb": obi
                })

                display_st += f"{price_str} @ {ts} {net_vol} {obi})"

                # Keep only last 10 records
                if len(all_history[sym]["records"]) > 10:
                    all_history[sym]["records"] = all_history[sym]["records"][-10:]

                # 8. Update state for next tick
                all_history[sym]["trade_price"] = trade_price
                all_history[sym]["obi"] = obi  # store CURRENT OBI

            except Exception as e:
                print(f"Polling error {sym}: {e}")

            time.sleep(0.1)  # 0.1s between symbols

        time.sleep(1)  # 1s between full cycles

def start_system():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Start 5-second yield updater
    loop.create_task(yield_updater())

    # Start 2-second gld updater
    loop.create_task(gld_updater())

    # Run fmp_poll_loop in a background thread
    loop.run_in_executor(None, fmp_poll_loop)

    loop.run_forever()

# 4. GRADIO UI
def update_ui():
    # --- TOP YIELD TICKER ---
    html_output = "<div style='font-family: monospace; margin-bottom: 25px;'>"

    # Header gld
    html_output += (
        "<div style='color: white; font-size: 18px; font-weight: bold; "
        "border-bottom:1px solid #333; margin-bottom:10px;'><span style='color: #FFD700;'>Gld </span>"
    )

    # --- NO RECORDS? ---
    if not gld_history:
        ts = datetime.now(ET).strftime("%D  %H:%M")
        html_output += "</div><div style='color: #666;'>Market closed @ " + ts + "</div>"
    else:
        # Render last 4 yield records
        for i, rec in enumerate(gld_history):
            value = rec["gld"]
            ts_y = rec["time"]

            if i == 0:
                # FIRST LINE — bold
                html_output += (
                    f"<span style='color: #FFD700;'>{value}</span>"
                    f"<span style='font-size: 16px; color:white;'> @ {ts_y}</span>"
                    f"</div>"
                )
            else:
                # Other lines — normal
                html_output += (
                    f"<div style='margin: 5px 0; color: #aaa; font-size: 1.0em;'>"
                    f"{value}"
                    f"@ {ts_y}"
                    f"</div>"
                )

    html_output += "</div>"

    # Header 10 year yield
    html_output += (
        "<div style='color: white; font-size: 20px; font-weight: bold; "
        "border-bottom:1px solid #333; margin-bottom:10px;'><span style='color: #4da6ff;'>10 Y&nbsp;&nbsp;</span>"
    )

    # --- NO RECORDS? ---
    if not yield_history:
        ts = datetime.now(ET).strftime("%D  %H:%M")
        html_output += "</div><div style='color: #666;'>Market closed @ " + ts + "</div>"
    else:
        # Render last 4 yield records
        for i, rec in enumerate(yield_history):
            value = rec["yield"]
            ts_y = rec["time"]

            if i == 0:
                # FIRST LINE — bold
                html_output += (
                    #f"<div style='font-size: 18px; font-weight: bold; margin: 3px 0;'>"
                    f"<span style='color:#4da6ff;'>{value}</span> "
                    f"<span style='font-size: 16px; color:white;'>&nbsp;&nbsp;@&nbsp;&nbsp;{ts_y}</span>"
                    f"</div>"
                )
            else:
                # Other lines — normal
                html_output += (
                    f"</div><div style='margin: 5px 0; color: #aaa; font-size: 1.0em;'>"
                    f"{value}"
                    f"@ {ts_y}"
                    f"</div>"
                )

    html_output += "</div>"

    # Nugt and Jdst prices
    html_output += "<div style='background-color: #1a1a1a; padding: 20px; border-radius: 10px;'>"

    for sym, color in [("NUGT", "#FFD700"), ("JDST", "#00FF00")]:

        # Last 4 records from all_history
        records_to_show = all_history[sym]["records"][:4]

        # Net volume + OBI
        net_vol = all_history[sym]["buy"] - all_history[sym]["sell"]
        obi = all_history[sym]["obi"]

        # Color rules
        vol_color = "white" if net_vol >= 0 else "red"
        obi_color = "white" if (obi is None or obi >= 0) else "red"

        # --- SYMBOL HEADER (ALL IN ONE LINE) ---
        html_output += "<div style='margin-bottom: 30px; font-family: monospace;'>"

        html_output += (
            f"<div style='display:flex; align-items:center; "
            f"font-size:24px; font-weight:bold; border-bottom:1px solid #333; "
            f"margin-bottom:10px;'>"

            # Symbol
            f"<span style='color:{color}; margin-right:15px;'>{sym[0]}</span>"

            # Volume
            f"<span style='color:{vol_color}; font-size:18px; margin-right:10px;'>"
            f"vol {net_vol}</span>"

            # OBI
            f"<span style='color:{obi_color}; font-size:18px;'>"
            f"obi {obi if obi is not None else 0:.2f}</span>"

            f"</div>"
        )

        # --- NO RECORDS? ---
        if not records_to_show:
            ts = datetime.now(ET).strftime("%D  %H:%M")
            html_output += "<div style='color: #666;'>Market closed @ " + ts + "</div>"
        else:
            # --- RECORD LINES ---
            for i, rec in enumerate(records_to_show):
                style = (
                    f"font-weight: bold; font-size: 1.2em; color:{color};"
                    if i == 0 else
                    "color: #aaa; font-size: 1.0em;"
                )
                style_time = (
                    f"font-weight: bold; font-size: 1.0em; color:white;"
                    if i == 0 else
                    "color: #aaa; font-size: 1.0em;"
                )

                # Only show price + time
                price = f"{rec['price']}"
                time_str = f" @ {rec['time']}"
                html_output += f"<div style='margin: 5px 0; {style}'>{price}<span style='{style_time}'>{time_str}</span></div>"

        html_output += "</div>"

    html_output += "</div>"
    return html_output


with gr.Blocks() as demo:
    #gr.Markdown("# 🚀 Real-Time ETF Tracker")
    
    # Main Data Display
    output_html = gr.HTML()
    
    # Control Row at the Bottom
    with gr.Row():
        gr.Markdown("&nbsp;") 
        
         # This column has scale=0 and a set min_width to keep it narrow
        with gr.Column(scale=0, min_width=80):
            refresh_speed = gr.Dropdown(
                choices=[1, 2, 3, 4, 5], 
                value=2, 
                label="Rate", 
                interactive=True,
                container=False,  # REMOVES the box/label that causes overlap
                show_label=False  # Hides the "Rate (s)" text to save space
            )

    # 1. Create a Timer. 'value' is the initial speed.
    timer = gr.Timer(value=2)
    
    # 2. Tell the Timer to update the UI
    timer.tick(fn=update_ui, outputs=output_html)
    
    # 3. LINK DROPDOWN TO TIMER:
    # When the dropdown changes, update the timer's 'value' property
    refresh_speed.change(fn=lambda x: x, inputs=refresh_speed, outputs=timer)

    # Load initial data
    demo.load(fn=update_ui, outputs=output_html)


# ── Start background polling thread ────────────────────────────────────────
# NOTE: No "if __name__ == '__main__'" wrapper — HuggingFace imports this
# file as a module, so that block never executes. Code here runs on import.

polling_thread = threading.Thread(target=start_system, daemon=True)
polling_thread.start()

# ── Launch Gradio ───────────────────────────────────────────────────────────
demo.launch(
    server_name="0.0.0.0",
    server_port=7860,
    ssr_mode=False,          # fixes "building" state on HuggingFace
    css=".gradio-container {background-color: #1a1a1a}"
)


# Local dev override — only triggers when run directly with "python app.py"
if __name__ == "__main__":
    pass  # launch() above already handles everything
