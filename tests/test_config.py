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


def test_follow_defaults_present(tmp_path):
    cfg = config.load(tmp_path / "nope.yaml")
    assert cfg.follow.enabled is False
    assert cfg.follow.caps.daily == 10
    assert cfg.follow.caps.weekly == 50


def test_follow_daily_above_weekly_rejected(tmp_path):
    path = _write_cfg(tmp_path, "follow:\n  caps:\n    daily: 50\n    weekly: 30\n")
    with pytest.raises(ValueError):
        config.load(path)


def test_follow_enabled_requires_target(tmp_path):
    path = _write_cfg(tmp_path, "follow:\n  enabled: true\n")
    with pytest.raises(ValueError):
        config.load(path)


def test_follow_enabled_with_page_name_ok(tmp_path):
    path = _write_cfg(tmp_path, "follow:\n  enabled: true\n  page_name: Atento\n")
    assert config.load(path).follow.page_name == "Atento"


def test_post_defaults_present(tmp_path):
    cfg = config.load(tmp_path / "nope.yaml")
    assert cfg.post.page_url == ""
    assert cfg.post.submit_button_text == "Post"


def test_post_page_url_must_be_company(tmp_path):
    path = _write_cfg(tmp_path, "post:\n  page_url: https://www.linkedin.com/feed/\n")
    with pytest.raises(ValueError):
        config.load(path)


def test_post_page_url_with_company_ok(tmp_path):
    path = _write_cfg(
        tmp_path,
        "post:\n"
        "  page_url: https://www.linkedin.com/company/125004230/admin/page-posts/published/\n",
    )
    assert config.load(path).post.page_url.endswith("page-posts/published/")


def test_browser_block_retry_defaults_and_override(tmp_path):
    """Edge-block retries are config-driven (default 1 retry / 120 s backoff)."""
    from linkedin_bot import config

    base = config.load(None)
    assert base.browser.block_retries == 1
    assert base.browser.block_backoff_seconds == 120

    path = tmp_path / "config.yaml"
    path.write_text("browser:\n  block_retries: 0\n  block_backoff_seconds: 30\n")
    tuned = config.load(path)
    assert tuned.browser.block_retries == 0
    assert tuned.browser.block_backoff_seconds == 30
    # merging must not drop the keys the file does not mention
    assert tuned.browser.headless is True
