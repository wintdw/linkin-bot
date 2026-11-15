"""Tests for the orphan reaper (pure logic + a fake /proc tree, no browser)."""

import time

import pytest

from linkedin_bot import reaper


def _stat_line(pid: int, comm: str, state: str, ppid: int) -> str:
    return f"{pid} ({comm}) {state} {ppid} {ppid} {pid} 0 -1 4194560 0 0 0"


def _fake_proc(root, entries: dict[int, tuple[str, str, int]]) -> str:
    """Build a fake /proc with ``{pid: (comm, state, ppid)}``; returns the root."""
    root.mkdir(parents=True, exist_ok=True)
    for pid, (comm, state, ppid) in entries.items():
        directory = root / str(pid)
        directory.mkdir()
        (directory / "stat").write_text(_stat_line(pid, comm, state, ppid))
    (root / "self").mkdir()  # /proc also holds non-numeric entries
    return str(root)


def test_is_browser_helper_matches_chromes_truncated_comm():
    # The kernel truncates comm to 15 chars, so chrome-headless-shell arrives short.
    assert reaper.is_browser_helper("chrome-headless")
    assert reaper.is_browser_helper("chrome_crashpad")
    assert reaper.is_browser_helper("headless_shell")
    assert reaper.is_browser_helper("crashpad_handler")
    assert not reaper.is_browser_helper("node")
    assert not reaper.is_browser_helper("python3.13")
    assert not reaper.is_browser_helper("")


def test_read_stat_handles_spaces_and_parens_in_comm(tmp_path):
    directory = tmp_path / "77"
    directory.mkdir()
    (directory / "stat").write_text(_stat_line(77, "chrome renderer (x)", "Z", 1))
    assert reaper._read_stat(str(directory / "stat")) == ("chrome renderer (x)", "Z", 1)


def test_read_stat_missing_file_is_none(tmp_path):
    assert reaper._read_stat(str(tmp_path / "nope" / "stat")) is None


def test_zombie_browser_children_selects_only_our_browser_zombies(tmp_path):
    root = _fake_proc(
        tmp_path / "proc",
        {
            42: ("chrome-headless", "Z", 1),  # ours, browser, zombie -> reap
            43: ("headless_shell", "Z", 1),  # ours, browser, zombie -> reap
            44: ("node", "Z", 1),  # ours, but the driver/library reaps it
            45: ("chrome-headless", "S", 1),  # ours, but still alive
            46: ("chrome-headless", "Z", 99),  # orphaned to another process
        },
    )
    assert reaper.zombie_browser_children(root, parent_pid=1) == [42, 43]


def test_zombie_browser_children_missing_proc_root_is_empty(tmp_path):
    assert reaper.zombie_browser_children(str(tmp_path / "gone"), parent_pid=1) == []


def test_reap_orphans_waits_on_each_candidate(tmp_path):
    root = _fake_proc(
        tmp_path / "proc",
        {
            42: ("chrome-headless", "Z", 1),
            43: ("node", "Z", 1),
            44: ("crashpad_handler", "Z", 1),
        },
    )
    asked = []

    def fake_wait(pid):
        asked.append(pid)
        return pid, 0

    assert reaper.reap_orphans(root, parent_pid=1, reap=fake_wait) == [42, 44]
    assert asked == [42, 44]


def test_reap_orphans_skips_a_child_someone_else_already_reaped(tmp_path):
    root = _fake_proc(tmp_path / "proc", {42: ("chrome-headless", "Z", 1)})

    def gone(pid):
        raise ChildProcessError(pid)

    assert reaper.reap_orphans(root, parent_pid=1, reap=gone) == []


def test_start_is_a_no_op_when_not_pid_1():
    called = []
    try:
        # A normal host run (or a `docker run` with an init) is never PID 1.
        assert reaper.start(parent_pid=4321, sweep=lambda: called.append(1)) is False
        assert called == []
    finally:
        reaper.stop()


def test_start_sweeps_when_this_process_is_pid_1(tmp_path):
    root = _fake_proc(tmp_path / "proc", {42: ("chrome-headless", "Z", 1)})
    calls = []

    def sweep():
        reaped = reaper.reap_orphans(root, parent_pid=1, reap=lambda pid: (pid, 0))
        calls.append(reaped)
        return reaped

    try:
        assert reaper.start(parent_pid=1, interval=0.01, sweep=sweep) is True
        deadline = time.monotonic() + 2.0
        while not calls and time.monotonic() < deadline:
            time.sleep(0.01)
        assert calls, "the sweeper never ran"
        assert calls[0] == [42]
        # Starting twice reuses the running thread instead of stacking them.
        assert reaper.start(parent_pid=1, interval=0.01, sweep=sweep) is True
    finally:
        reaper.stop()


def test_sweeper_survives_a_failing_sweep(tmp_path):
    calls = []

    def sweep():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return []

    try:
        reaper.start(parent_pid=1, interval=0.01, sweep=sweep)
        deadline = time.monotonic() + 2.0
        while len(calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(calls) >= 2
    finally:
        reaper.stop()
