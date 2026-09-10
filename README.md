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

## Setup

Requires Python 3.11+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env   # edit as needed — see Configuration below

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
Backtest / Monitor tabs). With zero configuration this runs entirely on
mock data — `MockBrokerClient` (a deterministic seeded synthetic price
series) and `MockLLMAdapter` (a safe canned HOLD response) — so the whole
platform is explorable with no API keys, no broker account, and no cost.

### Running a backtest from the CLI

```bash
python scripts/run_backtest.py RELIANCE --days 180
```

## Configuration

Everything is env-based (`vibetrading/config.py`, documented in
`.env.example`). Nothing requires real credentials to run — every
integration degrades to a mock/no-op when its keys are absent, and says so
loudly in the logs.

- **Execution mode**: `VIBETRADING_EXECUTION_MODE=paper|live`. Paper mode
  is the default and uses `MockBrokerClient`; nothing here defaults you
  into live trading.
- **LLM provider**: set `LLM_DEFAULT_PROVIDER` and the matching `*_API_KEY`
  (Anthropic, OpenAI, Gemini, or OpenRouter — routed through a single
  `LLMAdapter` interface via LiteLLM). No key configured → every agent runs
  against `MockLLMAdapter`.
- **News/chat sources**: each real source (NewsAPI, Twitter, Reddit,
  Telegram, StockTwits, ValuePickr) is off by default
  (`*_SOURCE_ENABLED=false` / `CHAT_SOURCE_*_ENABLED=false`). Enable a
  source only once you've confirmed your use is allowed under that
  platform's terms of service — this is a compliance decision, not a
  technical one.
- **Watchlist**: `WATCHLIST=RELIANCE,TCS,INFY` (comma-separated symbols).
- **Risk limits**: `RISK_MAX_POSITION_SIZE_INR`, `RISK_MAX_PCT_CAPITAL_PER_STOCK`,
  `RISK_MAX_CONCURRENT_POSITIONS`, `RISK_MAX_DAILY_LOSS_INR`,
  `RISK_MANDATORY_STOP_LOSS_PCT`, `RISK_MIN_SIGNAL_CONFIDENCE`,
  `RISK_MAX_TOTAL_EXPOSURE_PCT` — review every one of these before enabling
  live mode; the shipped defaults are reasonable placeholders, not a
  recommendation for your capital.

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
2. **Review every risk limit** in `.env` (`RISK_MAX_POSITION_SIZE_INR`,
   `RISK_MAX_PCT_CAPITAL_PER_STOCK`, `RISK_MAX_CONCURRENT_POSITIONS`,
   `RISK_MAX_DAILY_LOSS_INR`, `RISK_MANDATORY_STOP_LOSS_PCT`,
   `RISK_MIN_SIGNAL_CONFIDENCE`, `RISK_MAX_TOTAL_EXPOSURE_PCT`). The
   shipped defaults are placeholders sized for a hypothetical mid-size
   account — set them to numbers you would be fine losing in the worst
   case, because the worst case is what a risk limit exists for.
3. **Set a real `RISK_TOKEN_SECRET`.** The default
   (`dev-insecure-secret-change-me`) is fine for dev/paper only. Generate
   one with `python -c "import secrets; print(secrets.token_hex(32))"`.
4. **Verify the kill switch is reachable** before you need it: open the
   Risk tab, confirm the kill-switch button flips state, confirm
   `GET /api/risk/state` reflects it. Know this control exists and where it
   is *before* going live, not after something goes wrong.
5. **Get real Dhan credentials.** Set `DHAN_CLIENT_ID` and
   `DHAN_ACCESS_TOKEN`. Populate `Stock.dhan_security_id` for every
   watchlist symbol you intend to trade (see Dhan's published instrument
   master) — `DhanBrokerClient` refuses to trade a symbol it can't resolve
   a security ID for.
6. **Verify the installed `dhanhq` SDK surface** before trusting
   `DhanBrokerClient` with real money: `pip show dhanhq`, then
   `python -c "import dhanhq; help(dhanhq)"` to confirm the method names/
   parameters it wraps still match (SDK surfaces shift between releases —
   see the docstring in `vibetrading/broker/dhan_client.py`).
7. **Flip `VIBETRADING_EXECUTION_MODE=live`.** Restart the app. The mode
   banner in the dashboard header will switch from PAPER to LIVE — confirm
   you see that before you consider it live.
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
see the "Phase 1" through "Phase 7" commits for the reasoning behind each
layer as it was built.

## Known limitations

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
