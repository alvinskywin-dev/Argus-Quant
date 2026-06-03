# ARGUS QUANT — Coding Standards

The canonical source for code style, lint/format/type policy, and the
non-negotiable safety conventions. It pairs with the CI quality gate
(`.github/workflows/ci.yml`) and the [Git & Release Workflow](git-workflow.md).
Tool configuration lives in `pyproject.toml`; this document explains the *why*.

> **Trading-safety first.** Two rules override everything below and are never
> traded away for tidiness:
> 1. **Never change signal logic** as part of a refactor, format, or lint pass.
> 2. **Never flip a live-execution flag** (`LIVE_TRADING_ENABLED`,
>    `MOCK_EXCHANGE_MODE`) outside a dedicated, separately-reviewed PR.

---

## 1. Formatting — Black

- `black`, line length **100**, target `py311`.
- Black is authoritative for layout. Do not hand-format to fight it.
- `E501` (line length) is delegated to Black and ignored by Ruff.
- Excluded: `app/database/migrations`, `archive`, `backups`.

**Intentional exception:** `app/market_data/market_regime.py` is a
column-aligned scoring table (signal logic). It carries an `E701` ignore and is
**not** to be reformatted.

## 2. Lint — Ruff

Selected rule families (`pyproject.toml` → `[tool.ruff.lint] select`):

| Family | What it buys |
|--------|--------------|
| `E`, `W` | pycodestyle errors/warnings |
| `F` | pyflakes — undefined names, unused imports |
| `I` | isort — import ordering (`app` is first-party) |
| `B` | flake8-bugbear — **real-bug** coverage (exception chaining, zip-strict, mutable defaults) |

**Ratchet policy.** Families are added one focused PR at a time, fixing every
finding in the same PR so the gate never lands red. Broad *stylistic* rewrites
(`UP`/pyupgrade) stay deferred: a one-shot rewrite across the trading engine is
churn without safety value and is hard to review against the "no signal-logic
change" rule. Add them later, package by package, only when a change already
touches that area.

**Bugbear false-positive handling.** FastAPI's dependency-injection markers
(`Depends`, `Query`, `Body`, …) and app dependency-factories
(`app.auth.deps.require_role`) are the documented idiom in argument defaults —
not the mutable-default bug `B008` targets. They are whitelisted via
`[tool.ruff.lint.flake8-bugbear] extend-immutable-calls`. Prefer extending that
list over scattering `# noqa: B008`.

## 3. Exception handling

- Re-raising inside an `except` block must preserve or explicitly drop the
  cause (enforced by `B904`):
  - `raise NewError(...) from exc` when the original is relevant context.
  - `raise NewError(...) from None` for an **expected** handled path where the
    original internals should not surface (e.g. an invalid-token auth failure).
- Background loops and best-effort writes (audit, accounting, failure logging)
  catch broadly with `# noqa: BLE001` and **must never** let bookkeeping break
  the primary operation — log a warning and continue.
- `asyncio.CancelledError` is re-raised, never swallowed, so shutdown stays
  clean.

## 4. Types — MyPy (advisory)

MyPy runs in CI as **advisory / non-blocking**. `union-attr` and
`annotation-unchecked` are disabled: fully typing the legacy async SQLAlchemy
and trading internals is out of scope and low-value. Keep new code typed where
it is cheap; do not over-type to silence the legacy noise. Ratchet toward
blocking later, module by module.

## 5. Logging & observability

- Use the shared logger: `from app.utils.logger import logger` (loguru). No
  `print()` in request/loop paths — `print` is reserved for pre-logger startup
  diagnostics and the logger's own sink-failure fallback.
- HTTP requests carry a correlation id (`X-Request-ID`) bound to the log context
  by `CorrelationMiddleware`; include it implicitly by logging through `logger`.

## 6. Naming & layout

- Modules and functions: `snake_case`. Classes: `PascalCase`. Constants:
  `UPPER_SNAKE`.
- Module-private helpers are prefixed `_`.
- One subsystem per package under `app/`; routers expose HTTP, services hold
  logic, schemas hold Pydantic models.

## 7. Tests

- `pytest`; config in `pytest.ini` (single source of truth — not `pyproject`).
- Every bug fix lands with a regression test that fails before the fix.
- Safety-critical invariants (the live gate, stop-loss guards) are pinned with
  explicit tests so a refactor cannot silently weaken them.
- Test files may relax `F401`/`F811`.

## 8. The pre-PR checklist

```bash
black app/ tests/
ruff check app/ tests/
pytest -q
```

All three must be clean (mypy advisory) before opening a PR. CI runs the same.
