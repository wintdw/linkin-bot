# Deploying with Docker on a Linux host (with a GUI)

The bot runs headless inside a container for daily runs, but the **one-time
login needs a visible browser** (LinkedIn will ask for an OTP/security check).
The image runs Chromium as a non-root user; the container's window can appear
on your Linux desktop through the X11 socket. All state — session, ledger,
config — lives in the host's `data/` and `config.yaml`, so containers are
throwaway and clean.

## Layout

```
linkedin-connect-bot/
├── deploy/
│   ├── Dockerfile          # pinned python:3.13-slim + playwright 1.62 + chromium
│   ├── docker-compose.yml  # data/config mounts, X11 passthrough
│   └── DEPLOY.md
├── .env                    # credentials (host-side, git-ignored)
├── config.yaml             # caps/delays (host-side, live-mounted)
└── data/                   # bot.db + sessions (host-side, git-ignored)
```

## 1. Copy the project to the server

```bash
rsync -av --exclude .venv --exclude data ./linkedin-connect-bot/ \
      user@server:~/linkedin-connect-bot/
```

Create `.env` on the server (from `.env.example`) with the credentials, and
make sure `config.yaml` has the caps you want. Both are read from the host at
run time — no rebuild needed to change them.

## 2. Build the image

```bash
cd ~/linkedin-connect-bot
docker compose -f deploy/docker-compose.yml build
```

## 3. One-time login — needs your GUI desktop session

```bash
xhost +local: 2>/dev/null || true          # allow the container to open a window
docker compose -f deploy/docker-compose.yml run --rm bot \
    python -u -m linkedin_bot.cli login
```

- A Chromium window opens on your desktop. Auto-fill is best-effort; if it does
  not complete, type the email/password by hand.
- When the security check / OTP prompt appears, finish it **in that window**.
- Keep the window open until you see `Session saved to ...`.
  The session lands in the host's `data/sessions/` (mounted volume).

**Why not copy the Windows session?** Cookies move, but LinkedIn re-verifies
accounts that suddenly appear on a new IP (especially a datacenter IP). Logging
in once from the server makes the server IP the account's home. Expect that
first server login to be checkpoint-heavy; that is normal.

Verify headlessly (no X needed):

```bash
docker compose -f deploy/docker-compose.yml run --rm bot \
    python -u -m linkedin_bot.cli run --dry-run
```

## 4. Schedule the daily run (host cron)

A cron entry on the host is the simplest scheduler for a one-shot container:

```bash
crontab -e
# add:
30 9 * * * cd /home/you/linkedin-connect-bot && docker compose -f deploy/docker-compose.yml run --rm bot >> data/cron.log 2>&1
```

The default container command is `run`, which stops itself at the daily/weekly
caps in `config.yaml`. Note the 0-30 min humanization is not built into cron;
set the minute value wherever you like.

## Day-to-day operations

```bash
docker compose -f deploy/docker-compose.yml run --rm bot \
    python -u -m linkedin_bot.cli status            # caps usage
docker compose -f deploy/docker-compose.yml run --rm bot \
    python -u -m linkedin_bot.cli stats             # history
docker compose -f deploy/docker-compose.yml run --rm bot \
    python -u -m linkedin_bot.cli run --headed      # watch a run
tail -f data/cron.log                                # scheduled run output
```

## Emergency stop

```bash
touch data/STOP     # container halts before the next click; delete to resume
```

The bot also stops itself on a LinkedIn security check or LinkedIn's own
invitation-limit notice.

## Notes & backups

- `data/` and `config.yaml` are bind-mounted from the host; rebuild the image
  only when the code changes (`docker compose build`).
- `data/bot.db` is the ledger. Back it up while idle:
  `cp data/bot.db data/bot.db.$(date +%F).bak`.
- X11 tip: if the login window will not open, run `xhost +local:` (access
  control) and confirm `DISPLAY` is set in your shell. For headless-only hosts,
  replace the X socket mount with an Xvfb + noVNC container instead.
- If the account gets a hard checkpoint, remove the cron line or `touch
  data/STOP`, let the account rest, and resume manually after LinkedIn clears
  it.
