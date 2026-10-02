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
├── cli.py        # entry point (linkedin-bot): login / connect / follow / serve / status / stats
├── config.py     # yaml + DEFAULTS deep-merge; path resolution; validation
├── session.py    # manual GUI login (no auto-fill); persists storage_state
├── network.py    # CONNECT flow: My Network scan + Connect clicker + classification
├── follow.py     # FOLLOW flow: Page's "Invite to follow" dialog (select rows + bulk submit)
├── actions.py    # shared click primitives (pin handle, dispatch click, classify)
├── scheduler.py  # per-flow daily/weekly caps, warm-up ramp, delay helpers
├── monitor.py    # kill switch, checkpoint URL/text detection
├── db.py         # SQLite ledger (invitations, events) — invitations carry a `kind`
└── web.py        # FastAPI dashboard + built-in daily schedule (serve)
```

Two independent flows share one process and one LinkedIn account:

- **connect** (personal) — My Network suggestions, "Invite &lt;Name&gt; to connect".
- **follow** (company) — a managed Page's "Invite to follow" dialog.

They must **never run at the same time** (same account: double-actions + detection),
so `serve` runs both on a single asyncio lock. `connect`/`follow` are peer
subcommands (`run` is kept as an alias of `connect`).

Runtime flow (**connect**): `login` (once, GUI, saves cookies) → `connect`
(headless, loads cookies) → per cycle: click every visible Connect button (short
pause between clicks) → classify → record in SQLite → wait
`network.refresh_wait_seconds` → reload My Network → repeat until a cap or a
health stop. On each click the visible Connect buttons are re-queried and the
first *unseen* card (by member URN, else aria-label) is clicked, so a lingering
sent card is never clicked twice and a stale locator never stalls the run. Every
action taken is printed to stdout (`[connect]` lines: click outcomes, refreshes, stop
reasons), so a `serve` container shows the whole run live under `docker compose
logs -f`.

Runtime flow (**follow**): `follow` (headless) → warm up on `/feed/` (a restored
session deep-linking straight to the Page admin hits `/authwall`), then open the
Page (`follow.page_url`, else the homepage left rail by `follow.page_name`) →
click the Page's **"Invite to follow"** control to open the dialog → **select**
every not-yet-seen row checkbox the credits allow (rows live in a
`ul[role=listbox]`, de-duplicated by name; each row reads "Select &lt;Name&gt;") →
click the single bulk **"Invite N"** button to submit them → record each
selected person in SQLite with `kind='follow'` → dismiss/reopen the dialog and
repeat until the credits are exhausted or there are no new rows. Actions print as `[follow]` lines. Logins are
shared, but the two flows **must never run at once** (same account), so both go
through one lock in `serve`.

Note the follow dialog has **no per-row Invite buttons**: it is a
checkbox-multiselect + one bulk submit, and each invite spends a **credit**
("N/50 credits available", refill monthly). The flow is **credit-driven**: it
spends the credits instead of spreading invites across days, and never clicks a
row-agnostic "Invite all", "Unselect all" or the filter controls.

`serve` is the long-lived service mode: a FastAPI app (lazy-imported) that runs
both flows itself at daily `HH:MM` slots — connect at `--connect-at` (default
09:30) and follow at `--follow-at` (default 10:00), container-local TZ — no host
cron. Scheduled runs, manual triggers (`POST /run` = connect, `POST /run/follow`
= follow) and startup (`--boot-run`, connect) are single-flight via one asyncio
lock, and each slot fires at most once a day (same pattern as voz-bot/otofun-bot).
Dashboard on `/`, state as JSON on `/health`.

`connect`/`follow` exit codes: `0` clean stop · `1` missing session or
`data/STOP` present · `2` saved session no longer valid (re-run `login`) · `3`
unexpected crash. The end-of-run reason is recorded in the `RUN_END` event and
echoed by the CLI: `cap` (daily/weekly cap), `limit` (`--limit` budget hit),
`exhausted` (no more cards/rows after retrying), `checkpoint`, `kill-switch`,
`linkedin-limit`, `refresh-failed`, `no-modal` (follow: dialog never opened),
`disabled` (follow: `follow.enabled=false`), `end`.

### Data & state

- `data/bot.db` — SQLite. `invitations` rows: profile URL, name, outcome,
  `clicked_at` (UTC ISO8601, `Z` suffix), optional `details`, and `kind`
  (`connect`/`follow`, default `connect`; the column is auto-migrated onto older
  ledgers). `events` rows: `RUN_START`/`RUN_END` with a `message` (RUN_END records
  counts + reason) and a `flow` (`connect`/`follow`; auto-migrated + backfilled
  onto older ledgers). `status` and the dashboard list each flow's events
  separately, and `stats` reports outcome counts / the 14-day history per flow.
- `data/sessions/linkedin_storage_state.json` — saved cookies from `login`.
- `data/STOP` — kill-switch marker; bot halts before the next click while it
  exists.
- Credentials are never read or stored by the bot — `login` is fully manual
  (you type email/password and clear any OTP/CAPTCHA in the browser window).

### Outcome semantics (important)

- `SENT_PENDING` — the card was **removed** from the DOM after the click
  (detected via `el.isConnected === false`), the button text became
  pending/sent/withdraw, or the button node was recycled with a new aria-label.
  This is the normal success signal.
- `UNKNOWN` — clicked but no state change observed within ~5 s. **Counted as a
  send** against caps (conservative; it may have gone through). Rare now that
  removal is detected; it means the click genuinely did nothing.
- `ERROR` / `LIMIT` — click failed / LinkedIn's own invitation-limit notice
  shown (stops the run).

Note: a detached element handle does **not** raise in Playwright — `inner_text()`
and `get_attribute()` keep returning stale values — so detachment must be read
from `isConnected`, never from an exception (see Hard-won facts).

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

The **`follow`** block configures the second flow: `enabled` (off = the flow is a
no-op), `page_name` / `page_url` (how to reach the managed Page; `enabled` needs
one of them), `invite_button_text` (the "Invite to follow" control),
`refresh_wait_seconds` / `max_empty_refreshes` / `click_timeout_ms` (same meaning
as under `network`), and its own `caps.daily` / `caps.weekly` (counted separately
from the connect caps, and used only as a fallback when the dialog's credit
counter can't be read — the flow is otherwise credit-driven).

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
linkedin-bot connect --dry-run        # selector check; counts visible buttons
linkedin-bot connect                  # headless until cap (alias: `run`)
linkedin-bot connect --headed         # watch it
linkedin-bot connect --limit N        # cap one run
linkedin-bot follow --dry-run         # page check; opens dialog, counts rows + credits
linkedin-bot follow                   # headless until the credits run out
linkedin-bot serve --connect-at 09:30 --follow-at 10:00   # dashboard + schedule
linkedin-bot status | stats
```

Windows consoles: set `PYTHONIOENCODING=utf-8` before anything that prints
(`$env:PYTHONIOENCODING="utf-8"`) — LinkedIn pages contain Vietnamese text that
crashes cp1252 stdout. (macOS/Linux default to UTF-8.)

Tests (no browser needed, pure logic only):

```bash
python -m pytest                     # 54 tests
```

## Deployment (Linux, Docker)

Clean-room image + compose live at the repo root (`Dockerfile`,
`docker-compose.yml`, `.dockerignore`) — same layout as voz-bot/otofun-bot.
On this host the Docker socket needs root: prefix every docker command with
`sudo` (e.g. `sudo docker logs linkin-bot`, `sudo docker compose logs -f`,
`sudo docker compose run --rm linkin-bot status`).
Non-root uid 10001 (`linkinbot`); `./data` on the host must be writable by it
(`sudo chown -R 10001:10001 data` once). Data and `config.yaml` are
bind-mounted from the host; only code changes require `docker compose build`
(BuildKit pip cache keeps rebuilds fast).

The container runs `serve` (FastAPI dashboard on host port 8082, matching the
voz-bot=8080 / otofun-bot=8081 convention) and runs its own daily schedule —
**no host cron**. The one-time GUI login runs through the container on the
host's X11 socket (`xhost +local:` first). The full runbook lives in the
README. Watch a live run with `docker compose logs -f` (every click/scroll/stop
is logged); force a connect run with `POST localhost:8082/run`, a page follow run
with `POST localhost:8082/run/follow`.

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
  `get_by_role(name="Connect", exact=True)` finds nothing**, and a bare
  substring match on "Connect" is *too* loose — it also selects the
  **received**-invitation cards' Ignore/Accept controls, whose labels are
  "Ignore|Accept …invitation to connect from <Name>" (live log: `found 9 Connect
  button(s): Ignore an invitation to connect from <Name>, …`). Clicking Ignore
  **silently dismisses an incoming request** and would have been counted as a
  send. `connect_locator` therefore matches the invitation shape itself —
  `INVITE_LABEL_RE = ^invite (.+) to connect$` (case-insensitive), passed as
  `get_by_role("button", name=…)`, which Playwright tests against the normalized
  accessible name. Do not "simplify" this back to a substring match.
- The suggestion card no longer exposes the profile link beside the button: the
  parent chain has no `a[href*="/in/"]` until ~5 ancestors up, and cards are
  `<div>`s, not `<li>`s. Card **identity** therefore comes from the button's
  stable `componentkey` (`ConnectButtonstate:invitation:urn:li:member:<id>_connect`),
  and the profile URL is recovered by walking ancestors for the `/in/` anchor.
  A Connect button with neither an identity nor a matching aria-label is
  **skipped**, not clicked (those clicks only ever no-op'd).
- **Physical mouse clicks get swallowed by transient ad overlays** on the grow
  feed. The clicker dispatches `button.evaluate("el => el.click()")` instead.
- After a send, LinkedIn **removes** the card (it no longer merely recycles the
  button), so success is detected by the pinned node detaching OR its aria-label
  changing to a different "Invite ... to connect". **Detachment must be read from
  `el.isConnected`, never from an exception**: a detached handle still answers
  `inner_text()`/`get_attribute()` with stale values (verified Sept 2026), so the
  old "node detached ⇒ inner_text throws" logic mislabelled real sends as
  `UNKNOWN` (live repro: 5/5 sends → `UNKNOWN`, all confirmed on the Sent page).
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
  by **card identity** (member URN from `componentkey`, else the aria-label) via
  `_next_unseen`; otherwise the same person is clicked every pass until the cap.
- Background PowerShell tasks in this dev environment capture stdout
  unreliably (empty logs) — verify with `status`/`stats` (ledger) or run in the
  foreground.
- A restored session on a new IP first hits a transient
  `ssr-login/remember-me-auto-login` interstitial that re-validates the token
  and redirects to the target — **not** a checkpoint. The feed may not be
  rendered yet at that point, so `connect --dry-run` can report `0 Connect
  button(s)` on the first hit even though the session is fine; the real run's
  refresh/repoll loop absorbs the delay. `connect` itself polls for the global nav
  (~20 s, `cli.py`) before reporting exit 2, so a single interstitial passes;
  only re-login when the poll times out (a real checkpoint, or the interstitial
  stalled). Re-run the dry-run (or check the page title says "Grow") before
  declaring a session dead.
- **The FOLLOW flow, verified against live LinkedIn (Oct 2026).** The Page admin
  dashboard (`/company/<id>/admin/dashboard/`) exposes an **"Invite to follow"**
  link → a dialog with a `ul[role=listbox]` of ~20 candidate rows, each a
  checkbox labelled "Select &lt;Name&gt;"; an "N selected" counter; and ONE bulk
  **"Invite N"** submit button (disabled at 0 selected). There are **no per-row
  Invite buttons**. A submitted row relabels to "Invited" and its checkbox is
  removed. The header credit counter ("N/50 credits available") does **not**
  live-update in the open dialog, so success is read from the "Invited" relabel
  (or a credit drop / the dialog closing) — not from the header. Each invite
  spends a **credit** (50/month, refill monthly), so the flow spends the available
  credit balance rather than spreading invites across days.
- **After a bulk submit the dialog flips in place to an "Invitations sent"
  confirmation** — zero rows, no `Show more results` control — and it stays open
  (Escape does **not** close it), so `_dialog()` alone still reports "open". The
  loop must dismiss it via the dialog's `button.artdeco-modal__dismiss` and
  reopen "Invite to follow" to get the next rows; otherwise it stalls after one
  batch and ends `reason=exhausted`. Seen live Oct 2026: 20 candidates but only 5
  invited, because the loop then stopped at the old daily cap. Reopening also
  drops already-invited people from the suggestion list.
- **Deep-linking straight to `/company/<id>/admin/` on a restored session hits
  `/authwall`** (a checkpoint). Warm up on `/feed/` first, then navigate — both
  browser loops already open `/feed/` before anything else.
- The homepage left rail can contain **two links whose text is the page name**
  (a profile anchor and the `/company/...` admin anchor); `goto_page` prefers the
  one whose href contains `/company/`, but setting `follow.page_url` is the
  reliable path.

## Guardrails (do not silently weaken)

- Budgets — the connect caps, or the follow flow's credits — are hard floors: the
  loop re-checks remaining before every click.
- Only invite-shaped buttons (`INVITE_LABEL_RE`) are clickable. Anything else
  that merely contains "Connect" — LinkedIn's Ignore/Accept received-invitation
  controls — must stay excluded; keep the allowlist, not a denylist of known
  bad labels.
- The follow flow is **credit-driven**: it checks every not-yet-seen row checkbox
  the available credits allow, submits them with the one bulk "Invite N" button,
  and never clicks a row-agnostic "Invite all" — a bulk submit is unavoidable in
  this dialog, so the credits are what is controlled, not a per-day cap.
- Follow never clicks "Unselect all" or the filter controls, and de-duplicates
  rows by name so a submitted row (now "Invited") is not re-selected.
- Connect and follow keep **separate budgets** (connect `caps.*` vs the follow
  credit balance, with `follow.caps.*` only a fallback) and separate ledger rows
  (`invitations.kind`); never let one flow count against the other's budget.
- Both flows share one account, so they must never run concurrently. `serve`
  serializes them on one asyncio lock; do not run a manual `connect`/`follow`
  while another run (service or CLI) is active.
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
- Multiple managed Pages in the follow flow (`follow` currently targets one Page).
