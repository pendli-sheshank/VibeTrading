from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

# In-memory storage for now (single-process) -- becomes Redis-backed once
# Phase 21's cross-worker infra lands, so limits are shared across replicas
# rather than reset per-instance. Still meaningfully slows brute-force
# attempts against a single process in the meantime.
limiter = Limiter(key_func=get_remote_address)
