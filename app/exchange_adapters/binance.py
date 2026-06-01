"""
Sprint 20F — Binance USDT-M Futures adapter (LIVE).

Real signed REST calls to Binance Futures. This adapter is only ever
instantiated by resolve_adapter() when the live-trading gate is open, and it
ALSO guards every network method itself (defense in depth): if
LIVE_TRADING_ENABLED is false or MOCK_EXCHANGE_MODE is true, it raises instead
of touching the network.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any, Optional
from urllib.parse import urlencode

from app.config import settings
from app.exchange_adapters.base import (
    MODE_LIVE,
    AdapterError,
    BalanceInfo,
    ExchangeAdapter,
    OrderResult,
    PositionInfo,
    opposite_side,
)
from app.utils.logger import logger

_PROD_URL = "https://fapi.binance.com"
_TESTNET_URL = "https://testnet.binancefuture.com"


def sign_query(secret: str, params: dict[str, Any]) -> str:
    """HMAC-SHA256 signature of a urlencoded query string (Binance spec)."""
    query = urlencode(params)
    return hmac.new(secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()


class BinanceFuturesAdapter(ExchangeAdapter):
    name = "binance"
    mode = MODE_LIVE

    def __init__(self, api_key: str, api_secret: str, *, testnet: Optional[bool] = None):
        self._key = api_key
        self._secret = api_secret
        self._testnet = settings.binance_testnet if testnet is None else testnet
        self._base = _TESTNET_URL if self._testnet else _PROD_URL
        self._session = None  # lazy aiohttp session

    # ── gate ──────────────────────────────────────────────────────

    @staticmethod
    def _guard() -> None:
        if not settings.live_trading_enabled or settings.mock_exchange_mode:
            raise AdapterError(
                "Live-trading gate is closed (LIVE_TRADING_ENABLED must be true "
                "and MOCK_EXCHANGE_MODE false). Refusing to place a real order."
            )

    async def _client(self):
        import aiohttp
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=15, connect=5)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    # ── signed request ────────────────────────────────────────────

    async def _request(self, method: str, path: str, params: Optional[dict] = None, *, signed: bool = True) -> Any:
        self._guard()
        params = dict(params or {})
        if signed:
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = 5000
            params["signature"] = sign_query(self._secret, params)
        headers = {"X-MBX-APIKEY": self._key}
        url = f"{self._base}{path}"
        session = await self._client()
        async with session.request(method, url, params=params, headers=headers) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 400:
                msg = data.get("msg") if isinstance(data, dict) else str(data)
                raise AdapterError(f"Binance {resp.status}: {msg}")
            return data

    # ── interface ─────────────────────────────────────────────────

    async def connect(self) -> bool:
        await self._request("GET", "/fapi/v2/balance")
        return True

    async def get_balance(self, asset: str = "USDT") -> BalanceInfo:
        rows = await self._request("GET", "/fapi/v2/balance")
        for r in rows:
            if r.get("asset") == asset:
                return BalanceInfo(
                    asset=asset, balance=float(r["balance"]),
                    available=float(r.get("availableBalance", r["balance"])), mode=MODE_LIVE)
        return BalanceInfo(asset=asset, balance=0.0, available=0.0, mode=MODE_LIVE)

    async def get_positions(self) -> list[PositionInfo]:
        rows = await self._request("GET", "/fapi/v2/positionRisk")
        out: list[PositionInfo] = []
        for r in rows:
            amt = float(r.get("positionAmt", 0))
            if amt == 0:
                continue
            out.append(PositionInfo(
                symbol=r["symbol"], side="LONG" if amt > 0 else "SHORT", qty=abs(amt),
                entry_price=float(r.get("entryPrice", 0)), leverage=int(float(r.get("leverage", 1))),
                margin_type=r.get("marginType", "cross"),
                unrealized_pnl=float(r.get("unRealizedProfit", 0)),
                liquidation_price=float(r.get("liquidationPrice", 0)), mode=MODE_LIVE))
        return out

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        await self._request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": int(leverage)})

    async def set_margin_type(self, symbol: str, margin_type: str) -> None:
        mt = "ISOLATED" if margin_type.lower() == "isolated" else "CROSSED"
        try:
            await self._request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": mt})
        except AdapterError as exc:
            # -4046 "No need to change margin type" is benign.
            if "4046" not in str(exc):
                raise

    async def open_order(
        self, *, symbol: str, side: str, qty: float, order_type: str = "MARKET",
        price: Optional[float] = None, reduce_only: bool = False,
    ) -> OrderResult:
        params: dict[str, Any] = {
            "symbol": symbol, "side": side, "type": order_type, "quantity": qty,
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        if order_type == "LIMIT":
            params["price"] = price
            params["timeInForce"] = "GTC"
        data = await self._request("POST", "/fapi/v1/order", params)
        return self._to_order(data, symbol, side, order_type, reduce_only)

    async def close_order(self, *, symbol: str, side: str, qty: float) -> OrderResult:
        return await self.open_order(
            symbol=symbol, side=opposite_side(side), qty=qty,
            order_type="MARKET", reduce_only=True)

    async def set_tp_sl(
        self, *, symbol: str, side: str, qty: float,
        take_profit: Optional[float] = None, stop_loss: Optional[float] = None,
        trailing_pct: Optional[float] = None,
    ) -> list[OrderResult]:
        close_side = opposite_side(side)
        out: list[OrderResult] = []
        if take_profit:
            d = await self._request("POST", "/fapi/v1/order", {
                "symbol": symbol, "side": close_side, "type": "TAKE_PROFIT_MARKET",
                "stopPrice": take_profit, "closePosition": "true"})
            out.append(self._to_order(d, symbol, close_side, "TAKE_PROFIT_MARKET", True))
        if stop_loss:
            d = await self._request("POST", "/fapi/v1/order", {
                "symbol": symbol, "side": close_side, "type": "STOP_MARKET",
                "stopPrice": stop_loss, "closePosition": "true"})
            out.append(self._to_order(d, symbol, close_side, "STOP_MARKET", True))
        if trailing_pct:
            d = await self._request("POST", "/fapi/v1/order", {
                "symbol": symbol, "side": close_side, "type": "TRAILING_STOP_MARKET",
                "callbackRate": round(trailing_pct, 1), "quantity": qty, "reduceOnly": "true"})
            out.append(self._to_order(d, symbol, close_side, "TRAILING_STOP_MARKET", True))
        return out

    async def get_open_orders(self, symbol: Optional[str] = None) -> list[OrderResult]:
        params = {"symbol": symbol} if symbol else None
        rows = await self._request("GET", "/fapi/v1/openOrders", params)
        out: list[OrderResult] = []
        for d in (rows or []):
            out.append(self._to_order(
                d, d.get("symbol", symbol or ""), d.get("side", ""),
                d.get("type", d.get("origType", "MARKET")),
                str(d.get("reduceOnly", "")).lower() == "true"))
        return out

    async def cancel_all_orders(self, symbol: str) -> int:
        await self._request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})
        return 1

    async def get_order_status(self, *, symbol: str, order_id: str) -> OrderResult:
        d = await self._request("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})
        return self._to_order(d, symbol, d.get("side", ""), d.get("type", "MARKET"),
                              str(d.get("reduceOnly", "")).lower() == "true")

    # ── helpers ───────────────────────────────────────────────────

    @staticmethod
    def _to_order(d: dict, symbol: str, side: str, order_type: str, reduce_only: bool) -> OrderResult:
        return OrderResult(
            order_id=str(d.get("orderId", "")), symbol=symbol, side=side, type=order_type,
            status=d.get("status", "NEW"), price=float(d.get("price", 0) or 0),
            qty=float(d.get("origQty", 0) or 0), filled_qty=float(d.get("executedQty", 0) or 0),
            avg_price=float(d.get("avgPrice", 0) or 0), reduce_only=reduce_only,
            mode=MODE_LIVE, raw=d)
