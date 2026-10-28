"""
Free market-data source (no paid feed / credentials)
────────────────────────────────────────────────────
Pulls NIFTY 50 *index* 1-minute candles from Yahoo Finance's public chart endpoint and writes
them to `minute_candles` (symbol "NIFTY-I", same layout as the TrueData path), plus a live
price into the shared price cache that the dashboard already reads.

Limits you must know about:
  * Index (spot) prices only — NOT futures, NOT option premiums, NOT tick data. Option-premium
    based features, tick backtests and live option stop-loss monitoring therefore still need a
    real options feed (TrueData). Live broker orders are blocked while this source is active.
  * Yahoo serves 1-minute bars for the last ~30 days only (7 days per request).
  * Unofficial, rate-limited, possibly delayed endpoint — fine for charts/paper trading.
  * Never mix this data with TrueData futures data under the same symbol (spot vs futures differ
    by tens of points and poison the indicators). Pick one source per database.

Timestamps are stored as naive IST wall-clock time, exactly like the TrueData path does.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd
import requests

from utils import user_config
from utils.logger import get_logger

logger = get_logger("market_data")

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_SYMBOL = "^NSEI"
DB_SYMBOL = "NIFTY-I"                 # the symbol the rest of the app reads
LIVE_CACHE_FILE = "/tmp/td_live_prices.json"
IST = timezone(timedelta(hours=5, minutes=30))
_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AI-Trader/1.0", "Accept": "application/json"}

# has_option_prices: option premiums available (paper-trading features, entry prices, SL monitoring)
# live_ok: reliable enough to place REAL-money orders against (unofficial public feeds are not)
SOURCES = {
    "truedata": {"label": "TrueData (paid: futures, options, ticks)", "has_option_prices": True, "live_ok": True},
    "kite": {"label": "Zerodha Kite Connect (your account: futures, options, live ticks)", "has_option_prices": True, "live_ok": True},
    "public": {"label": "Free public: Yahoo index + NSE option chain", "has_option_prices": True, "live_ok": False},
    "yahoo": {"label": "Free: Yahoo index candles only", "has_option_prices": False, "live_ok": False},
}
FREE_SOURCES = ("public", "yahoo")


# ── Source selection ──────────────────────────────────────────────────

def get_source() -> str:
    """Explicit GUI choice wins; otherwise TrueData if credentials exist, else the free source."""
    chosen = user_config.get("market_data").get("source")
    if chosen in SOURCES:
        return chosen
    td = user_config.get("truedata")
    import os
    if (td.get("user") or os.getenv("TRUEDATA_USER")) and (td.get("password") or os.getenv("TRUEDATA_PASSWORD")):
        return "truedata"
    return "yahoo"


def has_option_prices() -> bool:
    return SOURCES[get_source()]["has_option_prices"]


def live_orders_ok() -> bool:
    return SOURCES[get_source()]["live_ok"]


# ── Yahoo fetch / parse ───────────────────────────────────────────────

class MarketDataError(Exception):
    pass


def parse_chart(payload: dict) -> pd.DataFrame:
    """Yahoo chart JSON → DataFrame[timestamp(IST naive), open, high, low, close, volume]."""
    try:
        res = payload["chart"]["result"][0]
        ts = res.get("timestamp") or []
        q = res["indicators"]["quote"][0]
    except (KeyError, IndexError, TypeError):
        err = (payload.get("chart") or {}).get("error")
        raise MarketDataError(f"Unexpected Yahoo response: {err or 'no data'}")
    if not ts:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame({
        "timestamp": pd.to_datetime(ts, unit="s", utc=True).tz_convert(IST).tz_localize(None),
        "open": q.get("open"), "high": q.get("high"), "low": q.get("low"),
        "close": q.get("close"), "volume": q.get("volume"),
    })
    df = df.dropna(subset=["open", "high", "low", "close"])
    df["volume"] = df["volume"].fillna(0).astype("int64")
    return df.reset_index(drop=True)


def _get(url: str, params: dict) -> dict:
    try:
        r = requests.get(url, params=params, headers=_HEADERS, timeout=20)
    except requests.RequestException as e:
        raise MarketDataError(f"Cannot reach Yahoo Finance: {e}") from e
    if r.status_code == 429:
        raise MarketDataError("Yahoo Finance rate limit hit — try again in a minute.")
    if not r.ok:
        raise MarketDataError(f"Yahoo Finance HTTP {r.status_code}")
    return r.json()


def fetch_candles(start: datetime, end: datetime) -> pd.DataFrame:
    """1-minute bars between two UTC datetimes (≤ 7 days per request, Yahoo's limit)."""
    payload = _get(YAHOO_CHART_URL.format(symbol=YAHOO_SYMBOL), {
        "interval": "1m", "includePrePost": "false",
        "period1": int(start.replace(tzinfo=timezone.utc).timestamp()),
        "period2": int(end.replace(tzinfo=timezone.utc).timestamp()),
    })
    return parse_chart(payload)


def _with_db_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["symbol"] = DB_SYMBOL
    typical = (df["high"] + df["low"] + df["close"]) / 3
    day = df["timestamp"].dt.date
    vol = df["volume"].astype(float)
    cum_v = vol.groupby(day).cumsum()
    cum_pv = (typical * vol).groupby(day).cumsum()
    # Index volume is usually 0 → fall back to a running mean of the typical price.
    running_mean = typical.groupby(day).expanding().mean().reset_index(level=0, drop=True)
    df["vwap"] = (cum_pv / cum_v).where(cum_v > 0, running_mean)
    df["oi"] = 0
    return df[["timestamp", "symbol", "open", "high", "low", "close", "volume", "vwap", "oi"]]


def sync_candles(days: float = 7) -> dict:
    """Download the last `days` (0.02–30; fractions allowed) of NIFTY 1-min candles into minute_candles."""
    from database.db import upsert_candles
    days = max(0.02, min(float(days), 30))
    end = datetime.utcnow() + timedelta(minutes=1)
    start = end - timedelta(days=days)
    frames, cursor = [], start
    while cursor < end:
        chunk_end = min(cursor + timedelta(days=7), end)
        frames.append(fetch_candles(cursor, chunk_end))
        cursor = chunk_end
        if cursor < end:
            time.sleep(0.5)          # be polite to the public endpoint
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if df.empty:
        return {"fetched": 0, "first": None, "last": None, "days": days}
    df = df.drop_duplicates(subset="timestamp").sort_values("timestamp")
    upsert_candles(_with_db_columns(df))
    user_config.update("market_data", {"last_sync": datetime.now().isoformat(timespec="seconds"),
                                       "last_sync_rows": int(len(df))})
    logger.info(f"Synced {len(df)} NIFTY 1-min candles from Yahoo ({days:g}d)")
    return {"fetched": int(len(df)), "first": str(df["timestamp"].iloc[0]),
            "last": str(df["timestamp"].iloc[-1]), "days": days}


def sync_recent(minutes: int = 45) -> dict:
    """Cheap incremental refresh used by the live feed (a few bars instead of a whole day)."""
    return sync_candles(days=minutes / 1440)


def latest_price() -> Optional[float]:
    """Most recent NIFTY index price from Yahoo (may be delayed)."""
    payload = _get(YAHOO_CHART_URL.format(symbol=YAHOO_SYMBOL), {"interval": "1m", "range": "1d"})
    try:
        meta = payload["chart"]["result"][0]["meta"]
        return float(meta["regularMarketPrice"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def write_live_price(price: float) -> None:
    write_live_prices({DB_SYMBOL: price})


def write_live_prices(prices: dict) -> None:
    """Same file/format collect_ticks.py writes, so the dashboard & tick monitor keep working."""
    cache = {}
    try:
        with open(LIVE_CACHE_FILE) as f:
            cache = json.load(f)
    except Exception:
        pass
    now = datetime.now().isoformat()
    for sym, price in prices.items():
        cache[sym] = {"price": float(price), "ts": now}
    tmp = LIVE_CACHE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f)
    import os
    os.replace(tmp, LIVE_CACHE_FILE)
