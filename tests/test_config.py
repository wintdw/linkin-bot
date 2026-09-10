from pathlib import Path

import pytest

from linkedin_bot import config


def _write_cfg(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_defaults_when_file_missing(tmp_path):
    cfg = config.load(tmp_path / "nope.yaml")
    assert cfg.caps.daily == 15
    assert cfg.caps.weekly == 90
    assert cfg.warmup.enabled is True


def test_overrides_and_keeps_defaults(tmp_path):
    path = _write_cfg(tmp_path, "caps:\n  daily: 7\n")
    cfg = config.load(path)
    assert cfg.caps.daily == 7
    assert cfg.caps.weekly == 90  # untouched default


def test_paths_resolve_under_data_dir(tmp_path):
    path = _write_cfg(tmp_path, "")
    cfg = config.load(path)
    assert Path(cfg.paths.db_file).name == "bot.db"
    assert Path(cfg.paths.session_file).name == "linkedin_storage_state.json"
    assert str(Path(cfg.paths.data_dir)).endswith("data")
    assert Path(cfg.paths.kill_file).name == "STOP"


def test_absolute_data_dir_respected(tmp_path):
    abs_dir = tmp_path / "elsewhere"
    path = _write_cfg(tmp_path, f"paths:\n  data_dir: {str(abs_dir).replace(chr(92), '/')}\n")
    cfg = config.load(path)
    assert Path(cfg.paths.db_file).parent == abs_dir


def test_invalid_delays_rejected(tmp_path):
    path = _write_cfg(tmp_path, "delays:\n  min_seconds: 50\n  max_seconds: 10\n")
    with pytest.raises(ValueError):
        config.load(path)


def test_daily_above_weekly_rejected(tmp_path):
    path = _write_cfg(tmp_path, "caps:\n  daily: 50\n  weekly: 30\n")
    with pytest.raises(ValueError):
        config.load(path)


def test_negative_refresh_wait_rejected(tmp_path):
    path = _write_cfg(tmp_path, "network:\n  refresh_wait_seconds: -1\n")
    with pytest.raises(ValueError):
        config.load(path)


def test_zero_max_empty_refreshes_rejected(tmp_path):
    path = _write_cfg(tmp_path, "network:\n  max_empty_refreshes: 0\n")
    with pytest.raises(ValueError):
        config.load(path)


def test_shipped_config_valid():
    cfg = config.load()
    assert cfg.caps.daily >= 1
    assert Path(cfg.paths.session_file).name == "linkedin_storage_state.json"
