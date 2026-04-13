---
title: Etf
emoji: 🐢
colorFrom: red
colorTo: pink
sdk: gradio
sdk_version: 6.10.0
app_file: app.py
pinned: false
license: apache-2.0
short_description: nugt and jdst real time
---

Check out the configuration reference at https://huggingface.co/docs/hub/spaces-config-reference


FMP API
1. executing price:
 r = requests.get(
       "https://financialmodelingprep.com/stable/quote",
        params={"symbol": sym, "apikey": FMP_KEY},
        timeout=5
        )
  r.raise_for_status()
  data = r.json()

2. real time ask/bid price
 url = f"https://financialmodelingprep.com/stable/aftermarket-quote"
            params = {"symbol": sym, "apikey": FMP_KEY}
            
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()                
        response_data = r.json()  


all_history = {
    "NUGT": {
        "records": [
            {"price": "199.25 - 199.84", "time": "15:40:23", "net_volume": 152, "olb": -0.42},
            {"price": "199.31 - 199.90", "time": "15:40:22", "net_volume": 151, "olb": -0.38},
            {"price": "199.40 - 199.95", "time": "15:40:21", "net_volume": 150, "olb": -0.35},
            {"price": "199.55 - 200.01", "time": "15:40:20", "net_volume": 149, "olb": -0.33},
        ],
        "trade_price": 199.55,
        "buy": 180,
        "sell": 28,
        "obi": -0.42
    },

    "JDST": {
        "records": [
            {"price": "9.12 - 9.15", "time": "15:40:23", "net_volume": -88, "olb": 0.12},
            {"price": "9.11 - 9.14", "time": "15:40:22", "net_volume": -85, "olb": 0.10},
            {"price": "9.10 - 9.13", "time": "15:40:21", "net_volume": -82, "olb": 0.08},
            {"price": "9.09 - 9.12", "time": "15:40:20", "net_volume": -80, "olb": 0.06},
        ],
        "trade_price": 9.12,
        "buy": 40,
        "sell": 128,
        "obi": 0.12
    }
}


old fetch 10 year yield from yahoo finance
async def fetch_yahoo_realtime(session):
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ETNX"
        params = {"interval": "1m", "range": "1d"}
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        async with session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as r:
            data = await r.json()
        result = data.get("chart", {}).get("result")
        if not result:
            return None
        closes = (
            result[0]
            .get("indicators", {})
            .get("quote", [{}])[0]
            .get("close", [])
        )
        valid = [c for c in closes if c is not None]
        if valid:
            return round(valid[-1] / 10, 4)
        raw = result[0].get("meta", {}).get("regularMarketPrice")
        return round(raw / 10, 4) if raw else None
    except Exception:
        return None

async def fetch_stooq(session):
    try:
        url = "https://stooq.com/q/l/?s=10us.b&f=sd2t2ohlcv&h&e=csv"
        headers = {"User-Agent": "Mozilla/5.0"}
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as r:
            text = await r.text()
        lines = text.strip().splitlines()
        if len(lines) < 2:
            return None
        parts = lines[1].split(",")
        return round(float(parts[6]), 4)
    except Exception:
        return None


async def fetch_cnbc(session):
    try:
        url = "https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol"
        params = {
            "symbols": "US10Y",
            "requestMethod": "itv",
            "noform": "1",
            "partnerId": "2",
            "fund": "1",
            "exthrs": "1",
            "output": "json",
            "events": "0",
        }
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.cnbc.com"}
        async with session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as r:
            data = await r.json(content_type=None)
        quote = data["FormattedQuoteResult"]["FormattedQuote"][0]
        return float(quote["last"])
    except Exception:
        return None

async def get_10y_yield() -> float | None:
    async with aiohttp.ClientSession() as session:
        for fetcher in [
            fetch_yahoo_realtime,
            fetch_stooq,
            fetch_cnbc,
        ]:
            val = await fetcher(session)
            if val:
                return val
    return None

# daily
async def fetch_fred(session):
    try:
        url = "https://api.stlouisfed.org/fred/series/observations"
        params = {
            "series_id": "DGS10",
            "api_key": FRED_KEY,
            "file_type": "json",
            "limit": 1,
            "sort_order": "desc"
        }
        async with session.get(url, params=params, timeout=5) as r:
            data = await r.json()

            obs = data["observations"][0]
            val = obs["value"]

            if val != ".":
                return float(val)
    except:
        return None

Sample data
all_history = {
    "NUGT": {
        "records": [
            {"price": "199.25 - 199.84", "time": "15:40:23", "net_volume": 152, "olb": -0.42},
            {"price": "199.31 - 199.90", "time": "15:40:22", "net_volume": 151, "olb": -0.38},
            {"price": "199.40 - 199.95", "time": "15:40:21", "net_volume": 150, "olb": -0.35},
            {"price": "199.55 - 200.01", "time": "15:40:20", "net_volume": 149, "olb": -0.33},
        ],
        "trade_price": 199.55,
        "buy": 180,
        "sell": 28,
        "obi": -0.42
    },

    "JDST": {
        "records": [
            {"price": "9.12 - 9.15", "time": "15:40:23", "net_volume": -88, "olb": 0.12},
            {"price": "9.11 - 9.14", "time": "15:40:22", "net_volume": -85, "olb": 0.10},
            {"price": "9.10 - 9.13", "time": "15:40:21", "net_volume": -82, "olb": 0.08},
            {"price": "9.09 - 9.12", "time": "15:40:20", "net_volume": -80, "olb": 0.06},
        ],
        "trade_price": 9.12,
        "buy": 40,
        "sell": 128,
        "obi": 0.12
    }
}
