"""
INDmoney (INDstocks) Adapter
────────────────────────────
Live execution through the INDstocks trading API, configured from the
dashboard (Settings → Broker) instead of `.env`.

Auth: a static access token generated at indstocks.com (FnO → Algos →
Access Tokens; requires a whitelisted static IP). Stored in the GUI config
store (utils.user_config, section "indmoney"), never sent back to the browser.

!! UNVERIFIED API SHAPE !!
The endpoint paths, payload field names and the auth scheme below were
written without access to the official docs (api-docs.indstocks.com was not
reachable when this was built). Everything is therefore configurable from the
GUI ("Advanced: endpoints") and `authenticate()` probes read-only endpoints
only. Verify against the official docs and place one 1-lot test order before
trusting it with real money.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import requests

from broker.base_adapter import (
    BrokerAdapter, OrderRequest, OrderResponse, OrderSide, OrderStatus,
    OrderType, Position, ProductType,
)
from utils import user_config
from utils.logger import get_logger

logger = get_logger("indmoney_adapter")

DEFAULT_BASE_URL = "https://api.indstocks.com"
DEFAULT_ENDPOINTS = {
    "funds": "/funds",
    "positions": "/portfolio/positions",
    "orders": "/order-book",
    "place_order": "/order",
    "modify_order": "/order/modify",
    "cancel_order": "/order/cancel",
    "instruments": "",   # symbol → security_id lookup; set from the docs
}
_ORDER_TYPE = {OrderType.MARKET: "MARKET", OrderType.LIMIT: "LIMIT",
               OrderType.SL: "SL", OrderType.SL_MARKET: "SL-M"}
_PRODUCT = {ProductType.MIS: "INTRADAY", ProductType.NRML: "MARGIN", ProductType.CNC: "CNC"}
_STATUS = {"COMPLETE": OrderStatus.COMPLETE, "EXECUTED": OrderStatus.COMPLETE,
           "REJECTED": OrderStatus.REJECTED, "CANCELLED": OrderStatus.CANCELLED,
           "OPEN": OrderStatus.OPEN, "PENDING": OrderStatus.PENDING}


class IndMoneyAdapter(BrokerAdapter):
    def __init__(self):
        self._connected = False
        self._sec_ids: dict[str, str] = {}
        self._reload()

    def _reload(self):
        cfg = user_config.get("indmoney")
        self._token = cfg.get("access_token", "")
        self._base = (cfg.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        self._ep = {**DEFAULT_ENDPOINTS, **cfg.get("endpoints", {})}
        self._scheme = cfg.get("auth_scheme", "auto")   # auto | raw | bearer
        self._exchange_segment = cfg.get("segment", "DERIVATIVE")

    # ── HTTP ─────────────────────────────────────────────────────────

    def _auth(self, scheme: Optional[str] = None) -> dict:
        s = scheme or self._scheme
        value = f"Bearer {self._token}" if s == "bearer" else self._token
        return {"Authorization": value, "Content-Type": "application/json"}

    def _call(self, method: str, key: str, *, params=None, body=None, scheme=None) -> requests.Response:
        path = self._ep.get(key, "")
        if not path:
            raise RuntimeError(f"Endpoint '{key}' is not configured (Settings → Broker → Advanced).")
        return requests.request(method, f"{self._base}{path}", params=params, json=body,
                                headers=self._auth(scheme), timeout=15)

    # ── Authentication ───────────────────────────────────────────────

    def authenticate(self) -> bool:
        self._reload()
        self._connected = False
        if not self._token:
            logger.error("INDmoney access token not set (Settings → Broker)")
            return False
        schemes = ["raw", "bearer"] if self._scheme == "auto" else [self._scheme]
        for s in schemes:
            try:
                r = self._call("GET", "funds", scheme=s)
            except Exception as e:
                logger.error(f"INDmoney auth probe failed: {e}")
                return False
            if r.status_code == 200:
                self._scheme = s
                user_config.update("indmoney", {"auth_scheme": s})
                self._connected = True
                logger.info(f"INDmoney connected (auth scheme: {s})")
                return True
            logger.warning(f"INDmoney auth probe ({s}) → HTTP {r.status_code}: {r.text[:200]}")
        return False

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def broker_name(self) -> str:
        return "INDmoney"

    # ── Helpers ──────────────────────────────────────────────────────

    def _security_id(self, symbol: str) -> str:
        if symbol in self._sec_ids:
            return self._sec_ids[symbol]
        r = self._call("GET", "instruments", params={"symbol": symbol, "search": symbol})
        if not r.ok:
            raise RuntimeError(f"instrument lookup HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        data = data.get("data", data) if isinstance(data, dict) else data
        rows = data if isinstance(data, list) else [data]
        for row in rows:
            if isinstance(row, dict) and row.get("security_id") and \
               symbol in (row.get("trading_symbol"), row.get("symbol"), row.get("tradingsymbol")):
                self._sec_ids[symbol] = str(row["security_id"])
                return self._sec_ids[symbol]
        raise RuntimeError(f"No security_id found for {symbol}")

    @staticmethod
    def _unwrap(r: requests.Response):
        j = r.json()
        return j.get("data", j) if isinstance(j, dict) else j

    # ── Orders ───────────────────────────────────────────────────────

    def place_order(self, request: OrderRequest) -> OrderResponse:
        if not self.is_connected:
            return OrderResponse(status=OrderStatus.ERROR, message="Not connected to INDmoney")
        try:
            body = {
                "txn_type": request.side.value,
                "exchange": "NSE" if request.exchange in ("NFO", "NSE") else request.exchange,
                "segment": self._exchange_segment if request.exchange == "NFO" else "EQUITY",
                "product": _PRODUCT.get(request.product, "INTRADAY"),
                "order_type": _ORDER_TYPE.get(request.order_type, "MARKET"),
                "validity": "DAY",
                "security_id": self._security_id(request.symbol),
                "qty": request.quantity,
                "is_amo": False,
            }
            if request.order_type in (OrderType.LIMIT, OrderType.SL):
                body["limit_price"] = request.price
            if request.order_type in (OrderType.SL, OrderType.SL_MARKET):
                body["trigger_price"] = request.trigger_price
            r = self._call("POST", "place_order", body=body)
            if not r.ok:
                return OrderResponse(status=OrderStatus.REJECTED,
                                     message=f"HTTP {r.status_code}: {r.text[:300]}", raw=body)
            d = self._unwrap(r)
            oid = str(d.get("order_id") or d.get("id") or "") if isinstance(d, dict) else ""
            if not oid:
                return OrderResponse(status=OrderStatus.ERROR, message=f"No order id in response: {r.text[:300]}", raw=body)
            logger.info(f"INDmoney ORDER: {request.side.value} {request.symbol} ×{request.quantity} → {oid}")
            return OrderResponse(order_id=oid, status=OrderStatus.OPEN, message="Order placed",
                                 timestamp=datetime.now(), raw=body)
        except Exception as e:
            logger.error(f"INDmoney order FAILED: {request.symbol} — {e}")
            return OrderResponse(status=OrderStatus.ERROR, message=str(e))

    def modify_order(self, order_id, quantity=None, price=None, trigger_price=None, order_type=None):
        if not self.is_connected:
            return OrderResponse(status=OrderStatus.ERROR, message="Not connected")
        body = {"order_id": order_id}
        if quantity is not None: body["qty"] = quantity
        if price is not None: body["limit_price"] = price
        if trigger_price is not None: body["trigger_price"] = trigger_price
        if order_type is not None: body["order_type"] = _ORDER_TYPE.get(order_type, "MARKET")
        try:
            r = self._call("POST", "modify_order", body=body)
            ok = r.ok
            return OrderResponse(order_id=order_id, status=OrderStatus.OPEN if ok else OrderStatus.ERROR,
                                 message="Modified" if ok else f"HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            return OrderResponse(order_id=order_id, status=OrderStatus.ERROR, message=str(e))

    def cancel_order(self, order_id: str) -> OrderResponse:
        if not self.is_connected:
            return OrderResponse(status=OrderStatus.ERROR, message="Not connected")
        try:
            r = self._call("POST", "cancel_order", body={"order_id": order_id})
            ok = r.ok
            return OrderResponse(order_id=order_id, status=OrderStatus.CANCELLED if ok else OrderStatus.ERROR,
                                 message="Cancelled" if ok else f"HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            return OrderResponse(order_id=order_id, status=OrderStatus.ERROR, message=str(e))

    # ── Queries ──────────────────────────────────────────────────────

    def _to_response(self, o: dict) -> OrderResponse:
        st = str(o.get("status", "")).upper()
        return OrderResponse(
            order_id=str(o.get("order_id") or o.get("id") or ""),
            status=_STATUS.get(st, OrderStatus.PENDING),
            filled_quantity=int(o.get("filled_qty") or o.get("filled_quantity") or 0),
            average_price=float(o.get("avg_price") or o.get("average_price") or 0),
            message=str(o.get("status_message") or o.get("remarks") or st),
            raw=o,
        )

    def get_orders_today(self) -> list[OrderResponse]:
        if not self.is_connected:
            return []
        try:
            r = self._call("GET", "orders")
            d = self._unwrap(r) if r.ok else []
            return [self._to_response(o) for o in d if isinstance(o, dict)]
        except Exception as e:
            logger.error(f"INDmoney get_orders failed: {e}")
            return []

    def get_order_status(self, order_id: str) -> OrderResponse:
        for o in self.get_orders_today():
            if o.order_id == order_id:
                return o
        return OrderResponse(order_id=order_id, status=OrderStatus.ERROR, message="Order not found")

    def get_positions(self) -> list[Position]:
        if not self.is_connected:
            return []
        try:
            r = self._call("GET", "positions")
            d = self._unwrap(r) if r.ok else []
            if isinstance(d, dict):
                d = d.get("net") or d.get("positions") or []
            out = []
            for p in d:
                qty = int(p.get("net_qty") or p.get("quantity") or p.get("qty") or 0)
                if qty:
                    sym = str(p.get("trading_symbol") or p.get("symbol") or "")
                    if p.get("security_id"):
                        self._sec_ids[sym] = str(p["security_id"])  # lets the kill switch exit it
                    out.append(Position(
                        symbol=sym,
                        exchange="NFO", quantity=qty,
                        average_price=float(p.get("avg_price") or p.get("average_price") or 0),
                        last_price=float(p.get("ltp") or p.get("last_price") or 0),
                        pnl=float(p.get("pnl") or p.get("unrealised_pnl") or 0),
                    ))
            return out
        except Exception as e:
            logger.error(f"INDmoney get_positions failed: {e}")
            return []

    def get_margins(self) -> dict:
        if not self.is_connected:
            return {}
        try:
            r = self._call("GET", "funds")
            return self._unwrap(r) if r.ok else {}
        except Exception:
            return {}

    # ── Safety ───────────────────────────────────────────────────────

    def kill_switch(self) -> list[OrderResponse]:
        if not self.is_connected:
            logger.error("KILL SWITCH: not connected to INDmoney!")
            return []
        out: list[OrderResponse] = []
        for o in self.get_orders_today():
            if o.status in (OrderStatus.OPEN, OrderStatus.PENDING):
                self.cancel_order(o.order_id)
        for pos in self.get_positions():
            side_fn = self.sell if pos.quantity > 0 else self.buy
            resp = side_fn(pos.symbol, abs(pos.quantity), tag="KILL_SWITCH")
            if resp.status in (OrderStatus.ERROR, OrderStatus.REJECTED):
                resp = side_fn(pos.symbol, abs(pos.quantity), tag="KILL_RETRY")
            out.append(resp)
        logger.warning(f"KILL SWITCH COMPLETE: {len(out)} exit orders placed")
        return out
