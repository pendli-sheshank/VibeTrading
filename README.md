# VibeTrading

A multi-tenant AI agent platform for trading Indian equities through the
Dhan broker (DhanHQ Trading APIs). Each registered account gets its own
fully isolated Dhan connection, watchlist, settings, and risk state. A
watchlist of stocks is monitored by specialized agents — technical
indicators, news, and social/forum sentiment — whose output is synthesized
into trade signals, checked by a deterministic Risk Agent, and (in live
mode) executed as real orders on Dhan.

## Architecture

```
VibeTrading UI (Strategy / Risk / Backtest / Monitor / Settings)  [per tenant]
        -> AI Orchestrator (one runtime per tenant)
             -> Research Agent   (news + social/forum sentiment collection)
             -> Strategy Agent   (technical indicators + trend synthesis -> Signal)
             -> Backtest Agent   (replays Strategy logic over historical data)
             -> Risk Agent       (deterministic gatekeeper: limits, stop-loss, exposure, order validation)
             -> Execution Agent  (broker adapter: Dhan now, pluggable for others)
             -> Monitoring Agent (P&L/fills/logs/alerts/kill switch, pushes to UI)
        -> Market Data (via broker), News/Social Data APIs, Strategy Engine (Python)

Deployment topology (see Deployment below):
  Web Service  (N replicas, stateless, no lease/scheduler) --\
                                                                +-- Postgres + Redis
  Background Worker (1+, owns every tenant's trading loop)  --/
```

The Risk Agent and Execution Agent are deliberately **not** LLM-driven —
they're deterministic rule engines / API wrappers. Every order placement
(`BrokerClient.place_order`) requires a `RiskApprovalToken` that only
`RiskEngine.approve_and_execute()` can mint, so no code path — live or
paper — can reach the broker without passing through the risk gate first.
This invariant is what the entire distributed-orchestrator design below
exists to preserve across a horizontally-scaled worker fleet: see
[Multi-tenancy & the distributed orchestrator](#multi-tenancy--the-distributed-orchestrator).

| Layer | Module | Notes |
|---|---|---|
| Auth & tenancy | `vibetrading/auth/` | `fastapi-users`, cookie sessions; `tenant_id == user.id` everywhere, no separate tenant table |
| Research Agent | `vibetrading/agents/research/` | News + chat/sentiment collectors, LLM-synthesized combined view |
| Strategy Agent | `vibetrading/agents/strategy/` | Technical indicators + LLM synthesis into a `Signal` |
| Backtest Agent | `vibetrading/agents/backtest/` | Replays `StrategyAgent.synthesize()` unmodified over historical candles |
| Risk Agent | `vibetrading/risk/` | Rule pipeline, kill switch, daily-loss circuit breaker, approval tokens, fencing-token lease check |
| Execution Agent | `vibetrading/broker/` | `BrokerClient` contract; `DhanBrokerClient` + `MockBrokerClient` |
| Orchestrator | `vibetrading/orchestrator/` | Per-tenant APScheduler wiring, distributed lease (`lease.py`), multi-tenant runtime manager, Redis-backed event bus |
| Monitoring / UI | `vibetrading/api/`, `vibetrading/dashboard/` | FastAPI + WebSocket + Jinja2/HTMX dashboard |
| Settings | `vibetrading/settings/`, `vibetrading/dashboard/routes_settings.py` | DB-backed, encrypted, live-editable, per-tenant configuration (see Configuration below) |
| Reliability | `vibetrading/core/reliability.py`, `vibetrading/logging_conf.py` | Circuit breakers + retry around Dhan/LLM calls, structured JSON logs |
| Observability | `vibetrading/observability/`, `vibetrading/api/routers/health.py` | `/metrics` (Prometheus), `/healthz`, `/readyz`, alert webhook |

## Setup

Requires Python 3.11+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env   # the defaults are enough to boot — see Configuration below

alembic upgrade head    # create the SQLite database (dev default) or your configured Postgres one

pytest                  # full test suite, should be green with zero configuration
```

To use the real Dhan broker instead of the mock, also install the optional
extra: `pip install -e ".[dhan]"`.

`DATABASE_URL` defaults to a local SQLite file — fine for a single
developer exploring the platform. Production runs against Postgres (see
[Multi-tenancy & the distributed orchestrator](#multi-tenancy--the-distributed-orchestrator)
and [Deployment](#deployment)); to run against Postgres locally instead,
set `DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/dbname` in
`.env` before running `alembic upgrade head` — the same migrations and
application code run unmodified against either dialect (this is
continuously verified: CI runs the full suite against both, see Testing
below).

### Running the app

```bash
uvicorn vibetrading.api.app:app --reload
```

Then open `http://localhost:8000` — you'll land on `/login`. Register an
account (`/register`); every registered account is its own fully isolated
tenant (`tenant_id == user.id`, see below) with its own watchlist,
settings, and risk state. With zero further configuration each new
account runs entirely on mock data — `MockBrokerClient` (a deterministic
seeded synthetic price series), `MockLLMAdapter` (a safe canned HOLD
response), and a default RELIANCE/TCS/INFY watchlist seeded on
registration — so the whole platform is explorable with no API keys, no
broker account, and no cost. Open `/settings` to configure everything
else, scoped to your account only.

### Running a backtest from the CLI

```bash
python scripts/run_backtest.py RELIANCE --days 180
```

## Multi-tenancy & the distributed orchestrator

Every registered user is a tenant; `tenant_id` is that user's own id, with
no separate tenant/org table. Every tenant-scoped table (`stocks`,
`settings`, `risk_state`, `orders`, `signals`, `positions`, `audit_log`,
`risk_events`, `backtest_runs`, `agent_runs`) carries a `tenant_id` foreign
key, every `vibetrading/persistence/repositories.py` function takes
`tenant_id` as an explicit first argument and filters every query by it,
and every route/service thread it through from the authenticated request
— there is no implicit/global tenant resolution anywhere in the codebase.
A per-tenant `Settings` cache (`vibetrading/settings/cache.py`) replaced
the single-process-singleton design earlier phases of this project used;
`risk_token_secret` is the one exception, kept platform-wide and env-only
since it proves a token was minted by this platform's `RiskEngine`, not a
tenant secret.

**The safety invariant that survives all of this**: every order placement
requires a `RiskApprovalToken`, minted only inside
`RiskEngine.approve_and_execute()`. Once the orchestrator scales beyond
one process, "only one worker is ever actively trading a given tenant" has
to be a durable, DB-enforced guarantee, not an assumption — that's what
`vibetrading/orchestrator/lease.py` is:

- A Postgres `tenant_leases` table (`tenant_id` PK, `worker_id`,
  `fencing_token`, `lease_expires_at`, `heartbeat_at`) with an atomic,
  row-locked (`SELECT ... FOR UPDATE`) acquire-or-renew: a renewal by the
  current owner leaves `fencing_token` unchanged, a takeover (lease free or
  expired) bumps it.
- `RiskEngine.approve_and_execute()` calls `verify_lease()` as its very
  first step, inside the same transaction as the risk-state read and order
  write. A fencing-token mismatch — another worker has since taken over,
  or this worker's lease lapsed — aborts the cycle before minting a token
  or calling the broker. Worst case is a skipped cycle, never a duplicate
  order.
- A durable `used_risk_tokens` table (not just an in-memory set) rejects a
  replayed token id even across a process restart.
- `MultiTenantRuntimeManager` (`vibetrading/orchestrator/manager.py`) runs
  a background lease-renewal loop per worker process, building a scheduler
  fenced with the acquired token for every tenant it currently holds the
  lease for, and stopping the scheduler locally for any tenant it doesn't
  — so a fleet of N replicas never runs N redundant copies of one tenant's
  loop.

This is proven three ways, each stronger than the last:
`tests/unit/test_lease.py` (every state transition),
`tests/integration/test_lease_concurrency.py` (two real racing Postgres
transactions via `asyncio.gather`, asserting exactly one winner), and
`scripts/chaos_test.py` (two real, separate OS processes, one `kill -9`'d
mid-cycle — see [Load & chaos testing](#load--chaos-testing)).

### Upgrading from a pre-auth, single-tenant deployment

This repository has never shipped a version without `tenant_id` — auth
(`fastapi-users`) and the multi-tenant data model landed together, on the
same branch, before any external deployment existed. If you deployed an
*earlier* commit of this project (pre-auth, single global account, no
`users` table) with real data in it, know before you run
`alembic upgrade head`: the `6b74cc33b5df` migration adds `tenant_id` as a
`NOT NULL` column with **no default and no backfill** — by design, since
autogenerate has no way to know which tenant pre-existing rows should
belong to, and guessing silently felt worse than failing loudly. Run it
against a database that already has rows in `stocks`/`orders`/`signals`/
etc. and it will fail outright (a `NOT NULL` column can't be added without
a value for existing rows, on either SQLite or Postgres).

The safe procedure, if this applies to you:

1. **Back this up first.** This path is not exercised by this repo's own
   test suite (every test starts from an empty schema) — treat it as a
   template to adapt and rehearse against a copy of your data, not a
   command to run against production directly.
2. Run migrations up to (not past) auth: `alembic upgrade fd58ea0e93ca`.
3. Register your account through the running app (`/register`) — this
   becomes the tenant every pre-existing row will be assigned to. Note its
   id (a fresh `users` table's first row is normally `id=1`; confirm with
   `SELECT id FROM users WHERE email = '...'`).
4. Manually backfill each tenant-scoped table before letting the shipped
   migration add its `NOT NULL` constraint + FK, e.g.:
   ```sql
   ALTER TABLE stocks ADD COLUMN tenant_id INTEGER;
   UPDATE stocks SET tenant_id = 1;  -- the id from step 3
   -- repeat for orders, signals, positions, audit_log, risk_events,
   -- backtest_runs, agent_runs, app_settings (composite PK -- see the
   -- migration file), risk_state (tenant_id becomes its sole PK)
   ```
   Then run `alembic upgrade head` — it will now find every column already
   present with real values and (for the columns it still owns adding)
   simply add the `NOT NULL` constraint and FK on top, since the app's
   migration files are idempotent about columns that already exist... in
   practice, the more robust path is to adapt `6b74cc33b5df`'s own
   `upgrade()` locally (drop the ones you've pre-backfilled, or set
   `server_default='1'` temporarily) rather than fighting a generated
   migration file — treat the SQL above as the backfill *values* you need,
   not a drop-in replacement for careful, tested execution against your
   own schema state.
5. Everything that account owned before this upgrade — watchlist,
   settings, order history — is preserved, now scoped to tenant 1.

If you don't have pre-existing production data, none of this applies:
`alembic upgrade head` against a fresh database, as in Setup above, just
works.

## Configuration

Almost everything about *how* your account trades is configured at
runtime from the dashboard's **Settings** page (`/settings`), not `.env`
— execution mode, Dhan credentials, LLM provider + API keys, risk limits,
the watchlist, and news/chat data-source toggles all live in the
database, scoped to your account only, and take effect without editing a
file or restarting the process by hand. A persistent mode control in the
top nav (and again on the Settings page) shows PAPER/LIVE and lets you
switch between them. `.env` holds the operational, platform-wide
variables every account shares — see `.env.example` for the full,
documented list: `DATABASE_URL` (+ pool sizing), `HOST`/`PORT`/`LOG_LEVEL`,
`WORKER_ROLE` (see Deployment), `REDIS_URL` (optional, see Multi-tenancy),
`ALERT_WEBHOOK_URL` (optional, see Observability), `APP_SECRETS_KEY`
(encrypts every tenant's stored secrets), `AUTH_SECRET_KEY` (signs every
login session), and `RISK_TOKEN_SECRET` (platform-wide, not per-tenant —
proves a `RiskApprovalToken` was minted by this platform's `RiskEngine`).
**Setting a per-account field's old-style env var (`DHAN_ACCESS_TOKEN`,
`ANTHROPIC_API_KEY`, `RISK_MAX_POSITION_SIZE_INR`, etc.) directly in `.env`
has no lasting effect** — on every startup, each of those fields is reset
to either its stored per-tenant database value or its built-in default,
discarding whatever `.env` says. Use the Settings UI.

Settings page sections, each independently saved:

- **Execution & Broker** — the PAPER/LIVE mode control (see below), Dhan
  client ID / access token, and the orchestrator's scheduler toggle +
  agent intervals. Nothing here defaults you into live trading; a fresh
  install starts in paper mode with `MockBrokerClient`.
- **Watchlist** — add/edit/remove stocks directly, including each one's
  Dhan security ID (needed before `DhanBrokerClient` can trade it — see
  Dhan's published instrument master). A fresh database seeds
  RELIANCE/TCS/INFY (no security IDs set) so paper mode works immediately;
  nothing here trades live until you add a security ID and go live.
- **LLM** — provider (Anthropic/OpenAI/Gemini/OpenRouter, routed through
  one `LLMAdapter` interface via LiteLLM) + API key + optional per-agent
  model overrides. No key configured → every agent runs against
  `MockLLMAdapter`.
- **Risk Limits** — `max_position_size_inr`, `max_pct_capital_per_stock`,
  `max_concurrent_positions`, `max_daily_loss_inr`, `mandatory_stop_loss_pct`,
  `min_signal_confidence`, `max_total_exposure_pct`. Review every one of
  these before enabling live mode; the shipped defaults are reasonable
  placeholders, not a recommendation for your capital. Unlike every other
  section, a risk-limit change is enforced on the *very next* trade
  evaluation with **no restart** — `RiskEngine` reads your account's live,
  per-tenant settings on every call.
- **Data Sources** — each real news/chat source (NewsAPI, Twitter, Reddit,
  Telegram, StockTwits, ValuePickr) is off by default. Enable one only
  once you've confirmed your use is allowed under that platform's terms of
  service — this is a compliance decision, not a technical one.

Every secret field (Dhan access token, LLM keys, data-source credentials)
is encrypted at rest and never round-tripped back into the page — you'll
see a masked "•••• (configured)" / "(not set)" placeholder, and an
explicit "Clear" checkbox is the only way to remove a stored credential;
leaving the field blank on save always means "leave it unchanged."

**Switching to live mode requires explicit confirmation, enforced by the
server, not just the UI**: clicking "Switch to live" expands an inline
panel showing your current risk limits and a "Confirm: switch to LIVE"
button; `POST /settings/execution-mode/live` only flips the mode when that
confirmation was actually submitted, and returns `400` leaving the mode
unchanged otherwise. Switching back to paper is always one click, no
confirmation needed.

Saving Execution & Broker, Watchlist, LLM, or Data Sources settings
restarts the orchestrator's broker + scheduler in the background (an
in-flight job is always drained first, never cancelled mid-flight) so the
change takes effect immediately; a Risk Limits-only save skips this,
since those already apply live. If a save can't be applied — a typo'd
credential, the `dhanhq` extra not being installed — the *previous*,
working configuration keeps running untouched and the Settings page
reports what went wrong, rather than leaving the app without a broker.

## Testing

```bash
pytest                                  # full suite (SQLite by default)
pytest tests/unit                       # unit tests only
pytest tests/integration                # integration tests only
ruff check vibetrading/ tests/ scripts/ # lint
```

Point the same suite at a real Postgres instance instead (this is exactly
what CI's `test-postgres` job does, and what catches dialect-specific
issues — missing FK enforcement, timezone-naive-vs-aware timestamps, real
row-locking — that SQLite's single shared connection can't exercise):

```bash
export TEST_DATABASE_URL="postgresql+asyncpg://user:pass@localhost:5432/vibetrading_test"
alembic upgrade head   # against that same database, if you also want to prove migrations apply cleanly
pytest
```

The Risk Agent (`vibetrading/risk/`) has 100% line coverage — every rule
is table-driven tested (at-limit, over-limit, protective-exit exemptions),
and the engine's ordering, audit-log-before-broker-call, and token
enforcement are all directly tested. `tests/integration/test_orchestrator_paper_mode.py`
drives the real end-to-end pipeline (Research → Technical → Strategy →
Risk → Execution) through several accelerated cycles in PAPER mode and
asserts rows accumulate and every WebSocket event type fires.
`tests/integration/test_lease_concurrency.py` fires real concurrent
Postgres transactions (`asyncio.gather`) at the distributed-lease
acquisition path and asserts exactly one winner each time — skipped
automatically under SQLite, which has no real cross-connection locking to
prove anything against.

CI (`.github/workflows/ci.yml`) runs three jobs on every PR: `test`
(lint + the SQLite tier, with a `redis:7` service for the cross-worker
fan-out tests), `test-postgres` (a `postgres:16` service, `alembic upgrade
head` against it, then the full suite again), and — on a push to `main`
only, gated on both passing — `docker-publish`, which builds and pushes
the image to `ghcr.io/<repo>:latest` and `:<sha>`.

## Load & chaos testing

Two scripts validate the production-readiness claims above for real,
beyond what a pytest assertion can (see `scripts/load_test.py` and
`scripts/chaos_test.py` for full docstrings):

**`scripts/load_test.py`** — simulates many tenants concurrently against a
*running* server over real HTTP. Each simulated tenant gets its own
cookie-jar client (so cross-tenant bleed is structurally impossible, not
just untested), registers (or logs back in, on a rerun), sets its kill
switch to a value derived from its own tenant index, then repeatedly hits
a mix of dashboard/API endpoints for a fixed duration — checking on every
`/api/risk/state` read that it still sees only *its own* kill-switch
value. Reports p50/p95/p99 latency per endpoint and fails loudly on any
error-rate, latency, or cross-tenant isolation violation.

```bash
uvicorn vibetrading.api.app:app &
python scripts/load_test.py --base-url http://127.0.0.1:8000 --tenants 5 --duration 30
```

`--tenants` defaults low deliberately: `/login` and `/register` are
rate-limited per source IP (a real anti-brute-force measure, not a script
limitation — see Configuration), and every simulated tenant here shares
one IP. The script's own backoff waits out a 429 rather than failing on
one; a larger `--tenants` still works, just takes longer to fully onboard.

**`scripts/chaos_test.py`** — proves the distributed-lease safety
invariant survives a real `kill -9`, not just an in-process test. It spawns
two real OS processes racing for one tenant's lease against a real
Postgres database, each running the exact `RiskEngine.approve_and_execute()`
call chain the real scheduler drives; the parent then SIGKILLs whichever
one currently holds the lease, mid-cycle, with no chance to release it
gracefully, and verifies afterward: zero duplicate orders, zero approved-
but-never-committed orders, and a real fencing-token takeover after the
kill (proving the survivor actually noticed and took over, not that it
held the lease the whole time).

```bash
python scripts/chaos_test.py --database-url postgresql+asyncpg://user:pass@localhost:5432/vibetrading_chaos
```

Needs a real Postgres instance (a throwaway database — the script resets
its schema on every run) — SQLite has no real cross-process row locking
for this to prove anything against. `--lease-ttl` is kept short (3s
default, vs. production's 90s) so a takeover happens within the script's
own runtime rather than requiring a multi-minute wait.

## Deployment

`Dockerfile` builds one multi-stage image (non-root runtime user) that
serves both roles in the plan's Web/Worker split — `render.yaml` is a
[Render Blueprint](https://render.com/docs/blueprint-spec) wiring them
together, and is the reference deployment topology this section describes
(the same image works on any container platform that can run two service
types against shared Postgres + Redis).

- **Web Service** (`WORKER_ROLE=web`, horizontally scaled): serves the
  dashboard/API, runs `uvicorn` (the image's default `CMD`). Never
  attempts lease acquisition and never runs a tenant's scheduler
  (`MultiTenantRuntimeManager(manage_leases=False)`) — a request here
  still gets a working broker connection (dashboard reads need that), just
  no autonomous trading loop. `preDeployCommand: alembic upgrade head`
  runs migrations exactly once per deploy, before traffic reaches a new
  instance — never baked into the container's own startup, which would
  race N web replicas against each other on every restart.
- **Background Worker** (`WORKER_ROLE=worker`, `scripts/run_worker.py` as
  the `dockerCommand`): no HTTP server at all. Owns every tenant's
  autonomous trading loop, competing for each tenant's lease like any
  other worker in the fleet. A graceful SIGTERM drains every tenant it
  owns (via the existing `OrchestratorScheduler.shutdown_gracefully()`)
  and releases its leases before exiting, rather than leaving the next
  owner to wait out the full TTL.
- Both share one Postgres database (`vibetrading-db`) and one Redis
  instance (`vibetrading-redis`) — Redis is what lets a WebSocket
  connected to one Web Service replica see an event a scheduler cycle on
  the Background Worker just published (`orchestrator/event_bus.py` +
  `redis_bus.py`); it's optional and degrades to no live push (never an
  error) if unset, since nothing durable depends on it.

`render.yaml` describes this topology; it does not provision an actual
Render account, DNS, or production secrets (`DHAN_ACCESS_TOKEN` isn't even
templated there — Dhan credentials are per-tenant, set from each account's
own `/settings`) — that's a deliberate action to take when you're ready,
not something this repo does on its own.

## Observability

- **`/healthz`** — pure liveness, no dependencies. A DB outage should show
  up as not-ready, not dead, so a load balancer doesn't restart every
  replica during a transient DB blip.
- **`/readyz`** — DB reachability and migration currency, checked and
  reported independently (a missing/stale `alembic_version` table means
  "migration state unknown," not "database down"). Returns 503 when
  either check fails.
- **`/metrics`** — Prometheus text format (`prometheus-fastapi-
  instrumentator` + custom counters/histograms: `orders_placed_total`,
  `risk_rejections_total`, `fencing_aborts_total`,
  `token_replay_rejections_total`, `lease_acquisition_failures_total`,
  `job_duration_seconds`, `circuit_breaker_opens_total`). Deliberately
  aggregate-only, never labeled by `tenant_id` or anything else with
  unbounded cardinality — per-tenant drill-down is what the structured
  logs below are for.
- **Structured JSON logs** — every line carries `tenant_id` and
  `request_id` via contextvars (`vibetrading/logging_conf.py`), bound once
  per request/job rather than threaded through every function signature.
- **Circuit breakers** (`vibetrading/core/reliability.py`) wrap every
  outbound Dhan/LLM call except `place_order` (not safe to blindly retry —
  see its own docstring) with `tenacity` retries + an open/closed/half-open
  breaker per Dhan client id / LLM provider, so one tenant's broken
  credentials or a provider outage can't cascade into hung jobs for
  everyone else. Opening one fires an alert (`observability/alerts.py`) —
  always a WARNING log, and a webhook POST if `ALERT_WEBHOOK_URL` is set.

## Enabling live trading — read this before you flip the switch

Live mode places **real orders with real money and no human approval
step**. The execution autonomy is intentional (see the project's design
decisions), which is exactly why the Risk Agent sits structurally between
every signal and the broker — but that only helps if it's configured
correctly. Do not skip steps here.

1. **Run in paper mode first, for real.** Let the platform run against
   `MockBrokerClient` for a period you're comfortable with. Watch the
   Strategy tab's signal history and the Monitor tab's audit log. A
   backtest (`/backtest` or `scripts/run_backtest.py`) is a sanity check on
   the technical half of the strategy, not a substitute for watching it run.
2. **Review every risk limit** on the Settings page's Risk Limits section
   (max position size, max % capital per stock, max concurrent positions,
   max daily loss, mandatory stop-loss %, min signal confidence, max total
   exposure %). The shipped defaults are placeholders sized for a
   hypothetical mid-size account — set them to numbers you would be fine
   losing in the worst case, because the worst case is what a risk limit
   exists for. These apply on your very next save, with no restart needed.
3. **Set a real risk approval token secret.** This is a platform-wide
   operator setting, not per-account — set `RISK_TOKEN_SECRET` in `.env`
   (or your deployment's environment) to a real random value before
   *any* tenant goes live; the default (`dev-insecure-secret-change-me`)
   is fine for dev/paper only. Generate one with
   `python -c "import secrets; print(secrets.token_hex(32))"`, and restart
   the app (or redeploy) for it to take effect — unlike the per-tenant
   Settings page fields, this one isn't hot-reloadable.
4. **Verify the kill switch is reachable** before you need it: open the
   Risk tab, confirm the kill-switch button flips state, confirm
   `GET /api/risk/state` reflects it. Know this control exists and where it
   is *before* going live, not after something goes wrong.
5. **Get real Dhan credentials.** On the Settings page's Execution &
   Broker section, enter your Dhan client ID and access token. On the
   Watchlist section, populate the Dhan security ID for every watchlist
   symbol you intend to trade (see Dhan's published instrument master) —
   `DhanBrokerClient` refuses to trade a symbol it can't resolve a
   security ID for.
6. **Verify the installed `dhanhq` SDK surface** before trusting
   `DhanBrokerClient` with real money: `pip show dhanhq`, then
   `python -c "import dhanhq; help(dhanhq)"` to confirm the method names/
   parameters it wraps still match (SDK surfaces shift between releases —
   see the docstring in `vibetrading/broker/dhan_client.py`).
7. **Switch to live from the mode control** (top nav, or the Settings
   page's Execution & Broker section). Clicking "Switch to live" expands
   an inline confirmation showing your current risk limits — review them
   one more time, then click "Confirm: switch to LIVE". The mode badge
   will switch from PAPER to LIVE — confirm you see that before you
   consider it live. If the switch reports it couldn't be applied (e.g. a
   credential problem), the app is still safely running in its previous
   configuration; fix the setting and try again.
8. **Watch the first live cycle closely.** Keep the Monitor and Risk tabs
   open. The audit log records every risk check each signal passed or
   failed, in order — if anything looks wrong, use the kill switch
   (`halt_new_orders` stops new entries while still allowing the
   stop-loss monitor to protect existing positions; `halt_all` is the
   hard stop).

None of this is optional ceremony — each step closes a real gap between
"the code is correct" and "this is safe to run with real money."

## Project structure

See the module table above; each package has a short module-level or
class-level docstring explaining its role. The original implementation
plan (phased build order, design rationale) is preserved in git history —
see the "Phase 1" through "Phase 25" commits for the reasoning behind each
layer as it was built: single-tenant foundation and the DB-backed Settings
UI (Phases 1-14), then the production-ready multi-tenant SaaS rebuild —
auth, tenancy, Postgres, the distributed orchestrator, cross-worker
realtime fan-out, reliability/observability hardening, and CI/CD/deployment
(Phases 15-25).

## Known limitations

- **`APP_SECRETS_KEY` rotation orphans stored secrets.** Every secret saved
  through the Settings UI (Dhan access token, LLM keys, data-source
  credentials) is encrypted with this key. Changing it after secrets have
  been stored makes them undecryptable — the app falls back to their
  class defaults (effectively "not set") until you re-enter them from
  `/settings`. Set a real value before storing anything you care about;
  don't change it afterward without expecting to re-enter every secret.
- **A freshly registered account seeds a default watchlist**
  (RELIANCE/TCS/INFY, no Dhan security IDs) and paper mode with the
  built-in secrets/token defaults — both editable immediately from
  `/settings`, no restart required to reach that page.
- **Backtests use technical signal only.** No historical archive of
  news/sentiment exists to replay, so `BacktestEngine` synthesizes signals
  from technical output alone. Live confidence (which also weighs
  Research) will differ from backtested confidence for the same setup.
- **Real chat/news sources are best-effort integrations**, not
  production-hardened clients: `TwitterChatSource` and
  `NewsAPISource` use documented REST endpoints but aren't tested against
  live accounts in this repo; `RedditChatSource` uses the unauthenticated
  public endpoint (more rate-limited than OAuth); `TelegramChatSource` is a
  stub pending an authenticated MTProto client; `ValuePickrChatSource`
  depends on an undocumented Discourse search endpoint. All are off by
  default.
- **Dhan's realized daily P&L** isn't cheaply queryable per fill, so
  `DhanBrokerClient.place_order` reports `realized_pnl=0.0` — meaning the
  daily-loss circuit breaker won't see live-mode losses until a dedicated
  reconciliation job (polling positions/funds) is added. `MockBrokerClient`
  computes this correctly for paper mode.
- **Postgres Row-Level Security is not yet enabled.** The plan calls for
  RLS policies (keyed off a per-request session GUC) as defense-in-depth
  on top of the tenant-scoped repository layer, which is today's primary
  (and, on its own, already-verified-by-tests) isolation mechanism — every
  query filters by `tenant_id` explicitly, no implicit/global resolution
  anywhere. RLS is deferred, not required for correctness today.
- **Rebalancing across the worker fleet is not implemented.** A tenant's
  lease is acquired greedily by whichever worker first calls
  `get_or_start()` for it (or picks it up on the periodic
  `_discover_new_tenants()` sweep) and held until that worker releases or
  loses it — there's no active load-spreading if the fleet size changes.
  This only affects even load distribution, never correctness: the
  fencing-token check inside `RiskEngine.approve_and_execute()` is what
  actually guarantees exactly-one-owner, independent of how leases happen
  to be distributed.
- **The load and chaos test scripts are operator-run, not part of CI.**
  `scripts/load_test.py` needs a running server and `scripts/chaos_test.py`
  needs a real Postgres instance with room to `kill -9` a process — neither
  fits a normal PR-gated CI job. Run them by hand before a real production
  rollout and after any change to the lease/fencing or auth-rate-limiting
  code paths (see [Load & chaos testing](#load--chaos-testing)).
