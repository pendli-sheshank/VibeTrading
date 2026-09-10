from __future__ import annotations

import logging

from vibetrading.config import get_settings


class ModeFilter(logging.Filter):
    """Prefixes every log record with the current execution mode.

    Live vs paper mode must be unmistakable in every log line — this is a
    safety property, not cosmetics.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        mode = get_settings().vibetrading_execution_mode.value.upper()
        record.msg = f"[MODE={mode}] {record.msg}"
        return True


def configure_logging() -> None:
    settings = get_settings()
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    handler.addFilter(ModeFilter())

    root = logging.getLogger()
    root.setLevel(settings.log_level)
    root.handlers = [handler]
