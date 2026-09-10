#!/usr/bin/env python
"""Phase 25 load test: simulates many tenants hitting a running VibeTrading
server concurrently, and proves two things at once -- the server holds up
under N-tenant concurrent load, and per-tenant isolation holds *under* that
load, not just in a single-request test.

Each simulated tenant gets its own httpx.AsyncClient (so its own cookie jar
-- no cross-tenant session bleed is possible even if the script had a bug).
Per tenant, the script:
  1. Logs in (registering first if the account doesn't exist yet -- reruns
     against the same server reuse previously-registered tenants instead of
     re-registering, which matters since /register is rate-limited).
  2. Sets its kill switch to a value derived deterministically from its own
     tenant index (even tenants -> active, odd -> inactive).
  3. Repeatedly, for --duration seconds: hits a mix of authenticated GET
     endpoints (dashboard pages + read-only API routes) and re-checks that
     its own /api/risk/state still reports exactly the kill-switch value it
     set in step 2 -- any other value means another tenant's write leaked
     into this one's read, a cross-tenant isolation failure worth treating
     as seriously as a latency regression.

Does NOT touch order placement or the risk-approval path at all -- that
safety invariant (zero duplicate/dropped orders under real process kill) is
what scripts/chaos_test.py proves instead. This script is about the API/
dashboard tier's horizontal-scale story (Phase 15-19's half of the plan),
not the distributed-orchestrator half.

Usage:
    python scripts/load_test.py --base-url http://127.0.0.1:8000 --tenants 20 --duration 30

Needs a real running server (uvicorn, or a deployed Web Service) -- this
drives it over real HTTP, not an in-process ASGI transport, so it also
exercises whatever's actually in front of it (a reverse proxy, TLS, ...).
"""

from __future__ import annotations

import argparse
import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field

import httpx

READ_ENDPOINTS = [
    "/",
    "/risk",
    "/monitor",
    "/backtest",
    "/api/watchlist",
    "/api/risk/state",
    "/api/monitor/positions",
    "/api/monitor/funds",
    "/api/monitor/orders",
    "/api/monitor/audit-log",
    "/healthz",
]


@dataclass
class TenantResult:
    tenant_index: int
    email: str
    registered: bool = False
    logged_in: bool = False
    isolation_violations: int = 0
    request_errors: int = 0
    requests_made: int = 0
    latencies_by_path: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))


async def _login_or_register(
    client: httpx.AsyncClient, base_url: str, email: str, password: str, max_attempts: int = 10
) -> bool:
    """/login and /register are rate-limited per source IP (10/min, 5/min --
    see auth/routes.py) as a real anti-brute-force measure, not a load-test
    inconvenience to route around -- simulating many tenants from one
    machine looks, correctly, like a single noisy client to that limiter.
    The backoff below waits it out rather than treating a 429 as a hard
    failure, capped so a burst of --tenants well beyond the per-minute
    limit still eventually gets everyone onboarded instead of spinning for
    minutes on one exponential sleep."""
    for attempt in range(max_attempts):
        resp = await client.post(
            f"{base_url}/login", data={"username": email, "password": password}, follow_redirects=False
        )
        if resp.status_code in (302, 303):
            return True
        if resp.status_code == 429:
            await asyncio.sleep(min(2**attempt, 8))
            continue
        # Not a recognized account yet -- register, then retry login.
        reg_resp = await client.post(f"{base_url}/register", data={"email": email, "password": password})
        if reg_resp.status_code == 429:
            await asyncio.sleep(min(2**attempt, 8))
            continue
        # A "this email is already registered but the password is wrong"
        # case would loop here uselessly -- fine for a throwaway load-test
        # tenant pool, since the point is a reachable account, not this
        # exact password surviving forever.
    resp = await client.post(
        f"{base_url}/login", data={"username": email, "password": password}, follow_redirects=False
    )
    return resp.status_code in (302, 303)


async def _run_tenant(
    base_url: str, tenant_index: int, email_prefix: str, password: str, duration: float, interval: float
) -> TenantResult:
    result = TenantResult(tenant_index=tenant_index, email=f"{email_prefix}{tenant_index}@loadtest.example.com")
    async with httpx.AsyncClient(timeout=15.0) as client:
        result.logged_in = await _login_or_register(client, base_url, result.email, password)
        if not result.logged_in:
            result.request_errors += 1
            return result
        result.registered = True

        expected_kill_switch = tenant_index % 2 == 0
        ks_resp = await client.post(f"{base_url}/api/risk/kill-switch", json={"active": expected_kill_switch})
        if ks_resp.status_code != 200:
            result.request_errors += 1

        deadline = time.monotonic() + duration
        endpoint_cycle = 0
        while time.monotonic() < deadline:
            path = READ_ENDPOINTS[endpoint_cycle % len(READ_ENDPOINTS)]
            endpoint_cycle += 1
            start = time.monotonic()
            try:
                resp = await client.get(f"{base_url}{path}")
                elapsed = time.monotonic() - start
                result.requests_made += 1
                result.latencies_by_path[path].append(elapsed)
                if resp.status_code >= 400:
                    result.request_errors += 1
                elif path == "/api/risk/state" and resp.json().get("kill_switch_active") != expected_kill_switch:
                    result.isolation_violations += 1
            except httpx.HTTPError:
                result.requests_made += 1
                result.request_errors += 1
            await asyncio.sleep(interval)

    return result


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * pct))
    return ordered[index]


def _print_summary(results: list[TenantResult], error_rate_threshold: float, p95_threshold_s: float) -> bool:
    logged_in = [r for r in results if r.logged_in]
    total_requests = sum(r.requests_made for r in results)
    total_errors = sum(r.request_errors for r in results)
    total_isolation_violations = sum(r.isolation_violations for r in results)

    by_path: dict[str, list[float]] = defaultdict(list)
    for r in results:
        for path, latencies in r.latencies_by_path.items():
            by_path[path].extend(latencies)

    print(f"\nTenants simulated: {len(results)}  (logged in: {len(logged_in)})")
    print(f"Total requests: {total_requests}  errors: {total_errors}  isolation violations: {total_isolation_violations}")
    print(f"\n{'endpoint':<24}{'n':>8}{'p50 ms':>10}{'p95 ms':>10}{'p99 ms':>10}{'max ms':>10}")
    worst_p95 = 0.0
    for path in READ_ENDPOINTS:
        latencies = by_path.get(path, [])
        if not latencies:
            continue
        p50, p95, p99 = (_percentile(latencies, p) for p in (0.5, 0.95, 0.99))
        worst_p95 = max(worst_p95, p95)
        print(
            f"{path:<24}{len(latencies):>8}{p50 * 1000:>10.1f}{p95 * 1000:>10.1f}"
            f"{p99 * 1000:>10.1f}{max(latencies) * 1000:>10.1f}"
        )

    error_rate = (total_errors / total_requests) if total_requests else 1.0
    ok = True
    if len(logged_in) < len(results):
        print(f"\nFAIL: {len(results) - len(logged_in)} tenant(s) never got a session (register/login failed).")
        ok = False
    if total_isolation_violations:
        print(f"\nFAIL: {total_isolation_violations} cross-tenant isolation violation(s) -- a tenant read another tenant's kill-switch state.")
        ok = False
    if error_rate > error_rate_threshold:
        print(f"\nFAIL: error rate {error_rate:.2%} exceeds threshold {error_rate_threshold:.2%}.")
        ok = False
    if worst_p95 > p95_threshold_s:
        print(f"\nFAIL: worst endpoint p95 latency {worst_p95 * 1000:.1f}ms exceeds threshold {p95_threshold_s * 1000:.0f}ms.")
        ok = False
    if ok:
        print(f"\nPASS: {len(results)} tenants sustained, {error_rate:.2%} error rate, zero isolation violations.")
    return ok


async def _main_async(args: argparse.Namespace) -> int:
    print(f"Simulating {args.tenants} tenants against {args.base_url} for {args.duration}s each ...")
    tasks = [
        _run_tenant(args.base_url, i, args.email_prefix, args.password, args.duration, args.interval)
        for i in range(args.tenants)
    ]
    results = await asyncio.gather(*tasks)
    ok = _print_summary(list(results), args.max_error_rate, args.max_p95_ms / 1000.0)
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--tenants",
        type=int,
        default=5,
        help=(
            "Default kept low deliberately: /login and /register are "
            "rate-limited per source IP, and every simulated tenant here "
            "shares one IP. A larger --tenants still works (the onboarding "
            "backoff waits out 429s) but takes proportionally longer to "
            "get everyone logged in before the timed load phase starts."
        ),
    )
    parser.add_argument("--duration", type=float, default=30.0, help="seconds each simulated tenant keeps making requests")
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between one tenant's requests")
    parser.add_argument("--email-prefix", default="loadtest-tenant-")
    parser.add_argument("--password", default="loadtest-password-123")
    parser.add_argument("--max-error-rate", type=float, default=0.01)
    parser.add_argument("--max-p95-ms", type=float, default=500.0)
    args = parser.parse_args()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
