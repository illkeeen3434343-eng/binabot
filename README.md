# binabot — subscription-based Bina.az monitor (Telegram)

Azerbaijani-language Telegram bot: users subscribe (Basic / Pro / Platinum), an admin
approves, users register Bina.az search links, the bot sends each **new** advertisement
once, under a rolling hourly limit. Completely separate from the `binaizleme` project
(own bot token, database, config, process).

Design, database schema and workflows: see `ARCHITECTURE.md`.

## Install (Ubuntu VM)

```bash
sudo apt install -y python3-venv sqlite3
cd ~ && unzip binabot.zip && cd binabot
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env        # BOT_TOKEN (new bot from @BotFather), ADMIN_IDS, PAYMENT_INFO
```

Your Telegram ID: send any message to @userinfobot. Python 3.11+ is required.

## First check from the VM (one request to Bina.az)

```bash
.venv/bin/python -m app.tools.probe "https://bina.az/baki/alqi-satqi/menziller?room_ids[]=3"
```

It must print `HTTP : 200`, a total close to what the website shows for the same
search, and a few ads. If it prints a persisted-query error, capture the new hash
(browser → F12 → Network → `graphql?operationName=SearchItems` → `sha256Hash`) and put
it in `.env` as `BINA_PERSISTED_HASH`.

## Run

```bash
.venv/bin/python run.py                  # foreground, Ctrl+C to stop
# as a service that survives reboots (edit user/paths in the unit first):
sudo cp deploy/binabot.service /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl enable --now binabot && journalctl -u binabot -f
```

Then in Telegram: `/start` (every user), `/admin` (admins only).

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q            # 61 tests, no internet needed
.venv/bin/python tests/smoke_process.py  # starts the real process against a fake Telegram API
```

## Operations

| What | Where |
|---|---|
| Database (users, links, history, queue) | `data/binabot.sqlite3` — back up with `deploy/backup.sh` |
| Logs | `logs/bot.log` (rotating, 8 × 2 MB) |
| Daily report to admins | `REPORT_HOUR` (default 09:00 Asia/Baku) |
| Errors | pushed to admins immediately (same error at most every 30 min) + `/admin → Sistem vəziyyəti` |
| HTTP 403 / 429 / 522 | requests stop, cool-down 15 → 30 → 60 → 120 min, admin notified, auto-resume |

## Supporting more Bina.az links

`bina_map.json` translates a link to a Bina.az filter. Shipped entries are the ones
proven by the existing project (Bakı, alqı-satqı, mənzillər, rooms, locations, price,
area, çıxarış, ipoteka, first/last floor) plus `has_repair` (checked locally on each
ad) and `kiraye` (rent → `leased: true`, **verify with the probe**). Any other
category or filter is rejected with a clear message instead of being guessed.
To add one: open the search on bina.az with F12 → Network, look at the `variables`
of the `SearchItems` request, add the id / parameter name to `bina_map.json`, then
confirm with the probe that the total matches the website. No code change needed.

## Settings

All in `.env` — see `.env.example` (intervals, timeouts, retries, concurrency, page
limits, queue rules, schedule hours). Plan limits live in `app/plans.py`.
