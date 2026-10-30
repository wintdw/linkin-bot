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
├── cli.py        # entry point (linkedin-bot): login / connect / follow / post / serve / status / stats
├── config.py     # yaml + DEFAULTS deep-merge; path resolution; validation
├── session.py    # manual GUI login; shared `open_logged_in_page` browser bootstrap
├── network.py    # CONNECT flow: My Network scan + Connect clicker + classification
├── follow.py     # FOLLOW flow: Page's "Invite to follow" dialog (select rows + bulk submit)
├── actions.py    # shared click primitives (pin handle, dispatch click, classify)
├── flow.py       # flow registry (SPECS/NAMES) + shared loop scaffolding (stop reason, stats, RUN_END)
├── scheduler.py  # per-flow daily/weekly caps, warm-up ramp, delay helpers
├── monitor.py    # kill switch, checkpoint URL/text detection
├── db.py         # SQLite ledger (invitations, events) — invitations carry a `kind`
├── publish.py    # Page posting via the browser session (backs /post)
└── web.py        # FastAPI dashboard + daily schedule (serve) + /post route
```

The `serve` web service is also the **posting gateway**: `POST /post` publishes
**as the managed Page** by driving the saved browser session — LinkedIn's API
cannot author as an organization without the Community Management API product,
which this app lacks — so it shares the flows' account and takes the same
single-flight lock (409 while a run is active). A sibling app (social-bot) posts
through `POST /post` over the internal network; the same request can also drop an
optional first `comment` on the new post, as the Page.

Two independent flows share one process and one LinkedIn account:

- **connect** (personal) — My Network suggestions, "Invite &lt;Name&gt; to connect".
- **follow** (company) — a managed Page's "Invite to follow" dialog.

They must **never run at the same time** (same account: double-actions + detection),
so `serve` runs both on a single asyncio lock. `connect`/`follow` are peer
subcommands (`run` is kept as an alias of `connect`).

The set of flows lives in one place — `flow.py` (`SPECS`/`NAMES`) — with the
shared loop scaffolding they both use (stop-reason detection, per-run stats,
`[flow]` logging, the `RUN_END` event). The CLI, web service and scheduler
iterate that registry instead of hard-coding `("connect", "follow")`; adding a
flow means a new module + a `SPECS` entry + its config block.

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
Dashboard on `/`, full status as JSON on `/status`, liveness as JSON on `/health`.
`/status` reports the saved browser **session / cookie store** (`session` block —
presence, when it was saved, which cookies it holds, and the last run's verdict
via `last_check`; never the cookie values, and file metadata alone does not prove
the session is still accepted). A run returning exit 2 marks the session invalid
there, exit 4 marks it **edge-blocked** (`kind: blocked`, a different verdict — see
below), and `/status` also carries a one-word `health` (`ok` / `degraded` /
`blocked`) plus `consecutive_failures` and `last_success_at`, because "the
process is up" is not the same as "the bot is working" (see the compose
healthcheck).

`connect`/`follow` exit codes: `0` clean stop · `1` missing session or
`data/STOP` present · `2` saved session no longer valid (re-run `login`) · `3`
unexpected crash · `4` LinkedIn's edge refused this browser (**not** a stale
session — never answer it with `login`; it is retried in-process first, per
`browser.block_retries`, then the run stops and reports it). The end-of-run
reason is recorded in the `RUN_END` event and
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
- Account credentials are never read or stored by the bot — `login` is fully
  manual (you type email/password and clear any OTP/CAPTCHA in the browser window).

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
and per-action `timeout_ms` (`slow_mo_ms` for debugging), plus
`block_retries` / `block_backoff_seconds` for an edge refusal (default 1 retry
after 120 s — an edge block is transient far more often than it is a dead
session; set `block_retries: 0` to stop on the first one). `network` controls the
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
python -m pytest                     # 153 tests
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
- **Page posting is browser-driven (`publish.py`, backs `/post`).**
  LinkedIn's API cannot author as an organization without the Community
  Management API product (scope `w_organization_social`), which this app does not
  have, so posting **as the Page** goes through the saved session like the flows
  (and shares their single-flight lock). Verified live (Oct 2026): the Page posts
  are composed at `post.page_url` = `/company/<id>/admin/page-posts/published/`
  (the **dashboard** has no composer) — a **Start a post** control opens a dialog
  holding a Quill `.ql-editor`, submitted with a **Post** button. Gotcha: that
  admin page pre-renders several inline editors (comment boxes), so an editor
  merely existing does **not** mean the composer is open; `_open_composer` clicks
  the trigger and waits for the *dialog*. The submit lookup is deliberately
  permissive (see `_submit_button`): it tries the composer dialog then the whole
  page, and the configured label exactly then as a substring — LinkedIn nests
  dialogs and can render the submit outside the one we scoped to, which is how a
  live run failed with "no 'Post' button in the composer". After a **photo** is
  attached the composer may show a media step whose primary action is **Next**,
  before the **Post** submit appears. That step can replace the composer, so the
  caption is typed **after** the image (live: a caption typed first went out
  image-only); `_fill_editor` also reads the editor back and types with real key
  events if Quill dropped the value. **The upload is waited on, not slept on**:
  `_advance_media` polls (up to `MEDIA_WAIT_SECONDS`, 30 s) until the submit or a
  Next-style control appears — clicking the latter to advance the step — because a
  fixed sleep either races a large image (posting without the photo) or wastes
  time. If nothing shows up it logs and lets the submit fail loudly rather than
  assuming the photo made it. **Clicks are dispatched on the node** (`_click`) once
  the control is enabled + visible: physical clicks get swallowed by transient
  overlays (the reason `actions.click_pinned_and_classify` does the same), while a
  disabled/overlaid control still falls back to a real click so it is not
  "clicked" silently. If the submit is still missing, the error lists the
  composer's actual button labels.

  On success `/post` also returns the ``post_urn``/``permalink`` of the new post
  plus ``post_verified``. `_latest_post` re-reads the Page's published-posts list
  (`_post_entries` — every activity link with its card's text) and picks the entry
  whose card **contains the caption we posted** (`_caption_key`: NFC, whitespace-
  and case-insensitive, first 40 chars — the card is truncated with "see more",
  and a caption under 20 chars is not a usable key), polling a moment while it
  appears. A match is `post_verified: true`; with none it falls back to the list's
  own newest entry, `post_verified: false`, so an id is never *claimed* to be
  matched when it is not (also false for an image-only post, which has no caption
  to match). Both are still best-effort/empty when the list can't be read — the
  browser composer never surfaces the created id directly. Probe anytime with
  `linkedin-bot post --dry-run --headed`, which now also reports how many posts the
  list scan sees and how many carry text.
- **Every publish reports a `status`, because a failed POST is ambiguous.** The
  composer closing is the only confirmed success; a 502 alone cannot say whether
  the post went out, and retrying a 502 is how you double-post. So `/post` (and
  the CLI, and `publish.PublishError`) carry `status` + `retryable`:
  `confirmed` (`retryable: false` — it is out), `refused` (`retryable: true` —
  positively nothing was sent: `_submit` checks the still-open composer *still
  holds the caption*, or LinkedIn showed a refusal notice, e.g.
  "composer not found", "could not attach the image"), and `unknown`
  (`retryable: false`, `PublishUnconfirmed` — the Post click was dispatched but
  nothing confirmed it, so the post **may** be live: check the Page's posts list,
  never retry). An unexpected failure too (a crash mid-publish) is reported as
  `unknown`/non-retryable rather than a bare 502 - we cannot tell whether it went
  out. A full-page composer (no dialog to watch close) is `unknown` by
  construction — the live-verified shape is the dialog. Only `refused` may be
  retried; `is_retryable()` is the one place that decides.
- **Commenting on the post just published (new, `post.comment_button_text`).**
  `/post` accepts an optional `comment` (CLI: `--comment`) and, after the post
  goes out, types it into the new post's own inline comment box on that same
  posts admin — where the acting author is the Page — so a caller can post and
  drop a first comment in one call. Nothing new is navigated: the post just
  published is identified by the **`post_urn`** the read-back recovered, and the
  box is scoped to **that post's card** (`_post_card` → `_comment_editors`), so a
  page-wide search can never land on another post's box. A comment box is
  recognised by its **markup** — a `...comment...` ancestor class
  (`COMMENT_CONTAINER_SELECTOR`) or a "comment" placeholder/aria-label hint — never
  by position: the old "no placeholder, take every editor" fallback is **gone**,
  and a box that is not recognisably a comment box is skipped (typing a comment
  into a composer or a search field is worse than reporting it failed). The class
  match is why localized Pages (Vietnamese) still work, where the placeholder is
  not the English hint. **The submit is Enter, not a button**: the post card
  carries a card-level **Comment** *action* button with the very same label that
  only opens the box, so a button-first lookup would click the wrong control —
  Enter submits a Quill comment box (Shift+Enter is a line break), and
  `post.comment_button_text` is only the fallback, looked up *inside the card*
  (`_comment_scope`). **Success = the box emptied while still attached**
  (`isConnected`): a detached/unreadable box is *not* confirmation — a detached
  handle hands back stale text (see above) and its locator would be re-resolved to
  another post's box — so the comment is reported failed and the button fallback is
  skipped (the card is gone, and a page-wide "Comment" button could belong to
  another post). The box is **pinned to an element handle** (`_pin_editor`) so the
  typing and that check read the same node; the confirm wait is
  `COMMENT_CONFIRM_SECONDS` (8 s). A caption/comment typed with real key events (when
  Quill dropped `fill`) breaks lines with **Shift+Enter**, never a bare Enter —
  Enter submits a Quill comment box, so multi-line text would post its first line and
  leave the rest behind. **Not yet
  verified against live LinkedIn** — probe it with `post --dry-run --headed`,
  which reports how many of the list's editors are recognised as comment boxes and
  whether a submit button resolves in the newest card, before trusting the comment
  path. A comment failure is deliberately **not** an error response: the post
  already published and a retry would duplicate it, so `/post` returns
  `"commented": false` + `"comment_error"` and logs
  `[post] comment failed: ...` (the CLI exits 1 on it, for the operator). Comment
  length is capped at LinkedIn's 1250 (`MAX_COMMENT`), checked before posting.
- **Cloudflare sits in front of LinkedIn and blocks the headless UA (Oct 2026).**
  Four days of runs (both accounts) exited 2 with a *valid* cookie: the saved
  session was fine, the browser was refused. `open_logged_in_page` now sends an
  ordinary desktop Chrome UA (`session.user_agent`, built from the launched
  browser's real version) and closes the two markers that are read *besides* the
  UA string — verified live in the bot's own container:

  | signal | default | after the fix |
  |---|---|---|
  | UA header | `HeadlessChrome/151` → **403** | `Chrome/151` → **200** |
  | `navigator.userAgentData.brands` | `HeadlessChrome/151` | `Chromium/151, Google Chrome/151` |
  | `navigator.webdriver` | `true` | `false` |

  Hiding the UA string alone leaves the same token one API away, so
  `_apply_stealth` also rewrites Client Hints via CDP
  (`Emulation.setUserAgentOverride`) and the launch adds
  `--disable-blink-features=AutomationControlled`. A cookie-less context was
  blocked too, which is the tell: if **any** context is blocked, the cause is the
  browser, never the session — check that before suggesting `login`.
- **The logged-in nav is not a fixed shape.** `is_logged_in()` proves a session
  by finding the global nav, and the nav now renders **absolute** hrefs with no
  trailing slash (`https://www.linkedin.com/mynetwork`). The old exact matches
  (`a[href="/mynetwork/"]`) matched **zero** elements on a loaded, logged-in
  feed — a live session would have been reported dead even after the block was
  cleared. Markers are substring matches (`href*`) precisely so both shapes
  work; do not "tighten" them back to exact hrefs.
- **A block is not a dead session, and must never be answered with `login`.**
  `looks_like_block` reads the edge page's own words ("Attention Required",
  "you have been blocked") plus a bare 403/429, and raises `BlockedError` →
  exit 4 (`/status` `kind: blocked`, `health: blocked`). A plain `/uas/login`
  redirect is *not* a block. The run retries in-process first
  (`browser.block_retries`, default 1, 120 s backoff) because edge refusals are
  usually transient; re-running `login` cannot clear one and only spends the
  account's patience.

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
- A publish **failure** is not automatically retryable. Only
  `status="refused"` (`is_retryable()`) means nothing was sent; a
  `status="unknown"` (`PublishUnconfirmed`) means the post may already be live, so
  retrying it is how a duplicate gets published. Never collapse the three statuses
  back into a bare boolean/502, and never turn a comment failure into a failed
  request (the post is out — a retry would double-post it).
- A comment is only ever typed into a **recognised comment box**, scoped to the
  published post's card when its `post_urn` is known. Do not reintroduce a
  "fall back to any editor on the page" path: reporting `commented: false` is the
  correct outcome when the box cannot be identified. Likewise, a comment counts as
  posted only when its box emptied **while still attached** — do not "confirm" it
  from an unreadable/detached box.
- The published post's id is **never claimed** to be matched when it was not:
  `post_verified: false` is the honest answer for a read-back that fell back to the
  list's own newest entry (or an image-only post with no caption to match), so a
  caller can tell the two apart. Do not drop that flag to "simplify" the result.
- Do not put a fixed sleep back into the image path. `_advance_media` waits for a
  real signal (the submit, or the media step's Next control); a guessed sleep either
  races a large upload — publishing without the photo — or wastes time. Same for
  clicks: use `_click` (dispatch on the node once actionable), not a bare
  `click()`, which transient overlays swallow.
- **Never restart a container because it is unhealthy, and never wire an
  autoheal/watchdog.** The healthcheck exists to make a broken bot *visible*
  (`docker ps` said "Up 12 days" through the Oct 2026 outage), not to act: a
  restart cannot clear an edge block or revive a dead cookie, and recreating the
  container drops the log stream that explains the failure. Failures are handled
  in-process (`browser.block_retries`) and then reported; a human decides.
  Note `restart: unless-stopped` only reacts to the process exiting, and
  `docker restart` keeps logs (only a *recreate* loses them).
- **An edge block must never be reported as "session invalid".** Keep
  `BlockedError` distinct (subclass, exit 4, `/status` `kind: blocked`): the
  message drives the operator's response, and "run `login`" for a Cloudflare
  block is actively misleading. Likewise do not let a block be masked by
  `health: ok` — `health_verdict` checks the block *first*.

## Scope / v2 (not implemented)

- Personalized "Add a note" via the profile-page flow.
- Acceptance-rate tracking (needs LinkedIn's Sent-invitations page).
- Withdrawing stale pending invitations.
- Multi-account support.
- Multiple managed Pages in the follow flow (`follow` currently targets one Page).
