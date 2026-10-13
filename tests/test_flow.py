from types import SimpleNamespace

import pytest

from linkedin_bot import config, db, flow


class _Page:
    def __init__(self, url: str):
        self.url = url


def test_registry_lists_both_flows_in_order():
    assert flow.NAMES == ("connect", "follow")
    assert flow.SPECS["connect"].module == "network"
    assert flow.SPECS["follow"].module == "follow"


def test_spec_unknown_flow_raises():
    with pytest.raises(ValueError):
        flow.spec("nope")


def test_spec_resolves_loop_and_dry_run_callables():
    for name in flow.NAMES:
        spec = flow.spec(name)
        assert callable(spec.loop_fn())
        assert callable(spec.dry_run_fn())


def test_new_stats_shape():
    assert flow.new_stats() == {
        "sent": 0,
        "errors": 0,
        "unknown": 0,
        "refreshed": 0,
        "reason": "end",
    }


def test_stop_reason_none_when_healthy(tmp_path):
    cfg = SimpleNamespace(paths=SimpleNamespace(kill_file=str(tmp_path / "STOP")))
    assert flow.stop_reason(_Page("https://www.linkedin.com/mynetwork/"), cfg) is None


def test_stop_reason_kill_switch(tmp_path):
    marker = tmp_path / "STOP"
    marker.write_text("halt")
    cfg = SimpleNamespace(paths=SimpleNamespace(kill_file=str(marker)))
    assert flow.stop_reason(_Page("https://www.linkedin.com/mynetwork/"), cfg) == "kill-switch"


def test_stop_reason_checkpoint_url(tmp_path):
    cfg = SimpleNamespace(paths=SimpleNamespace(kill_file=str(tmp_path / "STOP")))
    page = _Page("https://www.linkedin.com/checkpoint/challengesv2/ABC")
    assert flow.stop_reason(page, cfg) == "checkpoint"


def test_logger_prefixes_the_flow_name(capsys):
    flow.logger("follow")("hello")
    assert capsys.readouterr().out.strip() == "[follow] hello"


def test_finish_records_run_end(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    stats = flow.new_stats()
    stats.update(sent=3, errors=1, reason="cap")
    flow.finish(conn, "connect", stats)
    events = db.recent_events(conn, flow="connect")
    assert events[-1]["kind"] == "RUN_END"
    assert "sent=3" in events[-1]["message"]
    assert "reason=cap" in events[-1]["message"]


def test_usage_reads_each_flows_own_caps(tmp_path):
    cfg = config.load(tmp_path / "none.yaml")  # defaults
    conn = db.connect(str(tmp_path / "t.db"))
    usage = flow.usage(cfg, conn)
    assert set(usage) == {"connect", "follow"}
    assert usage["follow"]["day_cap"] == cfg.follow.caps.daily
    assert usage["follow"]["week_cap"] == cfg.follow.caps.weekly


def test_caps_lines_from_usage():
    usage = {
        "connect": {
            "sent_today": 1, "day_cap": 10, "day_remaining": 9,
            "sent_week": 2, "week_cap": 100, "week_remaining": 98,
        },
        "follow": {
            "sent_today": 3, "day_cap": 10, "day_remaining": 7,
            "sent_week": 4, "week_cap": 50, "week_remaining": 46,
        },
    }
    lines = flow.caps_lines(usage)
    assert len(lines) == 4
    assert lines[0].startswith("connect today: 1/10")
    assert lines[2].startswith("follow today: 3/10")
