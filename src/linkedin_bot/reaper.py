"""Reap browser helper processes that were reparented to us (PID 1).

Playwright closes the browser after every run, but Chromium is a process *tree*
(zygote, GPU/network utilities, renderers, crashpad). A few helpers are orphaned
while the tree is being torn down - their parent exits first - so the kernel
reparents them to **PID 1** and they stay ``<defunct>`` until PID 1 reaps them.

In a container without an init process, PID 1 *is* this Python service, and
Python only reaps the children it spawned itself (``subprocess`` / asyncio), so
those reparented helpers were never waited on: live, Nov 2026, the two account
containers held 17 and 14 ``chrome-headless`` zombies after two days (~3-4 per
browser session, growing forever - each zombie pins a PID and a task_struct).

Two fixes, both kept because they cover different launch paths:

* ``docker-compose.yml`` starts the services with ``init: true``, so tini is PID
  1 and reaps orphans. Nothing in the app has to know about it - this is the
  normal deployment.
* this module, for when the service *is* PID 1 anyway (``docker run`` without
  ``--init``, a systemd unit, ``docker compose run`` on an older compose, ...),
  which is exactly how the leak was first observed. ``start`` begins a small
  daemon thread that sweeps the process table and reaps **only** our own zombie
  children whose command name says they are browser helpers.

Reaping is deliberately narrow: Playwright's node driver and any asyncio
subprocess are children too, but their libraries reap them (and reading their
exit status is their business). Matching the comm *and* the zombie state means
this can never steal a live child's status - and because a reparented orphan has
no other reaper, it is the one thing nobody else will collect.
"""

from __future__ import annotations

import os
import threading
from typing import Callable

# /proc/<pid>/comm for the browser helpers this bot can leak. The kernel
# truncates comm to 15 characters, so the real names arrive as
# "chrome-headless" (chrome-headless-shell), "headless_shell", or
# "crashpad_handle"; matching by prefix covers both truncated and full forms.
BROWSER_HELPER_PREFIXES = ("chrome", "headless_shell", "crashpad")
SWEEP_INTERVAL_SECONDS = 5.0

_lock = threading.Lock()
_thread: threading.Thread | None = None
_stop = threading.Event()


def is_browser_helper(comm: str) -> bool:
    """Whether a process' comm names a Chromium/Playwright helper."""
    name = str(comm or "").strip().lower()
    return any(name.startswith(prefix) for prefix in BROWSER_HELPER_PREFIXES)


def _read_stat(path: str) -> tuple[str, str, int] | None:
    """``(comm, state, ppid)`` from a ``/proc/<pid>/stat`` file, or ``None``.

    The comm field is parenthesised and can itself contain spaces and ``)``
    (Chromium's renderers do), so it is split on the *last* ``)`` rather than
    with a field count.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            raw = handle.read()
    except OSError:
        return None
    start = raw.find("(")
    end = raw.rfind(")")
    if start < 0 or end < start:
        return None
    comm = raw[start + 1 : end]
    fields = raw[end + 1 :].split()
    if len(fields) < 2:
        return None
    try:
        ppid = int(fields[1])
    except ValueError:
        return None
    return comm, fields[0], ppid


def zombie_browser_children(proc_root: str = "/proc", parent_pid: int | None = None) -> list[int]:
    """PIDs of our zombie children that look like browser helpers (sorted)."""
    me = os.getpid() if parent_pid is None else int(parent_pid)
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return []
    found: list[int] = []
    for entry in entries:
        if not entry.isdigit():
            continue
        stat = _read_stat(os.path.join(proc_root, entry, "stat"))
        if stat is None:
            continue
        comm, state, ppid = stat
        if state == "Z" and ppid == me and is_browser_helper(comm):
            found.append(int(entry))
    return sorted(found)


def reap_orphans(
    proc_root: str = "/proc",
    parent_pid: int | None = None,
    reap: Callable[[int], tuple[int, int]] | None = None,
) -> list[int]:
    """Wait on each orphaned browser zombie reparented to us; return reaped PIDs.

    ``reap`` is injectable for tests (it defaults to ``os.waitpid(pid,
    WNOHANG)``). A PID that another reaper got to first is simply skipped - the
    goal is that no zombie is left behind, not that we are the one who reaped it.
    """
    wait = reap if reap is not None else (lambda pid: os.waitpid(pid, os.WNOHANG))
    reaped: list[int] = []
    for pid in zombie_browser_children(proc_root, parent_pid):
        try:
            got, _status = wait(pid)
        except OSError:  # ChildProcessError / ProcessLookupError: already gone
            continue
        if got:
            reaped.append(pid)
    return reaped


def _sweep_forever(
    sweep: Callable[[], list[int]], interval: float, stop: threading.Event
) -> None:
    while True:
        try:
            reaped = sweep()
        except Exception:  # noqa: BLE001 - a failed sweep must not kill the thread
            reaped = []
        if reaped:
            print(
                f"[reaper] collected {len(reaped)} orphaned browser process(es) "
                f"(this service is PID 1, so nothing else will)",
                flush=True,
            )
        if stop.wait(interval):
            return


def start(
    parent_pid: int | None = None,
    *,
    interval: float = SWEEP_INTERVAL_SECONDS,
    proc_root: str = "/proc",
    sweep: Callable[[], list[int]] | None = None,
) -> bool:
    """Start the sweeper if we are PID 1; return whether it is running.

    Anywhere else this is a no-op returning ``False``: normally there *is* an
    init process (or an asyncio/Playwright parent) responsible for reaping, and
    this process is not PID 1, so orphans never reparent to us.
    """
    me = os.getpid() if parent_pid is None else int(parent_pid)
    if me != 1:
        return False
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return True
        _stop.clear()
        run = sweep if sweep is not None else (lambda: reap_orphans(proc_root))
        _thread = threading.Thread(
            target=_sweep_forever,
            args=(run, interval, _stop),
            name="orphan-reaper",
            daemon=True,
        )
        _thread.start()
    print(
        "[reaper] no init process (this service is PID 1): sweeping orphaned "
        "browser processes",
        flush=True,
    )
    return True


def stop(timeout: float = 2.0) -> None:
    """Stop the sweeper (used on shutdown and by the tests)."""
    global _thread
    with _lock:
        thread, _thread = _thread, None
    _stop.set()
    if thread is not None and thread.is_alive():
        thread.join(timeout)
