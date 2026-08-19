"""Load SDLC fixture data."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def load_sdlc() -> dict:
    path = Path(__file__).with_name("sdlc.json")
    return json.loads(path.read_text(encoding="utf-8"))
