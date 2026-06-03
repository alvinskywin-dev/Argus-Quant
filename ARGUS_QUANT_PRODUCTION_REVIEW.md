# ARGUS QUANT — Production Hardening Review

**Program:** 12-phase production-hardening pass
**Branch:** `chore/devops-quality-foundation`
**Date:** 2026-06-03
**Status:** 10 phases complete · 1 deferred (P11) · this review closes the program
**Test baseline:** 327 passing · ruff + black clean · mypy advisory

> **Scope discipline.** Every phase preserved three invariants: no signal-logic
> change, no real-trading enablement, and no break to feature flags, routes, or
> DB compatibility. Work landed as one focused, independently-reviewable commit
> per subsystem and was **not** auto-pushed.

---

## 1. Executive summary

The platform entered this program functionally complete (V11 multi-user SaaS,
live-trading foundation, MTF signal engine) but carrying the usual
pre-production debt: no quality gate, a 5,400-line dashboard god-file, no
request observability, duplicated safety checks, and unguarded numeric edge
cases. The program added a CI gate, split the god-file, and closed the
observability, safety, frontend-security, and standards gaps — each with
regression tests that pin the fix so it cannot silently regress.

No defect found in this pass could place a real order or alter a generated
signal; the hardening is additive and reversible by feature flag or revert.

## 2. Phase outcomes

| # | Phase | Outcome | Commit |
|---|-------|---------|--------|
| 1 | CI/CD quality gate | `pyproject.toml` tooling + GitHub Actions (ruff/black blocking, mypy advisory, pytest) | `be02183`, `2933b63`, `04eeae4` |
| 2 | Git & release workflow | `docs/engineering/git-workflow.md` (branching, hotfix, rollback, live-safety PR rule) | `58b472a` |
| 3 | Dead-code removal | Deleted legacy `app/paper_trading` + `app/auto_trading` (paper_engine is canonical) | `2bb64df` |
| 4 | Dashboard de-god-file | `server.py` 5,461 → 3,426 LOC; 6 routers under `app/dashboard/routes/`; route set byte-identical (140 routes) | `df76236` |
| 5 | Report archival | Reports organised into `docs/reports/{sprint,fixes,audits,deployment}` | `1be52be` |
| 6 | Observability | Correlation IDs (`X-Request-ID`) + structured access logs + in-process metrics feeding `/metrics`; WS reconnect backoff | `4c665d7` |
| 7 | Live-safety audit V2 | All 4 LIVE adapter guards now delegate to canonical `live_gate_open()`; full guard regression matrix | `5d661c2` |
| 8 | Stop-loss engine V2 | Reject non-finite inputs that produced a NaN stop flagged valid | `8925b73` |
| 9 | Frontend security | XSS audit (clean) + scoped Content-Security-Policy on all dashboard responses | `31c84f3` |
| 10 | Timezone system | Shipped on the parent branch (`b098904`) | — |
| 11 | Google OAuth | **Deferred** — requires a Google client ID/secret (external). See §4. | — |
| 12 | Coding standards | Ruff ratcheted to `B` (bugbear) + fixes; `docs/engineering/coding-standards.md` | `1e9b1d5` |

## 3. Notable findings & fixes

- **Duplicated live gate (P7).** Four exchange adapters re-implemented the
  open-gate condition inline; a future change to the gate definition would have
  let them drift. All now delegate to the single `live_gate_open()`. The gate
  remains MOCK-by-default; defense-in-depth re-checks it at every network call.
- **Silent NaN stop-loss (P8).** A NaN `prev_1d_low/high/ATR` slipped every
  guard (NaN comparisons are all False) and returned `stop_loss=NaN` marked
  `sl_valid=True`. Now rejected before arithmetic; finite/valid-path math is
  untouched (verified identical output).
- **No CSP (P9).** Added a Content-Security-Policy scoped to the exact origins
  the UI loads (Chart.js, QR widget, Google Fonts) plus
  object-src/base-uri/frame-ancestors/form-action lockdown. Verified no
  `eval`/external WebSocket so the policy is non-breaking.
- **Exception chaining (P12).** 13 re-raises inside `except` now preserve the
  cause (`from exc`) or explicitly drop it (`from None`), improving production
  traceability.

## 4. Outstanding work

- **P11 — Google OAuth (deferred, not blocked by code).** Needs a Google OAuth
  client ID/secret that cannot be generated here. Recommended approach when
  resumed: implement the flow **feature-flagged off**, reading credentials from
  env (mirroring the existing `auth_enabled` / vault patterns), so the code can
  land and be reviewed before credentials exist, then validate end-to-end.
- **Ruff `UP` (pyupgrade) ratchet.** Deliberately deferred (≈660 stylistic
  rewrites). Adopt package-by-package as changes touch each area, never as a
  one-shot pass over the trading engine.
- **MyPy → blocking.** Currently advisory (`union-attr`/`annotation-unchecked`
  disabled). Ratchet toward blocking module-by-module once the type baseline is
  cleaned.
- **Dockerfile Python pin.** Image pins `python:3.11` while the stack/tests run
  3.12 — aligning them is tracked tech debt, intentionally untouched here.
- **Operational: live gate `.env`.** This dev host's git-ignored `.env` has the
  gate **open**; production must confirm it stays closed until a host is
  validated via the read-only Binance preflight.

## 5. Verification

```bash
black --check app tests   # clean
ruff check app tests      # clean (E,F,W,I,B)
pytest -q                 # 327 passed
```

Quality gate (`.github/workflows/ci.yml`) runs the same on every push/PR.

## 6. Recommendation

The branch is ready to merge to `develop` for release once P11 is either
scaffolded behind a flag or formally accepted as a follow-up. All hardening is
additive and flag-/revert-reversible; no change in this program affects signal
generation or enables real trading.
