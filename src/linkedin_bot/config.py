"""Configuration loading and path resolution.

Defaults live in DEFAULTS and are deep-merged with the optional config.yaml,
so the file only has to override what differs from the baseline.
"""

from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# A LinkedIn Page (organization) URL carries its numeric id in a /company/<id>/ path.
COMPANY_URL_RE = re.compile(r"/company/(\d+)")

# A browser proxy URL: scheme://[user:pass@]host:port (Playwright's "server" form).
PROXY_RE = re.compile(r"^(https?|socks5h?)://(\S+@)?[\w.\-\[\]:]+:\d+$")

# Credentials live OUTSIDE the repo and outside the data volume, one directory
# per account:
#
#   /var/secrets/linkin/<account>/linkedin_storage_state.json   (login cookie)
#   /var/secrets/linkin/<account>/credentials.yaml              (optional overlay)
#
# config.yaml is tracked by git, so a login must never be written into it; the
# optional `credentials.yaml` is merged over it last (same shape), which is where
# a proxy userinfo or any future secret belongs.
SECRETS_FILE_NAME = "credentials.yaml"
DEFAULT_ACCOUNT_DIR = "default"  # account label is empty in the image fallback

DEFAULTS: dict[str, Any] = {
    "account": "",  # label for this instance (one container per LinkedIn account)
    "browser": {
        "headless": True,
        "slow_mo_ms": 0,
        "timeout_ms": 20000,
        # LinkedIn's edge (Cloudflare) refuses a browser sometimes; that is
        # transient far more often than it is a dead session, so retry before
        # declaring the account blocked.
        "block_retries": 1,
        "block_backoff_seconds": 120,
        # Optional proxy the browser egresses through, e.g.
        # "socks5://172.17.0.1:40000" (a local Cloudflare WARP proxy). Needed
        # where the host's own uplink cannot reach www.linkedin.com — an
        # on-path block resets the connection, which is NOT a dead session.
        # Empty = direct connection.
        "proxy": "",
    },
    "network": {
        "url": "https://www.linkedin.com/mynetwork/",
        "refresh_wait_seconds": 20,
        "max_empty_refreshes": 3,
        "click_timeout_ms": 2000,
    },
    "caps": {"daily": 15, "weekly": 90},
    "follow": {
        "enabled": False,
        "page_name": "",
        "page_url": "",
        "invite_button_text": "Invite to follow",
        "refresh_wait_seconds": 20,
        "max_empty_refreshes": 3,
        "click_timeout_ms": 2000,
        "caps": {"daily": 10, "weekly": 50},
    },
    "warmup": {"enabled": True, "daily_start": 5, "daily_increment": 2},
    "delays": {
        "min_seconds": 45,
        "max_seconds": 120,
        "post_scroll_min": 6,
        "post_scroll_max": 12,
    },
    "login": {"checkpoint_wait_seconds": 1800},
    "monitor": {"security_scan_every": 5},
    "post": {
        "page_url": "",
        "composer_button_text": "Start a post",
        "submit_button_text": "Post",
        "comment_button_text": "Comment",
    },
    "paths": {
        # Non-secret state (SQLite ledger, STOP kill switch).
        "data_dir": "data",
        # CREDENTIALS: the saved LinkedIn session cookie store and the optional
        # per-account credentials.yaml live here, never in the repo or in the
        # data volume. One subdirectory per account (see SECRETS_FILE_NAME).
        "secrets_dir": "/var/secrets/linkin",
        # Relative names resolve under <secrets_dir>/<account>/; an absolute
        # path is accepted only when it is inside secrets_dir (see load()).
        "session_file": "linkedin_storage_state.json",
    },
}


def _merge(base: dict[str, Any], overlay: dict[str, Any]) -> None:
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value


def _validate(data: dict[str, Any]) -> None:
    proxy = str(data["browser"].get("proxy") or "").strip()
    if proxy and not PROXY_RE.match(proxy):
        raise ValueError(
            "browser.proxy must be empty or a proxy URL like socks5://host:port "
            "(or http://host:port)"
        )
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

    follow = data["follow"]
    fcaps = follow["caps"]
    follow_daily, follow_weekly = int(fcaps["daily"]), int(fcaps["weekly"])
    if follow_daily < 1 or follow_weekly < 1:
        raise ValueError("follow.caps.daily and follow.caps.weekly must be >= 1")
    if follow_daily > follow_weekly:
        raise ValueError("follow.caps.daily cannot exceed follow.caps.weekly")
    if float(follow["refresh_wait_seconds"]) < 0:
        raise ValueError("follow.refresh_wait_seconds must be >= 0")
    if int(follow["max_empty_refreshes"]) < 1:
        raise ValueError("follow.max_empty_refreshes must be >= 1")
    if int(follow["click_timeout_ms"]) < 1:
        raise ValueError("follow.click_timeout_ms must be >= 1")
    if follow["enabled"] and not (follow["page_url"] or follow["page_name"]):
        raise ValueError("follow.enabled needs follow.page_url or follow.page_name")

    post = data["post"]
    page_url = str(post["page_url"] or "")
    if page_url and not COMPANY_URL_RE.search(page_url):
        raise ValueError("post.page_url must contain a /company/<id>/ path")


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


def _read_yaml(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise ValueError(  # noqa: TRY004 - a bad config file, not a caller type error
            f"{path} must contain a YAML mapping"
        )
    return loaded


def _reject_credentials_in_config(overlay: dict[str, Any], path: Path) -> None:
    """Refuse a login written into the tracked config (it would be committed).

    ``credentials.yaml`` under the secrets directory is the one place a secret is
    allowed; it is merged over the config afterwards, so the proxy URL there may
    carry ``user:pass@``.
    """
    browser = overlay.get("browser")
    if not isinstance(browser, dict):
        return
    proxy = str(browser.get("proxy") or "").strip()
    if proxy and "@" in proxy.split("://", 1)[-1]:
        raise ValueError(
            f"{path.name} must not hold credentials: browser.proxy embeds a login. "
            f"Keep the tracked config credential-free and put the full proxy URL "
            f"in <paths.secrets_dir>/<account>/{SECRETS_FILE_NAME} instead."
        )


def _account_name(value: Any) -> str:
    """The per-account directory name under the secrets directory."""
    name = str(value or "").strip() or DEFAULT_ACCOUNT_DIR
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise ValueError(
            "account must be a simple label (letters, digits, '.', '_', '-'); it "
            "names the directory under paths.secrets_dir that holds the credentials"
        )
    return name


def _absolute(value: Any, base: Path) -> Path:
    path = Path(str(value))
    return path if path.is_absolute() else base / path


def _is_within(path: Path, parent: Path) -> bool:
    try:
        Path(os.path.abspath(str(path))).relative_to(os.path.abspath(str(parent)))
    except ValueError:
        return False
    return True


def load(config_path: str | Path | None = None) -> Cfg:
    data = copy.deepcopy(DEFAULTS)
    path = Path(config_path) if config_path is not None else PROJECT_ROOT / "config.yaml"
    if path.exists():
        overlay = _read_yaml(path)
        _reject_credentials_in_config(overlay, path)
        _merge(data, overlay)
    _validate(data)

    # The account names the directory the credentials live in, so it must come
    # from the config that was just read; the overlay is located with it.
    account = _account_name(data.get("account"))
    secrets_dir = _absolute(data["paths"]["secrets_dir"], PROJECT_ROOT)
    account_dir = secrets_dir / account
    secrets_file = account_dir / SECRETS_FILE_NAME
    try:
        present = secrets_file.is_file()
    except OSError:
        # An unreadable parent (e.g. root-owned /var/secrets on a dev box) means
        # no overlay here; a readable overlay that fails to parse still raises.
        present = False
    if present:
        overlay = _read_yaml(secrets_file)
        if "account" in overlay:
            raise ValueError(
                f"{secrets_file} must not set `account` - the label in config.yaml "
                "decides which secrets directory is used"
            )
        _merge(data, overlay)
        _validate(data)
        account = _account_name(data.get("account"))
        secrets_dir = _absolute(data["paths"]["secrets_dir"], PROJECT_ROOT)
        account_dir = secrets_dir / account

    data_dir = _absolute(data["paths"]["data_dir"], PROJECT_ROOT)
    session_file = Path(str(data["paths"]["session_file"]))
    if not session_file.is_absolute():
        session_file = account_dir / session_file
    if not _is_within(session_file, secrets_dir):
        raise ValueError(
            f"paths.session_file must live under paths.secrets_dir ({secrets_dir}); "
            f"got {session_file}. The session cookie store is a credential, so it "
            "does not belong in data/ - set paths.secrets_dir instead of pointing "
            "session_file outside it."
        )

    data["account"] = str(data.get("account") or "")
    data["paths"]["data_dir"] = str(data_dir)
    data["paths"]["secrets_dir"] = str(secrets_dir)
    data["paths"]["account_dir"] = str(account_dir)
    data["paths"]["session_file"] = str(session_file)
    data["paths"]["db_file"] = str(data_dir / "bot.db")
    data["paths"]["kill_file"] = str(data_dir / "STOP")
    return Cfg(data)
