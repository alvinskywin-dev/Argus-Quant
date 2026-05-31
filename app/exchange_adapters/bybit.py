"""
Sprint 20G — Bybit v5 USDT-perpetual (linear) adapter (LIVE).

Bybit v5 signing: hex( HMAC-SHA256( secret, timestamp + api_key + recv_window + payload ) )
where payload is the query string (GET) or the JSON body (POST). Gate-guarded.
"""
from __future__ import annotations

import hashlib
import hmac
import json
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

_BASE = "https://api.bybit.com"
_RECV_WINDOW = "5000"


def sign_bybit(secret: str, timestamp: str, api_key: str, recv_window: str, payload: str) -> str:
    """hex(HMAC-SHA256(secret, ts + key + recv_window + payload))."""
    prehash = f"{timestamp}{api_key}{recv_window}{payload}"
    return hmac.new(secret.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256).hexdigest()


def _bybit_side(side: str) -> str:
    return "Buy" if side.upper() == "BUY" else "Sell"


class BybitAdapter(ExchangeAdapter):
    name = "bybit"
    mode = MODE_LIVE

    def __init__(self, api_key: str, api_secret: str, passphrase: Optional[str] = None):
        self._key = api_key
        self._secret = api_secret
        self._session = None

    @staticmethod
    def _guard() -> None:
        if not settings.live_trading_enabled or settings.mock_exchange_mode:
            raise AdapterError("Live-trading gate is closed; refusing real Bybit order.")

    async def _client(self):
        import aiohttp
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15, connect=5))
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _request(self, method: str, path: str, params: Optional[dict] = None,
                       body: Optional[dict] = None) -> Any:
        self._guard()
        ts = str(int(time.time() * 1000))
        if method == "GET":
            payload = urlencode(params or {})
            url = f"{_BASE}{path}" + (f"?{payload}" if payload else "")
            data_arg = None
        else:
            payload = json.dumps(body or {})
            url = f"{_BASE}{path}"
            data_arg = payload
        headers = {
            "X-BAPI-API-KEY": self._key,
            "X-BAPI-SIGN": sign_bybit(self._secret, ts, self._key, _RECV_WINDOW, payload),
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": _RECV_WINDOW,
            "X-BAPI-SIGN-TYPE": "2",
            "Content-Type": "application/json",
        }
        session = await self._client()
        async with session.request(method, url, data=data_arg, headers=headers) as resp:
            data = await resp.json(content_type=None)
            if str(data.get("retCode", "0")) != "0":
                raise AdapterError(f"Bybit {data.get('retCode')}: {data.get('retMsg')}")
            return data.get("result", data)

    async def connect(self) -> bool:
        await self._request("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED"})
        return True

    async def get_balance(self, asset: str = "USDT") -> BalanceInfo:
        data = await self._request("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED"})
        for acct in data.get("list", []):
            for coin in acct.get("coin", []):
                if coin.get("coin") == asset:
                    return BalanceInfo(asset=asset, balance=float(coin.get("walletBalance", 0) or 0),
                                       available=float(coin.get("availableToWithdraw", 0) or 0), mode=MODE_LIVE)
        return BalanceInfo(asset=asset, balance=0.0, available=0.0, mode=MODE_LIVE)

    async def get_positions(self) -> list[PositionInfo]:
        data = await self._request("GET", "/v5/position/list", {"category": "linear", "settleCoin": "USDT"})
        out: list[PositionInfo] = []
        for p in data.get("list", []):
            size = float(p.get("size", 0) or 0)
            if size == 0:
                continue
            out.append(PositionInfo(
                symbol=p.get("symbol", ""), side="LONG" if p.get("side") == "Buy" else "SHORT",
                qty=size, entry_price=float(p.get("avgPrice", 0) or 0),
                leverage=int(float(p.get("leverage", 1) or 1)),
                margin_type="isolated" if p.get("tradeMode") == 1 else "cross",
                unrealized_pnl=float(p.get("unrealisedPnl", 0) or 0),
                liquidation_price=float(p.get("liqPrice", 0) or 0), mode=MODE_LIVE))
        return out

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        await self._request("POST", "/v5/position/set-leverage", body={
            "category": "linear", "symbol": symbol.upper(),
            "buyLeverage": str(leverage), "sellLeverage": str(leverage)})

    async def set_margin_type(self, symbol: str, margin_type: str) -> None:
        trade_mode = 1 if margin_type.lower() == "isolated" else 0
        try:
            await self._request("POST", "/v5/position/switch-isolated", body={
                "category": "linear", "symbol": symbol.upper(), "tradeMode": trade_mode,
                "buyLeverage": "5", "sellLeverage": "5"})
        except AdapterError as exc:
            if "110026" not in str(exc):  # already in that mode
                raise

    async def open_order(self, *, symbol: str, side: str, qty: float, order_type: str = "MARKET",
                         price: Optional[float] = None, reduce_only: bool = False) -> OrderResult:
        body = {
            "category": "linear", "symbol": symbol.upper(), "side": _bybit_side(side),
            "orderType": "Market" if order_type == "MARKET" else "Limit", "qty": str(qty),
            "reduceOnly": reduce_only,
        }
        if order_type == "LIMIT":
            body["price"] = str(price)
        data = await self._request("POST", "/v5/order/create", body=body)
        return OrderResult(order_id=str(data.get("orderId", "")), symbol=symbol, side=side,
                           type=order_type, status="NEW", qty=qty, reduce_only=reduce_only,
                           mode=MODE_LIVE, raw=data)

    async def close_order(self, *, symbol: str, side: str, qty: float) -> OrderResult:
        return await self.open_order(symbol=symbol, side=opposite_side(side), qty=qty,
                                     order_type="MARKET", reduce_only=True)

    async def set_tp_sl(self, *, symbol: str, side: str, qty: float, take_profit: Optional[float] = None,
                        stop_loss: Optional[float] = None, trailing_pct: Optional[float] = None) -> list[OrderResult]:
        body: dict[str, Any] = {"category": "linear", "symbol": symbol.upper()}
        if take_profit:
            body["takeProfit"] = str(take_profit)
        if stop_loss:
            body["stopLoss"] = str(stop_loss)
        if take_profit or stop_loss:
            await self._request("POST", "/v5/position/trading-stop", body=body)
            return [OrderResult(order_id="", symbol=symbol, side=opposite_side(side),
                                type="TP_SL", status="NEW", qty=qty, reduce_only=True, mode=MODE_LIVE)]
        return []

    async def get_order_status(self, *, symbol: str, order_id: str) -> OrderResult:
        data = await self._request("GET", "/v5/order/realtime",
                                   {"category": "linear", "symbol": symbol.upper(), "orderId": order_id})
        row = (data.get("list", [{}]) or [{}])[0]
        return OrderResult(order_id=order_id, symbol=symbol, side=row.get("side", ""),
                           type=row.get("orderType", "MARKET").upper(), status=row.get("orderStatus", "NEW"),
                           filled_qty=float(row.get("cumExecQty", 0) or 0),
                           avg_price=float(row.get("avgPrice", 0) or 0), mode=MODE_LIVE, raw=row)
