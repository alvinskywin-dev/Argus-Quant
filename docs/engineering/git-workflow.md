# ARGUS QUANT — Git & Release Workflow

This document defines how code moves from a developer's machine to production.
It is the canonical source for branching, merging, tagging, hotfixes, rollback,
and the deployment checklist. It pairs with the CI quality gate
(`.github/workflows/ci.yml`).

> **Trading-safety first.** `LIVE_TRADING_ENABLED` and `MOCK_EXCHANGE_MODE` are
> never changed as part of a routine merge. Any change to a live-execution flag
> is a dedicated, separately-reviewed PR (see [Live-safety changes](#live-safety-changes)).

---

## 1. Branch model

A trunk-based-with-integration model: three long-lived intents, short-lived work.

| Branch | Purpose | Who writes | Deploys to |
|--------|---------|------------|------------|
| `main` | **Production only.** Always deployable. Every commit is released or releasable. | Merges from `develop` (release) or `hotfix/*` only. | Production |
| `develop` | **Integration.** Where completed feature branches converge and are tested together before a release. | Merges from `feature/*`. | Staging / preview |
| `feature/*` | **Sprint / unit of work.** One logical change set. Short-lived. | Developers / agents. | CI only |

Supporting short-lived branches:

| Prefix | Branches off | Merges into | Example |
|--------|--------------|-------------|---------|
| `feature/*` | `develop` | `develop` | `feature/timezone-system-v1` |
| `chore/*` | `develop` | `develop` | `chore/devops-quality-foundation` |
| `fix/*` | `develop` | `develop` | `fix/paper-roe-realtime` |
| `hotfix/*` | `main` | `main` **and** `develop` | `hotfix/ws-reconnect-storm` |
| `release/*` | `develop` | `main` **and** `develop` | `release/v11.4.0` |

### Naming

```
<type>/<short-kebab-summary>
```

`type ∈ {feature, fix, chore, hotfix, release, docs, refactor}`. Keep it short
and descriptive. One branch = one reviewable concern.

---

## 2. Commit conventions

Conventional-Commits style, one subsystem per commit (never squash unrelated
changes together):

```
<type>(<scope>): <imperative summary>

<why + what, wrapped at ~72 cols>

Co-Authored-By: ...
```

Types: `feat`, `fix`, `refactor`, `chore`, `ci`, `docs`, `style`, `test`, `perf`.

Rules:

- **One subsystem per commit.** A formatting pass, a behaviour change, and a
  config change are three commits, not one.
- **Every commit compiles and passes tests.** No "WIP" on shared branches.
- Reference the report/issue when relevant (e.g. `See DEAD_CODE_REMOVAL_REPORT.md`).
- Never commit secrets. `.env` is git-ignored; only `.env.example` is tracked.

---

## 3. Merge policy

All merges into `develop` and `main` go through a **Pull Request**. Direct
pushes to `main` and `develop` are disabled (branch protection).

### Required to merge into `develop`
1. CI green: **lint**, **test**, **docker-build** pass (mypy is advisory).
2. At least **1 approving review**.
3. Branch up to date with `develop` (rebase or merge latest).
4. No unresolved conversations.

### Required to merge into `main` (release)
1. Source is a `release/*` or `hotfix/*` branch.
2. CI green on the merge commit.
3. **1 approving review** from a maintainer.
4. Release notes / tag prepared (see §4).
5. Deployment checklist (§7) reviewed.

### Merge strategy
- `feature/* -> develop`: **squash merge** is acceptable for small features;
  **merge commit** preferred when the per-subsystem commit history is valuable
  (e.g. a multi-phase refactor). Never squash a multi-subsystem branch into one
  opaque commit.
- `release/* -> main` and `hotfix/* -> main`: **merge commit** (no squash) so
  the release history is preserved.
- Always merge `main` back into `develop` after a release or hotfix so the
  branches never diverge.

---

## 4. Release tagging

Semantic-ish versioning aligned to the platform line: `vMAJOR.MINOR.PATCH`
(the product is "V11"; releases tag as `v11.x.y`).

| Bump | When |
|------|------|
| MAJOR | Breaking API/DB change, or a new platform generation (V11 → V12). |
| MINOR | New backward-compatible feature / sprint delivery. |
| PATCH | Backward-compatible bug fix or hotfix. |

### Cutting a release
```bash
git checkout develop && git pull
git checkout -b release/v11.4.0
# stabilise: only bugfixes + version bump on this branch
# open PR release/v11.4.0 -> main
# after merge:
git checkout main && git pull
git tag -a v11.4.0 -m "ARGUS QUANT v11.4.0 — <headline>"
git push origin v11.4.0
# sync back:
git checkout develop && git merge --no-ff main && git push
```

Tags are **annotated** (`-a`) and immutable. Never move or delete a published tag.

---

## 5. Hotfix flow

For a production-critical bug that cannot wait for the next release.

```bash
git checkout main && git pull
git checkout -b hotfix/ws-reconnect-storm
# minimal fix + test that reproduces the bug
# open PR hotfix/* -> main  (expedited review, CI green)
# after merge + tag (PATCH bump, e.g. v11.3.1):
git checkout develop && git merge --no-ff main && git push
```

Rules:
- A hotfix is the **smallest change** that resolves the incident. No drive-by refactors.
- It **must** include a regression test.
- It is **always** merged back into `develop`.

---

## 6. Rollback strategy

Production issues are resolved by **rolling forward** when safe, and **rolling
back** when not. Order of preference:

1. **Redeploy the previous image tag.** Images are tagged per release
   (`argus-quant:v11.3.0`). Re-point the running service to the last-known-good
   tag — fastest, no code change.
   ```bash
   docker compose pull            # or set IMAGE_TAG=v11.3.0
   docker compose up -d
   ```
2. **Revert the offending commit** on `main`, tag a PATCH, redeploy.
   ```bash
   git revert <sha> && git push           # via PR
   ```
3. **Feature flag off.** Most risky subsystems are flag-gated
   (`AUTO_TRADE_DEMO_ENABLED`, `LIVE_TRADING_API_ENABLED`, `*_ENABLED`).
   Disabling the flag and restarting is often faster than any code change and is
   the **first** lever for a misbehaving optional subsystem.

### Database
- Schema changes ship **expand → migrate → contract** so an older image keeps
  working against the new schema during a rollback window.
- Never write a migration that an N-1 release cannot tolerate within the rollback
  window. Destructive `DROP`/`NOT NULL` changes are split across two releases.

---

## 7. Deployment checklist

Run through this before promoting to production (`main` → deploy).

**Pre-deploy**
- [ ] CI green on the release commit (lint, test, docker-build).
- [ ] Release tagged (`v11.x.y`, annotated) and notes written.
- [ ] `.env` reviewed: `SECRET_KEY`, `DASHBOARD_PASSWORD` set; no debug flags on.
- [ ] `LIVE_TRADING_ENABLED` / `MOCK_EXCHANGE_MODE` verified **intentional**
      (default: live disabled, mock on).
- [ ] DB migration is N-1 tolerant (expand/contract); backup taken.
- [ ] Rollback target identified (previous image tag noted).

**Deploy**
- [ ] Pull/build image for the release tag.
- [ ] `docker compose up -d`; watch `docker compose logs -f bot`.
- [ ] Startup diagnostics show **Binance OK / Database OK / Redis OK / Telegram OK**.

**Post-deploy (smoke)**
- [ ] `/healthz` returns healthy; dashboard `/` and `/admin` render.
- [ ] Scanner emits a heartbeat; WebSocket price stream connected.
- [ ] No error spike in logs for 10 minutes.
- [ ] If a flag was toggled, confirm the subsystem behaves as expected.
- [ ] Merge `main` back into `develop` (post-release sync).

---

## 8. Live-safety changes

Any PR that touches real-order execution — exchange adapters, the live gate,
reconciliation, recovery, emergency-close, or the `LIVE_TRADING_*` /
`MOCK_EXCHANGE_MODE` flags — follows stricter rules:

- Dedicated PR; **never** bundled with unrelated changes.
- Two maintainer approvals.
- Explicit statement in the PR description of the real-money blast radius.
- Default-off: flags ship disabled and are enabled by a separate ops change,
  not by the code merge.

See `LIVE_SAFETY_AUDIT_V2.md` (when present) for the execution-safety invariants.
