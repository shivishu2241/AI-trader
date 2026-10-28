"""
GUI-managed configuration store
───────────────────────────────
Persists LLM provider and broker credentials entered in the dashboard
Settings page so nothing has to be edited by hand in `.env`.

File: `.user_config.json` at the repo root (git-ignored, chmod 600).
Override the location with the USER_CONFIG_PATH env var.

Secrets are never returned to the browser: `masked()` replaces them with
SECRET_MASK, and `update()` treats a masked/empty secret as "keep existing".
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from utils.logger import get_logger

logger = get_logger("user_config")

SECRET_MASK = "••••••••"
SECRET_KEYS = {"api_key", "api_secret", "access_token", "client_secret", "refresh_token", "password"}

_PATH = Path(os.getenv("USER_CONFIG_PATH") or Path(__file__).resolve().parent.parent / ".user_config.json")
_lock = threading.RLock()


def _read() -> dict:
    try:
        return json.loads(_PATH.read_text())
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.error(f"Could not read {_PATH}: {e}")
        return {}


def _write(data: dict) -> None:
    tmp = _PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass  # e.g. Windows-mounted drive in WSL
    tmp.replace(_PATH)


def get(section: str) -> dict:
    """Raw (unmasked) section — backend use only."""
    with _lock:
        return _read().get(section, {})


def _merge(old: Any, new: Any) -> Any:
    if isinstance(new, dict):
        base = dict(old) if isinstance(old, dict) else {}
        for k, v in new.items():
            if k in SECRET_KEYS and (v in ("", None) or v == SECRET_MASK):
                continue  # keep stored secret
            base[k] = _merge(base.get(k), v)
        return base
    return new


def update(section: str, values: dict) -> dict:
    with _lock:
        data = _read()
        data[section] = _merge(data.get(section, {}), values)
        _write(data)
        return data[section]


def set_secret(section_path: list[str], key: str, value: str) -> None:
    """Set one value deep inside a section (used by OAuth callbacks)."""
    patch: dict = {key: value}
    for part in reversed(section_path[1:]):
        patch = {part: patch}
    update(section_path[0], patch)


def mask(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: (SECRET_MASK if (k in SECRET_KEYS and v) else ("" if k in SECRET_KEYS else mask(v)))
            for k, v in obj.items()
        }
    return obj


def masked(section: str) -> dict:
    return mask(get(section))
