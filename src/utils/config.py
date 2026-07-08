"""Loads config/config.yaml once and exposes it as a plain dict."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"

_cached: dict[str, Any] | None = None


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    global _cached
    if _cached is None:
        with path.open("r", encoding="utf-8") as f:
            _cached = yaml.safe_load(f) or {}
    return _cached
