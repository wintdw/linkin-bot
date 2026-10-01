"""linkin-bot as a service: FastAPI dashboard + built-in daily schedule.

The ``linkedin-bot serve`` command runs one long-lived process that serves a
small status dashboard over HTTP and runs the connect loop itself at fixed
daily times (default 09:30, container-local via TZ). This replaces host-cron
one-shot containers: ``docker compose up -d`` keeps the service alive, and
``docker compose logs -f`` shows every action as it happens (each click, scroll
and stop reason is logged to stdout as ``[connect]`` / ``[follow]`` lines by the
flow machinery).

Run scheduling is single-flight: an on-schedule run, a ``POST /run`` manual
run, and a ``--boot-run`` startup run can never overlap. Each scheduled slot
(``HH:MM``) fires at most once per day.

fastapi/uvicorn are imported lazily so the plain CLI keeps zero web
dependencies; tests of the pure scheduling helpers below run without them.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import sys
import time
from typing import Any, AsyncIterator

from . import db, monitor, scheduler as scheduler_mod
from .cli import run_connect_once, run_follow_once
from .config import load

DEFAULT_CONNECT_TIMES = ("09:30",)
DEFAULT_FOLLOW_TIMES = ("10:00",)

# A scheduled slot only fires if its occurrence is within this many seconds of
# the wake-up. Prevents the low-frequency flow from being triggered by a stale
# occurrence while handling the other flow's slot.
SCHEDULE_WINDOW_SECONDS = 120


def _parse_hhmm(hhmm: str) -> tuple[int, int]:
    try:
        hour, minute = (int(part) for part in hhmm.split(":"))
    except (ValueError, TypeError):
        raise ValueError(f"run time must be HH:MM, got {hhmm!r}") from None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"run time must be HH:MM, got {hhmm!r}")
    return hour, minute


def seconds_until(hhmm: str, now: dt.datetime | None = None) -> float:
    """Seconds from *now* until the next occurrence of HH:MM today or tomorrow."""
    now = now or dt.datetime.now()
    hour, minute = _parse_hhmm(hhmm)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += dt.timedelta(days=1)
    return (target - now).total_seconds()


def seconds_until_any(times: list[str], now: dt.datetime | None = None) -> float:
    """Seconds until the earliest of the given HH:MM slots."""
    return min(seconds_until(hhmm, now) for hhmm in times)


def next_run_at(times: list[str], now: dt.datetime | None = None) -> dt.datetime:
    """The wall-clock datetime of the next scheduled run slot."""
    now = now or dt.datetime.now()
    return now + dt.timedelta(seconds=seconds_until_any(times, now))


def current_slot(times: list[str], now: dt.datetime | None = None) -> str | None:
    """The most recent slot occurrence as ``YYYY-MM-DD HH:MM``.

    The scheduler wakes just after a slot fires, so the most recent occurrence
    is the one that just triggered — used to run each slot at most once.
    """
    now = now or dt.datetime.now()
    recent: dt.datetime | None = None
    for hhmm in times:
        hour, minute = _parse_hhmm(hhmm)
        occ = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if occ > now:
            occ -= dt.timedelta(days=1)
        if recent is None or occ > recent:
            recent = occ
    return recent.strftime("%Y-%m-%d %H:%M") if recent else None


def _due_slot(times: list[str], last_slot: str | None, now: dt.datetime) -> str | None:
    """The slot that just fired (within the wake window), or ``None``.

    Guards the two-schedule loop: on any given wake only the flow whose slot
    actually landed a moment ago runs, so the other flow is never triggered by
    its stale, most-recent occurrence.
    """
    slot = current_slot(times, now)
    if slot is None or slot == last_slot:
        return None
    hour, minute = _parse_hhmm(slot[-5:])
    occurrence = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if occurrence > now:
        occurrence -= dt.timedelta(days=1)
    if (now - occurrence).total_seconds() > SCHEDULE_WINDOW_SECONDS:
        return None
    return slot


def _dashboard_text(
    cfg, state: dict[str, Any], connect_times: list[str], follow_times: list[str]
) -> str:
    """Plain-text status block shown on the dashboard page."""
    conn = db.connect(cfg.paths.db_file)
    usage = scheduler_mod.remaining_today(cfg, conn)
    follow_usage = scheduler_mod.remaining_today(cfg, conn, kind="follow")
    lines = [
        f"connect today: {usage['sent_today']}/{usage['day_cap']} sent, "
        f"{usage['day_remaining']} remaining",
        f"connect week : {usage['sent_week']}/{usage['week_cap']} sent, "
        f"{usage['week_remaining']} remaining",
        f"follow  today: {follow_usage['sent_today']}/{follow_usage['day_cap']} sent, "
        f"{follow_usage['day_remaining']} remaining",
        f"follow  week : {follow_usage['sent_week']}/{follow_usage['week_cap']} sent, "
        f"{follow_usage['week_remaining']} remaining",
    ]
    if monitor.kill_switch_present(cfg.paths.kill_file):
        lines.append("kill switch: STOP file present (runs refuse to start)")
    else:
        lines.append("kill switch: clear")
    lines.append("")
    lines.append(f"next connect run: {next_run_at(connect_times):%Y-%m-%d %H:%M} (container-local)")
    lines.append(f"next follow  run: {next_run_at(follow_times):%Y-%m-%d %H:%M} (container-local)")
    lines.append(f"now             : {dt.datetime.now():%Y-%m-%d %H:%M:%S} (container-local)")
    for flow in ("connect", "follow"):
        fstate = state["flows"][flow]
        lines.append(
            f"last {flow:<7}: started={fstate['last_started']} "
            f"finished={fstate['last_finished']} ok={fstate['last_ok']} "
            f"error={fstate['last_error'] or '-'}"
        )
    lines.append("")
    for flow in ("connect", "follow"):
        lines.append(f"{flow} events:")
        events = db.recent_events(conn, limit=6, flow=flow)
        if not events:
            lines.append("  (none)")
        for event in events:
            suffix = f" {event['message']}" if event["message"] else ""
            lines.append(f"  [{event['ts']}] {event['kind']}{suffix}")
    conn.close()
    return "\n".join(lines)


def _render_html(body: str) -> str:
    return (
        "<!DOCTYPE html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta http-equiv=\"refresh\" content=\"60\">"
        "<title>linkin-bot</title>"
        "<style>body{font-family:ui-monospace,Menlo,Consolas,monospace;"
        "margin:2rem auto;max-width:52rem;padding:0 1rem;color:#222}"
        "h1{font-size:1.2rem}pre{background:#f6f6f6;padding:1rem;overflow:auto}"
        ".muted{color:#777;font-size:.85rem}</style>"
        "</head><body><h1>linkin-bot</h1>"
        f"<pre>{body}</pre>"
        "<p class=\"muted\">POST /run starts a manual connect run · "
        "POST /run/follow starts a manual page follow run · /health shows state "
        "as JSON · docker compose logs -f shows every action</p>"
        "</body></html>\n"
    )


def create_app(
    connect_at: list[str] | tuple[str, ...] | None = None,
    follow_at: list[str] | tuple[str, ...] | None = None,
    boot_run: bool = False,
) -> Any:
    """Build the FastAPI app (imports fastapi lazily on first call).

    Two flows share the one process and one lock: ``connect`` (My Network
    requests) at ``connect_at`` and ``follow`` (Page follow invites) at
    ``follow_at``. Because both use the same LinkedIn account they can never
    overlap, so every scheduled/manual run goes through the same single-flight
    lock.
    """
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, JSONResponse, Response

    cfg = load()
    connect_times = list(connect_at) if connect_at else list(DEFAULT_CONNECT_TIMES)
    follow_times = list(follow_at) if follow_at else list(DEFAULT_FOLLOW_TIMES)
    if not connect_times:
        raise ValueError("serve needs at least one connect time (HH:MM)")
    if not follow_times:
        raise ValueError("serve needs at least one follow time (HH:MM)")
    for hhmm in list(connect_times) + list(follow_times):
        _parse_hhmm(hhmm)

    def _blank() -> dict[str, Any]:
        return {
            "last_started": None,
            "last_finished": None,
            "last_ok": None,
            "last_error": None,
            "last_slot": None,
        }

    state: dict[str, Any] = {
        "lock": asyncio.Lock(),
        "busy": False,
        "flows": {"connect": _blank(), "follow": _blank()},
    }

    async def _run_job(flow: str, reason: str) -> None:
        """Run one flow, single-flight, capturing the outcome in per-flow state."""
        async with state["lock"]:
            if state["busy"]:
                return
            state["busy"] = True
            fstate = state["flows"][flow]
            fstate["last_started"] = dt.datetime.now().isoformat(timespec="seconds")
            fstate["last_error"] = None
            started = time.monotonic()
            print(f"[web] {flow} run starting (reason={reason})", flush=True)
            try:
                runner = run_follow_once if flow == "follow" else run_connect_once
                exit_code, stats = await asyncio.to_thread(runner, cfg)
                fstate["last_ok"] = exit_code == 0
                if not fstate["last_ok"]:
                    fstate["last_error"] = f"run exited {exit_code}"
                detail = ""
                if stats:
                    detail = (
                        f", sent={stats['sent']}, errors={stats['errors']}, "
                        f"unknown={stats['unknown']}, reason={stats['reason']}"
                    )
                print(
                    f"[web] {flow} run finished (reason={reason}, ok={fstate['last_ok']}, "
                    f"exit={exit_code}{detail}, elapsed={time.monotonic() - started:.1f}s)",
                    flush=True,
                )
            except Exception as exc:  # noqa: BLE001 - report any failure
                fstate["last_ok"] = False
                fstate["last_error"] = f"{type(exc).__name__}: {exc}"
                print(
                    f"[web] {flow} run failed with {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
            finally:
                fstate["last_finished"] = dt.datetime.now().isoformat(timespec="seconds")
                state["busy"] = False

    async def _scheduler_loop() -> None:
        await asyncio.sleep(1)  # let the server bind
        all_times = list(connect_times) + list(follow_times)
        print(
            f"[web] scheduler up: connect at {', '.join(connect_times)} | "
            f"follow at {', '.join(follow_times)} container-local",
            flush=True,
        )
        if boot_run:
            state["flows"]["connect"]["last_slot"] = current_slot(connect_times)
            await _run_job("connect", "startup")
        while True:
            await asyncio.sleep(seconds_until_any(all_times))
            now = dt.datetime.now()
            for flow, flow_times in (("connect", connect_times), ("follow", follow_times)):
                fstate = state["flows"][flow]
                slot = _due_slot(flow_times, fstate["last_slot"], now)
                if slot is None:
                    continue
                fstate["last_slot"] = slot
                await _run_job(flow, f"scheduled {slot}")

    @contextlib.asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(_scheduler_loop())
        try:
            yield
        finally:
            task.cancel()

    app = FastAPI(
        title="linkin-bot",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        lifespan=_lifespan,
    )

    @app.get("/", response_class=HTMLResponse)
    def index() -> Response:
        return HTMLResponse(
            _render_html(_dashboard_text(cfg, state, connect_times, follow_times))
        )

    @app.get("/health")
    def health() -> JSONResponse:
        connect = state["flows"]["connect"]
        return JSONResponse(
            {
                "status": "ok",
                "busy": state["busy"],
                "last_started": connect["last_started"],
                "last_finished": connect["last_finished"],
                "last_ok": connect["last_ok"],
                "last_error": connect["last_error"],
                "flows": state["flows"],
                "schedule": {"connect": connect_times, "follow": follow_times},
                "next_run": {
                    "connect": next_run_at(connect_times).strftime("%Y-%m-%d %H:%M"),
                    "follow": next_run_at(follow_times).strftime("%Y-%m-%d %H:%M"),
                },
            }
        )

    async def _start(flow: str) -> JSONResponse:
        if state["busy"]:
            return JSONResponse(
                {"started": False, "reason": "run already in progress"}, status_code=409
            )
        flow_times = connect_times if flow == "connect" else follow_times
        state["flows"][flow]["last_slot"] = current_slot(flow_times)
        asyncio.create_task(_run_job(flow, "manual"))
        return JSONResponse({"started": True, "flow": flow})

    @app.post("/run")
    async def run_now() -> JSONResponse:
        return await _start("connect")

    @app.post("/run/follow")
    async def run_follow_now() -> JSONResponse:
        return await _start("follow")

    return app
