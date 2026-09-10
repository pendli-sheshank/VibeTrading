from __future__ import annotations

import json
import re

_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_json_response(content: str) -> dict | None:
    """Best-effort JSON parsing of an LLM response.

    Strips common ```json ... ``` code-fence wrapping before parsing. Returns
    None (never raises) on anything unparseable, so every call site is
    expected to have an explicit fallback for malformed LLM output.
    """
    if not content:
        return None
    cleaned = _CODE_FENCE_RE.sub("", content).strip()
    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None
