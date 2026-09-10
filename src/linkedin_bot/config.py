"""Configuration loading and path resolution.

Defaults live in DEFAULTS and are deep-merged with the optional config.yaml,
so the file only has to override what differs from the baseline.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]

DEFAULTS: dict[str, Any] = {
    "browser": {"headless": True, "slow_mo_ms": 0, "timeout_ms": 20000},
    "network": {
        "url": "https://www.linkedin.com/mynetwork/",
        "refresh_wait_seconds": 20,
        "max_empty_refreshes": 3,
        "click_timeout_ms": 2000,
    },
    "caps": {"daily": 15, "weekly": 90},
    "warmup": {"enabled": True, "daily_start": 5, "daily_increment": 2},
    "delays": {
        "min_seconds": 45,
        "max_seconds": 120,
        "post_scroll_min": 6,
        "post_scroll_max": 12,
    },
    "login": {"checkpoint_wait_seconds": 1800},
    "monitor": {"security_scan_every": 5},
    "paths": {
        "data_dir": "data",
        "session_file": "sessions/linkedin_storage_state.json",
    },
}


def _merge(base: dict[str, Any], overlay: dict[str, Any]) -> None:
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value


def _validate(data: dict[str, Any]) -> None:
    caps = data["caps"]
    daily, weekly = int(caps["daily"]), int(caps["weekly"])
    if daily < 1 or weekly < 1:
        raise ValueError("caps.daily and caps.weekly must be >= 1")
    if daily > weekly:
        raise ValueError("caps.daily cannot exceed caps.weekly")
    delays = data["delays"]
    if float(delays["min_seconds"]) < 0:
        raise ValueError("delays.min_seconds must be >= 0")
    if float(delays["max_seconds"]) < float(delays["min_seconds"]):
        raise ValueError("delays.max_seconds must be >= delays.min_seconds")
    network = data["network"]
    if float(network["refresh_wait_seconds"]) < 0:
        raise ValueError("network.refresh_wait_seconds must be >= 0")
    if int(network["max_empty_refreshes"]) < 1:
        raise ValueError("network.max_empty_refreshes must be >= 1")
    if int(network["click_timeout_ms"]) < 1:
        raise ValueError("network.click_timeout_ms must be >= 1")


class Cfg:
    """Read-only attribute access over a nested dict."""

    def __init__(self, data: dict[str, Any]) -> None:
        object.__setattr__(self, "_data", data)

    def __getattr__(self, name: str) -> Any:
        try:
            value = self._data[name]
        except KeyError as exc:
            raise AttributeError(name) from exc
        return Cfg(value) if isinstance(value, dict) else value


def load(config_path: str | Path | None = None) -> Cfg:
    data = copy.deepcopy(DEFAULTS)
    path = Path(config_path) if config_path is not None else PROJECT_ROOT / "config.yaml"
    if path.exists():
        with open(path, "r", encoding="utf-8") as handle:
            overlay = yaml.safe_load(handle) or {}
        _merge(data, overlay)
    _validate(data)

    data_dir = Path(data["paths"]["data_dir"])
    if not data_dir.is_absolute():
        data_dir = PROJECT_ROOT / data_dir
    session_file = Path(data["paths"]["session_file"])
    if not session_file.is_absolute():
        session_file = data_dir / session_file
    data["paths"]["data_dir"] = str(data_dir)
    data["paths"]["session_file"] = str(session_file)
    data["paths"]["db_file"] = str(data_dir / "bot.db")
    data["paths"]["kill_file"] = str(data_dir / "STOP")
    return Cfg(data)
