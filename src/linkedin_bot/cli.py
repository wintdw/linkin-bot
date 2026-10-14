"""linkedin-bot command line interface: login, connect, follow, serve, status, stats."""

from __future__ import annotations

import argparse
from pathlib import Path

from . import config as cfgmod
from . import db, flow, monitor
from .session import SessionInvalidError, open_logged_in_page, session_exists, session_status


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="linkedin-bot",
        description="Capped, human-paced LinkedIn 'My Network' connection bot.",
    )
    parser.add_argument("--config", help="path to config.yaml (default: project config.yaml)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="open a visible browser and save the LinkedIn session")

    p_connect = sub.add_parser(
        "connect",
        aliases=["run"],
        help="open My Network and click Connect up to today's cap",
    )
    p_connect.add_argument("--headed", action="store_true", help="show the browser while running")
    p_connect.add_argument("--dry-run", action="store_true", help="count Connect buttons, click nothing")
    p_connect.add_argument("--limit", type=int, default=None, help="override this run's cap")

    p_follow = sub.add_parser(
        "follow",
        help="invite connections to follow a managed Page, up to the credits available",
    )
    p_follow.add_argument("--headed", action="store_true", help="show the browser while running")
    p_follow.add_argument(
        "--dry-run", action="store_true", help="open the invite dialog, count rows, click nothing"
    )
    p_follow.add_argument("--limit", type=int, default=None, help="override this run's cap")

    p_post = sub.add_parser(
        "post",
        help="publish a post as the managed Page (browser; the service also posts as you)",
    )
    p_post.add_argument("--text", default="", help="post text")
    p_post.add_argument("--image", default=None, help="path to an image to attach")
    p_post.add_argument(
        "--comment",
        default=None,
        help="comment to post on the new post, as the Page (e.g. a first comment)",
    )
    p_post.add_argument(
        "--dry-run", action="store_true", help="open the composer page, report, post nothing"
    )
    p_post.add_argument("--headed", action="store_true", help="show the browser while running")

    p_serve = sub.add_parser(
        "serve", help="FastAPI dashboard + built-in daily schedule (long-lived service)"
    )
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8080)
    p_serve.add_argument(
        "--connect-at",
        "--run-at",
        dest="connect_at",
        nargs="+",
        default=list(flow.SPECS["connect"].default_at),
        metavar="HH:MM",
        help="daily connect-run time(s), local to the container (default: 09:30)",
    )
    p_serve.add_argument(
        "--follow-at",
        nargs="+",
        default=list(flow.SPECS["follow"].default_at),
        metavar="HH:MM",
        help="daily page follow-invite time(s), local to the container (default: 10:00)",
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


def run_connect_once(cfg, dry_run: bool = False, limit: int | None = None, headed: bool = False):
    """Connect-flow run (personal My Network requests). See ``run_flow_once``."""
    return run_flow_once(cfg, "connect", dry_run=dry_run, limit=limit, headed=headed)


def run_follow_once(cfg, dry_run: bool = False, limit: int | None = None, headed: bool = False):
    """Follow-flow run (invite connections to follow a managed Page)."""
    return run_flow_once(cfg, "follow", dry_run=dry_run, limit=limit, headed=headed)


def run_flow_once(
    cfg,
    flow_name: str = "connect",
    dry_run: bool = False,
    limit: int | None = None,
    headed: bool = False,
) -> tuple[int, dict | None]:
    """Execute one full run of a flow (prechecks + browser). Returns (exit_code, stats).

    ``flow_name`` selects the flow from the registry (``connect`` / ``follow``).
    Shared by the CLI commands and the ``serve`` scheduler. Exit codes: 0 clean
    stop, 1 missing session or kill switch, 2 stale session, 3 crash.
    """
    spec = flow.spec(flow_name)

    if monitor.kill_switch_present(cfg.paths.kill_file):
        print("data/STOP present - remove it before running.", flush=True)
        return 1, None
    if not session_exists(cfg):
        print("No saved session found - run `linkedin-bot login` first.", flush=True)
        return 1, None

    conn = db.connect(cfg.paths.db_file)
    headless = bool(cfg.browser.headless) and not headed
    print(f"[{spec.name}] starting" + (" (headless)" if headless else " (headed)"), flush=True)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        try:
            browser, _context, page = open_logged_in_page(playwright, cfg, headed=headed)
        except SessionInvalidError as exc:
            print(
                f"{exc} - the saved session is likely invalid or blocked; "
                "run `linkedin-bot login`.",
                flush=True,
            )
            return 2, None

        try:
            if dry_run:
                print(spec.dry_run_fn()(page, cfg), flush=True)
                return 0, None

            db.record_event(conn, "RUN_START", "", spec.name)

            stats = None
            try:
                stats = spec.loop_fn()(page, cfg, conn, extra_limit=limit)
            except Exception as exc:
                db.record_event(conn, "RUN_END", f"crash: {exc.__class__.__name__}: {exc}", spec.name)
                print(f"[{spec.name}] run crashed: {exc.__class__.__name__}: {exc}", flush=True)
                return 3, None
        finally:
            browser.close()

    print(
        f"[{spec.name}] finished: sent={stats['sent']} errors={stats['errors']} "
        f"unknown={stats['unknown']} refreshed={stats['refreshed']} reason={stats['reason']}",
        flush=True,
    )
    return 0, stats


def cmd_connect(args) -> int:
    cfg = cfgmod.load(args.config)
    code, _ = run_connect_once(cfg, dry_run=args.dry_run, limit=args.limit, headed=args.headed)
    return code


def cmd_follow(args) -> int:
    cfg = cfgmod.load(args.config)
    code, _ = run_follow_once(cfg, dry_run=args.dry_run, limit=args.limit, headed=args.headed)
    return code


def cmd_post(args) -> int:
    from . import publish

    cfg = cfgmod.load(args.config)
    image = None
    filename = None
    if args.image:
        path = Path(args.image)
        try:
            image = path.read_bytes()
        except OSError as exc:
            print(f"could not read {path}: {exc}", flush=True)
            return 1
        filename = path.name
    try:
        result = publish.publish_to_page(
            cfg,
            text=args.text,
            image=image,
            image_filename=filename,
            comment=args.comment,
            dry_run=args.dry_run,
            headed=args.headed,
        )
    except publish.PublishError as exc:
        status = str(getattr(exc, "status", publish.STATUS_REFUSED))
        print(f"[post] failed ({status}): {exc}", flush=True)
        if not publish.is_retryable(status):
            print(
                "[post] the post may have gone out - do NOT retry; check the Page's posts list",
                flush=True,
            )
        return 1
    if result.get("detail"):
        print(result["detail"], flush=True)
    else:
        line = f"[post] ok: {result.get('author')}"
        if result.get("permalink"):
            line += f" {result['permalink']}"
        print(line, flush=True)
        if result.get("commented"):
            print("[post] commented on the new post", flush=True)
        elif result.get("comment_error"):
            print(f"[post] comment failed: {result['comment_error']}", flush=True)
            return 1
    return 0


def cmd_serve(args) -> int:
    from .web import create_app

    import uvicorn

    times = {name: getattr(args, f"{name}_at") for name in flow.NAMES}
    app = create_app(times=times, boot_run=args.boot_run)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def cmd_status(args) -> int:
    cfg = cfgmod.load(args.config)
    conn = db.connect(cfg.paths.db_file)
    for line in flow.caps_lines(flow.usage(cfg, conn)):
        print(line)
    if monitor.kill_switch_present(cfg.paths.kill_file):
        print("kill switch: STOP file present (runs will refuse to start)")
    else:
        print("kill switch: clear")
    print(session_status(cfg)["summary"])
    for name in flow.NAMES:
        print(f"{name} events:")
        events = db.recent_events(conn, limit=6, flow=name)
        if not events:
            print("  (none)")
        for event in events:
            suffix = f" {event['message']}" if event["message"] else ""
            print(f"  [{event['ts']}] {event['kind']}{suffix}")
    conn.close()
    return 0


def cmd_stats(args) -> int:
    cfg = cfgmod.load(args.config)
    conn = db.connect(cfg.paths.db_file)
    for name in flow.NAMES:
        print(name.upper())
        counts = db.outcome_counts(conn, kind=name)
        if not counts:
            print("  (nothing recorded yet)")
        else:
            for outcome, count in sorted(counts.items()):
                print(f"  {outcome:<16} {count}")
        print("  last 14 days:")
        rows = db.last_days_summary(conn, days=14, kind=name)
        if not rows:
            print("    (no sends yet)")
        for row in rows:
            print(f"    {row['day']}  sent={row['sent']}  errors={row['errors']}")
        print()
    conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "login":
        return cmd_login(args)
    if args.command in ("connect", "run"):
        return cmd_connect(args)
    if args.command == "follow":
        return cmd_follow(args)
    if args.command == "post":
        return cmd_post(args)
    if args.command == "serve":
        return cmd_serve(args)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "stats":
        return cmd_stats(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
