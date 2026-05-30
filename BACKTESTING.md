# Backtesting Engine — ALPHA RADAR SIGNALS

## Overview

The backtesting module (`app/backtesting/`) replays historical signals stored in PostgreSQL to compute performance metrics. It is not a price-replay simulator — it uses actual signal outcomes (TP1/TP2/TP3/SL) recorded by the live system.

## Usage

```python
from app.backtesting import BacktestEngine

engine = BacktestEngine()

# Full 90-day backtest
result = await engine.run(days=90)

# Filtered backtest — LONG signals on BTCUSDT only
result = await engine.run(
    days=30,
    symbol="BTCUSDT",
    side="LONG",
    min_confidence=85.0,
    min_rr=2.5,
)

print(result.to_dict())
```

## Metrics Computed

| Metric | Description |
|--------|-------------|
| `win_rate` | Wins / (Wins + Losses) × 100 |
| `avg_pnl` | Average PnL % per closed trade |
| `avg_rr` | Average Risk/Reward ratio |
| `profit_factor` | Gross wins / Gross losses |
| `sharpe_ratio` | avg_pnl / std_pnl (simplified) |
| `max_drawdown_pct` | Largest peak-to-trough drawdown on cumulative PnL |
| `net_pnl_pct` | Sum of all trade PnL % |
| `best_trade_pnl` | Single best trade PnL % |
| `worst_trade_pnl` | Single worst trade PnL % |

## Breakdowns

- `by_symbol` — per-symbol win rate and avg PnL
- `by_timeframe` — per-timeframe win rate
- `by_side` — LONG vs SHORT comparison
- `trades` — full list of individual trade records

## Filter Options

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `days` | int | 30 | Lookback window |
| `symbol` | str | None | Filter by symbol (e.g. `BTCUSDT`) |
| `side` | str | None | `LONG` or `SHORT` |
| `timeframe` | str | None | e.g. `15m`, `1h` |
| `min_confidence` | float | 0.0 | Minimum confidence % |
| `min_rr` | float | 0.0 | Minimum risk/reward |

## Limitations

- Results depend on the live system having correctly tracked TP/SL hits.
- Signals still `OPEN` are counted separately and excluded from metrics.
- This is not a tick-level price simulation — timing of TP/SL hits is not modelled.
- Slippage and trading fees are not included in PnL calculations.

## Roadmap

- [ ] Price-replay simulation using stored OHLCV data
- [ ] Strategy comparison (A vs B)
- [ ] Walk-forward analysis
- [ ] Monte Carlo simulation
- [ ] API endpoint: `GET /api/backtest?days=90&symbol=BTCUSDT`
