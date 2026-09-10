# AGENTS.md — LinkedIn Connect Bot

Guidance for agents and humans working on this project. Read this before
touching code or running anything.

## What this project is

A Python 3 + Playwright bot that logs into LinkedIn with account credentials,
opens the **My Network** suggestions page (`/mynetwork/grow/`), and clicks
**Connect** on suggested people — paced by randomized delays and hard-capped per
day/week. No API, no CSV targets: it works the live suggestions feed. State is
tracked in a local SQLite ledger.

**Compliance context (do not lose this):** automating connection requests
breaches LinkedIn's User Agreement §8.2 and its Prohibited Software policy.
Accounts can be restricted or banned. The code is deliberately conservative:
caps, randomized pacing, checkpoint detection, kill switch. Any change that
raises volume or removes safety controls should be treated as high-risk and
flagged to the operator.

## Architecture

```
src/linkedin_bot/
├── cli.py        # entry point (linkedin-bot): login / run / serve / status / stats
├── config.py     # yaml + DEFAULTS deep-merge; path resolution; validation
├── session.py    # manual GUI login (no auto-fill); persists storage_state
├── network.py    # My Network scan + Connect clicker + outcome classification
├── scheduler.py  # daily/weekly caps, warm-up ramp, delay helpers
├── monitor.py    # kill switch, checkpoint URL/text detection
├── db.py         # SQLite ledger (invitations, events)
└── web.py        # FastAPI dashboard + built-in daily schedule (serve)
```

Runtime flow: `login` (once, GUI, saves cookies) → `run` (headless, loads
cookies) → per cycle: click every visible Connect button (short pause between
clicks) → classify → record in SQLite → wait `network.refresh_wait_seconds` →
reload My Network → repeat until a cap or a health stop. On each click the
visible Connect buttons are re-queried and the first *unseen* card (by
aria-label) is clicked, so a lingering sent card is never clicked twice and a
stale locator never stalls the run. Every action taken is printed to stdout
(`[run]` lines: click outcomes, refreshes, stop reasons), so a `serve` container
shows the whole run live under `docker compose logs -f`.

`serve` is the long-lived service mode: a FastAPI app (lazy-imported) that
runs the connect loop itself at daily `HH:MM` slots (default 09:30,
container-local TZ) — no host cron. Scheduled/manual (`POST /run`)/startup
(`--boot-run`) runs are single-flight via an asyncio lock, and each slot fires
at most once a day (same pattern as voz-bot/otofun-bot). Dashboard on `/`,
state as JSON on `/health`.

`run` exit codes: `0` clean stop · `1` missing session or `data/STOP` present ·
`2` saved session no longer valid (re-run `login`) · `3` unexpected crash. The
end-of-run reason is recorded in the `RUN_END` event and echoed by the CLI:
`cap` (daily/weekly cap), `limit` (`--limit` budget hit), `exhausted` (no more
cards after `network.max_empty_refreshes` reloads), `checkpoint`, `kill-switch`,
`linkedin-limit`, `refresh-failed`, `end`.

### Data & state

- `data/bot.db` — SQLite. `invitations` rows: profile URL, name, outcome,
  `clicked_at` (UTC ISO8601, `Z` suffix), optional `details`. `events` rows:
  `RUN_START`/`RUN_END` with a `message` (RUN_END records counts + reason).
- `data/sessions/linkedin_storage_state.json` — saved cookies from `login`.
- `data/STOP` — kill-switch marker; bot halts before the next click while it
  exists.
- Credentials are never read or stored by the bot — `login` is fully manual
  (you type email/password and clear any OTP/CAPTCHA in the browser window).

### Outcome semantics (important)

- `SENT_PENDING` — card detached or its aria-label changed after the click
  (LinkedIn recycles the button node for the next suggestion).
- `UNKNOWN` — clicked but no state change observed within ~5 s. **Counted as a
  send** against caps (conservative; it may have gone through).
- `ERROR` / `LIMIT` — click failed / LinkedIn's own invitation-limit notice
  shown (stops the run).

### Config (`config.yaml`, live-editable)

Caps (`caps.daily` = 10, `caps.weekly` = 100) are **never exceeded**; the
scheduler counts `SENT_PENDING` + `UNKNOWN` against both. `warmup.enabled` is
off (established account); re-enable for a fresh account ramp. `delays` are the
randomized pause between clicks (`min_seconds`–`max_seconds`, plus short
`post_scroll_*` pauses after each page load/refresh). `browser` sets `headless`
and per-action `timeout_ms` (`slow_mo_ms` for debugging). `network` controls the
refresh cycle (`refresh_wait_seconds` wait before reloading, `max_empty_refreshes`
reloads with no cards before stopping, `click_timeout_ms` per-element timeout so
a stale card fails fast instead of waiting out `browser.timeout_ms`).
`monitor.security_scan_every` paces page-text security scans (every N sends).
`login.checkpoint_wait_seconds` bounds the manual-login wait. Relative `paths`
resolve against the repo root. Bad values fail fast at load (caps `daily <=
weekly`, `delays.max_seconds >= min_seconds`); the file is re-read on every
run, so edits apply without a rebuild.

## Running locally

One-time setup from the repo root:

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
python -m pip install -e ".[dev]"
playwright install chromium
```

(`serve` additionally needs fastapi + uvicorn: install `".[dev,server]"`.)

Always use the venv interpreter (`.venv/bin/python` on macOS/Linux,
`.venv\Scripts\python.exe` on Windows), never a system `python` — the package
is only installed into the venv. `python -m linkedin_bot.cli` and the installed
`linkedin-bot` script are equivalent:

```powershell
linkedin-bot login                    # manual GUI login by hand, once
linkedin-bot run --dry-run            # selector check; counts visible buttons
linkedin-bot run                      # headless until cap
linkedin-bot run --headed             # watch it
linkedin-bot run --limit N            # cap one run
linkedin-bot serve --run-at 09:30     # FastAPI dashboard + daily schedule
linkedin-bot status | stats
```

Windows consoles: set `PYTHONIOENCODING=utf-8` before anything that prints
(`$env:PYTHONIOENCODING="utf-8"`) — LinkedIn pages contain Vietnamese text that
crashes cp1252 stdout. (macOS/Linux default to UTF-8.)

Tests (no browser needed, pure logic only):

```bash
python -m pytest                     # 24 tests
```

## Deployment (Linux, Docker)

Clean-room image + compose live at the repo root (`Dockerfile`,
`docker-compose.yml`, `.dockerignore`) — same layout as voz-bot/otofun-bot.
Non-root uid 10001 (`linkinbot`); `./data` on the host must be writable by it
(`sudo chown -R 10001:10001 data` once). Data and `config.yaml` are
bind-mounted from the host; only code changes require `docker compose build`
(BuildKit pip cache keeps rebuilds fast).

The container runs `serve` (FastAPI dashboard on host port 8082, matching the
voz-bot=8080 / otofun-bot=8081 convention) and runs its own daily schedule —
**no host cron**. The one-time GUI login runs through the container on the
host's X11 socket (`xhost +local:` first). The full runbook lives in the
README. Watch a live run with `docker compose logs -f` (every click/scroll/stop
is logged); force a run with `POST localhost:8082/run`.

**Session portability rule:** do not transplant `storage_state` across
machines/IPs. LinkedIn re-verifies accounts that appear on a new IP. Log in
fresh on the server and let that IP become the account's home. (Pilot result,
Sept 2026: a transplanted session *was* accepted from the server IP after a
`remember-me-auto-login` handshake — treat acceptance as probable for an
established account, not guaranteed.) Never run two instances against the same
account concurrently (double-sends + detection).

## Hard-won facts (verified against live LinkedIn, Sept 2026)

- `/mynetwork/` redirects to `/mynetwork/grow/`; suggestion Connect buttons
  carry `aria-label="Invite <Name> to connect"`. **An exact
  `get_by_role(name="Connect", exact=True)` finds nothing** — the loose
  substring match is required (`connect_locator` in `network.py`).
- **Physical mouse clicks get swallowed by transient ad overlays** on the grow
  feed. The clicker dispatches `button.evaluate("el => el.click()")` instead.
- After a send, LinkedIn removes/recycles the card, so success is detected by
  the original node detaching OR its aria-label changing to a different
  "Invite ... to connect".
- Auto-filling the login form is unreliable (localized button text such as
  Vietnamese "Đăng nhập", empty-field pages, evolving DOM), so `login` no
  longer attempts it: the operator signs in by hand and the wait loop just
  polls for the global nav (`a[href="/feed/"]`, `/mynetwork/`, `/messaging/`).
- Fresh automated logins almost always trigger a security check; `login` waits
  (default 30 min) for the operator to finish OTP/CAPTCHA in the open window.
  **Closing the window early kills the wait** with a `TargetClosedError`.
- LinkedIn rejects a Connect click with a **transient error toast** when the
  invitation limit is hit; live wording (Sept 2026): "Your invitation to X was
  not sent because you have reached the weekly limit for connection
  invitations." The toast is the **only** signal (the Connect button itself
  stays "Connect"), and it is easy to miss: if not caught, every rejected click
  looks like a send. Matched by `monitor.INVITATION_LIMIT_RE` and stops the run
  (`LIMIT`).
- `_click_connect` **pins the button to an element handle** before clicking.
  The `connect_locator` locator re-resolves by index and the suggestion list
  shifts as cards are sent, so comparing a re-resolved locator's aria-label
  reports a send even when the same card was rejected — the classic false
  `SENT_PENDING`.
- The Connect-button list must be **re-queried per click**: after a send the
  button turns "Pending", so a list collected once goes stale and its tail
  indices stop resolving — each miss then waits out the full page timeout, and
  three element ops per click ≈ a 60 s stall per card. Sent cards can also
  linger at the head with an unchanged aria-label, so clicks are de-duplicated
  by aria-label (`_next_unseen`); otherwise the same person is clicked every
  pass until the cap.
- Background PowerShell tasks in this dev environment capture stdout
  unreliably (empty logs) — verify with `status`/`stats` (ledger) or run in the
  foreground.
- A restored session on a new IP first hits a transient
  `ssr-login/remember-me-auto-login` interstitial that re-validates the token
  and redirects to the target — **not** a checkpoint. The feed may not be
  rendered yet at that point, so `run --dry-run` can report `0 Connect
  button(s)` on the first hit even though the session is fine; the real run's
  refresh/repoll loop absorbs the delay. `run` itself polls for the global nav
  (~20 s, `cli.py`) before reporting exit 2, so a single interstitial passes;
  only re-login when the poll times out (a real checkpoint, or the interstitial
  stalled). Re-run the dry-run (or check the page title says "Grow") before
  declaring a session dead.

## Guardrails (do not silently weaken)

- Caps are hard floors — the loop re-checks remaining before every click.
- Kill switch (`data/STOP`), checkpoint URL detection (`/checkpoint/`,
  `/authwall`, "challenge"), and periodic security-text scans stop runs. The
  scan runs every `monitor.security_scan_every` sends (default 5).
- `delays` between clicks default to 0.8–1.2 s in `config.yaml` (operator-set to
  batch each refresh cycle quickly — 8 clicks in ~10 s). The pacing is therefore
  far tighter than the original 45–120 s; raising volume further or shortening
  these delays again is an operator decision, not a code fix.
- Caps are 50→**10/day** and 300→**100/week**: LinkedIn enforces its own weekly
  invitation limit (hit Sept 2026) and it binds before our caps — the bot now
  detects that rejection toast and stops (`LIMIT`). Changing caps upward is an
  operator decision, not a code fix.

## Scope / v2 (not implemented)

- Personalized "Add a note" via the profile-page flow.
- Acceptance-rate tracking (needs LinkedIn's Sent-invitations page).
- Withdrawing stale pending invitations.
- Multi-account support.
