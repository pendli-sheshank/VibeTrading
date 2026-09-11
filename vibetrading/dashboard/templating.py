from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# NOTE: there is deliberately no get_settings() Jinja global here (there
# was, pre-multi-tenancy) -- settings are per-tenant now (see
# settings/cache.py), so every route that renders a page including
# _mode_control.html must pass its own tenant's `settings` object
# explicitly into the template context instead of a template reaching for
# a process-wide global.
