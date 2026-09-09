# LinkedIn Connect Bot

Python 3 + Playwright bot: logs into LinkedIn with your credentials, opens the **My Network**
page, and clicks **Connect** on suggested people — paced with randomized delays and hard-capped
per day and week so the activity looks slow and human.

> **Terms of Service warning.** Automating connection requests breaches LinkedIn's User
> Agreement (Section 8.2) and its Prohibited Software policy. Accounts using such tooling risk
> restriction or suspension. Run this only on a **company-owned, non-branded account**, at
> conservative volume, and stop at the first sign of a security check. You accept this risk.

## Setup

```
python -m venv .venv
.venv\Scripts\activate            # Windows  (source .venv/bin/activate on macOS/Linux)
pip install -e ".[dev]"
playwright install chromium
copy .env.example .env            # then fill in LINKEDIN_EMAIL / LINKEDIN_PASSWORD
```

## Usage

| Command | What it does |
|---|---|
| `linkedin-bot login` | Opens a visible browser and signs in with the `.env` credentials. If a security check appears, complete it **by hand** in that window — the script waits and then saves the session. |
| `linkedin-bot run` | Loads the saved session, opens My Network, and clicks **Connect** on suggestion cards up to today's cap. Headless by default; `--headed` to watch. |
| `linkedin-bot run --dry-run` | Counts visible Connect buttons without clicking anything. Use this first to verify the selectors still work. |
| `linkedin-bot run --limit 3` | Caps this single run at 3 sends (for trials). |
| `linkedin-bot status` | Shows today's and this week's usage vs. caps, plus recent events. |
| `linkedin-bot stats` | Send history and outcome counts from the SQLite ledger. |

**Emergency stop:** create `data/STOP` — the bot checks for it before every action and halts.

## Guardrails baked in

- `caps.daily` (50) and `caps.weekly` (300) are never exceeded; the scheduler counts both
  confirmed and uncertain sends against the caps.
- `warmup` optionally ramps from `daily_start` (5/day) up to the daily cap across your first
  active days (disabled in the shipped config for the established pilot account).
- Randomized `delays` (45–120 s default) between clicks; configurable.
- `login` waits for you to clear security checks manually, then persists the session so later
  runs stay headless and quiet.
- Every run stops immediately on a `/checkpoint/` URL, security text on the page, or LinkedIn's
  own "invitation limit" notice.

## Data & state

- `data/bot.db` — SQLite ledger of every click outcome (`SENT_PENDING`, `ERROR`, `UNKNOWN`,
  `LIMIT`) plus run events.
- `data/sessions/` — saved cookies (`storage_state`); git-ignored.
- `.env` — credentials; git-ignored.

## Tests

```
pytest
```

## Running on a Linux server (Docker)

A clean-room deployment lives under `deploy/`: a pinned Python + Playwright
image, a compose file mounting `data/` and `config.yaml` from the host, and a
full runbook. See `deploy/DEPLOY.md` — build, one-time GUI login for the
session, daily cron scheduling, and ops commands.

## Not implemented (v2 ideas)

- Personalized "Add a note" via the profile-page flow.
- Acceptance-rate tracking against LinkedIn's Sent-invitations page.
- Withdrawing stale pending invitations.
