# Architecture

## 1. The existing project (`binaizleme`) — what was inspected

`monitor.py` (3 291 lines, one file) is a **single-user, run-to-completion script**
started by cron / GitHub Actions. One run: load `seen.json` (GitHub Contents API or
local file) → fetch every source → compare → send Telegram messages to one
`TELEGRAM_CHAT_ID` → save `seen.json`.

Bina.az part:

| Mechanism | How it works there |
|---|---|
| Retrieval | `GET https://bina.az/graphql`, operation `SearchItems`, Apollo **persisted query** (`sha256Hash`), `sort=BUMPED_AT_DESC`, 16 ads/page, cursor paging. It scans the **whole** result set (up to 1000 pages) because it is a price tracker. |
| Link → filter | `bina_filter_vars()`: `room_ids[]`, `location_ids[]`, `price_from/to`, `area_from/to`, `has_bill_of_sale`, `has_mortgage`, `floor_first/last`; `cityId=1`, `categoryId=1`, `leased=false` are hard-coded. `has_repair` in the URL is silently not sent. |
| Extraction | `_bina_node()`: id, path, price.total/currency, rooms, area.value/units, floor/floors, location.fullName, preview.f460x345, hasRepair… |
| Duplicates | dictionary `seen[id]` in `seen.json`; first run seeds silently. |
| Promotion | `isFeatured` / `isVipped` (+ regex fallback); promoted **and** old ⇒ ignored. Age from `createdAt`, else estimated from the ID (IDs grow ≈ 2 800/day). |
| Images | download bytes and upload → by URL → text. |
| Errors | per-page retry on network / 5xx / 429, `PersistedQueryError` alert, "alert only when the error changes", recovery message. |

## 2. Reused (re-implemented, not imported — the projects stay independent)

GraphQL request shape and headers · the proven link→filter vocabulary · node field
names · promotion flags · ID-based age estimate · silent first-run seeding · image
fallback chain · alert-on-change / recovery notices · curl_cffi Chrome fingerprint.

**Not** reused: full-set scanning, price tracking, owner/agent detection, other
portals, JSON state on GitHub, the single global chat.

## 3. New architecture

```
Telegram ──► aiogram Dispatcher ──► bot/admin.py, bot/user.py ─┐
                                                               ▼
                     ┌──────────── SQLite (db.py, WAL) ◄───────┤
                     │                                         │
 monitor loop ──► services/monitor.py ──► bina/client.py ──► Bina.az
 (5 min)            one fetch per distinct search, fan-out      (throttled)
                     │  writes deliveries(status=queued)
 dispatch loop ─► services/dispatcher.py ─► services/messenger.py ─► users
 (15 s)             rolling hourly limit per plan
 jobs loop ─────► services/jobs.py  expiry · reminders · daily report · queue rules
 (60 s)
 any failure ───► services/alerts.py ─► admins (deduplicated) + errors table
```

Key decisions

* **One process, three loops, one database.** No Redis/Celery: right size for a VM,
  and each layer can be replaced without touching the others.
* **Search de-duplication.** A link is normalised (host, path, sorted known
  parameters). Equal searches share one `searches` row ⇒ one request per cycle no
  matter how many users track it.
* **"New" = unseen listing ID for that search AND actually recent.** Because of
  `BUMPED_AT_DESC`, paid bumps lift old ads to the top; age (creation date, else
  ID-clock) filters them. Promoted + new is still announced.
* **Adaptive paging.** Page 2+ is requested only while the previous page contained
  unseen IDs (max `MAX_PAGES`) ⇒ a quiet search costs exactly one request.
* **Queue + rolling limit.** Every (user, ad) pair is one `deliveries` row, UNIQUE,
  so an ad can never be sent twice to a user — also after a restart and also when two
  of the user's links match it. The limit counts rows `sent` in the last 3 600 s.
* **Circuit breaker.** HTTP 403/429/522 (anything that is not 200/5xx) is never
  retried: the cycle stops, a cool-down is stored in the DB, the admin is told once.
* **Bina.az knowledge is isolated**: `bina/parser.py` (field names),
  `bina/client.py` (request shape), `bina_map.json` (link vocabulary).
* **Dialog state lives in the DB** (`users.state`), so a restart in the middle of
  "send me the link" loses nothing.

## 4. Database schema (SQLite)

| Table | Purpose | Key columns |
|---|---|---|
| `users` | registration + subscription | user_id PK, username, first_name, registered_at, status, plan, requested_plan, activated_at, expires_at, state, last_reminder_day, last_limit_notice_at, blocked_bot |
| `searches` | one row per distinct Bina.az search | id, url UNIQUE (normalised), filter_json, client_filter_json, seeded, last_ok_at, last_error |
| `links` | user ↔ search | id, user_id, search_id, UNIQUE(user_id, search_id) |
| `search_seen` | advertisement history per search | PK(search_id, ad_id) |
| `ads` | ad snapshot used for the message | ad_id PK, data (JSON) |
| `deliveries` | queue + "who received what" | UNIQUE(user_id, ad_id), status queued/sent/expired/gone/cancelled/failed, created_at, sent_at |
| `cycles` | monitoring runs | started_at, searches_ok/failed, new_ads, note |
| `errors` | scraper / bot errors | ts, source, code, message |
| `audit` | admin actions | ts, admin_id, action, target_user, details |
| `kv` | small persistent state | cool-down, max listing ID, last report day |

Statuses: `new` (registered, never requested) → `pending` → `active` | `rejected`;
`active` → `suspended` ↔ `active`; `active` → `expired` (validity ended).

## 5. Workflows

**User.** `/start` → registered, welcome, main menu `[📊 Statusum] [🔗 Linklərim]
[💎 Abunəlik] [ℹ️ Kömək]`. Abunəlik → pick plan → confirm → `pending`, admins get
the request with ✅/❌. After approval: Linklərim → ➕ → paste link → validated,
normalised, de-duplicated. Link card → ✏️ / 🗑, both behind a Bəli/Xeyr
confirmation. Every sub-screen ends with `[⬅️ Geri] [🏠 Ana səhifə]`; Geri goes to
the parent screen, Ana səhifə always to the main menu.

**Admin** (`/admin`, IDs from `ADMIN_IDS`, checked on every update): requests →
approve (choose 30/90/180/365 days or type a date) / reject; users list → user card
→ plan, expiry, suspend / reactivate, links (view / remove); statistics; system
health (last cycle, cool-down, recent errors); audit log; run a cycle now.

**Ad lifecycle.** detected → `queued` for every entitled link owner → `sent` when
the user's rolling window has room → otherwise waits; `expired` after
`QUEUE_TTL_HOURS` or at 00:00 (`QUEUE_RESET_AT_MIDNIGHT`), `gone` if the ad was
deleted while waiting > `VERIFY_AFTER_MIN`, `cancelled` when the user loses access
or removes the link.

## 6. Folder structure

```
binabot/
├── run.py                  start the bot
├── .env.example            every setting, documented
├── bina_map.json           link → Bina.az filter vocabulary (data, not code)
├── requirements.txt / requirements-dev.txt / pytest.ini
├── app/
│   ├── main.py             wiring, logging, background loops, shutdown
│   ├── config.py           .env → Config
│   ├── plans.py            Basic / Pro / Platinum limits, status names
│   ├── texts.py            ALL Azerbaijani text + ad caption
│   ├── db.py               schema + every SQL statement
│   ├── bina/
│   │   ├── links.py        validate / normalise / translate a link
│   │   ├── client.py       throttled HTTP, retries, error model, images
│   │   └── parser.py       Bina.az JSON → Ad
│   ├── services/
│   │   ├── monitor.py      cycles, new-ad detection, cool-down
│   │   ├── dispatcher.py   queue → users under the rolling limit
│   │   ├── messenger.py    sending (photo → upload → text), admin broadcast
│   │   ├── jobs.py         expiry, reminders, daily report, queue rules
│   │   └── alerts.py       error log + deduplicated admin alerts
│   ├── bot/
│   │   ├── common.py       context, keyboards, screen rendering, registration
│   │   ├── user.py         user menus
│   │   └── admin.py        admin panel
│   └── tools/probe.py      one-request check of a link against Bina.az
├── tests/                  61 automated tests + smoke_process.py
└── deploy/                 systemd unit, backup script
```

## 7. Implementation phases (each finished with its tests green)

1. Foundation — config, plans, database, link validation (`tests/test_links.py`).
2. Bina.az client + monitoring engine (`tests/test_monitor.py`).
3. Delivery queue, rolling limits, expiry / reminders / reports (`tests/test_delivery.py`).
4. User and admin Telegram interface (`tests/test_bot.py`).
5. Entry point, probe tool, real-process smoke test, deployment files.

## 8. What is verified and what is not

Verified by automated tests: everything above the network boundary — handlers are
driven with real aiogram updates, Bina.az and Telegram are replaced by fakes at the
HTTP layer.

**Not verified: live Bina.az.** The build environment could not reach bina.az. The
request shape, hash and field names are taken from `monitor.py` as of 2026-11-12.
Run `python -m app.tools.probe <link>` on the VM before inviting users. `kiraye →
leased:true` is an inference, not something the old project exercised.

Interpretations made (each is one `.env` line to change):

* Over-limit ads are queued and trickle out as the rolling window frees; whatever is
  still queued at 00:00 is dropped and the new day starts clean
  (`QUEUE_RESET_AT_MIDNIGHT=false` keeps them up to `QUEUE_TTL_HOURS`).
* A link with a filter the bot cannot translate is rejected (`UNKNOWN_PARAMS=ignore`
  accepts it and tells the user which filters are ignored).
* After a downgrade, extra links are kept but paused (oldest links stay monitored).
* A search nobody was entitled to for < 24 h resumes where it stopped (ads up to
  `NEW_MAX_AGE_DAYS` old may arrive); after a longer gap it is re-baselined silently.
