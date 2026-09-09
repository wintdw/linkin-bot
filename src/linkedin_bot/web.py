"""linkin-bot as a service: FastAPI dashboard + built-in daily schedule.

The ``linkedin-bot serve`` command runs one long-lived process that serves a
small status dashboard over HTTP and runs the connect loop itself at fixed
daily times (default 09:30, container-local via TZ). This replaces host-cron
one-shot containers: ``docker compose up -d`` keeps the service alive, and
``docker compose logs -f`` shows every action as it happens (each click, scroll
and stop reason is logged to stdout by the ``run`` machinery).

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
from .cli import run_once
from .config import load

DEFAULT_RUN_TIMES = ("09:30",)


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


def _dashboard_text(cfg, state: dict[str, Any], times: list[str]) -> str:
    """Plain-text status block shown on the dashboard page."""
    conn = db.connect(cfg.paths.db_file)
    usage = scheduler_mod.remaining_today(cfg, conn)
    lines = [
        f"today: {usage['sent_today']}/{usage['day_cap']} sent, "
        f"{usage['day_remaining']} remaining",
        f"week : {usage['sent_week']}/{usage['week_cap']} sent, "
        f"{usage['week_remaining']} remaining",
    ]
    if monitor.kill_switch_present(cfg.paths.kill_file):
        lines.append("kill switch: STOP file present (runs refuse to start)")
    else:
        lines.append("kill switch: clear")
    lines.append("")
    lines.append(f"next run: {next_run_at(times):%Y-%m-%d %H:%M} (container-local)")
    lines.append(f"now     : {dt.datetime.now():%Y-%m-%d %H:%M:%S} (container-local)")
    lines.append(
        f"last run: started={state['last_started']} finished={state['last_finished']} "
        f"ok={state['last_ok']} error={state['last_error'] or '-'}"
    )
    lines.append("")
    lines.append("recent events:")
    for event in db.recent_events(conn, limit=12):
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
        "<p class=\"muted\">POST /run starts a manual run · /health shows state "
        "as JSON · docker compose logs -f shows every action</p>"
        "</body></html>\n"
    )


def create_app(
    run_at: list[str] | tuple[str, ...] | None = None,
    boot_run: bool = False,
) -> Any:
    """Build the FastAPI app (imports fastapi lazily on first call)."""
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, JSONResponse, Response

    cfg = load()
    times = list(run_at) if run_at else list(DEFAULT_RUN_TIMES)
    if not times:
        raise ValueError("serve needs at least one run time (HH:MM)")
    for hhmm in times:
        _parse_hhmm(hhmm)

    state: dict[str, Any] = {
        "lock": asyncio.Lock(),
        "busy": False,
        "last_started": None,
        "last_finished": None,
        "last_ok": None,
        "last_error": None,
        "last_slot": None,
    }

    async def _run_job(reason: str) -> None:
        """Run one connect run, single-flight, capturing outcome in state."""
        async with state["lock"]:
            if state["busy"]:
                return
            state["busy"] = True
            state["last_started"] = dt.datetime.now().isoformat(timespec="seconds")
            state["last_error"] = None
            started = time.monotonic()
            print(f"[web] run starting (reason={reason})", flush=True)
            try:
                exit_code, stats = await asyncio.to_thread(run_once, cfg)
                state["last_ok"] = exit_code == 0
                if not state["last_ok"]:
                    state["last_error"] = f"run exited {exit_code}"
                detail = ""
                if stats:
                    detail = (
                        f", sent={stats['sent']}, errors={stats['errors']}, "
                        f"unknown={stats['unknown']}, reason={stats['reason']}"
                    )
                print(
                    f"[web] run finished (reason={reason}, ok={state['last_ok']}, "
                    f"exit={exit_code}{detail}, elapsed={time.monotonic() - started:.1f}s)",
                    flush=True,
                )
            except Exception as exc:  # noqa: BLE001 - report any failure
                state["last_ok"] = False
                state["last_error"] = f"{type(exc).__name__}: {exc}"
                print(
                    f"[web] run failed with {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
            finally:
                state["last_finished"] = dt.datetime.now().isoformat(timespec="seconds")
                state["busy"] = False

    async def _scheduler_loop() -> None:
        await asyncio.sleep(1)  # let the server bind
        print(
            f"[web] scheduler up: run at {', '.join(times)} container-local "
            f"(next: {next_run_at(times):%Y-%m-%d %H:%M})",
            flush=True,
        )
        if boot_run:
            await _run_job("startup")
            state["last_slot"] = current_slot(times)
        while True:
            await asyncio.sleep(seconds_until_any(times))
            slot = current_slot(times)
            if slot is None or state["last_slot"] == slot:
                continue  # this slot already ran (e.g. startup or manual run)
            state["last_slot"] = slot
            await _run_job(f"scheduled {slot}")

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
        return HTMLResponse(_render_html(_dashboard_text(cfg, state, times)))

    @app.get("/health")
    def health() -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "busy": state["busy"],
                "last_started": state["last_started"],
                "last_finished": state["last_finished"],
                "last_ok": state["last_ok"],
                "last_error": state["last_error"],
                "schedule": times,
                "next_run": next_run_at(times).strftime("%Y-%m-%d %H:%M"),
            }
        )

    @app.post("/run")
    async def run_now() -> JSONResponse:
        if state["busy"]:
            return JSONResponse({"started": False, "reason": "run already in progress"}, status_code=409)
        state["last_slot"] = current_slot(times)
        asyncio.create_task(_run_job("manual"))
        return JSONResponse({"started": True})

    return app
