"""
Zerodha Kite Connect as the market-data source
──────────────────────────────────────────────
Uses the SAME Zerodha login as order execution (Settings → Broker → "Login with Zerodha"; the access
token is saved in the GUI config and expires every morning ~06:00 IST).

  * History : 1-minute candles of the current NIFTY *futures* contract → minute_candles as "NIFTY-I"
              (futures, exactly what the rest of the app expects — no spot/futures mismatch).
  * Live    : KiteTicker WebSocket (FULL mode) for NIFTY futures + ATM ± 8 strikes of the nearest
              weekly expiry → live price cache, tick_data (bid/ask/OI), 1-minute candles for every
              subscribed contract. Ticks accumulate day by day, which is what the tick-replay
              backtest needs.

Requires a Kite Connect subscription that includes market data (the free "personal" plan does not —
Kite answers "Insufficient permission"). Use Settings → Market data → "Test Kite data" to check.
Written against the documented kiteconnect API; test it on your account before relying on it.
"""

from __future__ import annotations

import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

import pandas as pd

from data.nse_feed import MinuteAggregator
from utils import user_config
from utils.logger import get_logger

logger = get_logger("kite_data")

IST = timezone(timedelta(hours=5, minutes=30))
DB_SYMBOL = "NIFTY-I"
STRIKES_EACH_SIDE = 8
HISTORY_CHUNK_DAYS = 55            # Kite allows ≤ 60 days per 1-minute request
STATE: dict = {"connected": False, "last_tick": None, "subscribed": 0, "error": None, "ticks": 0}


class KiteDataError(Exception):
    pass


def option_symbol(expiry: date, strike: float, kind: str) -> str:
    return f"NIFTY{expiry:%y%m%d}{int(strike)}{kind}"


class KiteData:
    def __init__(self, kite_factory: Optional[Callable] = None, ticker_factory: Optional[Callable] = None):
        self._kite_factory, self._ticker_factory = kite_factory, ticker_factory
        self._kite = None
        self._insts: list[dict] = []
        self._insts_at = 0.0
        self._ticker = None
        self._token_used = ""
        self._tokens: dict[int, str] = {}          # instrument_token → our symbol
        self._agg = MinuteAggregator()
        self._tick_rows: list[dict] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # ── session ──────────────────────────────────────────────────────

    def _creds(self) -> tuple[str, str]:
        z = user_config.get("zerodha")
        if not z.get("api_key"):
            raise KiteDataError("Zerodha API key not set (Settings → Broker → Zerodha).")
        if not z.get("access_token"):
            raise KiteDataError("Not logged in to Zerodha today. Settings → Broker → LOGIN WITH ZERODHA.")
        return z["api_key"], z["access_token"]

    def kite(self):
        if self._kite is None:
            key, token = self._creds()
            if self._kite_factory:
                self._kite = self._kite_factory(key, token)
            else:
                try:
                    from kiteconnect import KiteConnect
                except ImportError as e:
                    raise KiteDataError("kiteconnect is not installed (pip install kiteconnect).") from e
                self._kite = KiteConnect(api_key=key)
                self._kite.set_access_token(token)
        return self._kite

    def _call(self, fn, *a, **kw):
        """Kite call with friendly errors; drops the cached session on auth failures."""
        try:
            return fn(*a, **kw)
        except Exception as e:
            msg = str(e)
            if "token" in msg.lower() or "login" in msg.lower() or type(e).__name__ == "TokenException":
                self._kite = None
                raise KiteDataError("Zerodha session expired — LOGIN WITH ZERODHA again (tokens expire daily).") from e
            if "permission" in msg.lower():
                raise KiteDataError("Your Kite Connect plan has no market-data permission (needs the paid data plan).") from e
            raise KiteDataError(msg[:200]) from e

    # ── instruments ──────────────────────────────────────────────────

    def instruments(self) -> list[dict]:
        if self._insts and time.time() - self._insts_at < 6 * 3600:
            return self._insts
        rows = self._call(self.kite().instruments, "NFO")
        self._insts = [r for r in rows if r.get("name") == "NIFTY"]
        self._insts_at = time.time()
        return self._insts

    def nearest_future(self) -> dict:
        today = datetime.now(IST).date()
        futs = [i for i in self.instruments() if i.get("instrument_type") == "FUT" and _d(i["expiry"]) >= today]
        if not futs:
            raise KiteDataError("No NIFTY futures contract found in Kite instruments.")
        return min(futs, key=lambda i: _d(i["expiry"]))

    def option_contracts(self, centre: float) -> list[dict]:
        today = datetime.now(IST).date()
        opts = [i for i in self.instruments() if i.get("instrument_type") in ("CE", "PE") and _d(i["expiry"]) >= today]
        if not opts:
            return []
        expiry = min(_d(i["expiry"]) for i in opts)
        atm = round(centre / 50) * 50
        return [i for i in opts if _d(i["expiry"]) == expiry and abs(i["strike"] - atm) <= 50 * STRIKES_EACH_SIDE]

    # ── history ──────────────────────────────────────────────────────

    def sync_history(self, days: int = 30) -> dict:
        from database.db import upsert_candles
        days = max(1, min(int(days), 60))
        fut = self.nearest_future()
        end = datetime.now(IST).replace(tzinfo=None)
        start, frames = end - timedelta(days=days), []
        cursor = start
        while cursor < end:
            chunk_end = min(cursor + timedelta(days=HISTORY_CHUNK_DAYS), end)
            rows = self._call(self.kite().historical_data, fut["instrument_token"], cursor, chunk_end, "minute",
                              False, True)
            frames.append(pd.DataFrame(rows))
            cursor = chunk_end
            time.sleep(0.4)                               # Kite: 3 historical requests / second
        df = pd.concat([f for f in frames if not f.empty], ignore_index=True) if any(not f.empty for f in frames) else pd.DataFrame()
        if df.empty:
            return {"fetched": 0, "contract": fut["tradingsymbol"], "days": days}
        ts = pd.to_datetime(df["date"])
        if ts.dt.tz is not None:
            ts = ts.dt.tz_convert(IST).dt.tz_localize(None)    # naive IST, like every other path
        out = pd.DataFrame({
            "timestamp": ts, "symbol": DB_SYMBOL, "open": df["open"], "high": df["high"], "low": df["low"],
            "close": df["close"], "volume": df["volume"].fillna(0).astype("int64"),
            "oi": df["oi"].fillna(0).astype("int64") if "oi" in df else 0,
        })
        typical = (out["high"] + out["low"] + out["close"]) / 3
        day = out["timestamp"].dt.date
        cv = out["volume"].astype(float).groupby(day).cumsum()
        cpv = (typical * out["volume"]).groupby(day).cumsum()
        out["vwap"] = (cpv / cv).where(cv > 0, typical)
        out = out.drop_duplicates("timestamp").sort_values("timestamp")
        upsert_candles(out[["timestamp", "symbol", "open", "high", "low", "close", "volume", "vwap", "oi"]])
        user_config.update("market_data", {"last_sync": datetime.now().isoformat(timespec="seconds"),
                                           "last_sync_rows": int(len(out))})
        return {"fetched": int(len(out)), "first": str(out["timestamp"].iloc[0]), "last": str(out["timestamp"].iloc[-1]),
                "contract": fut["tradingsymbol"], "days": days}

    # ── connectivity test ────────────────────────────────────────────

    def test(self) -> dict:
        try:
            profile = self._call(self.kite().profile)
            fut = self.nearest_future()
            q = self._call(self.kite().ltp, [f"NFO:{fut['tradingsymbol']}"])
            price = list(q.values())[0]["last_price"]
            return {"ok": True, "user": profile.get("user_id"), "contract": fut["tradingsymbol"],
                    "expiry": str(_d(fut["expiry"])), "price": price}
        except KiteDataError as e:
            return {"ok": False, "error": str(e)}

    # ── live feed ────────────────────────────────────────────────────

    def feed_running(self) -> bool:
        return bool(self._ticker) and STATE["connected"]

    def up_to_date(self) -> bool:
        """True if a feed exists for the CURRENT login token (KiteTicker reconnects by itself; only a new
        daily login needs a restart)."""
        try:
            return self._ticker is not None and self._token_used == self._creds()[1]
        except KiteDataError:
            return self._ticker is not None

    def feed_fresh(self, max_age: float = 20.0) -> bool:
        t = STATE["last_tick"]
        return bool(t) and (time.time() - t) < max_age

    def start_feed(self):
        if self._ticker is not None:
            return
        key, token = self._creds()
        self._token_used = token
        fut = self.nearest_future()
        price = self._call(self.kite().ltp, [f"NFO:{fut['tradingsymbol']}"])
        centre = list(price.values())[0]["last_price"]
        self._tokens = {fut["instrument_token"]: DB_SYMBOL}
        for i in self.option_contracts(centre):
            self._tokens[i["instrument_token"]] = option_symbol(_d(i["expiry"]), i["strike"], i["instrument_type"])
        if self._ticker_factory:
            tk = self._ticker_factory(key, token)
        else:
            try:
                from kiteconnect import KiteTicker
            except ImportError as e:
                raise KiteDataError("kiteconnect is not installed (pip install kiteconnect).") from e
            tk = KiteTicker(key, token)
        tk.on_connect = self._on_connect
        tk.on_ticks = self._on_ticks
        tk.on_close = lambda ws, code, reason: self._on_close(code, reason)
        tk.on_error = lambda ws, code, reason: self._on_close(code, reason)
        self._ticker, self._stop = tk, threading.Event()
        STATE.update(error=None, subscribed=len(self._tokens))
        tk.connect(threaded=True)
        flusher = threading.Thread(target=self._flush_loop, daemon=True, name="kite-flush")
        flusher.start()
        self._threads = [flusher]
        logger.info(f"Kite feed starting: {len(self._tokens)} contracts (futures {fut['tradingsymbol']})")

    def stop_feed(self):
        self._stop.set()
        try:
            if self._ticker:
                self._ticker.close()
        except Exception:
            pass
        self._ticker = None
        self._kite = None                 # force a fresh session (new daily token) on next use
        STATE["connected"] = False

    def _on_connect(self, ws, response=None):
        toks = list(self._tokens)
        ws.subscribe(toks)
        ws.set_mode(ws.MODE_FULL, toks)
        STATE.update(connected=True, error=None)
        logger.info(f"Kite ticker connected, subscribed {len(toks)} instruments")

    def _on_close(self, code, reason):
        STATE["connected"] = False
        text = f"{code} {reason}"
        if "token" in text.lower() or "403" in text:
            STATE["error"] = "Zerodha session expired — LOGIN WITH ZERODHA again (tokens expire daily)."
            self._kite = None
        else:
            STATE["error"] = f"Kite feed closed: {text[:120]}"
        logger.warning(STATE["error"])

    def _on_ticks(self, ws, ticks):
        from data.market_data import write_live_prices
        now_ist = datetime.now(IST).replace(tzinfo=None)
        prices, bars = {}, []
        for t in ticks:
            sym = self._tokens.get(t.get("instrument_token"))
            price = t.get("last_price")
            if not sym or not price:
                continue
            ts = t.get("exchange_timestamp") or t.get("last_trade_time") or now_ist
            if getattr(ts, "tzinfo", None):
                ts = ts.astimezone(IST).replace(tzinfo=None)
            depth = t.get("depth") or {}
            bid = (depth.get("buy") or [{}])[0]
            ask = (depth.get("sell") or [{}])[0]
            prices[sym] = price
            row = {"timestamp": ts, "symbol": sym, "price": float(price),
                   "volume": int(t.get("last_traded_quantity") or 0),
                   "bid_price": bid.get("price"), "ask_price": ask.get("price"),
                   "bid_qty": bid.get("quantity"), "ask_qty": ask.get("quantity"),
                   "oi": t.get("oi"), "atp": t.get("average_traded_price"),
                   "total_volume": t.get("volume_traded")}
            with self._lock:
                self._tick_rows.append(row)
                bars += self._agg.add(sym, ts, float(price), int(t.get("oi") or 0))
        if prices:
            write_live_prices(prices)
            STATE.update(last_tick=time.time(), ticks=STATE["ticks"] + len(ticks), error=None)
        if bars:
            self._write_bars(bars)

    def _write_bars(self, bars: list[dict]):
        try:
            from database.db import upsert_candles
            df = pd.DataFrame(bars).drop(columns=["_n", "_sum"], errors="ignore")
            upsert_candles(df[["timestamp", "symbol", "open", "high", "low", "close", "volume", "vwap", "oi"]])
        except Exception as e:
            logger.warning(f"Kite candle write failed: {e}")

    def flush_ticks(self):
        with self._lock:
            rows, self._tick_rows = self._tick_rows, []
        if not rows:
            return
        try:
            from database.db import write_df
            write_df(pd.DataFrame(rows), "tick_data")
        except Exception as e:
            logger.warning(f"Kite tick write failed ({len(rows)} ticks dropped): {e}")

    def _flush_loop(self):
        while not self._stop.wait(3):
            self.flush_ticks()


def _d(x) -> date:
    return x.date() if isinstance(x, datetime) else x


_instance: Optional[KiteData] = None


def instance() -> KiteData:
    global _instance
    if _instance is None:
        _instance = KiteData()
    return _instance
