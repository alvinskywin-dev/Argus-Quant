"""
Phase 21F — Binance Futures TESTNET preflight CLI
=================================================
scripts/binance_testnet_preflight.py

Validates Binance USDT-M Futures **testnet** execution readiness end-to-end,
using the dedicated BINANCE_TESTNET_* credentials. Read-only by default — it
places NO order unless you pass --execute-test-order, and it refuses to run at
all unless testnet is enabled, keys are set, and the base URL is the testnet
host (never mainnet). See app/exchange_vault/binance_testnet.py for the guard.

Manual flow (read-only):
  1) validate key            5) TP/SL capability check
  2) account check           6) reconciliation read-only check
  3) filter check            7) recovery read-only check
  4) tiny position planning

Usage (inside the bot image or a host with deps + .env):

    # read-only validation (no orders)
    python -m scripts.binance_testnet_preflight --symbol BTCUSDT --notional 20

    # place ONE tiny real order on TESTNET, then reduce-only close it
    python -m scripts.binance_testnet_preflight --symbol BTCUSDT --notional 20 --execute-test-order

Get testnet keys at https://testnet.binancefuture.com (separate from prod) and
set BINANCE_TESTNET_ENABLED=true plus BINANCE_TESTNET_API_KEY/SECRET in .env.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

from app.exchange_vault.binance_preflight import (
    parse_symbol_filters,
    plan_order_quantity,
    round_step_down,
    run_binance_preflight,
)
from app.exchange_vault.binance_testnet import (
    BinanceTestnetConfig,
    BinanceTestnetGuardError,
    resolve_testnet_config,
)
from app.exchange_vault.permission_validator import validate_binance

EXECUTE_CONFIRM = "I UNDERSTAND THIS PLACES A REAL TESTNET ORDER"


async def _public_get(base: str, path: str) -> dict:
    import aiohttp

    async with aiohttp.ClientSession() as s:
        async with s.get(f"{base}{path}") as r:
            return await r.json(content_type=None)


async def _signed_request(cfg: BinanceTestnetConfig, method: str, path: str, params: dict) -> dict:
    """Signed request hardcoded to the resolved TESTNET base URL. Bypasses the
    live-execution gate intentionally (mainnet stays fully gated) because the
    host can only ever be testnet — resolve_testnet_config refuses otherwise."""
    import aiohttp

    from app.exchange_adapters.binance import sign_query

    p = dict(params)
    p["timestamp"] = int(time.time() * 1000)
    p["recvWindow"] = 5000
    p["signature"] = sign_query(cfg.api_secret, p)
    headers = {"X-MBX-APIKEY": cfg.api_key}
    async with aiohttp.ClientSession() as s:
        async with s.request(method, f"{cfg.base_url}{path}", params=p, headers=headers) as r:
            return await r.json(content_type=None)


async def _read_only_checks(cfg: BinanceTestnetConfig, symbol: str, notional: float) -> bool:
    print(f"== Binance TESTNET preflight — {cfg.base_url} — symbol={symbol} — NO ORDERS ==\n")

    # 1) validate key (signed read only)
    perm = await validate_binance(cfg.api_key, cfg.api_secret, testnet=True)
    print("[1] Key/permission validation:")
    print(json.dumps(perm.to_public_dict(), indent=2, default=str), "\n")

    # 2-3) account + exchange-filter check via the read-only preflight
    pre = await run_binance_preflight(cfg.api_key, cfg.api_secret, testnet=True, symbol=symbol)
    print("[2-3] Account + filter check:")
    print(json.dumps(pre.to_public_dict(), indent=2, default=str), "\n")

    # 4) tiny test-position planning (pure; no order)
    info = await _public_get(cfg.base_url, f"/fapi/v1/exchangeInfo?symbol={symbol}")
    filt = parse_symbol_filters(info, symbol)
    px_doc = await _public_get(cfg.base_url, f"/fapi/v1/premiumIndex?symbol={symbol}")
    mark = float(px_doc.get("markPrice", 0) or 0)
    plan = plan_order_quantity(filt, price=mark, target_notional=notional or 20.0)
    print(
        f"[4] Position plan ~{notional or 20.0} USDT @ {mark}: ok={plan.ok} "
        f"qty={plan.qty} notional≈{plan.notional:.4f} — {plan.reason}\n"
    )

    # 5) TP/SL capability check — filters expose a usable tick/step for protection
    tpsl_ok = bool(filt.found and filt.tick_size > 0 and filt.step_size > 0)
    print(
        f"[5] TP/SL capability: {'OK' if tpsl_ok else 'MISSING'} "
        f"(tick={filt.tick_size} step={filt.step_size})\n"
    )

    # 6) reconciliation read-only — positions are readable
    positions = await _signed_request(cfg, "GET", "/fapi/v2/positionRisk", {})
    pos_open = [p for p in positions if abs(float(p.get("positionAmt", 0) or 0)) > 0]
    print(f"[6] Reconciliation read-only: positionRisk readable, open={len(pos_open)}\n")

    # 7) recovery read-only — open orders are readable
    open_orders = await _signed_request(cfg, "GET", "/fapi/v1/openOrders", {"symbol": symbol})
    print(f"[7] Recovery read-only: openOrders readable, count={len(open_orders)}\n")

    ok = perm.is_connectable() and pre.ok and plan.ok and tpsl_ok
    print(f"== READ-ONLY RESULT: {'PASS ✅' if ok else 'CHECK ⚠️'} ==")
    return ok


async def _execute_test_order(cfg: BinanceTestnetConfig, symbol: str, notional: float) -> int:
    """Place ONE tiny MARKET order on TESTNET, then reduce-only close it."""
    info = await _public_get(cfg.base_url, f"/fapi/v1/exchangeInfo?symbol={symbol}")
    filt = parse_symbol_filters(info, symbol)
    px_doc = await _public_get(cfg.base_url, f"/fapi/v1/premiumIndex?symbol={symbol}")
    mark = float(px_doc.get("markPrice", 0) or 0)
    plan = plan_order_quantity(filt, price=mark, target_notional=notional or 20.0)
    if not plan.ok or plan.qty <= 0:
        print(f"refusing to execute — invalid plan: {plan.reason}")
        return 2

    print(f"[EXEC] TESTNET MARKET BUY {symbol} qty={plan.qty} (~{plan.notional:.2f} USDT)…")
    opened = await _signed_request(
        cfg,
        "POST",
        "/fapi/v1/order",
        {"symbol": symbol, "side": "BUY", "type": "MARKET", "quantity": plan.qty},
    )
    print(json.dumps(opened, indent=2, default=str))
    if opened.get("code"):
        print("[EXEC] open failed — no position to close.")
        return 1

    close_qty = round_step_down(plan.qty, filt.step_size)
    print(f"[EXEC] reduce-only close SELL {symbol} qty={close_qty}…")
    closed = await _signed_request(
        cfg,
        "POST",
        "/fapi/v1/order",
        {
            "symbol": symbol,
            "side": "SELL",
            "type": "MARKET",
            "quantity": close_qty,
            "reduceOnly": "true",
        },
    )
    print(json.dumps(closed, indent=2, default=str))
    return 0 if not closed.get("code") else 1


async def _main() -> int:
    ap = argparse.ArgumentParser(description="Binance TESTNET preflight (read-only by default).")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument(
        "--notional", type=float, default=20.0, help="target USDT notional for planning"
    )
    ap.add_argument("--key", default=None, help="override BINANCE_TESTNET_API_KEY")
    ap.add_argument("--secret", default=None, help="override BINANCE_TESTNET_API_SECRET")
    ap.add_argument(
        "--execute-test-order",
        action="store_true",
        help="place ONE tiny real TESTNET order then close it",
    )
    ap.add_argument(
        "--confirm", default="", help=f'required with --execute-test-order: "{EXECUTE_CONFIRM}"'
    )
    args = ap.parse_args()

    try:
        cfg = resolve_testnet_config(api_key=args.key, api_secret=args.secret)
    except BinanceTestnetGuardError as exc:
        print(f"REFUSED: {exc}")
        return 2

    ok = await _read_only_checks(cfg, args.symbol, args.notional)

    if args.execute_test_order:
        if args.confirm != EXECUTE_CONFIRM:
            print(f'\nREFUSED execute — pass --confirm "{EXECUTE_CONFIRM}"')
            return 2
        if not ok:
            print("\nREFUSED execute — read-only checks did not pass.")
            return 1
        print()
        return await _execute_test_order(cfg, args.symbol, args.notional)

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
