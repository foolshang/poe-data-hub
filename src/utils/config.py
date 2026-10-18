"""Loads config/config.yaml once and exposes it as a plain dict."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"

_cached: dict[str, Any] | None = None


_ENV_SUFFIX = "_ping_url_env"


def resolve_healthcheck_urls(cfg: dict[str, Any], environ: dict[str, str] | None = None) -> None:
    """healthchecks.<job>_ping_url_env names an environment variable; put its value in
    healthchecks.<job>_ping_url (blank when unset, which disables that ping). The URLs are
    secrets and must never be written in config.yaml: the repo is public."""
    env = os.environ if environ is None else environ
    block = cfg.get("healthchecks")
    if not isinstance(block, dict):
        return
    for key in [k for k in block if k.endswith(_ENV_SUFFIX)]:
        block[key[: -len("_env")]] = (env.get(str(block[key])) or "").strip()


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    global _cached
    if _cached is None:
        with path.open("r", encoding="utf-8") as f:
            _cached = yaml.safe_load(f) or {}
        resolve_healthcheck_urls(_cached)
    return _cached
