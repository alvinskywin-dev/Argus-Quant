"""
Sprint 20F — Binance USDT-M Futures adapter (LIVE).

Real signed REST calls to Binance Futures. This adapter is only ever
instantiated by resolve_adapter() when the live-trading gate is open, and it
ALSO guards every network method itself (defense in depth): if
LIVE_TRADING_ENABLED is false or MOCK_EXCHANGE_MODE is true, it raises instead
of touching the network.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from typing import Any, Optional
from urllib.parse import urlencode

from app.config import settings
from app.exchange_adapters import live_gate_open
from app.exchange_adapters.base import (
    MODE_LIVE,
    AdapterError,
    AdapterTimeoutError,
    BalanceInfo,
    ExchangeAdapter,
    OrderResult,
    PositionInfo,
    opposite_side,
)
from app.utils.logger import logger

# Binance error code returned when an order does not exist (used to resolve an
# ambiguous timeout: "did my order actually land?").
_ERR_ORDER_NOT_FOUND = -2013
# Timestamp outside recvWindow / ahead of server time — clock drift. We resync
# the server-time offset and retry once.
_ERR_TIMESTAMP_DRIFT = -1021
# Re-sync the server-time offset at most this often (seconds).
_TIME_SYNC_TTL_SEC = 1800

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
        self._filters: dict = {}  # symbol -> SymbolFilters (per-instance cache)
        # Server-time sync (#7): offset = serverTime - localTime, applied to every
        # signed request so host clock drift never causes a -1021 rejection.
        self._time_offset_ms = 0
        self._time_synced_at = 0.0

    # ── gate ──────────────────────────────────────────────────────

    @staticmethod
    def _guard() -> None:
        # Defense-in-depth: re-check the single canonical gate at the network
        # chokepoint, even though resolve_adapter already gated construction.
        if not live_gate_open():
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

    # ── server-time sync (#7) ─────────────────────────────────────

    async def _sync_time(self, *, force: bool = False) -> None:
        """Refresh the local→server clock offset from /fapi/v1/time.

        Best-effort: on failure we keep the previous offset (recvWindow still
        gives slack). Skips work when a recent sync is still fresh.
        """
        now = time.time()
        if not force and self._time_synced_at and (now - self._time_synced_at) < _TIME_SYNC_TTL_SEC:
            return
        data = await self._request("GET", "/fapi/v1/time", signed=False)
        server_ms = int(data.get("serverTime", 0)) if isinstance(data, dict) else 0
        if server_ms:
            self._time_offset_ms = server_ms - int(time.time() * 1000)
            self._time_synced_at = time.time()
            logger.debug(f"[binance] time offset synced: {self._time_offset_ms}ms")

    async def _ensure_time_offset(self) -> None:
        try:
            await self._sync_time()
        except Exception as exc:  # noqa: BLE001 — non-fatal; fall back to local clock
            logger.debug(f"[binance] time sync failed (using local clock): {exc!r}")

    # ── signed request ────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        *,
        signed: bool = True,
        _retry_on_drift: bool = True,
    ) -> Any:
        import aiohttp

        self._guard()
        p = dict(params or {})
        if signed:
            await self._ensure_time_offset()
            p["timestamp"] = int(time.time() * 1000) + self._time_offset_ms
            p["recvWindow"] = 5000
            p["signature"] = sign_query(self._secret, p)
        headers = {"X-MBX-APIKEY": self._key}
        url = f"{self._base}{path}"
        session = await self._client()
        try:
            async with session.request(method, url, params=p, headers=headers) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 400:
                    code = data.get("code") if isinstance(data, dict) else None
                    msg = data.get("msg") if isinstance(data, dict) else str(data)
                    # Clock drift outside recvWindow — resync once and retry with a
                    # corrected timestamp (re-signs from the original params).
                    if signed and _retry_on_drift and code == _ERR_TIMESTAMP_DRIFT:
                        logger.warning("[binance] -1021 clock drift — resyncing time and retrying")
                        await self._sync_time(force=True)
                        return await self._request(
                            method, path, params, signed=signed, _retry_on_drift=False
                        )
                    raise AdapterError(f"Binance {resp.status}: {msg}")
                return data
        except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
            # Ambiguous: the request may have reached the exchange and mutated
            # state (e.g. placed an order) even though we never saw the response.
            # Surface a distinct error so callers resolve the real state instead
            # of assuming failure and retrying blindly.
            raise AdapterTimeoutError(f"Binance request timed out/dropped: {exc!r}") from exc

    # ── interface ─────────────────────────────────────────────────

    async def connect(self) -> bool:
        await self._request("GET", "/fapi/v2/balance")
        return True

    async def get_balance(self, asset: str = "USDT") -> BalanceInfo:
        rows = await self._request("GET", "/fapi/v2/balance")
        for r in rows:
            if r.get("asset") == asset:
                return BalanceInfo(
                    asset=asset,
                    balance=float(r["balance"]),
                    available=float(r.get("availableBalance", r["balance"])),
                    mode=MODE_LIVE,
                )
        return BalanceInfo(asset=asset, balance=0.0, available=0.0, mode=MODE_LIVE)

    async def get_positions(self) -> list[PositionInfo]:
        rows = await self._request("GET", "/fapi/v2/positionRisk")
        out: list[PositionInfo] = []
        for r in rows:
            amt = float(r.get("positionAmt", 0))
            if amt == 0:
                continue
            out.append(
                PositionInfo(
                    symbol=r["symbol"],
                    side="LONG" if amt > 0 else "SHORT",
                    qty=abs(amt),
                    entry_price=float(r.get("entryPrice", 0)),
                    leverage=int(float(r.get("leverage", 1))),
                    margin_type=r.get("marginType", "cross"),
                    unrealized_pnl=float(r.get("unRealizedProfit", 0)),
                    liquidation_price=float(r.get("liquidationPrice", 0)),
                    mode=MODE_LIVE,
                )
            )
        return out

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        await self._request(
            "POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": int(leverage)}
        )

    async def set_margin_type(self, symbol: str, margin_type: str) -> None:
        mt = "ISOLATED" if margin_type.lower() == "isolated" else "CROSSED"
        try:
            await self._request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": mt})
        except AdapterError as exc:
            # -4046 "No need to change margin type" is benign.
            if "4046" not in str(exc):
                raise

    async def _symbol_filters(self, symbol: str):
        """Fetch + cache this symbol's trading filters (public exchangeInfo)."""
        from app.exchange_vault.binance_preflight import parse_symbol_filters

        symbol = symbol.upper()
        cached = self._filters.get(symbol)
        if cached is not None and cached.found:
            return cached
        info = await self._request("GET", "/fapi/v1/exchangeInfo", {"symbol": symbol}, signed=False)
        f = parse_symbol_filters(info if isinstance(info, dict) else {}, symbol)
        self._filters[symbol] = f
        return f

    async def open_order(
        self,
        *,
        symbol: str,
        side: str,
        qty: float,
        order_type: str = "MARKET",
        price: Optional[float] = None,
        reduce_only: bool = False,
        client_order_id: Optional[str] = None,
    ) -> OrderResult:
        # Round to the symbol's valid step/tick precision before sending so a
        # raw computed quantity never triggers a PRECISION_ERROR / opaque reject.
        from app.exchange_vault.binance_preflight import enforce_order_precision

        prec = enforce_order_precision(await self._symbol_filters(symbol), qty, price, order_type)
        if not prec.ok:
            raise AdapterError(f"Binance precision: {prec.reason}")
        qty, price = prec.qty, prec.price
        params: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "quantity": qty,
        }
        if client_order_id:
            # Idempotency key — Binance rejects a duplicate id (-4015), so a
            # blind retry can never double-fill, and we can look the order up by
            # this id after a timeout.
            params["newClientOrderId"] = client_order_id
        if reduce_only:
            params["reduceOnly"] = "true"
        if order_type == "LIMIT":
            params["price"] = price
            params["timeInForce"] = "GTC"
        data = await self._request("POST", "/fapi/v1/order", params)
        result = self._to_order(data, symbol, side, order_type, reduce_only)
        # A MARKET order's POST response often omits avgPrice (returns 0); re-read
        # the order to capture the true fill price/qty so PnL is based on reality.
        if order_type == "MARKET" and result.avg_price <= 0 and result.order_id:
            result = await self._reconcile_fill(symbol, result)
        return result

    async def _reconcile_fill(self, symbol: str, result: OrderResult) -> OrderResult:
        """Re-query a just-placed order to capture its real avgPrice / executedQty.

        Best-effort: returns the original result unchanged if the lookup fails or
        the exchange still reports no fill price.
        """
        try:
            latest = await self.get_order_status(symbol=symbol, order_id=result.order_id)
        except AdapterError as exc:
            logger.debug(f"[binance] fill reconcile failed for {symbol} {result.order_id}: {exc}")
            return result
        if latest.avg_price > 0:
            result.avg_price = latest.avg_price
            result.price = latest.avg_price
            result.filled_qty = latest.filled_qty or result.filled_qty
            result.status = latest.status or result.status
        return result

    async def get_order_by_client_id(
        self, *, symbol: str, client_order_id: str
    ) -> Optional[OrderResult]:
        """Resolve an order by its client id. Returns None if it never landed."""
        try:
            d = await self._request(
                "GET",
                "/fapi/v1/order",
                {"symbol": symbol, "origClientOrderId": client_order_id},
            )
        except AdapterError as exc:
            if str(_ERR_ORDER_NOT_FOUND) in str(exc):
                return None
            raise
        return self._to_order(
            d,
            symbol,
            d.get("side", ""),
            d.get("type", "MARKET"),
            str(d.get("reduceOnly", "")).lower() == "true",
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
        close_side = opposite_side(side)
        out: list[OrderResult] = []
        if take_profit:
            d = await self._request(
                "POST",
                "/fapi/v1/order",
                {
                    "symbol": symbol,
                    "side": close_side,
                    "type": "TAKE_PROFIT_MARKET",
                    "stopPrice": take_profit,
                    "closePosition": "true",
                },
            )
            out.append(self._to_order(d, symbol, close_side, "TAKE_PROFIT_MARKET", True))
        if stop_loss:
            d = await self._request(
                "POST",
                "/fapi/v1/order",
                {
                    "symbol": symbol,
                    "side": close_side,
                    "type": "STOP_MARKET",
                    "stopPrice": stop_loss,
                    "closePosition": "true",
                },
            )
            out.append(self._to_order(d, symbol, close_side, "STOP_MARKET", True))
        if trailing_pct:
            d = await self._request(
                "POST",
                "/fapi/v1/order",
                {
                    "symbol": symbol,
                    "side": close_side,
                    "type": "TRAILING_STOP_MARKET",
                    "callbackRate": round(trailing_pct, 1),
                    "quantity": qty,
                    "reduceOnly": "true",
                },
            )
            out.append(self._to_order(d, symbol, close_side, "TRAILING_STOP_MARKET", True))
        return out

    async def get_open_orders(self, symbol: Optional[str] = None) -> list[OrderResult]:
        params = {"symbol": symbol} if symbol else None
        rows = await self._request("GET", "/fapi/v1/openOrders", params)
        out: list[OrderResult] = []
        for d in rows or []:
            out.append(
                self._to_order(
                    d,
                    d.get("symbol", symbol or ""),
                    d.get("side", ""),
                    d.get("type", d.get("origType", "MARKET")),
                    str(d.get("reduceOnly", "")).lower() == "true",
                )
            )
        return out

    async def cancel_all_orders(self, symbol: str) -> int:
        await self._request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})
        return 1

    async def get_order_status(self, *, symbol: str, order_id: str) -> OrderResult:
        d = await self._request("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})
        return self._to_order(
            d,
            symbol,
            d.get("side", ""),
            d.get("type", "MARKET"),
            str(d.get("reduceOnly", "")).lower() == "true",
        )

    # ── helpers ───────────────────────────────────────────────────

    @staticmethod
    def _to_order(
        d: dict, symbol: str, side: str, order_type: str, reduce_only: bool
    ) -> OrderResult:
        return OrderResult(
            order_id=str(d.get("orderId", "")),
            symbol=symbol,
            side=side,
            type=order_type,
            status=d.get("status", "NEW"),
            price=float(d.get("price", 0) or 0),
            qty=float(d.get("origQty", 0) or 0),
            filled_qty=float(d.get("executedQty", 0) or 0),
            avg_price=float(d.get("avgPrice", 0) or 0),
            reduce_only=reduce_only,
            mode=MODE_LIVE,
            raw=d,
        )
