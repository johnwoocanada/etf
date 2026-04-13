# -*- coding: utf-8 -*-
"""
forecast.py
-----------
Forecasts today's High, Low, and Close for NUGT and JDST.

Two-layer design
~~~~~~~~~~~~~~~~
Layer 1 — ML model (overnight):
    Weighted ensemble of:
      - Model A: Open-only linear regression
      - Model B: Lagged OHLC + RSI + MACD + SMA + EMA + ATR + Bollinger position
    Uses only historical data + today's open price.
    This is the *only* layer available before the market opens.

Layer 2 — Intraday live blend (during market hours):
    As the trading day progresses, the running intraday High/Low/last-price
    are known. The final forecast blends the ML prediction with these live
    actuals using a time-decay weight:

        live_weight = (minutes_elapsed / total_trading_minutes) ^ DECAY_POWER

        At 09:30 -> live_weight ~ 0.00  (model dominates)
        At 12:00 -> live_weight ~ 0.43
        At 15:30 -> live_weight ~ 0.97  (live actuals dominate)

    Because there is no direct intraday High/Low feed, app.py tracks
    the running high/low from the trade_price stream it already polls, and
    passes those values in when calling get_forecast().

Public API
----------
    from forecast import get_forecast, get_forecast_with_diagnostics

    # --- Before market open (model only) ---
    result = get_forecast(nugt_open=217.36, jdst_open=29.78)

    # --- During market hours (blended with live intraday data) ---
    result = get_forecast(
        nugt_open=217.36, jdst_open=29.78,
        live={
            "NUGT": {"high": 219.5, "low": 215.0, "last": 218.2},
            "JDST": {"high":  30.1, "low":  29.4, "last":  29.8},
        }
    )

    # Both return:
    # {
    #   "NUGT": {"open": 217.36, "high": 221.5, "low": 214.2, "close": 219.8},
    #   "JDST": {"open":  29.78, "high":  30.4, "low":  29.1, "close":  29.9},
    # }

Integration with app.py
-----------------------
Step 1 — Add intraday tracking fields to all_history at the top of app.py:

    all_history = {
        "NUGT": {
            ...existing fields...,
            "intraday_high": None,
            "intraday_low":  None,
        },
        "JDST": {
            ...existing fields...,
            "intraday_high": None,
            "intraday_low":  None,
        }
    }

Step 2 — Update intraday_high / intraday_low inside fmp_poll_loop()
after the line that sets all_history[sym]["trade_price"] = trade_price:

    if trade_price is not None:
        h = all_history[sym]["intraday_high"]
        l = all_history[sym]["intraday_low"]
        all_history[sym]["intraday_high"] = (
            trade_price if h is None else max(h, trade_price)
        )
        all_history[sym]["intraday_low"] = (
            trade_price if l is None else min(l, trade_price)
        )

Step 3 — Reset at the start of each new trading day (add to start_system
or wherever you handle daily reset logic):

    for sym in SYMBOLS:
        all_history[sym]["intraday_high"] = None
        all_history[sym]["intraday_low"]  = None

Step 4 — Call get_forecast() with the live dict:

    from forecast import get_forecast

    live = {}
    for sym in ["NUGT", "JDST"]:
        h = all_history[sym]["intraday_high"]
        l = all_history[sym]["intraday_low"]
        t = all_history[sym]["trade_price"]
        if h is not None and l is not None and t is not None:
            live[sym] = {"high": h, "low": l, "last": t}

    result = get_forecast(
        nugt_open=NUGT_OPEN_PRICE,
        jdst_open=JDST_OPEN_PRICE,
        live=live if live else None,
    )

Notes
-----
- No talib dependency — all indicators are pure pandas / numpy.
- Historical data is cached at module level; pass force_refresh=True to reload.
"""

import warnings
warnings.filterwarnings("ignore")

import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime, time as dtime
from sklearn.linear_model import LinearRegression
from sklearn.model_selection import train_test_split
from sklearn.metrics import r2_score
import pytz

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DATA_START       = "2024-01-01"   # enough history for all lag indicators
DATA_END         = "2026-12-31"
RSI_PERIOD       = 7
BB_PERIOD        = 20
ATR_PERIOD       = 14
MACD_FAST        = 12
MACD_SLOW        = 26
DEFAULT_WEIGHT_B = 0.6            # prior weight for lag model vs simple model

# Intraday blend
MARKET_OPEN_ET  = dtime(9, 30)
MARKET_CLOSE_ET = dtime(16, 0)
TOTAL_MINUTES   = 390.0           # 6.5 trading hours
DECAY_POWER     = 1.5             # curve shape: >1 ramps slowly then fast

ET      = pytz.timezone("America/New_York")
SYMBOLS = ["NUGT", "JDST"]

# Module-level historical data cache
_cache: dict = {}


# ---------------------------------------------------------------------------
# Technical indicators (pure pandas / numpy — no talib)
# ---------------------------------------------------------------------------

def _rsi(series: pd.Series, period: int) -> pd.Series:
    series = pd.to_numeric(series, errors="coerce")
    delta    = series.diff(1)
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
    rs = avg_gain / (avg_loss + 1e-10)
    return 100 - (100 / (1 + rs))


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _macd(series: pd.Series, fast: int, slow: int) -> pd.Series:
    return _ema(series, fast) - _ema(series, slow)


def _atr(high: pd.Series, low: pd.Series,
         close: pd.Series, period: int) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def _bollinger_position(close: pd.Series, period: int) -> pd.Series:
    """0 = at lower band, 1 = at upper band, 0.5 = at SMA."""
    sma   = close.rolling(window=period).mean()
    std   = close.rolling(window=period).std()
    upper = sma + 2 * std
    lower = sma - 2 * std
    width = (upper - lower).replace(0, np.nan)
    return (close - lower) / width


# ---------------------------------------------------------------------------
# Data download & feature engineering
# ---------------------------------------------------------------------------

def _download(symbol: str) -> pd.DataFrame:
    df = yf.download(tickers=symbol, start=DATA_START, end=DATA_END,
                     interval="1d", progress=False, auto_adjust=True)
    if df.empty:
        raise ValueError(f"No data returned for {symbol}")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.reset_index()
    for col in ["Open", "High", "Low", "Close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    return df.sort_values("Date").reset_index(drop=True)


def _build_features(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["Lag1_Open"]  = d["Open"].shift(1)
    d["Lag1_High"]  = d["High"].shift(1)
    d["Lag1_Low"]   = d["Low"].shift(1)
    d["Lag1_Close"] = d["Close"].shift(1)
    d["Lag2_Close"] = d["Close"].shift(2)
    d["SMA"]        = d["Close"].rolling(BB_PERIOD).mean()
    d["EMA"]        = _ema(d["Close"], BB_PERIOD)
    d["RSI"]        = _rsi(d["Close"], RSI_PERIOD)
    d["MACD"]       = _macd(d["Close"], MACD_FAST, MACD_SLOW)
    d["ATR"]        = _atr(d["High"], d["Low"], d["Close"], ATR_PERIOD)
    d["BB_pos"]     = _bollinger_position(d["Close"], BB_PERIOD)
    return d.dropna().reset_index(drop=True)


# ---------------------------------------------------------------------------
# Model A — Open only
# ---------------------------------------------------------------------------
FEATURES_A = ["Open"]

def _train_a(df: pd.DataFrame):
    models, r2s = {}, {}
    for target in ("High", "Low", "Close"):
        X = df[FEATURES_A]
        y = df[target]
        Xtr, Xte, ytr, yte = train_test_split(
            X, y, test_size=0.2, random_state=42)
        m = LinearRegression().fit(Xtr, ytr)
        r2s[target]    = r2_score(yte, m.predict(Xte))
        models[target] = m
    return models, r2s


def _predict_a(models: dict, open_price: float) -> dict:
    x = pd.DataFrame([[open_price]], columns=FEATURES_A)
    return {t: float(models[t].predict(x)[0]) for t in ("High", "Low", "Close")}


# ---------------------------------------------------------------------------
# Model B — Lagged OHLC + indicators
# ---------------------------------------------------------------------------
FEATURES_B = [
    "Open",
    "Lag1_Open", "Lag1_High", "Lag1_Low", "Lag1_Close", "Lag2_Close",
    "SMA", "EMA", "RSI", "MACD", "ATR", "BB_pos",
]

def _train_b(df: pd.DataFrame):
    models, r2s = {}, {}
    for target in ("High", "Low", "Close"):
        X = df[FEATURES_B]
        y = df[target]
        Xtr, Xte, ytr, yte = train_test_split(
            X, y, test_size=0.2, random_state=42)
        m = LinearRegression().fit(Xtr, ytr)
        r2s[target]    = r2_score(yte, m.predict(Xte))
        models[target] = m
    return models, r2s


def _predict_b(models: dict, feat: pd.DataFrame, open_price: float) -> dict:
    row = {f: feat.iloc[-1][f] for f in FEATURES_B}
    row["Open"] = open_price   # override with today's actual open
    x = pd.DataFrame([row])
    return {t: float(models[t].predict(x)[0]) for t in ("High", "Low", "Close")}


# ---------------------------------------------------------------------------
# Dynamic ensemble weighting by R²
# ---------------------------------------------------------------------------

def _ml_ensemble(pred_a: dict, r2_a: dict,
                 pred_b: dict, r2_b: dict) -> dict:
    result = {}
    for t in ("High", "Low", "Close"):
        ra = max(r2_a[t], 0)
        rb = max(r2_b[t], 0)
        total = ra + rb
        if total == 0:
            wa = wb = 0.5
        else:
            # Blend data-driven ratio with the 0.4/0.6 prior
            wa = 0.5 * (ra / total) + 0.5 * (1 - DEFAULT_WEIGHT_B)
            wb = 0.5 * (rb / total) + 0.5 * DEFAULT_WEIGHT_B
        result[t] = wa * pred_a[t] + wb * pred_b[t]
    return result


# ---------------------------------------------------------------------------
# Intraday time-decay weight
# ---------------------------------------------------------------------------

def _live_weight(now_et: datetime = None) -> float:
    """
    How far through the trading day are we?
    0.0 = market just opened, 1.0 = market closed / after hours.
    Uses DECAY_POWER so trust in live data ramps up non-linearly.
    """
    if now_et is None:
        now_et = datetime.now(ET)
    t = now_et.time()
    if t <= MARKET_OPEN_ET:
        return 0.0
    if t >= MARKET_CLOSE_ET:
        return 1.0
    open_min  = MARKET_OPEN_ET.hour  * 60 + MARKET_OPEN_ET.minute
    close_min = MARKET_CLOSE_ET.hour * 60 + MARKET_CLOSE_ET.minute
    now_min   = t.hour * 60 + t.minute
    elapsed   = now_min - open_min
    return (elapsed / TOTAL_MINUTES) ** DECAY_POWER


# ---------------------------------------------------------------------------
# Blend ML forecast with live intraday actuals
# ---------------------------------------------------------------------------

def _blend_with_live(ml: dict, live: dict, weight: float) -> dict:
    """
    Merge ML predictions with live intraday observations.

    High: blend, then floor to the live high already observed
          (the day's high can only go up, never down)
    Low:  blend, then ceil to the live low already observed
          (the day's low can only go down, never up)
    Close: weighted blend toward the last known trade price
    """
    mw = 1.0 - weight
    lw = weight

    # High — blended, but hard floor at live high already printed
    blended_high = mw * ml["High"] + lw * live["high"]
    final_high   = max(blended_high, live["high"])

    # Low — blended, but hard ceiling at live low already printed
    blended_low = mw * ml["Low"] + lw * live["low"]
    final_low   = min(blended_low, live["low"])

    # Close — straight blend toward last known price
    final_close = mw * ml["Close"] + lw * live["last"]

    # Sanity: ensure low <= close <= high
    final_high  = max(final_high,  final_close, final_low)
    final_low   = min(final_low,   final_close, final_high)
    final_close = max(final_low,   min(final_close, final_high))

    return {"High": final_high, "Low": final_low, "Close": final_close}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_forecast(
    nugt_open: float,
    jdst_open: float,
    live: dict = None,
    force_refresh: bool = False,
) -> dict:
    """
    Returns forecasted Open / High / Low / Close for NUGT and JDST.

    Parameters
    ----------
    nugt_open     : today's opening price for NUGT
    jdst_open     : today's opening price for JDST
    live          : optional live intraday data, keyed by symbol:
                    {
                      "NUGT": {"high": float, "low": float, "last": float},
                      "JDST": {"high": float, "low": float, "last": float},
                    }
                    Omit (or pass None) before market open.
    force_refresh : re-download historical data even if already cached

    Returns
    -------
    {
        "NUGT": {"open": float, "high": float, "low": float, "close": float},
        "JDST": {"open": float, "high": float, "low": float, "close": float},
    }
    """
    open_prices = {"NUGT": nugt_open, "JDST": jdst_open}
    weight      = _live_weight()
    output      = {}

    for symbol in SYMBOLS:
        open_price = open_prices[symbol]

        # 1. Load / refresh historical data
        if force_refresh or symbol not in _cache:
            raw  = _download(symbol)
            feat = _build_features(raw)
            _cache[symbol] = {"raw": raw, "feat": feat}
        feat = _cache[symbol]["feat"]

        if len(feat) < MACD_SLOW + 5:
            raise ValueError(f"Not enough history for {symbol}.")

        # 2. Train
        models_a, r2_a = _train_a(feat)
        models_b, r2_b = _train_b(feat)

        # 3. ML prediction
        pred_a = _predict_a(models_a, open_price)
        pred_b = _predict_b(models_b, feat, open_price)
        ml     = _ml_ensemble(pred_a, r2_a, pred_b, r2_b)

        # 4. Blend with live intraday data if available
        sym_live = (live or {}).get(symbol)
        if sym_live and weight > 0.0:
            final = _blend_with_live(ml, sym_live, weight)
        else:
            # Pure ML — basic sanity check
            if ml["Low"] > ml["High"]:
                mid       = (ml["Low"] + ml["High"]) / 2
                ml["Low"] = mid - abs(mid * 0.001)
                ml["High"]= mid + abs(mid * 0.001)
            final = ml

        output[symbol] = {
            "open":  round(open_price,     2),
            "high":  round(final["High"],  2),
            "low":   round(final["Low"],   2),
            "close": round(final["Close"], 2),
        }

    return output


def get_forecast_with_diagnostics(
    nugt_open: float,
    jdst_open: float,
    live: dict = None,
    force_refresh: bool = False,
) -> dict:
    """
    Same as get_forecast() but also returns R² scores, live_weight,
    and the raw ML-only prediction before any intraday blending.
    """
    open_prices = {"NUGT": nugt_open, "JDST": jdst_open}
    weight      = _live_weight()
    output      = {}

    for symbol in SYMBOLS:
        open_price = open_prices[symbol]

        if force_refresh or symbol not in _cache:
            raw  = _download(symbol)
            feat = _build_features(raw)
            _cache[symbol] = {"raw": raw, "feat": feat}
        feat = _cache[symbol]["feat"]

        models_a, r2_a = _train_a(feat)
        models_b, r2_b = _train_b(feat)
        pred_a = _predict_a(models_a, open_price)
        pred_b = _predict_b(models_b, feat, open_price)
        ml     = _ml_ensemble(pred_a, r2_a, pred_b, r2_b)

        sym_live = (live or {}).get(symbol)
        if sym_live and weight > 0.0:
            final = _blend_with_live(ml, sym_live, weight)
        else:
            final = ml

        output[symbol] = {
            "open":        round(open_price,     2),
            "high":        round(final["High"],  2),
            "low":         round(final["Low"],   2),
            "close":       round(final["Close"], 2),
            # Diagnostics
            "ml_only":     {k: round(v, 2) for k, v in ml.items()},
            "live_weight": round(weight, 3),
            "r2_a":        {k: round(v, 3) for k, v in r2_a.items()},
            "r2_b":        {k: round(v, 3) for k, v in r2_b.items()},
        }

    return output


# ---------------------------------------------------------------------------
# CLI — python forecast.py <nugt_open> <jdst_open>
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    nugt_o = float(sys.argv[1]) if len(sys.argv) > 1 else 217.36
    jdst_o = float(sys.argv[2]) if len(sys.argv) > 2 else 29.78

    result = get_forecast_with_diagnostics(nugt_o, jdst_o)

    print("\n========== Forecast ==========")
    for sym, v in result.items():
        print(f"\n{sym}")
        print(f"  Open:        {v['open']}")
        print(f"  High:        {v['high']}")
        print(f"  Low:         {v['low']}")
        print(f"  Close:       {v['close']}")
        print(f"  Live weight: {v['live_weight']}  "
              f"(0.0 = model only, 1.0 = live only)")
        print(f"  ML-only  ->  High {v['ml_only']['High']}  "
              f"Low {v['ml_only']['Low']}  Close {v['ml_only']['Close']}")
        print(f"  R² model A - High {v['r2_a']['High']}  "
              f"Low {v['r2_a']['Low']}  Close {v['r2_a']['Close']}")
        print(f"  R² model B - High {v['r2_b']['High']}  "
              f"Low {v['r2_b']['Low']}  Close {v['r2_b']['Close']}")
