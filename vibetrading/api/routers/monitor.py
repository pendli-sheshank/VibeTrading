from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from vibetrading.api.deps import get_broker, get_db
from vibetrading.auth.backend import current_active_user
from vibetrading.broker.base import BrokerClient
from vibetrading.persistence.orm_models import UserORM
from vibetrading.persistence.repositories import list_recent_audit_log, list_recent_orders

router = APIRouter(prefix="/api/monitor", tags=["monitor"])


@router.get("/positions")
async def get_positions(broker: BrokerClient = Depends(get_broker)) -> list[dict]:
    positions = await broker.get_positions()
    return [p.model_dump(mode="json") for p in positions]


@router.get("/funds")
async def get_funds(broker: BrokerClient = Depends(get_broker)) -> dict:
    funds = await broker.get_funds()
    return funds.model_dump(mode="json")


@router.get("/orders")
async def get_orders(
    limit: int = 50, session: AsyncSession = Depends(get_db), user: UserORM = Depends(current_active_user)
) -> list[dict]:
    orders = await list_recent_orders(session, user.id, limit=limit)
    return [
        {
            "order_id": o.order_id,
            "broker_order_id": o.broker_order_id,
            "stock_symbol": o.stock_symbol,
            "side": o.side,
            "quantity": o.quantity,
            "status": o.status,
            "mode": o.mode,
            "filled_quantity": o.filled_quantity,
            "filled_price": o.filled_price,
            "timestamp": o.timestamp,
        }
        for o in orders
    ]


@router.get("/audit-log")
async def get_audit_log(
    limit: int = 50, session: AsyncSession = Depends(get_db), user: UserORM = Depends(current_active_user)
) -> list[dict]:
    entries = await list_recent_audit_log(session, user.id, limit=limit)
    return [
        {
            "id": e.id,
            "order_id": e.order_id,
            "signal_id": e.signal_id,
            "risk_checks_passed": e.risk_checks_passed,
            "mode": e.mode,
            "status": e.status,
            "timestamp": e.timestamp,
        }
        for e in entries
    ]
