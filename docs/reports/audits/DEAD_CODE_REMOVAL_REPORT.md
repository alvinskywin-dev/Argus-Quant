# Dead Code Removal Report — Phase 3

**Date:** 2026-06-03
**Branch:** `chore/devops-quality-foundation`
**Scope:** Remove provably-dead modules. Preserve `paper_engine` as the single paper-trading engine. Zero behavioural change.

---

## 1. Method

A module was classified **dead** only when *all* of the following held:

1. No `from app.X import …` / `import app.X` anywhere in `app/`, `tests/`, or `scripts/` (excluding the package's own `__init__`).
2. No dynamic import (`importlib`, `__import__`, `import_module`) referencing it.
3. No reference in templates/HTML/text served by the dashboard.
4. Removing it does **not** alter the database schema (the classes are **not** SQLAlchemy-mapped — no `__tablename__`, no `Base`/`mapped_column`).
5. Full test suite (292 tests) still passes after removal.

Verification commands used:

```bash
grep -rn "from app.paper_trading\|import app.paper_trading" app tests scripts --include='*.py'
grep -rn "from app.auto_trading\|import app.auto_trading"   app tests scripts --include='*.py'
grep -rn "importlib\|__import__\|import_module" app --include='*.py' | grep -iE "paper_trading|auto_trading"
grep -nE "__tablename__|Base|mapped_column|Mapped|Column" app/paper_trading/account.py app/auto_trading/models.py
```

All inbound-reference greps returned **only the package's own `__init__.py`** (a self-import), confirming zero external consumers.

---

## 2. Removed modules

| Path | LOC | Contents | Why dead |
|------|----:|----------|----------|
| `app/paper_trading/__init__.py` | 3 | re-export of `PaperAccount` | self-import only |
| `app/paper_trading/account.py` | 177 | plain dataclasses `PaperPosition`, `PaperAccountState`, `PaperAccount` | superseded by `app/paper_engine/` (Sprint 20B); no importers |
| `app/auto_trading/__init__.py` | 12 | re-export of legacy models | self-import only |
| `app/auto_trading/models.py` | 102 | plain dataclasses `RiskProfile`, `Member`, `AutoTradingConfig`, `AuditLogEntry` | superseded by `app/auto_engine/`; no importers |

**Total removed:** 294 LOC across 4 files / 2 packages.

### Database safety
None of the removed classes were SQLAlchemy-mapped (no `__tablename__`, no declarative `Base`). They were in-memory dataclasses. **No table, column, migration, or persisted data is affected.** Database compatibility is fully preserved.

---

## 3. Explicitly preserved (NOT removed)

These showed up in heuristic "zero inbound import" scans but are **live** and were deliberately kept:

| Item | Why kept |
|------|----------|
| `settings.paper_trading`, `settings.paper_trading_enabled`, `settings.auto_trading_enabled`, `settings.auto_trading_max_position_pct`, `settings.auto_trading_daily_loss_limit_pct` | **Feature flags** — referenced by `dashboard/server.py` health/config views and `admin/service.py`. Preserved per project rule "preserve all feature flags". These are config attributes, unrelated to the removed packages. |
| `app/paper/trading.py` | Live — Sprint 6 portfolio simulation, called from `app/main.py` (`open_paper_position`, `on_signal_event`); referenced 23×. |
| `app/paper_engine/` | **The canonical per-user paper engine** (Sprint 20B). Preserved as the single paper-trading engine. |
| `app/auto_engine/` | Live demo auto-trading engine, called from `app/main.py` under `auto_trade_demo_enabled`. |
| `app/ai_scoring/scorer.py` | Live — re-exported by `ai_scoring/__init__`; `score_side`/`aggregate` used by `tests/test_scoring.py`. |
| Entry-point / job scripts (`main.py`, `daily_report.py`, `*_stats_job.py`, `uptime_monitor.py`, migrations) | Invoked via `python -m`, not imported — not dead. |

> **Note on the three "paper" packages:** the codebase had `paper/` (Sprint 6 global sim — live), `paper_engine/` (Sprint 20B per-user — live, canonical), and `paper_trading/` (legacy dataclasses — **dead, removed**). After this change, the only two paper code paths remaining are both live and serve distinct purposes; `paper_engine` is the SaaS per-user engine as required.

---

## 4. Verification

| Check | Result |
|-------|--------|
| `python3 -c "import app.main; import app.dashboard.server; import app.config"` | ✅ imports clean |
| `python3 -m pytest -q` | ✅ **292 passed** |
| Routes preserved | ✅ no route touched |
| DB schema | ✅ unchanged (no mapped models removed) |
| Feature flags | ✅ all preserved |

---

## 5. Follow-up candidates (NOT actioned — require deeper review)

None confirmed. The remaining "low inbound reference" modules are all reachable via package `__init__` re-exports or as `python -m` entry points. No further removals are safe without a dedicated audit, so none were performed (rule: never remove functionality silently).
