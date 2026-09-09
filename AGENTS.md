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
├── cli.py        # entry point (linkedin-bot): login / run / status / stats
├── config.py     # yaml + DEFAULTS deep-merge; path resolution; validation
├── session.py    # GUI login w/ manual OTP fallback; persists storage_state
├── network.py    # My Network scan + Connect clicker + outcome classification
├── scheduler.py  # daily/weekly caps, warm-up ramp, delay helpers
├── monitor.py    # kill switch, checkpoint URL/text detection
└── db.py         # SQLite ledger (invitations, events)
```

Runtime flow: `login` (once, GUI, saves cookies) → `run` (headless, loads
cookies) → for each visible Connect button until cap: click → classify →
record in SQLite → sleep.

### Data & state

- `data/bot.db` — SQLite. `invitations` rows: profile URL, name, outcome,
  `clicked_at` (UTC ISO8601, `Z` suffix). `events` rows: RUN_START/RUN_END.
- `data/sessions/linkedin_storage_state.json` — saved cookies from `login`.
- `data/STOP` — kill-switch marker; bot halts before the next click while it
  exists.
- `.env` — `LINKEDIN_EMAIL` / `LINKEDIN_PASSWORD` (loaded via `python-dotenv`
  in `cli.main`; never hardcode creds).

### Outcome semantics (important)

- `SENT_PENDING` — card detached or its aria-label changed after the click
  (LinkedIn recycles the button node for the next suggestion).
- `UNKNOWN` — clicked but no state change observed within ~5 s. **Counted as a
  send** against caps (conservative; it may have gone through).
- `ERROR` / `LIMIT` — click failed / LinkedIn's own invitation-limit notice
  shown (stops the run).

### Config (`config.yaml`, live-editable)

Caps (`caps.daily` = 50, `caps.weekly` = 300) are **never exceeded**; the
scheduler counts `SENT_PENDING` + `UNKNOWN` against both. `warmup.enabled` is
off (established account); re-enable for a fresh account ramp. `delays` are the
randomized pause between clicks. `browser.headless` is true for `run`.
`login.checkpoint_wait_seconds` bounds the manual-login wait.

## Running locally (Windows dev machine)

```powershell
cd C:\Users\DW\Workspace\linkedin-connect-bot
.venv\Scripts\python.exe -m linkedin_bot.cli login      # GUI + manual OTP once
.venv\Scripts\python.exe -m linkedin_bot.cli run --dry-run   # selector check
.venv\Scripts\python.exe -m linkedin_bot.cli run        # headless until cap
.venv\Scripts\python.exe -m linkedin_bot.cli run --headed   # watch it
.venv\Scripts\python.exe -m linkedin_bot.cli run --limit N  # cap one run
.venv\Scripts\python.exe -m linkedin_bot.cli status | stats
```

Always use the venv interpreter (`.venv\Scripts\python.exe`), never the system
`python` — the package is only installed into the venv. `python -m
linkedin_bot.cli` and the installed `linkedin-bot` script are equivalent.

Set `PYTHONIOENCODING=utf-8` on Windows consoles before running anything that
prints — LinkedIn pages contain Vietnamese text that crashes cp1252 stdout
(e.g. `$env:PYTHONIOENCODING="utf-8"`).

Tests (no browser needed, pure logic only):

```powershell
.venv\Scripts\python.exe -m pytest    # 18 tests
```

## Deployment (Linux, Docker)

Clean-room image + compose live under `deploy/` (`Dockerfile`,
`docker-compose.yml`, `DEPLOY.md`). Data and `config.yaml` are bind-mounted
from the host; only code changes require `docker compose build`. The one-time
GUI login runs through the container on the host's X11 socket
(`xhost +local:` first). Host `crontab` schedules the daily run.

**Session portability rule:** do not transplant `storage_state` across
machines/IPs. LinkedIn re-verifies accounts that appear on a new IP. Log in
fresh on the server and let that IP become the account's home. Never run two
instances against the same account concurrently (double-sends + detection).

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
- Login submit must target `button[type="submit"]` (or press Enter) — LinkedIn
  localizes the button text (Vietnamese "Đăng nhập"), so text-based locators
  fail. The login page can also be served with empty fields; auto-fill is
  best-effort and falls back to "complete it by hand" while the wait loop polls
  for the global nav (`a[href="/feed/"]`, `/mynetwork/`, `/messaging/`).
- Fresh automated logins almost always trigger a security check; `login` waits
  (default 30 min) for the operator to finish OTP/CAPTCHA in the open window.
  **Closing the window early kills the wait** with a `TargetClosedError`.
- LinkedIn's own limit notice matches `text=/weekly invitation limit|daily
  invitation limit/i` and stops the run (`LIMIT`).
- Background PowerShell tasks in this dev environment capture stdout
  unreliably (empty logs) — verify with `status`/`stats` (ledger) or run in the
  foreground.

## Guardrails (do not silently weaken)

- Caps are hard floors — the loop re-checks remaining before every click.
- Kill switch (`data/STOP`), checkpoint URL detection (`/checkpoint/`,
  `/authwall`, "challenge"), and periodic security-text scans stop runs.
- 45–120 s randomized delays between clicks by default.
- Warning: 50/day and 300/week exceed the widely cited safe ceiling (~100/week)
  — this was operator-approved for an established pilot account. Changing caps
  upward further is an operator decision, not a code fix.

## Scope / v2 (not implemented)

- Personalized "Add a note" via the profile-page flow.
- Acceptance-rate tracking (needs LinkedIn's Sent-invitations page).
- Withdrawing stale pending invitations.
- Multi-account support.
