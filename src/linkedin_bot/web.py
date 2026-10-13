"""linkin-bot as a service: FastAPI dashboard + built-in daily schedule.

The ``linkedin-bot serve`` command runs one long-lived process that serves a
small status dashboard over HTTP and runs the flows itself at fixed daily times
(default connect 09:30, follow 10:00, container-local via TZ). This replaces
host-cron one-shot containers: ``docker compose up -d`` keeps the service alive,
and ``docker compose logs -f`` shows every action as it happens (each click,
scroll and stop reason is logged to stdout as ``[connect]`` / ``[follow]`` lines
by the flow machinery).

Run scheduling is single-flight: an on-schedule run, a ``POST /run`` manual run,
and a ``--boot-run`` startup run can never overlap. Each scheduled slot
(``HH:MM``) fires at most once per day.

fastapi/uvicorn are imported lazily so the plain CLI keeps zero web
dependencies; tests of the pure scheduling helpers below run without them.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime as dt
import sys
import time
from typing import Any, AsyncIterator

from . import db, flow, monitor, publish, session
from .cli import run_flow_once
from .config import load

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

    Guards the multi-schedule loop: on any given wake only the flow whose slot
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


def session_check(flow_name: str, exit_code: int, now: dt.datetime | None = None) -> dict | None:
    """The cookie/session verdict a run implies, or ``None`` if the run says nothing.

    A run's exit code is the cheapest reliable session probe: 0 means the saved
    session reached LinkedIn; 2 means it was rejected (redirect loop / not logged
    in). Other codes (kill switch, crash) tell us nothing about the session.
    """
    now = now or dt.datetime.now()
    at = now.isoformat(timespec="seconds")
    if exit_code == 0:
        return {"at": at, "ok": True, "detail": f"{flow_name} run reached LinkedIn"}
    if exit_code == 2:
        return {"at": at, "ok": False, "detail": f"{flow_name} run: session invalid or blocked"}
    return None


def session_check_line(check: dict | None) -> str:
    """One dashboard line for the last session verdict."""
    if not check or check.get("at") is None:
        return "session check: not run yet"
    mark = "ok" if check.get("ok") else "FAILED"
    return f"session check: {mark} ({check.get('detail', '-')}, {check['at']})"


def _status_dict(cfg, state: dict[str, Any], times: dict[str, list[str]]) -> dict[str, Any]:
    """Machine-readable status: the same data the dashboard page shows, as JSON."""
    conn = db.connect(cfg.paths.db_file)
    usages = flow.usage(cfg, conn)
    events = {name: db.recent_events(conn, limit=6, flow=name) for name in flow.NAMES}
    conn.close()
    return {
        "status": "ok",
        "busy": state["busy"],
        "now": dt.datetime.now().isoformat(timespec="seconds"),
        "kill_switch": monitor.kill_switch_present(cfg.paths.kill_file),
        "posts": {
            "page_url": cfg.post.page_url,
        },
        "session": {**session.session_status(cfg), "last_check": state.get("session")},
        "caps": usages,
        "schedule": {name: list(times[name]) for name in flow.NAMES},
        "next_run": {
            name: next_run_at(times[name]).strftime("%Y-%m-%d %H:%M") for name in flow.NAMES
        },
        "flows": {name: dict(state["flows"][name]) for name in flow.NAMES},
        "events": events,
    }


def _dashboard_text(cfg, state: dict[str, Any], times: dict[str, list[str]]) -> str:
    """Plain-text status block shown on the dashboard page."""
    status = _status_dict(cfg, state, times)
    lines = flow.caps_lines(status["caps"])
    if status["kill_switch"]:
        lines.append("kill switch: STOP file present (runs refuse to start)")
    else:
        lines.append("kill switch: clear")
    lines.append(
        f"post page: {status['posts']['page_url'] or '-'}"
    )
    lines.append(status["session"]["summary"])
    lines.append(session_check_line(status["session"]["last_check"]))
    lines.append("")
    for name in flow.NAMES:
        lines.append(f"next {name} run: {status['next_run'][name]} (container-local)")
    lines.append(f"now             : {status['now']} (container-local)")
    for name in flow.NAMES:
        fstate = status["flows"][name]
        lines.append(
            f"last {name:<7}: started={fstate['last_started']} "
            f"finished={fstate['last_finished']} ok={fstate['last_ok']} "
            f"error={fstate['last_error'] or '-'}"
        )
    lines.append("")
    for name in flow.NAMES:
        lines.append(f"{name} events:")
        flow_events = status["events"][name]
        if not flow_events:
            lines.append("  (none)")
        for event in flow_events:
            suffix = f" {event['message']}" if event["message"] else ""
            lines.append(f"  [{event['ts']}] {event['kind']}{suffix}")
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
        "POST /run/follow starts a manual page follow run · "
        "POST /post publishes as the Page · /status shows status "
        "as JSON · /health shows liveness as JSON · "
        "docker compose logs -f shows every action</p>"
        "</body></html>\n"
    )


def create_app(
    times: dict[str, list[str]] | None = None,
    boot_run: bool = False,
) -> Any:
    """Build the FastAPI app (imports fastapi lazily on first call).

    ``times`` maps a flow name to its daily ``HH:MM`` slot(s); any flow left out
    falls back to its registry default. Every flow shares the one process and
    one lock — they use the same LinkedIn account, so no two runs may overlap.
    """
    from fastapi import Body, FastAPI
    from fastapi.responses import HTMLResponse, JSONResponse, Response

    cfg = load()
    schedule: dict[str, list[str]] = {
        name: list(flow.SPECS[name].default_at) for name in flow.NAMES
    }
    for name, value in (times or {}).items():
        if name not in flow.SPECS:
            raise ValueError(f"unknown flow {name!r}; known: {', '.join(flow.NAMES)}")
        schedule[name] = list(value)
    for name in flow.NAMES:
        if not schedule[name]:
            raise ValueError(f"serve needs at least one {name} time (HH:MM)")
        for hhmm in schedule[name]:
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
        "flows": {name: _blank() for name in flow.NAMES},
        "session": None,
    }

    async def _run_job(name: str, reason: str) -> None:
        """Run one flow, single-flight, capturing the outcome in per-flow state."""
        async with state["lock"]:
            if state["busy"]:
                return
            state["busy"] = True
            fstate = state["flows"][name]
            fstate["last_started"] = dt.datetime.now().isoformat(timespec="seconds")
            fstate["last_error"] = None
            started = time.monotonic()
            print(f"[web] {name} run starting (reason={reason})", flush=True)
            try:
                exit_code, stats = await asyncio.to_thread(run_flow_once, cfg, name)
                fstate["last_ok"] = exit_code == 0
                if not fstate["last_ok"]:
                    fstate["last_error"] = f"run exited {exit_code}"
                check = session_check(name, exit_code)
                if check is not None:
                    state["session"] = check
                detail = ""
                if stats:
                    detail = (
                        f", sent={stats['sent']}, errors={stats['errors']}, "
                        f"unknown={stats['unknown']}, reason={stats['reason']}"
                    )
                print(
                    f"[web] {name} run finished (reason={reason}, ok={fstate['last_ok']}, "
                    f"exit={exit_code}{detail}, elapsed={time.monotonic() - started:.1f}s)",
                    flush=True,
                )
            except Exception as exc:  # noqa: BLE001 - report any failure
                fstate["last_ok"] = False
                fstate["last_error"] = f"{type(exc).__name__}: {exc}"
                print(
                    f"[web] {name} run failed with {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
            finally:
                fstate["last_finished"] = dt.datetime.now().isoformat(timespec="seconds")
                state["busy"] = False

    async def _scheduler_loop() -> None:
        await asyncio.sleep(1)  # let the server bind
        all_times = [slot for name in flow.NAMES for slot in schedule[name]]
        for name in flow.NAMES:
            print(
                f"[web] schedule: {name} at {', '.join(schedule[name])} container-local",
                flush=True,
            )
        if boot_run:
            state["flows"]["connect"]["last_slot"] = current_slot(schedule["connect"])
            await _run_job("connect", "startup")
        while True:
            await asyncio.sleep(seconds_until_any(all_times))
            now = dt.datetime.now()
            for name in flow.NAMES:
                fstate = state["flows"][name]
                slot = _due_slot(schedule[name], fstate["last_slot"], now)
                if slot is None:
                    continue
                fstate["last_slot"] = slot
                await _run_job(name, f"scheduled {slot}")

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
        return HTMLResponse(_render_html(_dashboard_text(cfg, state, schedule)))

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
                "schedule": {name: list(schedule[name]) for name in flow.NAMES},
                "next_run": {
                    name: next_run_at(schedule[name]).strftime("%Y-%m-%d %H:%M")
                    for name in flow.NAMES
                },
            }
        )

    @app.get("/status")
    def status() -> JSONResponse:
        """Full status (caps, kill switch, posts, session, events) as JSON, for peer apps."""
        return JSONResponse(_status_dict(cfg, state, schedule))

    async def _start(name: str) -> JSONResponse:
        if state["busy"]:
            return JSONResponse(
                {"started": False, "reason": "run already in progress"}, status_code=409
            )
        state["flows"][name]["last_slot"] = current_slot(schedule[name])
        asyncio.create_task(_run_job(name, "manual"))
        return JSONResponse({"started": True, "flow": name})

    @app.post("/run")
    async def run_now() -> JSONResponse:
        return await _start("connect")

    @app.post("/run/follow")
    async def run_follow_now() -> JSONResponse:
        return await _start("follow")

    @app.post("/post")
    async def publish_post(payload: dict = Body(...)) -> JSONResponse:
        """Publish a post as the managed Page (the posts admin, via a browser).

        The gateway a peer app (social-bot) calls. Body: ``{"text": "..."}``, plus
        an optional image as base64 ``{"image_base64": "...",
        "image_content_type": "image/png", "image_filename": "..."}``. Posting as
        the Page drives a browser on the same account as the flows, so it takes
        the shared lock and returns 409 while another run is in progress.
        """
        text = str(payload.get("text") or "").strip()
        image = None
        image_b64 = payload.get("image_base64")
        if image_b64:
            try:
                image = base64.b64decode(image_b64)
            except ValueError as exc:
                return JSONResponse(
                    {"ok": False, "error": f"invalid image_base64: {exc}"}, status_code=400
                )
            if not image:
                return JSONResponse(
                    {"ok": False, "error": "image_base64 decoded to empty data"}, status_code=400
                )
        if not text and image is None:
            return JSONResponse(
                {"ok": False, "error": "missing 'text' (or 'image_base64')"}, status_code=400
            )
        if state["busy"]:
            return JSONResponse(
                {"ok": False, "error": "a run is in progress; try again shortly"},
                status_code=409,
            )
        async with state["lock"]:
            state["busy"] = True
            try:
                result = await asyncio.to_thread(
                    publish.publish_to_page,
                    cfg,
                    text=text,
                    image=image,
                    image_filename=payload.get("image_filename"),
                    image_content_type=str(
                        payload.get("image_content_type")
                        or publish.IMAGE_CONTENT_TYPE_DEFAULT
                    ),
                )
            except publish.PublishError as exc:
                print(f"[post] failed: {exc}", flush=True)
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
            except Exception as exc:  # noqa: BLE001 - report any failure
                print(f"[post] failed: {type(exc).__name__}: {exc}", flush=True)
                return JSONResponse(
                    {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=502
                )
            finally:
                state["busy"] = False
        return JSONResponse({"ok": True, "as": "page", **result})

    return app
