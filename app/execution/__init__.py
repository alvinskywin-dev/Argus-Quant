"""Live execution domain.

Groups the real-money execution stack and its access gate:
  * live_trading — Binance USDT-M Futures order/position/TP-SL execution.
  * live_beta    — controlled multi-user access gate (decides who may trade).

Shadow simulation (app.shadow) and per-user demo accounts (app.paper_engine)
live outside this package by design.
"""
