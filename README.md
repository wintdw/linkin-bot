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
| `linkedin-bot serve` | Long-lived FastAPI service: status dashboard + the daily schedule runs itself (no cron). See the Docker section. |
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
- Each cycle clicks every visible Connect button with a short `delays` pause (0.8–1.2 s in the
  shipped config) between clicks, then waits `network.refresh_wait_seconds` (20 s), reloads My
  Network for fresh suggestions, and repeats until a cap or a health stop. Cards are re-queried and
  de-duplicated by identity each click, so a lingering sent card is never clicked twice.
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

Pure logic, no browser needed (24 tests). Run from the venv:

```
pytest
```

## Running on a Linux server (Docker)

A pinned Python + Playwright image (`Dockerfile`), a compose file
(`docker-compose.yml`) mounting `data/` and `config.yaml` from the host, and a
`.dockerignore` keeping the build context clean. All state — session, ledger,
config — lives on the host; containers are throwaway.

The container runs `linkin-bot serve`: a small FastAPI dashboard plus the
built-in daily schedule — the connect loop runs itself at 09:30 container-local
(TZ `Asia/Ho_Chi_Minh`) every day, **no host cron needed**. Every action the
bot takes is logged to stdout, so `docker compose logs -f` shows each click,
refresh and stop reason as it happens.

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

### Start the service and verify

```bash
docker compose up -d          # dashboard on http://localhost:8082
docker compose logs -f        # follow every action the bot takes
curl localhost:8082/          # status dashboard (caps, events, next run)
```

The first scheduled run waits for the next 09:30. To run now (e.g. right
after deploying), trigger a manual run while it is up:

```bash
curl -X POST localhost:8082/run
```

(`serve` also accepts `--boot-run` to run shortly after startup — handy on the
first deploy — by adding it to the compose `command`.)

Scheduled, manual and startup runs are single-flight: they can never overlap,
and each scheduled slot fires at most once a day. A run stops itself at the
caps in `config.yaml` (time depends on the pacing — each 8-click batch plus the
20 s refresh wait takes roughly a minute).

### Day-to-day operations

```bash
curl localhost:8082/health             # state as JSON
docker compose run --rm linkin-bot status           # caps usage from the CLI
docker compose run --rm linkin-bot stats            # history from the CLI
docker compose run --rm linkin-bot run --limit 3    # one-shot trial run
```

### Emergency stop & notes

- `touch data/STOP` — the bot refuses to start and halts before the next click;
  delete to resume. Runs are also refused while it exists.
- Rebuild only when code changes (`docker compose build && docker compose up -d`);
  caps/delays edits apply on the next run (config is mounted live).
- Back up the ledger while idle: `cp data/bot.db data/bot.db.$(date +%F).bak`.
- If the account gets a hard checkpoint: `touch data/STOP`, let the account
  rest, then log in fresh on the server and delete `data/STOP` to resume.

## Not implemented (v2 ideas)

- Personalized "Add a note" via the profile-page flow.
- Acceptance-rate tracking against LinkedIn's Sent-invitations page.
- Withdrawing stale pending invitations.
- Multi-account support.
