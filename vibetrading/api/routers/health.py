from __future__ import annotations

import logging
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import APIRouter, Response
from sqlalchemy import text

from vibetrading.persistence.db import get_session

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "persistence" / "migrations"


def _migrations_head_revision() -> str | None:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    return ScriptDirectory.from_config(config).get_current_head()


@router.get("/healthz")
async def liveness() -> dict:
    """Pure liveness: this process is up and answering requests. No
    dependencies checked -- a DB outage should show up as NOT ready
    (/readyz), not dead (a load balancer that kills "unhealthy" pods on
    /healthz failure would otherwise restart every replica in a DB outage,
    the opposite of what you want)."""
    return {"status": "ok"}


@router.get("/readyz")
async def readiness(response: Response) -> dict:
    """Readiness: can this replica actually serve traffic right now --
    DB reachable, and at the migrations this build expects (a replica
    running old code against a not-yet-migrated DB, or vice versa during a
    rolling deploy, should be taken out of rotation rather than serve
    requests against a schema it doesn't match). The two checks are
    deliberately independent: a missing/unreadable alembic_version table
    means "migration state unknown," not "database down" -- only a
    connection failure on the plain SELECT 1 means that.
    """
    checks: dict[str, bool | str | None] = {}
    healthy = True

    try:
        async with get_session() as session:
            await session.execute(text("SELECT 1"))
        checks["database"] = True
    except Exception:
        logger.exception("Readiness check: database unreachable")
        checks["database"] = False
        healthy = False

    current_revision: str | None = None
    if checks["database"]:
        try:
            async with get_session() as session:
                result = await session.execute(text("SELECT version_num FROM alembic_version"))
                current_revision = result.scalar_one_or_none()
        except Exception:
            logger.exception("Readiness check: could not read migration state")

    try:
        head_revision = _migrations_head_revision()
    except Exception:
        logger.exception("Readiness check: could not resolve migrations head")
        head_revision = None

    checks["migration_current"] = current_revision
    checks["migration_head"] = head_revision
    if checks["database"] and current_revision != head_revision:
        healthy = False

    if not healthy:
        response.status_code = 503
    return {"status": "ok" if healthy else "not_ready", "checks": checks}
