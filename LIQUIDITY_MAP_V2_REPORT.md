# Sprint 22F — Liquidity Map V2

## Goal
Upgrade liquidity intelligence from single-pattern detection to a structured map
of resting liquidity, with scoring that can bias confidence, TP and SL.

## What shipped
- `app/liquidity/liquidity_map_v2.py` — pure analytic over candle data.
- 18 unit tests in `tests/test_liquidity_map_v2.py`.
- `LIQUIDITY_MAP_V2_ENABLED` flag.

## What it maps (`build_liquidity_map`)
1. Equal-highs / equal-lows clustering (multi-touch pools).
2. Previous-day high/low (PDH/PDL) and weekly high/low (PWH/PWL) — supplied or
   inferred from candle extremes.
3. Volume-profile point of control (POC) — the high-liquidity node.
4. Liquidity-sweep detection + probability (wick beyond a pool, body reclaimed).
5. Stop-hunt zones and nearest magnet targets above/below price.

## Output (`LiquidityMap`)
- `nodes` (each: price, kind ∈ EQH/EQL/PDH/PDL/PWH/PWL/POC, touches, strength,
  side), `nearest_magnet_above/below`, `sweep_detected` + `sweep_direction` +
  `sweep_probability`, `stop_hunt_zones`, `liquidity_score` (0-100), `bias`.
- Helpers: `magnet_for_side()` (TP target), `stop_zone_for_side()` (SL zone).

## Scoring → trade
`liquidity_score_adjustment(lm, side)` returns a bounded confidence delta
(-10..+15): positive when the map's `bias` agrees with the trade and a sweep
reclaims in the trade's favour. The map's magnets/zones inform TP selection and
SL placement.

## Inputs
Accepts a pandas DataFrame **or** a list of OHLCV dicts / sequences. Fewer than
20 candles ⇒ an empty, well-formed map (no fake data).

## Safety / compatibility
Side-effect-free; does not replace the existing `app/indicators/liquidity.py`
(Sprint 17) — it is an additive V2 the caller opts into via the flag.

## Validation
`compileall` ✓ · `ruff` ✓ · `black` ✓ · `pytest tests/test_liquidity_map_v2.py`
→ 18 passed.
