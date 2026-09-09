"""linkedin-bot command line interface: login, run, serve, status, stats."""

from __future__ import annotations

import argparse
from pathlib import Path

from . import config as cfgmod
from . import db, monitor, scheduler
from .session import is_logged_in, session_exists

FEED_URL = "https://www.linkedin.com/feed/"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="linkedin-bot",
        description="Capped, human-paced LinkedIn 'My Network' connection bot.",
    )
    parser.add_argument("--config", help="path to config.yaml (default: project config.yaml)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_login = sub.add_parser("login", help="open a visible browser and save the LinkedIn session")

    p_run = sub.add_parser("run", help="open My Network and click Connect up to today's cap")
    p_run.add_argument("--headed", action="store_true", help="show the browser while running")
    p_run.add_argument("--dry-run", action="store_true", help="count Connect buttons, click nothing")
    p_run.add_argument("--limit", type=int, default=None, help="override this run's cap")

    p_serve = sub.add_parser(
        "serve", help="FastAPI dashboard + built-in daily schedule (long-lived service)"
    )
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8080)
    p_serve.add_argument(
        "--run-at",
        nargs="+",
        default=["09:30"],
        metavar="HH:MM",
        help="daily run time(s), local to the container (default: 09:30)",
    )
    p_serve.add_argument(
        "--boot-run",
        action="store_true",
        help="also run the connect loop shortly after startup",
    )

    sub.add_parser("status", help="show caps usage and health")
    sub.add_parser("stats", help="show send history from the ledger")
    return parser


def cmd_login(args) -> int:
    from .session import login

    cfg = cfgmod.load(args.config)
    return 0 if login(cfg) else 1


def run_once(
    cfg,
    dry_run: bool = False,
    limit: int | None = None,
    headed: bool = False,
) -> tuple[int, dict | None]:
    """Execute one full connect run (prechecks + browser). Returns (exit_code, stats).

    Shared by the `run` command and the `serve` scheduler. Exit codes: 0 clean
    stop, 1 missing session or kill switch, 2 stale session, 3 crash.
    """
    from .network import run_connect_loop

    if monitor.kill_switch_present(cfg.paths.kill_file):
        print("data/STOP present - remove it before running.", flush=True)
        return 1, None
    if not session_exists(cfg):
        print("No saved session found - run `linkedin-bot login` first.", flush=True)
        return 1, None

    conn = db.connect(cfg.paths.db_file)
    headless = bool(cfg.browser.headless) and not headed
    print("[run] starting" + (" (headless)" if headless else " (headed)"), flush=True)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        try:
            context = browser.new_context(
                storage_state=str(Path(cfg.paths.session_file)),
                viewport={"width": 1366, "height": 900},
            )
            page = context.new_page()
            page.set_default_timeout(int(cfg.browser.timeout_ms))

            page.goto(FEED_URL, wait_until="domcontentloaded")
            # A restored session can pass through LinkedIn's transient
            # ssr-login/remember-me-auto-login redirect before the global nav
            # renders, so poll for the nav (bounded) before declaring the
            # session invalid.
            logged_in = False
            for _ in range(10):
                if is_logged_in(page):
                    logged_in = True
                    break
                page.wait_for_timeout(2000)
            if not logged_in:
                print("Session is no longer valid - run `linkedin-bot login` again.", flush=True)
                return 2, None

            if dry_run:
                from .network import connect_locator

                page.goto(cfg.network.url, wait_until="domcontentloaded")
                scheduler.sleep_post_scroll(cfg)
                buttons = connect_locator(page).all()
                visible = sum(1 for b in buttons if b.is_visible())
                print(f"dry-run: {visible} Connect button(s) visible on My Network", flush=True)
                return 0, None

            db.record_event(conn, "RUN_START")

            stats = None
            try:
                stats = run_connect_loop(page, cfg, conn, extra_limit=limit)
            except Exception as exc:
                db.record_event(conn, "RUN_END", f"crash: {exc.__class__.__name__}: {exc}")
                print(f"run crashed: {exc.__class__.__name__}: {exc}", flush=True)
                return 3, None
        finally:
            browser.close()

    print(
        f"[run] finished: sent={stats['sent']} errors={stats['errors']} "
        f"unknown={stats['unknown']} scrolled={stats['scrolled']} reason={stats['reason']}",
        flush=True,
    )
    return 0, stats


def cmd_run(args) -> int:
    cfg = cfgmod.load(args.config)
    code, _ = run_once(cfg, dry_run=args.dry_run, limit=args.limit, headed=args.headed)
    return code


def cmd_serve(args) -> int:
    from .web import create_app

    import uvicorn

    app = create_app(run_at=args.run_at, boot_run=args.boot_run)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def cmd_status(args) -> int:
    cfg = cfgmod.load(args.config)
    conn = db.connect(cfg.paths.db_file)
    usage = scheduler.remaining_today(cfg, conn)
    print(f"today: {usage['sent_today']}/{usage['day_cap']} sent, {usage['day_remaining']} remaining")
    print(f"week : {usage['sent_week']}/{usage['week_cap']} sent, {usage['week_remaining']} remaining")
    if monitor.kill_switch_present(cfg.paths.kill_file):
        print("kill switch: STOP file present (runs will refuse to start)")
    else:
        print("kill switch: clear")
    print("recent events:")
    for event in db.recent_events(conn, limit=8):
        suffix = f" {event['message']}" if event["message"] else ""
        print(f"  [{event['ts']}] {event['kind']}{suffix}")
    conn.close()
    return 0


def cmd_stats(args) -> int:
    cfg = cfgmod.load(args.config)
    conn = db.connect(cfg.paths.db_file)
    counts = db.outcome_counts(conn)
    if not counts:
        print("No invitations recorded yet.")
    else:
        for outcome, count in sorted(counts.items()):
            print(f"{outcome:<16} {count}")
    print("\nlast 14 days:")
    for row in db.last_days_summary(conn, days=14):
        print(f"  {row['day']}  sent={row['sent']}  errors={row['errors']}")
    conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "login":
        return cmd_login(args)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "serve":
        return cmd_serve(args)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "stats":
        return cmd_stats(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
