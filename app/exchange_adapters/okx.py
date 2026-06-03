"""
Sprint 20G — OKX USDT perpetual-swap adapter (LIVE).

OKX signing: base64( HMAC-SHA256( secret, timestamp + method + requestPath + body ) )
with an ISO-8601 millisecond timestamp. Requires a passphrase. Gate-guarded
like the Binance adapter — refuses to touch the network unless the live gate
is open.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
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

_BASE = "https://www.okx.com"


def okx_timestamp() -> str:
    return (
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.")
        + f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"
    )


def sign_okx(secret: str, timestamp: str, method: str, request_path: str, body: str = "") -> str:
    """base64(HMAC-SHA256(secret, ts + METHOD + path + body))."""
    prehash = f"{timestamp}{method.upper()}{request_path}{body}"
    mac = hmac.new(secret.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode("ascii")


def to_inst_id(symbol: str) -> str:
    """BTCUSDT -> BTC-USDT-SWAP."""
    s = symbol.upper()
    for q in ("USDT", "USDC"):
        if s.endswith(q):
            return f"{s[:-len(q)]}-{q}-SWAP"
    return s


class OKXAdapter(ExchangeAdapter):
    name = "okx"
    mode = MODE_LIVE

    def __init__(self, api_key: str, api_secret: str, passphrase: str):
        self._key = api_key
        self._secret = api_secret
        self._passphrase = passphrase or ""
        self._session = None

    @staticmethod
    def _guard() -> None:
        if not settings.live_trading_enabled or settings.mock_exchange_mode:
            raise AdapterError("Live-trading gate is closed; refusing real OKX order.")

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
        if params:
            path = f"{path}?{urlencode(params)}"
        body_str = json.dumps(body) if body else ""
        ts = okx_timestamp()
        headers = {
            "OK-ACCESS-KEY": self._key,
            "OK-ACCESS-SIGN": sign_okx(self._secret, ts, method, path, body_str),
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self._passphrase,
            "Content-Type": "application/json",
        }
        session = await self._client()
        async with session.request(
            method, f"{_BASE}{path}", data=body_str or None, headers=headers
        ) as resp:
            data = await resp.json(content_type=None)
            if str(data.get("code", "0")) != "0":
                raise AdapterError(f"OKX {data.get('code')}: {data.get('msg')}")
            return data.get("data", data)

    async def connect(self) -> bool:
        await self._request("GET", "/api/v5/account/balance")
        return True

    async def get_balance(self, asset: str = "USDT") -> BalanceInfo:
        data = await self._request("GET", "/api/v5/account/balance")
        details = data[0].get("details", []) if data else []
        for d in details:
            if d.get("ccy") == asset:
                return BalanceInfo(
                    asset=asset,
                    balance=float(d.get("eq", 0)),
                    available=float(d.get("availBal", 0)),
                    mode=MODE_LIVE,
                )
        return BalanceInfo(asset=asset, balance=0.0, available=0.0, mode=MODE_LIVE)

    async def get_positions(self) -> list[PositionInfo]:
        data = await self._request("GET", "/api/v5/account/positions", {"instType": "SWAP"})
        out: list[PositionInfo] = []
        for p in data or []:
            pos = float(p.get("pos", 0) or 0)
            if pos == 0:
                continue
            out.append(
                PositionInfo(
                    symbol=p.get("instId", ""),
                    side="LONG" if p.get("posSide") == "long" or pos > 0 else "SHORT",
                    qty=abs(pos),
                    entry_price=float(p.get("avgPx", 0) or 0),
                    leverage=int(float(p.get("lever", 1) or 1)),
                    margin_type=p.get("mgnMode", "cross"),
                    unrealized_pnl=float(p.get("upl", 0) or 0),
                    liquidation_price=float(p.get("liqPx", 0) or 0),
                    mode=MODE_LIVE,
                )
            )
        return out

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        await self._request(
            "POST",
            "/api/v5/account/set-leverage",
            body={"instId": to_inst_id(symbol), "lever": str(leverage), "mgnMode": "isolated"},
        )

    async def set_margin_type(self, symbol: str, margin_type: str) -> None:
        # OKX margin mode is set per-order (tdMode); nothing to do up front.
        return None

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
            "instId": to_inst_id(symbol),
            "tdMode": "isolated",
            "side": side.lower(),
            "ordType": order_type.lower(),
            "sz": str(qty),
        }
        if order_type == "LIMIT":
            body["px"] = str(price)
        if reduce_only:
            body["reduceOnly"] = "true"
        data = await self._request("POST", "/api/v5/trade/order", body=body)
        row = data[0] if isinstance(data, list) and data else {}
        return OrderResult(
            order_id=str(row.get("ordId", "")),
            symbol=symbol,
            side=side,
            type=order_type,
            status="NEW",
            qty=qty,
            reduce_only=reduce_only,
            mode=MODE_LIVE,
            raw=row,
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
        algo = {
            "instId": to_inst_id(symbol),
            "tdMode": "isolated",
            "side": close_side.lower(),
            "ordType": "oco",
            "sz": str(qty),
            "reduceOnly": "true",
        }
        if take_profit:
            algo["tpTriggerPx"] = str(take_profit)
            algo["tpOrdPx"] = "-1"
        if stop_loss:
            algo["slTriggerPx"] = str(stop_loss)
            algo["slOrdPx"] = "-1"
        if take_profit or stop_loss:
            data = await self._request("POST", "/api/v5/trade/order-algo", body=algo)
            row = data[0] if isinstance(data, list) and data else {}
            out.append(
                OrderResult(
                    order_id=str(row.get("algoId", "")),
                    symbol=symbol,
                    side=close_side,
                    type="OCO",
                    status="NEW",
                    qty=qty,
                    reduce_only=True,
                    mode=MODE_LIVE,
                )
            )
        return out

    async def get_order_status(self, *, symbol: str, order_id: str) -> OrderResult:
        data = await self._request(
            "GET", "/api/v5/trade/order", {"instId": to_inst_id(symbol), "ordId": order_id}
        )
        row = data[0] if isinstance(data, list) and data else {}
        return OrderResult(
            order_id=order_id,
            symbol=symbol,
            side=row.get("side", ""),
            type=row.get("ordType", "MARKET").upper(),
            status=row.get("state", "NEW"),
            filled_qty=float(row.get("accFillSz", 0) or 0),
            avg_price=float(row.get("avgPx", 0) or 0),
            mode=MODE_LIVE,
            raw=row,
        )
