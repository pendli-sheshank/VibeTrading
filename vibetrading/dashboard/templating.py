from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from vibetrading.config import get_settings

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# Lets _mode_control.html (embedded in base.html's nav on every page, and
# again in the Settings page) read the live execution mode / credential
# state directly, without every route in routes.py and routes_settings.py
# having to thread it through their own context dict.
templates.env.globals["get_settings"] = get_settings
