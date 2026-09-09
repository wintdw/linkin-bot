# LinkedIn Connect Bot

Python 3 + Playwright bot: logs into LinkedIn with your credentials, opens the **My Network**
suggestions page (`/mynetwork/grow/`), and clicks **Connect** on suggested people — paced with
randomized delays and hard-capped per day and week so the activity looks slow and human.

> **Terms of Service warning.** Automating connection requests breaches LinkedIn's User
> Agreement (Section 8.2) and its Prohibited Software policy. Accounts using such tooling risk
> restriction or suspension. Run this only on a **company-owned, non-branded account**, at
> conservative volume, and stop at the first sign of a security check. You accept this risk.

## Setup

```
python -m venv .venv
source .venv/bin/activate            # macOS/Linux   (Windows: .venv\Scripts\activate)
pip install -e ".[dev]"
playwright install chromium
```

## Usage

| Command | What it does |
|---|---|
| `linkedin-bot login` | Opens a visible browser at the LinkedIn login page and waits while you sign in **by hand** — credentials plus any OTP/CAPTCHA — then saves the session. |
| `linkedin-bot run` | Loads the saved session, opens My Network, and clicks **Connect** on suggestion cards up to today's cap. Headless by default; `--headed` to watch. |
| `linkedin-bot run --dry-run` | Counts visible Connect buttons without clicking anything. Use this first to verify the selectors still work. (First hit from a new IP can briefly report 0 while LinkedIn runs a remember-me auto-login — re-run before concluding the session is dead.) |
| `linkedin-bot run --limit 3` | Caps this single run at 3 sends (for trials). |
| `linkedin-bot status` | Shows today's and this week's usage vs. caps, plus recent events. |
| `linkedin-bot stats` | Outcome counts plus a last-14-day send/error history from the SQLite ledger. |

`linkedin-bot run` exit codes: `0` clean stop · `1` missing session or `data/STOP` present ·
`2` saved session no longer valid (re-run `login`) · `3` unexpected crash. Pass `--config PATH`
before the subcommand to point any command at an alternate config file.

**Emergency stop:** create `data/STOP` — the bot refuses to start and re-checks before every
click while it exists.

## Guardrails baked in

- `caps.daily` (50) and `caps.weekly` (300) are never exceeded; the scheduler counts both
  confirmed and uncertain sends against the caps.
- `warmup` optionally ramps from `daily_start` (5/day) up to the daily cap across your first
  active days (disabled in the shipped config for the established pilot account).
- Randomized `delays` (45–120 s default) between clicks; configurable.
- `login` never auto-fills: you sign in by hand and it waits for the global nav to appear, then
  persists the session so later runs stay headless and quiet.
- Every run halts on a `/checkpoint/` URL or security text (scanned every 5 sends), or LinkedIn's
  own "invitation limit" notice.

## Data & state

- `data/bot.db` — SQLite ledger of every click outcome (`SENT_PENDING`, `ERROR`, `UNKNOWN`,
  `LIMIT`) plus run events.
- `data/sessions/` — saved cookies (`storage_state`); git-ignored.

No credentials are stored or read by the bot — `login` is a manual, human-in-the-loop step.

## Tests

Pure logic, no browser needed (18 tests). Run from the venv:

```
pytest
```

## Running on a Linux server (Docker)

A pinned Python + Playwright image (`Dockerfile`), a compose file
(`docker-compose.yml`) mounting `data/` and `config.yaml` from the host, and a
`.dockerignore` keeping the build context clean. All state — session, ledger,
config — lives on the host; containers are throwaway.

### First-time setup on the server

```bash
# copy the project (without local state) and make ./data writable by uid 10001
rsync -av --exclude .venv --exclude data ./linkin-bot/ user@server:~/linkin-bot/
sudo chown -R 10001:10001 ~/linkin-bot/data

docker compose build          # first build also pulls Chromium (~1 GB image)
```

### One-time login — needs your GUI desktop session

```bash
xhost +local: 2>/dev/null || true
docker compose run --rm linkin-bot login
```

- A Chromium window opens on the LinkedIn login page. Sign in **by hand** —
  the bot never auto-fills credentials.
- Finish any security check / OTP prompt in that window; keep it open until
  you see `Session saved to ...` (it lands in `data/sessions/` on the host).
- **Why not copy the session from another machine?** LinkedIn re-verifies
  accounts that suddenly appear on a new IP. (Pilot result, Sept 2026: the
  transplanted session *was* accepted from the server IP after a
  `remember-me-auto-login` handshake — probable, not guaranteed.) If a run
  exits 2, log in fresh on the server.

Verify headlessly (no X needed):

```bash
docker compose run --rm linkin-bot run --dry-run
```

### Schedule the daily run (host cron)

```bash
crontab -e
# add:
30 9 * * * cd /home/you/linkin-bot && docker compose run --rm linkin-bot >> data/cron.log 2>&1
```

The container command is `run`, which stops itself at the caps in
`config.yaml`. The 45–120 s pacing is inside the bot; cron just triggers it.

### Day-to-day operations

```bash
docker compose run --rm linkin-bot status           # caps usage
docker compose run --rm linkin-bot stats            # history
docker compose run --rm linkin-bot run --headed     # watch a run (needs X)
tail -f data/cron.log                               # scheduled output
```

### Emergency stop & notes

- `touch data/STOP` — halts before the next click; delete to resume.
- Rebuild only when code changes (`docker compose build`); caps/delays edits
  apply on the next run (config is mounted live).
- Back up the ledger while idle: `cp data/bot.db data/bot.db.$(date +%F).bak`.
- If the account gets a hard checkpoint: remove the cron line or touch
  `data/STOP`, let the account rest, and resume manually.

## Not implemented (v2 ideas)

- Personalized "Add a note" via the profile-page flow.
- Acceptance-rate tracking against LinkedIn's Sent-invitations page.
- Withdrawing stale pending invitations.
- Multi-account support.
