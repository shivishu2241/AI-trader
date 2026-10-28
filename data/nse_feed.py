"""
NSE option-chain feed (public website API, no account)
──────────────────────────────────────────────────────
Polls https://www.nseindia.com/api/option-chain-indices for the NIFTY option chain and turns it into:
  * live option premiums in the shared price cache (so the dashboard, scanner entry prices and the
    paper-position stop-loss/target monitor see real option LTPs),
  * `option_chain` snapshots in the database,
  * 1-minute option candles in `minute_candles` (built from the polled LTPs).

Reality check (unofficial endpoint — verify on your machine with Settings → "Test NSE feed"):
  * NSE can block or throttle scripted access (cookies are required; some networks/IPs get 401/403).
  * Samples arrive every ~30 s, so option "1-minute candles" are coarse (≈2 samples/bar).
  * Data may lag the exchange by a few seconds up to a minute.
  → Fine for charts and paper trading. NOT acceptable for real-money stop-loss monitoring, so live
    broker orders stay blocked with this source.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import pandas as pd
import requests

from utils.logger import get_logger

logger = get_logger("nse_feed")

BASE = "https://www.nseindia.com"
CHAIN_PATH = "/api/option-chain-indices"            # legacy endpoint
CHAIN_V3_PATH = "/api/option-chain-v3"                # current endpoint (needs an expiry)
CONTRACT_INFO_PATH = "/api/option-chain-contract-info"  # lists expiries
IST = timezone(timedelta(hours=5, minutes=30))
STRIKES_EACH_SIDE = 8          # ATM ± 8 strikes (50 pt gap) are kept
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",     # no 'br': requests can't decode Brotli without extras
    "Referer": BASE + "/option-chain",
}

# Last poll outcome, shown in the dashboard
FEED_STATE: dict = {"last_ok": None, "last_error": None, "expiry": None, "underlying": None, "rows": 0}


class NSEError(Exception):
    pass


TIMEOUT = (5, 8)               # (connect, read) seconds — never block the feed thread for long


class NSEClient:
    def __init__(self, base: str = BASE):
        self.base = base
        self.s = requests.Session()
        self.s.headers.update(_HEADERS)
        self._warmed_at = 0.0
        self._expiries: list[date] = []
        self._expiries_at = 0.0

    def _warm(self):
        """NSE only answers API calls that carry the cookies set by a normal page visit."""
        try:
            self.s.get(self.base + "/option-chain", timeout=TIMEOUT)
        except requests.RequestException as e:
            raise NSEError(f"Cannot reach nseindia.com: {type(e).__name__}") from e
        self._warmed_at = time.time()

    def _get_json(self, path: str, params: dict) -> dict:
        """GET with one cookie-refresh retry. Raises NSEError('HTTP <code>') when no JSON object comes back."""
        last = "no response"
        for attempt in (1, 2):
            if time.time() - self._warmed_at > 240 or attempt == 2:
                self._warm()
            try:
                r = self.s.get(self.base + path, params=params, timeout=TIMEOUT)
            except requests.RequestException as e:
                raise NSEError(f"Cannot reach nseindia.com: {type(e).__name__}") from e
            body = r.text.strip()
            if r.ok and body.startswith("{") and body != "{}":
                return r.json()
            last = f"HTTP {r.status_code}"
        raise NSEError(last)

    def _upcoming_expiries(self, symbol: str) -> list[date]:
        if self._expiries and time.time() - self._expiries_at < 3600:
            return self._expiries
        info = self._get_json(CONTRACT_INFO_PATH, {"symbol": symbol})
        exp = sorted({_parse_expiry(e) for e in info.get("expiryDates", [])})
        self._expiries = [e for e in exp if e >= datetime.now(IST).date()]
        self._expiries_at = time.time()
        return self._expiries

    def option_chain(self, symbol: str = "NIFTY") -> tuple[dict, Optional[date]]:
        """Return (payload, expiry_hint). Tries NSE's current v3 endpoint, then the legacy one."""
        tried = []
        try:
            exp = self._upcoming_expiries(symbol)
            if exp:
                payload = self._get_json(CHAIN_V3_PATH, {"type": "Indices", "symbol": symbol,
                                                        "expiry": f"{exp[0]:%d-%b-%Y}"})
                if (payload.get("records") or {}).get("data"):
                    return payload, exp[0]
                tried.append("v3: empty")
            else:
                tried.append("v3: no expiries")
        except NSEError as e:
            tried.append(f"v3: {e}")
        try:
            payload = self._get_json(CHAIN_PATH, {"symbol": symbol})
            if (payload.get("records") or {}).get("data"):
                return payload, None
            tried.append("legacy: empty")
        except NSEError as e:
            tried.append(f"legacy: {e}")
        raise NSEError("NSE gave no option chain (" + "; ".join(tried) + "). Blocked/rate-limited, or the endpoint changed.")


def _parse_expiry(s: str) -> date:
    return datetime.strptime(s, "%d-%b-%Y").date()


def parse_chain(payload: dict, today: Optional[date] = None, expiry_hint: Optional[date] = None) -> dict:
    """NSE JSON → {underlying, timestamp, expiry, rows: DataFrame} for the nearest expiry."""
    today = today or datetime.now(IST).date()
    rec = payload.get("records") or {}
    data = rec.get("data") or []
    if not data:
        raise NSEError("NSE option chain was empty")
    def _row_expiry(d: dict) -> Optional[date]:
        raw = d.get("expiryDate") or d.get("expiryDates")
        try:
            return _parse_expiry(raw) if raw else None
        except ValueError:
            return None

    listed = {_parse_expiry(e) for e in rec.get("expiryDates", [])} | {e for e in map(_row_expiry, data) if e}
    live = sorted(e for e in listed if e >= today)
    expiry = expiry_hint or (live[0] if live else None)
    if expiry is None:
        raise NSEError("No upcoming NIFTY expiry in NSE data")
    underlying = float(rec.get("underlyingValue") or 0)
    rows = []
    for d in data:
        row_exp = _row_expiry(d)
        if row_exp is not None and row_exp != expiry:
            continue
        strike = float(d["strikePrice"])
        for side in ("CE", "PE"):
            leg = d.get(side)
            if not leg:
                continue
            rows.append({
                "expiry": expiry, "strike": strike, "option_type": side,
                "ltp": float(leg.get("lastPrice") or 0),
                "bid_price": float(leg.get("bidprice") or 0), "ask_price": float(leg.get("askPrice") or 0),
                "oi": int(leg.get("openInterest") or 0), "oi_change": int(leg.get("changeinOpenInterest") or 0),
                "iv": float(leg.get("impliedVolatility") or 0), "volume": int(leg.get("totalTradedVolume") or 0),
            })
    df = pd.DataFrame(rows)
    if df.empty:
        raise NSEError(f"No rows for expiry {expiry}")
    if not underlying:
        underlying = float(data[0].get("CE", {}).get("underlyingValue") or 0)
    atm = round(underlying / 50) * 50 if underlying else float(df["strike"].median())
    df["relative_strike"] = ((df["strike"] - atm) / 50).round().astype(int)
    df = df[df["relative_strike"].abs() <= STRIKES_EACH_SIDE].copy()
    df["symbol"] = df.apply(lambda r: f"NIFTY{expiry:%y%m%d}{int(r['strike'])}{r['option_type']}", axis=1)
    return {"underlying": underlying, "timestamp": rec.get("timestamp"), "expiry": expiry, "rows": df}


class MinuteAggregator:
    """Builds 1-minute OHLC bars out of irregular price samples; emits a bar when its minute ends."""

    def __init__(self):
        self.cur: dict[str, dict] = {}

    def add(self, symbol: str, ts: datetime, price: float, oi: int = 0) -> list[dict]:
        minute = ts.replace(second=0, microsecond=0)
        done = []
        c = self.cur.get(symbol)
        if c and c["timestamp"] != minute:
            done.append(c)
            c = None
        if c is None:
            c = {"timestamp": minute, "symbol": symbol, "open": price, "high": price, "low": price,
                 "close": price, "volume": 0, "vwap": price, "oi": oi, "_n": 1, "_sum": price}
        else:
            c["high"], c["low"], c["close"], c["oi"] = max(c["high"], price), min(c["low"], price), price, oi
            c["_n"] += 1
            c["_sum"] += price
            c["vwap"] = c["_sum"] / c["_n"]
        self.cur[symbol] = c
        return done


_client: Optional[NSEClient] = None
_agg = MinuteAggregator()


def poll_once(write_db: bool = True) -> dict:
    """One poll: refresh the price cache, store snapshot + finished option candles."""
    global _client
    from data.market_data import write_live_prices, DB_SYMBOL
    _client = _client or NSEClient()
    try:
        payload, hint = _client.option_chain("NIFTY")
        chain = parse_chain(payload, expiry_hint=hint)
    except Exception as e:
        FEED_STATE.update(last_error=str(e)[:200])
        raise
    df, spot = chain["rows"], chain["underlying"]
    now_ist = datetime.now(IST).replace(tzinfo=None)

    prices = {r.symbol: r.ltp for r in df.itertuples() if r.ltp > 0}
    if spot:
        prices[DB_SYMBOL] = spot
    write_live_prices(prices)

    if write_db:
        from database.db import write_df, upsert_candles
        snap = df.copy()
        snap["timestamp"] = now_ist
        snap["underlying"] = "NIFTY"
        write_df(snap[["timestamp", "symbol", "underlying", "expiry", "strike", "option_type", "relative_strike",
                       "ltp", "volume", "oi", "oi_change", "iv", "bid_price", "ask_price"]], "option_chain")
        bars = []
        for r in df.itertuples():
            if r.ltp > 0:
                bars += _agg.add(r.symbol, now_ist, r.ltp, r.oi)
        if bars:
            out = pd.DataFrame(bars).drop(columns=["_n", "_sum"])
            upsert_candles(out[["timestamp", "symbol", "open", "high", "low", "close", "volume", "vwap", "oi"]])

    FEED_STATE.update(last_ok=datetime.now().isoformat(timespec="seconds"), last_error=None,
                      expiry=str(chain["expiry"]), underlying=spot, rows=int(len(df)))
    return {"underlying": spot, "expiry": str(chain["expiry"]), "rows": int(len(df)), "timestamp": chain["timestamp"]}


def test() -> dict:
    """Single fetch without writing anything — for the 'Test NSE feed' button."""
    try:
        return {"ok": True, **poll_once(write_db=False)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}
