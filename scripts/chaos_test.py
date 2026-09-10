#!/usr/bin/env python
"""Phase 25 chaos test: proves Phase 20's distributed lease + fencing-token
mechanism survives a real `kill -9` of a worker process mid-cycle, not just
the in-process asyncio.gather races tests/integration/test_lease_concurrency.py
already covers.

Two real OS processes (this script re-execs itself in "worker" mode) race
for the same tenant's lease against a real Postgres database, each
repeatedly: acquire-or-renew the lease, and if it currently owns it, run
RiskEngine.approve_and_execute() end to end (mint a token, "place" an order
via MockBrokerClient, commit) for the next stock in a fixed pool -- the
exact call chain the real orchestrator/scheduler.py drives, minus the
scheduling and market-data/LLM layers around it, which aren't what's being
tested here. The orchestrator process then SIGKILLs whichever of the two
currently holds the lease -- not a graceful shutdown, no chance to release
it -- and waits for the survivor to take over.

Pass criteria (the plan's Phase 20 DoD, run for real this time):
  - zero duplicate orders: every order_id a worker's own log says it got
    approval for exists exactly once in the orders table.
  - zero silently-dropped approved orders: every order_id logged as
    approved is actually present in the orders table.
  - a real takeover happened: more than one fencing_token value was
    observed being held, proving the kill actually forced a handover
    rather than the survivor having owned it the whole time.

Usage (needs a real Postgres instance -- SQLite has no real cross-process
row locking, see test_lease_concurrency.py's own skip condition):
    python scripts/chaos_test.py \\
        --database-url postgresql+asyncpg://vibetrading:vibetrading@localhost:5432/vibetrading_chaos
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

CHAOS_TENANT_ID = 999
STOCK_POOL = [f"CHAOS{i}" for i in range(30)]


def _build_config():
    from vibetrading.core.enums import KillSwitchMode
    from vibetrading.risk.config import RiskConfig

    return RiskConfig(
        max_position_size_inr=100_000,
        max_pct_capital_per_stock=0.5,
        max_concurrent_positions=len(STOCK_POOL),
        max_daily_loss_inr=10_000_000,
        mandatory_stop_loss_pct=0.03,
        min_signal_confidence=0.5,
        max_total_exposure_pct=0.95,
        kill_switch_mode=KillSwitchMode.HALT_NEW_ORDERS,
    )


async def _reset_and_seed(database_url: str) -> None:
    from vibetrading.persistence.orm_models import Base, UserORM

    engine = create_async_engine(database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(UserORM(id=CHAOS_TENANT_ID, email="chaos@test.local", hashed_password="x"))
        await session.commit()
    await engine.dispose()


async def _current_lease_holder(database_url: str) -> str | None:
    from vibetrading.persistence.orm_models import TenantLeaseORM

    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        lease = await session.get(TenantLeaseORM, CHAOS_TENANT_ID)
    await engine.dispose()
    return lease.worker_id if lease else None


async def _run_worker(worker_id: str, database_url: str, duration: float, results_path: str, lease_ttl: float) -> None:
    from vibetrading.broker.mock_client import MockBrokerClient
    from vibetrading.core.enums import ActionType, SignalSource
    from vibetrading.core.models import Signal, Stock
    from vibetrading.orchestrator.lease import acquire_or_renew_lease
    from vibetrading.risk.engine import RiskEngine

    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    broker = MockBrokerClient(seed=abs(hash(worker_id)) % 10_000, initial_funds=50_000_000.0)
    config = _build_config()

    deadline = time.monotonic() + duration
    cycle = 0
    with open(results_path, "a") as f:  # noqa: ASYNC230 -- a tiny append every few seconds, not a library
        while time.monotonic() < deadline:
            cycle += 1
            symbol = STOCK_POOL[cycle % len(STOCK_POOL)]
            try:
                async with session_factory() as session:
                    fencing_token = await acquire_or_renew_lease(
                        session, CHAOS_TENANT_ID, worker_id, ttl_seconds=lease_ttl
                    )
                    await session.commit()
                    if fencing_token is None:
                        f.write(json.dumps({"worker_id": worker_id, "cycle": cycle, "held_lease": False}) + "\n")
                        f.flush()
                    else:
                        risk_engine = RiskEngine(
                            broker=broker, tenant_id=CHAOS_TENANT_ID, config=config, fencing_token=fencing_token
                        )
                        signal = Signal(
                            stock_symbol=symbol,
                            timestamp=datetime.now(UTC),
                            source=SignalSource.STRATEGY_AGENT,
                            action=ActionType.BUY,
                            confidence=0.9,
                            reasoning="chaos test",
                            reference_price=100.0,
                        )
                        result = await risk_engine.approve_and_execute(session, signal, Stock(symbol=symbol))
                        await session.commit()
                        f.write(
                            json.dumps(
                                {
                                    "worker_id": worker_id,
                                    "cycle": cycle,
                                    "held_lease": True,
                                    "fencing_token": fencing_token,
                                    "symbol": symbol,
                                    "approved": result.approved,
                                    "order_id": result.order_result.order_id if result.order_result else None,
                                }
                            )
                            + "\n"
                        )
                        f.flush()
            except Exception as exc:  # noqa: BLE001 -- a chaos worker logging its own crash is the point
                f.write(json.dumps({"worker_id": worker_id, "cycle": cycle, "error": str(exc)}) + "\n")
                f.flush()
            await asyncio.sleep(lease_ttl / 6)
    await engine.dispose()


async def _summarize(database_url: str, results_path: str) -> bool:
    from vibetrading.persistence.orm_models import OrderORM

    with open(results_path) as f:  # noqa: ASYNC230 -- one-shot read after both workers have exited
        records = [json.loads(line) for line in f if line.strip()]

    approved = [r for r in records if r.get("approved")]
    order_ids_logged = {r["order_id"] for r in approved if r.get("order_id")}

    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        db_order_ids = (await session.execute(select(OrderORM.order_id))).scalars().all()
    await engine.dispose()

    duplicate_db_orders = len(db_order_ids) != len(set(db_order_ids))
    missing_from_db = order_ids_logged - set(db_order_ids)
    fencing_tokens_seen = sorted({r["fencing_token"] for r in records if r.get("held_lease")})
    takeover_happened = len(fencing_tokens_seen) > 1

    print(f"\nCycles logged: {len(records)}")
    print(f"Orders approved (per worker logs): {len(approved)}")
    print(f"Orders committed to DB: {len(db_order_ids)}")
    print(f"Distinct fencing tokens observed: {fencing_tokens_seen}")
    print(f"Takeover after kill happened: {takeover_happened}")

    ok = True
    if duplicate_db_orders:
        print("FAIL: duplicate order_id rows in the orders table -- two workers both believed they owned the lease.")
        ok = False
    if missing_from_db:
        print(f"FAIL: {len(missing_from_db)} approved order(s) never committed to the DB: {sorted(missing_from_db)}")
        ok = False
    if not takeover_happened:
        print(
            "FAIL: no lease takeover was observed -- the kill never forced a handover. "
            "Rerun with a longer --duration or shorter --lease-ttl so the survivor has time to notice."
        )
        ok = False
    if ok:
        print("\nPASS: zero duplicate orders, zero dropped approved orders, and a real cross-process takeover occurred after the kill.")
    return ok


def _spawn_worker(script_path: str, worker_id: str, database_url: str, duration: float, results_path: str, lease_ttl: float) -> subprocess.Popen:
    return subprocess.Popen(
        [
            sys.executable,
            script_path,
            "--role",
            "worker",
            "--worker-id",
            worker_id,
            "--database-url",
            database_url,
            "--duration",
            str(duration),
            "--results-path",
            results_path,
            "--lease-ttl",
            str(lease_ttl),
        ],
        env=dict(os.environ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--role", choices=["orchestrator", "worker"], default="orchestrator")
    parser.add_argument("--worker-id")
    parser.add_argument(
        "--database-url",
        default=os.environ.get(
            "CHAOS_DATABASE_URL", "postgresql+asyncpg://vibetrading:vibetrading@localhost:5432/vibetrading_chaos"
        ),
    )
    parser.add_argument("--duration", type=float, default=20.0, help="total seconds each worker process runs for")
    parser.add_argument(
        "--kill-after", type=float, default=6.0, help="seconds after start before SIGKILLing the current lease holder"
    )
    parser.add_argument(
        "--lease-ttl",
        type=float,
        default=3.0,
        help="lease TTL in seconds -- kept short (vs. production's 90s default) so a takeover happens within this script's runtime",
    )
    parser.add_argument("--results-path", default=None)
    args = parser.parse_args()

    if not args.database_url.startswith("postgresql"):
        print(
            "This chaos test needs a real Postgres DSN -- SQLite has no real "
            "cross-process row locking to exercise. Pass --database-url or set CHAOS_DATABASE_URL."
        )
        return 2

    if args.role == "worker":
        asyncio.run(_run_worker(args.worker_id, args.database_url, args.duration, args.results_path, args.lease_ttl))
        return 0

    results_path = args.results_path or f"/tmp/vibetrading_chaos_{int(time.time())}.jsonl"
    open(results_path, "w").close()

    print(f"Resetting schema and seeding chaos tenant {CHAOS_TENANT_ID} at {args.database_url} ...")
    asyncio.run(_reset_and_seed(args.database_url))

    print(f"Spawning worker-a and worker-b (lease TTL={args.lease_ttl}s, duration={args.duration}s) ...")
    script_path = os.path.abspath(__file__)
    proc_a = _spawn_worker(script_path, "worker-a", args.database_url, args.duration, results_path, args.lease_ttl)
    proc_b = _spawn_worker(script_path, "worker-b", args.database_url, args.duration, results_path, args.lease_ttl)

    time.sleep(args.kill_after)
    holder = asyncio.run(_current_lease_holder(args.database_url))
    victim = proc_a if holder != "worker-b" else proc_b
    victim_id = "worker-a" if victim is proc_a else "worker-b"
    print(f"Current lease holder: {holder!r} -- SIGKILLing {victim_id} mid-cycle ...")
    victim.kill()
    victim.wait(timeout=10)

    survivor = proc_b if victim is proc_a else proc_a
    survivor.wait(timeout=args.duration + 15)

    ok = asyncio.run(_summarize(args.database_url, results_path))
    print(f"\nFull cycle log: {results_path}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
