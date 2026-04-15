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
        "obi": None,             # Order Book Imbalance
        "intraday_high": None,   # running intraday high (for forecast blending)
        "intraday_low":  None,   # running intraday low  (for forecast blending)
    },
    "JDST": {
        "records": [],
        "trade_price": None,
        "buy": 0,
        "sell": 0,
        "obi": None,
        "intraday_high": None,
        "intraday_low":  None,
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
                if not yield_history or val != yield_history[-1]["yield"]:
                    # Only append when value changes — old timestamp preserved when flat
                    # so you can tell exactly when the yield started holding steady
                    yield_history.append({"yield": val, "time": ts})
                    yield_history = yield_history[-4:]

            await asyncio.sleep(5)

last_gld_update = 0
gld_history = []

# Fetch from Railway endpoint
async def fetch_gld_from_railway(session) -> float | None:
    try:
        url = "https://playwright-production-da6f.up.railway.app/gld"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as r:
            data = await r.json()
            raw = data.get("gold")

            if raw is None:
                return None

            # Convert "4,739.725" → 4739.725
            if isinstance(raw, str):
                raw = raw.replace(",", "")

            return float(raw)
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
                if not gld_history or val != gld_history[-1]["gld"]:
                    # Only append when value changes — old timestamp preserved when flat
                    gld_history.append({"gld": val, "time": ts})
                    gld_history = gld_history[-4:]

            await asyncio.sleep(2)


# ── Forecast state ─────────────────────────────────────────────────────────
# Stores up to 10 forecast records; UI shows latest 4.
# Each record: {"NUGT": {open, high, low, close}, "JDST": {...}, "time": "HH:MM"}
forecast_history    = []
_last_forecast_slot = -1   # 10-min slot index last successfully completed

# Today's open prices — captured automatically from FMP's open field.
NUGT_OPEN = None
JDST_OPEN = None
_open_captured = {"NUGT": False, "JDST": False}   # capture only once per day


# 2. REST FALLBACK (To get the last price when market is closed)
def fmp_poll_loop():

    FMP_KEY = os.environ.get("FMP_API_KEY")
    if not FMP_KEY:
        print("CRITICAL: FMP_API_KEY missing")
        return

    # Reset intraday tracking at start of each new trading day
    last_date = None

    while True:
        # Block here (zero CPU) until market_open_event is set by market_watcher
        market_open_event.wait()

        # ── Daily reset ──────────────────────────────────────────────────
        today = datetime.now(ET).strftime("%Y-%m-%d")
        if today != last_date:
            last_date = today
            for sym in SYMBOLS:
                all_history[sym]["intraday_high"] = None
                all_history[sym]["intraday_low"]  = None
                all_history[sym]["buy"]  = 0
                all_history[sym]["sell"] = 0
            _open_captured["NUGT"] = False
            _open_captured["JDST"] = False
            print(f"Daily reset for {today}")

        for sym in SYMBOLS:

            # bid/ask/trade price only — fast, every 1 s
            # open / dayHigh / dayLow are fetched in forecast_loop every 10 min
            url_quote = "https://financialmodelingprep.com/stable/aftermarket-quote"
            url_trade = "https://financialmodelingprep.com/stable/quote"
            params    = {"symbol": sym, "apikey": FMP_KEY}

            try:
                r = requests.get(url=url_quote, params=params, timeout=5)
                r.raise_for_status()
                quote = r.json()

                if not quote:
                    print(f"No quote data for {sym}")
                    continue

                display_st = ""
                quote   = quote[0]
                bid     = quote.get("bidPrice")
                ask     = quote.get("askPrice")
                bidSize = quote.get("bidSize", 0)
                askSize = quote.get("askSize", 0)
                ts      = datetime.now(ET).strftime("%H:%M:%S")

                # trade price only from stable/quote
                r2 = requests.get(url=url_trade, params=params, timeout=5)
                r2.raise_for_status()
                trade_price = r2.json()[0].get("price")

                # OBI
                if bidSize + askSize > 0:
                    obi = (bidSize - askSize) / (bidSize + askSize)
                else:
                    obi = 0
                obi = float(f"{obi:.2f}")

                # BUY/SELL classification
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

                # 7. Format price as "bid - ask"
                if bid is not None and ask is not None:
                    price_str = f"{bid:.2f} - {ask:.2f}"
                elif bid is not None:
                    price_str = f"{bid:.2f} - None"
                elif ask is not None:
                    price_str = f"None - {ask:.2f}"
                else:
                    price_str = "None - None"

                # 8. Append to all_history records — only when price or net_vol changed
                #    Timestamp is frozen when values are flat (same as gold/yield behaviour)
                records = all_history[sym]["records"]
                last    = records[-1] if records else None
                if last is None or last["price"] != price_str or last["net_volume"] != net_vol:
                    records.append({
                        "price":      price_str,
                        "time":       ts,
                        "net_volume": net_vol,
                        "olb":        obi
                    })
                    all_history[sym]["records"] = records[-10:]

                display_st += f"{price_str} @ {ts} {net_vol} {obi})"

                # 9. Update state for next tick
                all_history[sym]["trade_price"] = trade_price
                all_history[sym]["obi"] = obi  # store CURRENT OBI

            except Exception as e:
                print(f"Polling error {sym}: {e}")

            time.sleep(0.1)  # 0.1s between symbols

        time.sleep(1)  # 1s between full cycles

def _load_history(FC_SYMBOLS, _download, _build_features, _cache, label="load"):
    """Download and cache historical data for all symbols."""
    for sym in FC_SYMBOLS:
        try:
            raw  = _download(sym)
            feat = _build_features(raw)
            _cache[sym] = {"raw": raw, "feat": feat}
            print(f"forecast_loop: {label} {len(feat)} rows for {sym}")
        except Exception as e:
            print(f"forecast_loop: {label} failed for {sym}: {e}")


def forecast_loop():
    """
    Dedicated blocking loop for forecast — mirrors fmp_poll_loop structure:
      1. Check market open       → sleep 60 s and retry if closed
      2. Daily reset             → reload history cache for new trading day
      3. Fetch open/high/low     → from FMP once per 10-min slot
      4. Run forecast            → model already warm, no download needed
      5. Sleep 10 min
    Runs in its own executor thread, never touches fmp_poll_loop.
    """
    global forecast_history, _last_forecast_slot

    from forecast import get_forecast, _download, _build_features, _cache, SYMBOLS as FC_SYMBOLS

    # last_date = None ensures the daily-reset block fires on the very first
    # iteration, which handles both startup pre-load and subsequent day resets
    # in one place — no duplicate loop needed.
    last_date = None

    while True:
        # Block here (zero CPU) until market_open_event is set by market_watcher
        market_open_event.wait()

        # 2. Daily reset — also serves as the startup pre-load on first iteration
        today = datetime.now(ET).strftime("%Y-%m-%d")
        if today != last_date:
            label     = "pre-loaded" if last_date is None else f"refreshed ({today})"
            last_date = today
            _last_forecast_slot = -1
            print(f"forecast_loop: {label} — loading history")
            _load_history(FC_SYMBOLS, _download, _build_features, _cache, label)

        # 2. Skip if already ran this 10-min slot
        now  = datetime.now(ET)
        slot = (now.hour * 60 + now.minute) // 10
        if slot == _last_forecast_slot:
            time.sleep(30)   # already done, check again in 30 s
            continue

        # 3. Fetch open / dayHigh / dayLow from FMP once per slot
        #    (stable intraday values — no need to poll every second)
        FMP_KEY_FC = os.environ.get("FMP_API_KEY")
        for sym in SYMBOLS:
            try:
                url    = "https://financialmodelingprep.com/stable/quote"
                params = {"symbol": sym, "apikey": FMP_KEY_FC}
                q      = requests.get(url=url, params=params, timeout=5).json()[0]

                open_price = q.get("open")
                day_high   = q.get("dayHigh")
                day_low    = q.get("dayLow")

                # Capture official open (retry each slot until non-zero)
                if open_price and not _open_captured[sym]:
                    if sym == "NUGT":
                        globals()["NUGT_OPEN"] = float(open_price)
                    else:
                        globals()["JDST_OPEN"] = float(open_price)
                    _open_captured[sym] = True
                    print(f"forecast_loop: captured open for {sym}: {open_price}")

                # Update intraday high — only if higher than existing
                # (dayHigh can only go up; guards against stale FMP data)
                if day_high is not None:
                    new_high = float(day_high)
                    cur_high = all_history[sym]["intraday_high"]
                    if cur_high is None or new_high > cur_high:
                        all_history[sym]["intraday_high"] = new_high

                # Update intraday low — only if lower than existing
                if day_low is not None:
                    new_low = float(day_low)
                    cur_low = all_history[sym]["intraday_low"]
                    if cur_low is None or new_low < cur_low:
                        all_history[sym]["intraday_low"] = new_low

            except Exception as e:
                print(f"forecast_loop: FMP fetch error for {sym}: {e}")

        # Skip if open prices still not available after fetch
        if NUGT_OPEN is None or JDST_OPEN is None:
            time.sleep(30)
            continue

        # 4. Run forecast — cache already warm, no download needed
        try:
            live = {}
            for sym in SYMBOLS:
                h = all_history[sym]["intraday_high"]
                l = all_history[sym]["intraday_low"]
                t = all_history[sym]["trade_price"]
                if h is not None and l is not None and t is not None:
                    live[sym] = {"high": h, "low": l, "last": t}

            result = get_forecast(
                nugt_open=NUGT_OPEN,
                jdst_open=JDST_OPEN,
                live=live if live else None,
                force_refresh=False,   # cache managed explicitly above
            )

            ts = now.strftime("%H:%M")
            forecast_history.append({**result, "time": ts})
            forecast_history    = forecast_history[-10:]
            _last_forecast_slot = slot
            print(f"Forecast updated @ {ts}: {result}")

        except Exception as e:
            print(f"Forecast error: {e}")

        # 5. Sleep until next 10-min slot boundary
        time.sleep(600)


# ── Market gate ───────────────────────────────────────────────────────────
# fmp_poll_loop and forecast_loop block on this event when market is closed.
# market_watcher sets/clears it exactly once per open/close transition.
market_open_event = threading.Event()


async def market_watcher():
    """
    Async watcher that sets/clears market_open_event on open/close transitions.
    Checks every 30 s — cheap, and transitions are never missed by more than 30 s.
    Triggers exactly once per status change so threads are started/stopped once.
    """
    last_status = None
    while True:
        open_now = is_market_open()
        if open_now != last_status:
            last_status = open_now
            if open_now:
                print("market_watcher: market OPEN  — starting poll + forecast threads")
                market_open_event.set()      # unblocks both threads
            else:
                print("market_watcher: market CLOSED — pausing poll + forecast threads")
                market_open_event.clear()    # blocks both threads at next .wait()
        await asyncio.sleep(30)


def start_system():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Start 5-second yield updater
    loop.create_task(yield_updater())

    # Start 2-second gld updater
    loop.create_task(gld_updater())

    # Start market open/close watcher — sets market_open_event
    loop.create_task(market_watcher())

    # Run fmp_poll_loop in a background thread
    # (blocks on market_open_event when market is closed)
    loop.run_in_executor(None, fmp_poll_loop)

    # Run forecast_loop in its own background thread
    # (blocks on market_open_event when market is closed)
    loop.run_in_executor(None, forecast_loop)

    loop.run_forever()

# 4. GRADIO UI
def update_ui():

    # --- TOP YIELD TICKER ---
    html_output = "<div style='font-family: monospace; margin-bottom: 6px;'>"

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
        # Render newest first — reverse so index 0 = most recent
        for i, rec in enumerate(reversed(gld_history)):
            value = rec["gld"]
            ts_y = rec["time"]

            if i == 0:
                # FIRST LINE — bold
                html_output += (
                    f"<span style='color: #FFD700;'>{value}</span>"
                    f"<span style='font-size: 16px; color: #aaa;'> @ {ts_y}</span>"
                    f"</div>"
                )
            else:
                # Other lines — normal
                html_output += (
                    f"<div style='margin: 5px 0; color: #aaa; font-size: 1.0em;'>"
                    f"{value}"
                    f"  @ {ts_y}"
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
        # Render newest first — reverse so index 0 = most recent
        for i, rec in enumerate(reversed(yield_history)):
            value = rec["yield"]
            ts_y = rec["time"]

            if i == 0:
                # FIRST LINE — bold
                html_output += (
                    f"<span style='color:#4da6ff;'>{value}</span> "
                    f"<span style='font-size: 16px; color: #aaa;'>&nbsp;&nbsp;@&nbsp;&nbsp;{ts_y}</span>"
                    f"</div>"
                )
            else:
                # Other lines — normal
                html_output += (
                    f"<div style='margin: 5px 0; color: #aaa; font-size: 1.0em;'>"
                    f"{value}"
                    f"&nbsp;&nbsp;@&nbsp;{ts_y}"
                    f"</div>"
                )

    html_output += "</div>"

    # Nugt and Jdst prices
    html_output += "<div style='background-color: #1a1a1a; padding: 8px 12px; border-radius: 10px;'>"

    for sym, color in [("NUGT", "#FFD700"), ("JDST", "#00FF00")]:

        # Last 4 records — newest first
        records_to_show = list(reversed(all_history[sym]["records"][-4:]))

        # Net volume + OBI
        net_vol = all_history[sym]["buy"] - all_history[sym]["sell"]
        obi = all_history[sym]["obi"]

        # Color rules
        vol_color = "#aaa" if net_vol >= 0 else "red"
        obi_color = "#aaa" if (obi is None or obi >= 0) else "red"

        # --- SYMBOL HEADER (ALL IN ONE LINE) ---
        html_output += "<div style='margin-bottom: 8px; font-family: monospace;'>"

        html_output += (
            f"<div style='display:flex; align-items:center; "
            f"font-size:24px; font-weight:bold; border-bottom:1px solid #333; "
            f"margin-bottom:10px;'>"

            # Symbol
            f"<span style='color:{color}; margin-right:15px;'>{sym[0]}</span>"

            # Volume
            f"<span style='color:{vol_color}; font-weight: normal; font-size:18px; margin-right:10px;'>"
            f"vol {net_vol}</span>"

            # OBI
            f"<span style='color:{obi_color}; font-weight: normal; font-size:18px;'>"
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
                    f"font-weight: normal; font-size: 1.0em; color:#aaa;"
                    if i == 0 else
                    "color: #aaa; font-size: 1.0em;"
                )

                # Only show price + time
                price = f"{rec['price']}"
                time_str = f" @ {rec['time']}"
                html_output += f"<div style='margin: 5px 0; {style}'>{price}<span style='{style_time}'>{time_str}</span></div>"

        html_output += "</div>"

    html_output += "</div>"

    # ── Forecast section ──────────────────────────────────────────────────
    html_output += "<div style='font-family: monospace; margin-top: 6px;'>"

    if not forecast_history or not is_market_open():
        # Market closed — single line, same style as Gld/yield closed state
        ts = datetime.now(ET).strftime("%D  %H:%M")
        html_output += (
            "<div style='color: white; font-size: 18px; font-weight: bold; "
            "border-bottom:1px solid #333; margin-bottom:10px;'>"
            "<span style='color: #FFD700;'>N</span>"
            "<span style='color: #aaa; font-weight: normal; font-size: 16px;'>"
            f"  Market closed @ {ts}</span></div>"
            "<div style='color: white; font-size: 18px; font-weight: bold; "
            "margin-bottom:10px;'>"
            "<span style='color: #00FF00;'>J</span>"
            "<span style='color: #aaa; font-weight: normal; font-size: 16px;'>"
            f"  Market closed @ {ts}</span></div>"
        )
    else:
        # Show latest 4 forecast records (most recent first)
        records_to_show = list(reversed(forecast_history[-4:]))

        for sym, color in [("NUGT", "#FFD700"), ("JDST", "#00FF00")]:
            label = sym[0]   # "N" or "J"

            for i, rec in enumerate(records_to_show):
                fc   = rec.get(sym, {})
                low  = fc.get("low",   "—")
                high = fc.get("high",  "—")
                close= fc.get("close", "—")
                ts   = rec.get("time", "")

                if i == 0:
                    # Latest record — bold, colored, with border-bottom separator between N and J
                    border = "border-bottom:1px solid #333; margin-bottom:6px;" if sym == "NUGT" else ""
                    html_output += (
                        f"<div style='font-size: 18px; font-weight: bold; "
                        f"{border} padding-bottom:6px; margin-bottom:4px;'>"
                        f"<span style='color:{color};'>{label}</span>"
                        f"<span style='color:{color};'> {low} - {high}</span>"
                        f"<span style='color: #aaa; font-weight: normal; font-size: 15px;'>"
                        f", closed {close} @ {ts}</span>"
                        f"</div>"
                    )
                else:
                    # Older records — dimmed, no label repeated
                    html_output += (
                        f"<div style='color: #aaa; font-size: 1.0em; margin: 3px 0 3px 18px;'>"
                        f"{low} - {high}, closed {close} @ {ts}"
                        f"</div>"
                    )

    html_output += "</div>"
    # ── End forecast section ───────────────────────────────────────────────

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