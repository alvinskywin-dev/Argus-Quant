"""
Sprint 21F — Binance testnet preflight CLI
==========================================
scripts/binance_testnet_preflight.py

Exercises the REAL (read-only) Binance validation network paths that CI never
touches, against the **futures testnet** by default. It places NO orders — only
the permission validator's signed read and the read-only preflight (server time,
balance, exchangeInfo filters, positions).

This is the manual check called for in the Sprint 21 report (§11.1): "validate
the real validators against exchange testnets" before any real-money test.

Usage (host with deps, or inside the bot image):

    # Use the configured BINANCE_API_KEY / BINANCE_API_SECRET from .env
    BINANCE_TESTNET=true python -m scripts.binance_testnet_preflight

    # Or pass keys explicitly and a symbol
    python -m scripts.binance_testnet_preflight --key XXX --secret YYY --symbol ETHUSDT

    # Validate against PRODUCTION (read-only; still no orders). Use a trade-only key.
    python -m scripts.binance_testnet_preflight --prod --key XXX --secret YYY

Get testnet keys at https://testnet.binancefuture.com (separate from prod).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.config import settings
from app.exchange_vault.binance_preflight import plan_order_quantity, parse_symbol_filters, run_binance_preflight
from app.exchange_vault.permission_validator import validate_binance


async def _main() -> int:
    ap = argparse.ArgumentParser(description="Binance read-only preflight (no orders placed).")
    ap.add_argument("--key", default=settings.binance_api_key, help="API key (default: from .env)")
    ap.add_argument("--secret", default=settings.binance_api_secret, help="API secret (default: from .env)")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--prod", action="store_true", help="Run against production instead of testnet")
    ap.add_argument("--plan-notional", type=float, default=0.0,
                    help="If set, also show a Binance-valid order qty for this USDT notional")
    args = ap.parse_args()

    testnet = not args.prod
    if not args.key or not args.secret:
        print("ERROR: no API key/secret. Set BINANCE_API_KEY/SECRET in .env or pass --key/--secret.")
        return 2

    target = "TESTNET" if testnet else "PRODUCTION"
    print(f"== Binance {target} preflight — symbol={args.symbol} — NO ORDERS PLACED ==\n")

    # 1) permission validation (signed read only)
    perm = await validate_binance(args.key, args.secret, testnet=testnet)
    print("[1] Permission validation:")
    print(json.dumps(perm.to_public_dict(), indent=2, default=str))
    print()

    # 2) read-only preflight
    pre = await run_binance_preflight(args.key, args.secret, testnet=testnet, symbol=args.symbol)
    print("[2] Read-only preflight:")
    print(json.dumps(pre.to_public_dict(), indent=2, default=str))
    print()

    # 3) optional order-quantity plan (pure; still no order)
    if args.plan_notional > 0:
        # Pull the live filters out of the preflight's exchangeInfo by re-reading once.
        import aiohttp
        from app.exchange_vault.binance_preflight import fapi_base
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{fapi_base(testnet)}/fapi/v1/exchangeInfo") as r:
                info = await r.json(content_type=None)
        f = parse_symbol_filters(info, args.symbol)
        # A mark price is needed; reuse the preflight symbol's last price via premiumIndex.
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{fapi_base(testnet)}/fapi/v1/premiumIndex?symbol={args.symbol}") as r:
                px = float((await r.json(content_type=None)).get("markPrice", 0) or 0)
        plan = plan_order_quantity(f, price=px, target_notional=args.plan_notional)
        print(f"[3] Order plan for ~{args.plan_notional} USDT @ mark {px}:")
        print(f"    ok={plan.ok} qty={plan.qty} notional≈{plan.notional:.4f} — {plan.reason}")
        print()

    ok = perm.is_connectable() and pre.ok
    print(f"== RESULT: {'PASS ✅' if ok else 'CHECK ⚠️ — see details above'} ==")
    if pre.warnings:
        print("Warnings:")
        for w in pre.warnings:
            print(f"  - {w}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
