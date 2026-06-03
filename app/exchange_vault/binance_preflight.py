"""
Sprint 21F — Binance live/testnet validation (read-only preflight).

The Sprint 21 foundation shipped a permission *classifier* but its real network
paths were never exercised, and there was no way to prove — before risking real
money — that a connected Binance key can actually be used end-to-end. This module
fills that gap with a **strictly read-only preflight**:

  * server-clock skew (signed Binance requests fail with -1021 if the local clock
    drifts past ``recvWindow``),
  * futures account reachable + USDT balance present,
  * symbol trading filters (step size / tick size / min-notional) parsed so the
    executor can round an order correctly instead of getting PRECISION_ERROR /
    MIN_NOTIONAL rejections at order time,
  * positions readable.

It **never places, cancels, or modifies an order** — it only calls public data
and signed *read* endpoints, exactly like the permission validator. As with the
rest of Sprint 21, the decision logic is split into pure functions (unit-tested,
no network) and a thin network runner (exercised against testnet, never in CI).

The same pure helpers also let the system turn "risk ~25 USDT" into a Binance-
valid order quantity (:func:`plan_order_quantity`), which is the practical
prerequisite for a small live test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal, InvalidOperation
from typing import Any, Optional

from app.utils.logger import logger

# Binance signed requests carry recvWindow=5000; a signed call is rejected
# (-1021) when the local timestamp is ahead of the server beyond that window.
# We warn well before that and fail before it can break signing.
CLOCK_WARN_MS = 500
CLOCK_FAIL_MS = 1000

_PROD_FAPI = "https://fapi.binance.com"
_TESTNET_FAPI = "https://testnet.binancefuture.com"


def fapi_base(testnet: bool) -> str:
    """Resolve the Binance USDT-M futures REST base for the given mode."""
    return _TESTNET_FAPI if testnet else _PROD_FAPI


# ════════════════════════════════════════════════════════════════════
#  Pure helpers (unit-tested, no network)
# ════════════════════════════════════════════════════════════════════


@dataclass
class ClockSkewResult:
    ok: bool
    skew_ms: int  # local - server  (positive == local clock is ahead)
    abs_skew_ms: int
    severity: str  # OK / WARN / FAIL
    message: str


def classify_clock_skew(
    local_ms: int,
    server_ms: int,
    *,
    warn_ms: int = CLOCK_WARN_MS,
    fail_ms: int = CLOCK_FAIL_MS,
) -> ClockSkewResult:
    """Classify the local↔Binance clock skew that signed requests depend on."""
    skew = int(local_ms) - int(server_ms)
    a = abs(skew)
    if a >= fail_ms:
        return ClockSkewResult(
            ok=False,
            skew_ms=skew,
            abs_skew_ms=a,
            severity="FAIL",
            message=(
                f"Clock skew {skew:+d}ms exceeds {fail_ms}ms — signed requests will be "
                f"rejected (-1021). Sync the host clock (NTP) before live trading."
            ),
        )
    if a >= warn_ms:
        return ClockSkewResult(
            ok=True,
            skew_ms=skew,
            abs_skew_ms=a,
            severity="WARN",
            message=f"Clock skew {skew:+d}ms is high; sync the host clock to be safe.",
        )
    return ClockSkewResult(
        ok=True,
        skew_ms=skew,
        abs_skew_ms=a,
        severity="OK",
        message=f"Clock skew {skew:+d}ms is within tolerance.",
    )


def _precision_from_step(step: float) -> int:
    """Number of decimal places implied by a step/tick like 0.001 -> 3."""
    if step <= 0:
        return 0
    s = format(Decimal(str(step)).normalize(), "f")
    return len(s.split(".", 1)[1]) if "." in s else 0


@dataclass
class SymbolFilters:
    symbol: str
    found: bool = False
    step_size: float = 0.0
    min_qty: float = 0.0
    tick_size: float = 0.0
    min_notional: float = 0.0
    qty_precision: int = 0
    price_precision: int = 0
    status: str = ""

    @property
    def tradable(self) -> bool:
        return self.found and self.status in ("", "TRADING")


def parse_symbol_filters(exchange_info: dict, symbol: str) -> SymbolFilters:
    """
    Extract the trading filters for ``symbol`` from a /fapi/v1/exchangeInfo body.

    Reads LOT_SIZE (stepSize/minQty), PRICE_FILTER (tickSize) and MIN_NOTIONAL
    (futures exposes ``notional``; spot uses ``minNotional`` — both accepted).
    Returns ``found=False`` when the symbol is absent, never raises.
    """
    symbol = (symbol or "").upper()
    for s in (exchange_info or {}).get("symbols", []) or []:
        if s.get("symbol") != symbol:
            continue
        filters = {f.get("filterType"): f for f in (s.get("filters") or [])}
        lot = filters.get("LOT_SIZE", {}) or {}
        price = filters.get("PRICE_FILTER", {}) or {}
        mn = filters.get("MIN_NOTIONAL", {}) or {}
        step = _as_float(lot.get("stepSize"))
        tick = _as_float(price.get("tickSize"))
        return SymbolFilters(
            symbol=symbol,
            found=True,
            step_size=step,
            min_qty=_as_float(lot.get("minQty")),
            tick_size=tick,
            min_notional=_as_float(mn.get("notional", mn.get("minNotional"))),
            qty_precision=int(s.get("quantityPrecision", _precision_from_step(step))),
            price_precision=int(s.get("pricePrecision", _precision_from_step(tick))),
            status=str(s.get("status", "")),
        )
    return SymbolFilters(symbol=symbol, found=False)


def _as_float(v: Any) -> float:
    try:
        return float(v) if v not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def round_step_down(value: float, step: float) -> float:
    """Round ``value`` DOWN to a multiple of ``step`` (quantity rounding)."""
    if step <= 0:
        return float(value)
    try:
        d = (Decimal(str(value)) / Decimal(str(step))).to_integral_value(ROUND_DOWN) * Decimal(
            str(step)
        )
        return float(d)
    except InvalidOperation:
        return float(value)


def round_step_up(value: float, step: float) -> float:
    """Round ``value`` UP to a multiple of ``step``."""
    if step <= 0:
        return float(value)
    try:
        d = (Decimal(str(value)) / Decimal(str(step))).to_integral_value(ROUND_UP) * Decimal(
            str(step)
        )
        return float(d)
    except InvalidOperation:
        return float(value)


def round_price(value: float, tick: float) -> float:
    """Round ``value`` to the nearest multiple of ``tick`` (price rounding)."""
    if tick <= 0:
        return float(value)
    try:
        d = (Decimal(str(value)) / Decimal(str(tick))).to_integral_value(ROUND_HALF_UP) * Decimal(
            str(tick)
        )
        return float(d)
    except InvalidOperation:
        return float(value)


def check_min_notional(qty: float, price: float, min_notional: float) -> tuple[bool, float]:
    """Return (passes, notional). A zero/absent min_notional always passes."""
    notional = float(qty) * float(price)
    if min_notional <= 0:
        return True, notional
    return notional >= min_notional, notional


@dataclass
class OrderPlan:
    ok: bool
    qty: float = 0.0
    notional: float = 0.0
    reason: str = ""


def plan_order_quantity(filters: SymbolFilters, price: float, target_notional: float) -> OrderPlan:
    """
    Turn a target notional (e.g. margin × leverage) into a Binance-valid quantity.

    Rounds the raw quantity DOWN to ``step_size``, then bumps it up to satisfy
    ``min_qty`` and ``min_notional`` if needed. Returns ``ok=False`` with a reason
    when no valid quantity exists for the given price/notional. Pure — no order is
    ever placed here.
    """
    if not filters.found:
        return OrderPlan(ok=False, reason=f"Symbol {filters.symbol} not found in exchangeInfo")
    if not filters.tradable:
        return OrderPlan(
            ok=False, reason=f"Symbol {filters.symbol} is not TRADING (status={filters.status!r})"
        )
    if price <= 0 or target_notional <= 0:
        return OrderPlan(ok=False, reason="Price and target notional must be positive")

    qty = round_step_down(target_notional / price, filters.step_size)
    if filters.min_qty and qty < filters.min_qty:
        qty = round_step_up(filters.min_qty, filters.step_size)
    # Ensure min-notional after the step/min-qty rounding.
    if filters.min_notional and qty * price < filters.min_notional:
        qty = round_step_up(filters.min_notional / price, filters.step_size)
        if filters.min_qty and qty < filters.min_qty:
            qty = round_step_up(filters.min_qty, filters.step_size)

    notional = qty * price
    if qty <= 0:
        return OrderPlan(
            ok=False,
            qty=0.0,
            notional=0.0,
            reason="Rounded quantity is zero for this price/notional",
        )
    passes, notional = check_min_notional(qty, price, filters.min_notional)
    if not passes:
        return OrderPlan(
            ok=False,
            qty=qty,
            notional=notional,
            reason=f"Notional {notional:.4f} below exchange minimum {filters.min_notional}",
        )
    return OrderPlan(
        ok=True, qty=qty, notional=notional, reason=f"qty={qty} notional≈{notional:.4f} USDT"
    )


@dataclass
class PrecisionResult:
    ok: bool
    qty: float
    price: Optional[float] = None
    reason: str = ""


def enforce_order_precision(
    filters: SymbolFilters,
    qty: float,
    price: Optional[float],
    order_type: str,
) -> PrecisionResult:
    """
    Round an order to the symbol's valid precision before it is sent.

    Quantity is rounded **DOWN** to ``step_size`` (never silently bumped up — that
    would increase position size / risk beyond intent). LIMIT prices are rounded
    to ``tick_size``. A quantity that falls below ``min_qty`` after rounding is a
    hard ``ok=False`` (the caller should reject rather than let the exchange
    return an opaque rejection). With no filters available, passes through
    unchanged so behaviour is never worse than before.
    """
    if not filters.found:
        return PrecisionResult(True, float(qty), price, "no filters available; passthrough")
    rq = round_step_down(qty, filters.step_size)
    if rq <= 0 or (filters.min_qty and rq < filters.min_qty):
        return PrecisionResult(
            False,
            rq,
            price,
            f"quantity {qty} rounds to {rq}, below minimum {filters.min_qty} for {filters.symbol}",
        )
    rp = price
    if order_type.upper() in ("LIMIT", "STOP", "TAKE_PROFIT") and price:
        rp = round_price(price, filters.tick_size)
    return PrecisionResult(True, rq, rp, "")


# ── preflight aggregation ───────────────────────────────────────────


@dataclass
class PreflightCheck:
    name: str
    ok: bool
    detail: str = ""
    severity: str = "OK"  # OK / WARN / FAIL


@dataclass
class BinancePreflightResult:
    exchange: str = "binance"
    testnet: bool = False
    ok: bool = False
    base_url: str = ""
    checks: list[PreflightCheck] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "", severity: Optional[str] = None) -> None:
        sev = severity or ("OK" if ok else "FAIL")
        if sev == "WARN":
            self.warnings.append(f"{name}: {detail}")
        self.checks.append(PreflightCheck(name=name, ok=ok, detail=detail, severity=sev))

    def finalize(self) -> "BinancePreflightResult":
        # The run is OK only when no hard check failed (WARN is tolerated).
        self.ok = all(c.ok for c in self.checks) and bool(self.checks)
        return self

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "exchange": self.exchange,
            "testnet": self.testnet,
            "ok": self.ok,
            "base_url": self.base_url,
            "warnings": self.warnings,
            "checks": [
                {"name": c.name, "ok": c.ok, "severity": c.severity, "detail": c.detail}
                for c in self.checks
            ],
        }


def build_preflight_summary(checks: list[PreflightCheck]) -> bool:
    """Overall pass = at least one check and no hard FAIL."""
    return bool(checks) and all(c.ok for c in checks)


# ════════════════════════════════════════════════════════════════════
#  Network runner (read-only; exercised against testnet, never in CI)
# ════════════════════════════════════════════════════════════════════


async def run_binance_preflight(
    api_key: str,
    api_secret: str,
    *,
    testnet: bool = True,
    symbol: str = "BTCUSDT",
) -> BinancePreflightResult:
    """
    Run the read-only preflight against Binance USDT-M futures.

    SAFETY: every call here is either a public data endpoint or a signed *read*
    (balance / positionRisk). No order is ever placed, cancelled, or modified.
    Defaults to testnet so it is safe to run before opening the live gate.
    """
    import time

    import aiohttp

    from app.exchange_adapters.binance import sign_query

    base = fapi_base(testnet)
    res = BinancePreflightResult(testnet=testnet, base_url=base)
    timeout = aiohttp.ClientTimeout(total=15, connect=5)

    def _signed(params: dict) -> dict:
        p = dict(params)
        p["timestamp"] = int(time.time() * 1000)
        p["recvWindow"] = 5000
        p["signature"] = sign_query(api_secret, p)
        return p

    headers = {"X-MBX-APIKEY": api_key}
    try:
        async with aiohttp.ClientSession(timeout=timeout) as s:
            # 1) server time + clock skew (public)
            try:
                async with s.get(f"{base}/fapi/v1/time") as r:
                    body = await r.json(content_type=None)
                server_ms = int(body.get("serverTime", 0))
                skew = classify_clock_skew(int(time.time() * 1000), server_ms)
                res.add("clock_skew", skew.ok, skew.message, severity=skew.severity)
            except Exception as exc:  # noqa: BLE001
                res.add("clock_skew", False, f"could not read server time: {exc!s:.80}")

            # 2) futures balance (signed read) — proves the key authenticates
            try:
                async with s.get(
                    f"{base}/fapi/v2/balance", params=_signed({}), headers=headers
                ) as r:
                    bal = await r.json(content_type=None)
                if r.status >= 400 or isinstance(bal, dict):
                    msg = bal.get("msg") if isinstance(bal, dict) else str(bal)
                    res.add("account_read", False, f"balance read failed: {msg}")
                else:
                    usdt = next((b for b in bal if b.get("asset") == "USDT"), {})
                    avail = _as_float(usdt.get("availableBalance", usdt.get("balance")))
                    res.add("account_read", True, f"USDT available≈{avail}")
                    res.add(
                        "balance_positive",
                        avail > 0,
                        f"available USDT={avail}",
                        severity="OK" if avail > 0 else "WARN",
                    )
            except Exception as exc:  # noqa: BLE001
                res.add("account_read", False, f"balance read error: {exc!s:.80}")

            # 3) exchangeInfo symbol filters (public)
            try:
                async with s.get(f"{base}/fapi/v1/exchangeInfo") as r:
                    info = await r.json(content_type=None)
                f = parse_symbol_filters(info if isinstance(info, dict) else {}, symbol)
                if f.found:
                    res.add(
                        "symbol_filters",
                        True,
                        f"{symbol}: step={f.step_size} tick={f.tick_size} minNotional={f.min_notional}",
                    )
                else:
                    res.add("symbol_filters", False, f"{symbol} not found in exchangeInfo")
            except Exception as exc:  # noqa: BLE001
                res.add("symbol_filters", False, f"exchangeInfo error: {exc!s:.80}")

            # 4) positions readable (signed read)
            try:
                async with s.get(
                    f"{base}/fapi/v2/positionRisk", params=_signed({}), headers=headers
                ) as r:
                    pos = await r.json(content_type=None)
                ok = isinstance(pos, list)
                res.add(
                    "positions_read",
                    ok,
                    f"{len(pos)} position rows" if ok else f"unexpected: {str(pos)[:80]}",
                )
            except Exception as exc:  # noqa: BLE001
                res.add("positions_read", False, f"positionRisk error: {exc!s:.80}")
    except Exception as exc:  # noqa: BLE001 — never raise out of a preflight
        logger.warning(f"[preflight] binance preflight aborted: {exc!s:.120}")
        res.add("session", False, f"could not open session: {exc!s:.80}")

    return res.finalize()
