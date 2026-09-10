# VibeTrading

An AI agent platform for trading Indian equities through the Dhan broker
(DhanHQ Trading APIs). A watchlist of stocks is monitored by specialized
agents — technical indicators, news, and social/forum sentiment — whose
output is synthesized into trade signals, checked by a deterministic Risk
Agent, and (in live mode) executed as real orders on Dhan.

## Architecture

```
VibeTrading UI (Strategy / Risk / Backtest / Monitor)
        -> AI Orchestrator
             -> Research Agent   (news + social/forum sentiment collection)
             -> Strategy Agent   (technical indicators + trend synthesis -> Signal)
             -> Backtest Agent   (replays Strategy logic over historical data)
             -> Risk Agent       (deterministic gatekeeper: limits, stop-loss, exposure, order validation)
             -> Execution Agent  (broker adapter: Dhan now, pluggable for others)
             -> Monitoring Agent (P&L/fills/logs/alerts/kill switch, pushes to UI)
        -> Market Data (via broker), News/Social Data APIs, Strategy Engine (Python)
```

The Risk Agent and Execution Agent are deliberately **not** LLM-driven —
they're deterministic rule engines / API wrappers. Every order placement
(`BrokerClient.place_order`) requires a `RiskApprovalToken` that only
`RiskEngine.approve_and_execute()` can mint, so no code path — live or
paper — can reach the broker without passing through the risk gate first.

| Layer | Module | Notes |
|---|---|---|
| Research Agent | `vibetrading/agents/research/` | News + chat/sentiment collectors, LLM-synthesized combined view |
| Strategy Agent | `vibetrading/agents/strategy/` | Technical indicators + LLM synthesis into a `Signal` |
| Backtest Agent | `vibetrading/agents/backtest/` | Replays `StrategyAgent.synthesize()` unmodified over historical candles |
| Risk Agent | `vibetrading/risk/` | Rule pipeline, kill switch, daily-loss circuit breaker, approval tokens |
| Execution Agent | `vibetrading/broker/` | `BrokerClient` contract; `DhanBrokerClient` + `MockBrokerClient` |
| Orchestrator | `vibetrading/orchestrator/` | APScheduler wiring, live pipeline glue, stop-loss monitor, event bus |
| Monitoring / UI | `vibetrading/api/`, `vibetrading/dashboard/` | FastAPI + WebSocket + Jinja2/HTMX dashboard |
| Settings | `vibetrading/settings/`, `vibetrading/dashboard/routes_settings.py` | DB-backed, encrypted, live-editable configuration (see Configuration below) |

## Setup

Requires Python 3.11+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env   # the defaults are enough to boot — see Configuration below

alembic upgrade head    # create the SQLite database

pytest                  # full test suite, should be green with zero configuration
```

To use the real Dhan broker instead of the mock, also install the optional
extra: `pip install -e ".[dhan]"`.

### Running the app

```bash
uvicorn vibetrading.main:app --reload
```

Then open `http://localhost:8000` for the dashboard (Strategy / Risk /
Backtest / Monitor / **Settings** tabs). With zero configuration this runs
entirely on mock data — `MockBrokerClient` (a deterministic seeded synthetic
price series), `MockLLMAdapter` (a safe canned HOLD response), and a
default RELIANCE/TCS/INFY watchlist seeded into the database on first boot
— so the whole platform is explorable with no API keys, no broker account,
and no cost. Open `/settings` to configure everything else from there.

### Running a backtest from the CLI

```bash
python scripts/run_backtest.py RELIANCE --days 180
```

## Configuration

Almost everything about *how* VibeTrading trades is configured at runtime
from the dashboard's **Settings** page (`/settings`), not `.env` — execution
mode, Dhan credentials, LLM provider + API keys, risk limits, the
watchlist, and news/chat data-source toggles all live in the database and
take effect without editing a file or restarting the process by hand. A
persistent mode control in the top nav (and again on the Settings page)
shows PAPER/LIVE and lets you switch between them. `.env` now only holds
the handful of variables the app needs before it can even open that
database — see `.env.example`: `DATABASE_URL`, `HOST`, `PORT`, `LOG_LEVEL`,
and `APP_SECRETS_KEY` (the encryption key for secrets stored in the
Settings UI). **Setting the old per-domain env vars (`DHAN_ACCESS_TOKEN`,
`ANTHROPIC_API_KEY`, `RISK_MAX_POSITION_SIZE_INR`, etc.) directly in `.env`
no longer has any lasting effect** — on every startup, each of those
fields is reset to either its stored database value or its built-in
default, discarding whatever `.env` says. Use the Settings UI.

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
  evaluation with **no restart** — `RiskEngine` reads the live settings
  singleton on every call.
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
pytest                                  # full suite
pytest tests/unit                       # unit tests only
pytest tests/integration                # integration tests only
ruff check vibetrading/ tests/ scripts/ # lint
```

The Risk Agent (`vibetrading/risk/`) has 100% line coverage — every rule
is table-driven tested (at-limit, over-limit, protective-exit exemptions),
and the engine's ordering, audit-log-before-broker-call, and token
enforcement are all directly tested. `tests/integration/test_orchestrator_paper_mode.py`
drives the real end-to-end pipeline (Research → Technical → Strategy →
Risk → Execution) through several accelerated cycles in PAPER mode and
asserts rows accumulate and every WebSocket event type fires.

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
3. **Set a real risk approval token secret.** On the Settings page's
   Execution & Broker section, set "Risk approval token secret" to a real
   random value — the default (`dev-insecure-secret-change-me`) is fine
   for dev/paper only. Generate one with
   `python -c "import secrets; print(secrets.token_hex(32))"`.
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
see the "Phase 1" through "Phase 13" commits for the reasoning behind each
layer as it was built, including the move from `.env`-only configuration
to the DB-backed Settings UI (Phases 9-13).

## Known limitations

- **`APP_SECRETS_KEY` rotation orphans stored secrets.** Every secret saved
  through the Settings UI (Dhan access token, LLM keys, data-source
  credentials) is encrypted with this key. Changing it after secrets have
  been stored makes them undecryptable — the app falls back to their
  class defaults (effectively "not set") until you re-enter them from
  `/settings`. Set a real value before storing anything you care about;
  don't change it afterward without expecting to re-enter every secret.
- **A fresh database seeds a default watchlist** (RELIANCE/TCS/INFY, no
  Dhan security IDs) and paper mode with the built-in secrets/token
  defaults — both editable immediately from `/settings`, no restart
  required to reach that page.
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
- **No automated CI workflow** is included yet (noted as optional/
  non-blocking in the original plan) — run `pytest` and `ruff check`
  locally before pushing.
