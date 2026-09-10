from __future__ import annotations

from prometheus_client import Counter, Histogram

# Every metric here is aggregate-only, process-wide -- never labeled by
# tenant_id (or anything else with unbounded, user-controlled cardinality,
# like a Dhan client_id or LLM API key). A Prometheus series-per-label-
# combination is expensive at scale; per-tenant drill-down belongs in
# structured logs (see logging_conf.py's tenant_id-tagged JSON records),
# which are built for exactly that kind of high-cardinality lookup.

orders_placed_total = Counter(
    "vibetrading_orders_placed_total",
    "Orders successfully placed through RiskEngine.approve_and_execute().",
)

risk_rejections_total = Counter(
    "vibetrading_risk_rejections_total",
    "Signals rejected by a risk rule, labeled by the rule that rejected them.",
    ["rule"],
)

job_duration_seconds = Histogram(
    "vibetrading_job_duration_seconds",
    "Scheduler job cycle duration, labeled by job type.",
    ["job_type"],
)

lease_acquisition_failures_total = Counter(
    "vibetrading_lease_acquisition_failures_total",
    "Lease acquire-or-renew attempts that did not win/keep ownership (another worker holds it, or lost a race).",
)

fencing_aborts_total = Counter(
    "vibetrading_fencing_aborts_total",
    "approve_and_execute() calls aborted because the caller's lease fencing_token was stale or lost.",
)

token_replay_rejections_total = Counter(
    "vibetrading_token_replay_rejections_total",
    "approve_and_execute() calls aborted because the minted token_id had already been consumed.",
)

circuit_breaker_opens_total = Counter(
    "vibetrading_circuit_breaker_opens_total",
    "Circuit breaker open transitions, labeled by integration kind (dhan/llm/other) -- never by the "
    "specific per-account/per-provider circuit name, which would blow up cardinality.",
    ["kind"],
)


def circuit_kind(circuit_name: str) -> str:
    """Maps a CircuitBreaker's name (e.g. "dhan:<client_id>",
    "llm:<provider>") to a small, bounded label value for
    circuit_breaker_opens_total."""
    prefix = circuit_name.split(":", 1)[0]
    return prefix if prefix in {"dhan", "llm"} else "other"
