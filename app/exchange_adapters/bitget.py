"""
Sprint 20G — Bitget v2 mix (USDT-FUTURES) adapter (LIVE).

Bitget signing: base64( HMAC-SHA256( secret, timestamp + method + requestPath + body ) )
with a millisecond-epoch timestamp. Requires a passphrase. Gate-guarded.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any, Optional
from urllib.parse import urlencode

from app.exchange_adapters import live_gate_open
from app.exchange_adapters.base import (
    MODE_LIVE,
    AdapterError,
    BalanceInfo,
    ExchangeAdapter,
    OrderResult,
    PositionInfo,
    opposite_side,
)

_BASE = "https://api.bitget.com"
_PRODUCT = "USDT-FUTURES"


def sign_bitget(secret: str, timestamp: str, method: str, request_path: str, body: str = "") -> str:
    """base64(HMAC-SHA256(secret, ts + METHOD + path + body))."""
    prehash = f"{timestamp}{method.upper()}{request_path}{body}"
    mac = hmac.new(secret.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode("ascii")


class BitgetAdapter(ExchangeAdapter):
    name = "bitget"
    mode = MODE_LIVE

    def __init__(self, api_key: str, api_secret: str, passphrase: str):
        self._key = api_key
        self._secret = api_secret
        self._passphrase = passphrase or ""
        self._session = None

    @staticmethod
    def _guard() -> None:
        # Defense-in-depth: re-check the single canonical gate at the network
        # chokepoint, even though resolve_adapter already gated construction.
        if not live_gate_open():
            raise AdapterError("Live-trading gate is closed; refusing real Bitget order.")

    async def _client(self):
        import aiohttp

        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15, connect=5)
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _request(
        self, method: str, path: str, params: Optional[dict] = None, body: Optional[dict] = None
    ) -> Any:
        self._guard()
        request_path = path
        if params:
            request_path = f"{path}?{urlencode(params)}"
        body_str = json.dumps(body) if body else ""
        ts = str(int(time.time() * 1000))
        headers = {
            "ACCESS-KEY": self._key,
            "ACCESS-SIGN": sign_bitget(self._secret, ts, method, request_path, body_str),
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-PASSPHRASE": self._passphrase,
            "Content-Type": "application/json",
            "locale": "en-US",
        }
        session = await self._client()
        async with session.request(
            method, f"{_BASE}{request_path}", data=body_str or None, headers=headers
        ) as resp:
            data = await resp.json(content_type=None)
            if str(data.get("code", "00000")) != "00000":
                raise AdapterError(f"Bitget {data.get('code')}: {data.get('msg')}")
            return data.get("data", data)

    async def connect(self) -> bool:
        await self._request("GET", "/api/v2/mix/account/accounts", {"productType": _PRODUCT})
        return True

    async def get_balance(self, asset: str = "USDT") -> BalanceInfo:
        data = await self._request("GET", "/api/v2/mix/account/accounts", {"productType": _PRODUCT})
        for a in data or []:
            if a.get("marginCoin") == asset:
                return BalanceInfo(
                    asset=asset,
                    balance=float(a.get("accountEquity", 0) or 0),
                    available=float(a.get("available", 0) or 0),
                    mode=MODE_LIVE,
                )
        return BalanceInfo(asset=asset, balance=0.0, available=0.0, mode=MODE_LIVE)

    async def get_positions(self) -> list[PositionInfo]:
        data = await self._request(
            "GET", "/api/v2/mix/position/all-position", {"productType": _PRODUCT}
        )
        out: list[PositionInfo] = []
        for p in data or []:
            total = float(p.get("total", 0) or 0)
            if total == 0:
                continue
            out.append(
                PositionInfo(
                    symbol=p.get("symbol", ""),
                    side="LONG" if p.get("holdSide") == "long" else "SHORT",
                    qty=total,
                    entry_price=float(p.get("openPriceAvg", 0) or 0),
                    leverage=int(float(p.get("leverage", 1) or 1)),
                    margin_type=p.get("marginMode", "isolated"),
                    unrealized_pnl=float(p.get("unrealizedPL", 0) or 0),
                    liquidation_price=float(p.get("liquidationPrice", 0) or 0),
                    mode=MODE_LIVE,
                )
            )
        return out

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        await self._request(
            "POST",
            "/api/v2/mix/account/set-leverage",
            body={
                "symbol": symbol.upper(),
                "productType": _PRODUCT,
                "marginCoin": "USDT",
                "leverage": str(leverage),
            },
        )

    async def set_margin_type(self, symbol: str, margin_type: str) -> None:
        mode = "isolated" if margin_type.lower() == "isolated" else "crossed"
        try:
            await self._request(
                "POST",
                "/api/v2/mix/account/set-margin-mode",
                body={
                    "symbol": symbol.upper(),
                    "productType": _PRODUCT,
                    "marginCoin": "USDT",
                    "marginMode": mode,
                },
            )
        except AdapterError as exc:
            if "already" not in str(exc).lower():
                raise

    async def open_order(
        self,
        *,
        symbol: str,
        side: str,
        qty: float,
        order_type: str = "MARKET",
        price: Optional[float] = None,
        reduce_only: bool = False,
    ) -> OrderResult:
        body = {
            "symbol": symbol.upper(),
            "productType": _PRODUCT,
            "marginCoin": "USDT",
            "size": str(qty),
            "side": "buy" if side.upper() == "BUY" else "sell",
            "orderType": order_type.lower(),
        }
        if order_type == "LIMIT":
            body["price"] = str(price)
        if reduce_only:
            body["reduceOnly"] = "YES"
        data = await self._request("POST", "/api/v2/mix/order/place-order", body=body)
        return OrderResult(
            order_id=str(data.get("orderId", "")),
            symbol=symbol,
            side=side,
            type=order_type,
            status="NEW",
            qty=qty,
            reduce_only=reduce_only,
            mode=MODE_LIVE,
            raw=data,
        )

    async def close_order(self, *, symbol: str, side: str, qty: float) -> OrderResult:
        return await self.open_order(
            symbol=symbol, side=opposite_side(side), qty=qty, order_type="MARKET", reduce_only=True
        )

    async def set_tp_sl(
        self,
        *,
        symbol: str,
        side: str,
        qty: float,
        take_profit: Optional[float] = None,
        stop_loss: Optional[float] = None,
        trailing_pct: Optional[float] = None,
    ) -> list[OrderResult]:
        out: list[OrderResult] = []
        close_side = opposite_side(side)
        for plan, trigger in (("pos_profit", take_profit), ("pos_loss", stop_loss)):
            if not trigger:
                continue
            await self._request(
                "POST",
                "/api/v2/mix/order/place-tpsl-order",
                body={
                    "symbol": symbol.upper(),
                    "productType": _PRODUCT,
                    "marginCoin": "USDT",
                    "planType": plan,
                    "triggerPrice": str(trigger),
                    "holdSide": side.lower(),
                },
            )
            out.append(
                OrderResult(
                    order_id="",
                    symbol=symbol,
                    side=close_side,
                    type=plan.upper(),
                    status="NEW",
                    qty=qty,
                    reduce_only=True,
                    mode=MODE_LIVE,
                )
            )
        return out

    async def get_order_status(self, *, symbol: str, order_id: str) -> OrderResult:
        data = await self._request(
            "GET",
            "/api/v2/mix/order/detail",
            {"symbol": symbol.upper(), "productType": _PRODUCT, "orderId": order_id},
        )
        row = data if isinstance(data, dict) else {}
        return OrderResult(
            order_id=order_id,
            symbol=symbol,
            side=row.get("side", ""),
            type=row.get("orderType", "MARKET").upper(),
            status=row.get("state", "NEW"),
            filled_qty=float(row.get("baseVolume", 0) or 0),
            avg_price=float(row.get("priceAvg", 0) or 0),
            mode=MODE_LIVE,
            raw=row,
        )
