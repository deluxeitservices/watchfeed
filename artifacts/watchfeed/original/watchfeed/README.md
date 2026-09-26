# Watch Trading Feed

This tool reads your WhatsApp dealer groups. AI pulls every **WTS / WTB** watch out of each message: brand, reference, dial, condition, set, year, price and dealer. Staff get one searchable feed.

```
Your WhatsApp (linked device) ─► WAHA bridge ─► app (webhook) ─► Postgres
                                                 │
                                                 └─► AI parser (Claude) ─► offers ─► Staff feed  /
                                                                                     Admin      /admin
```

* **Read-only.** It never sends messages.
* **Only groups you tick are stored.** Private chats and unticked groups are dropped.
* **Duplicates are merged.** If the same dealer posts the same watch at the same price in several groups within 14 days, it shows as one offer ("Posts ×3").
* **AI costs are kept down.** Chat like "ok", "thanks" and "good morning" is skipped before the AI sees it. Text copy-pasted into many groups is parsed once.

---

## 1. What you need

| Item | Where | Approx cost |
|---|---|---|
| Small Linux server (2 GB RAM) | Hetzner CX22, DigitalOcean, etc. | £4–10 / month |
| Domain / subdomain, e.g. `feed.yourdomain.co.uk` | A record → server IP | – |
| Anthropic API key | console.anthropic.com → API keys | ~$2–3 per 1,000 offer messages parsed |

## 2. Install (on the server)

```bash
# Docker
curl -fsSL https://get.docker.com | sh

# Project
mkdir -p /opt/watchfeed && cd /opt/watchfeed
# upload the contents of this folder here (scp / git), then:
cp .env.example .env
nano .env            # fill in DOMAIN, passwords, ANTHROPIC_API_KEY
# generate the 3 random secrets:
for k in POSTGRES_PASSWORD WAHA_API_KEY WEBHOOK_SECRET; do sed -i "s|^$k=.*|$k=$(openssl rand -hex 24)|" .env; done

docker compose up -d --build
docker compose logs -f app      # Ctrl+C to exit logs
```

Open `https://feed.yourdomain.co.uk/admin` and log in with ADMIN_USER / ADMIN_PASSWORD.

### VPS with an existing Apache website

If Apache already owns ports 80/443, **do not** run the default Compose command
above or stop Apache. Keep this repository outside the public web root and use
the included override instead:

```bash
docker compose -f docker-compose.yml -f compose.apache.yml up -d --build
```

This leaves Caddy stopped and binds the Python app only to
`127.0.0.1:18080` on the VPS. Set up a separate Apache HTTPS virtual host for
the feed's own subdomain, proxying `/` to `http://127.0.0.1:18080/`. Verify
that port 18080 is unused before starting (or set `WATCHFEED_HOST_PORT` to a
different unused local port). Do not point Apache's public document root at
this project or expose its `.env` file. Ensure the domain's A record points
to the VPS and that the Apache site has a valid certificate before using the
admin login. Cloudflare proxy may remain enabled if its origin and HTTPS
settings are correct; test those after Apache is configured.

### Upgrade an existing Apache-hosted Watchfeed to v2

**Do not replace the VPS `.env`, run `docker compose down -v`, or copy the ZIP
over the live installation.** Existing PostgreSQL data needs an additive schema
upgrade before the new app starts. Keep the Apache override and the WhatsApp
session volume in place:

```bash
cd /opt/watchfeed-repo/artifacts/watchfeed/original/watchfeed
# Pull the reviewed code before continuing. Confirm compose.apache.yml is present.
umask 077
backup="$HOME/watchfeed-before-v2-$(date +%Y%m%d-%H%M%S).dump"
docker compose -f docker-compose.yml -f compose.apache.yml exec -T db \
  sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -h 127.0.0.1 -U watchfeed -d watchfeed -Fc' \
  > "$backup"
test -s "$backup" || { echo "Backup failed; stop here"; exit 1; }
docker compose -f docker-compose.yml -f compose.apache.yml build app
docker compose -f docker-compose.yml -f compose.apache.yml run --rm --no-deps app python -m app.migrate_v2
docker compose -f docker-compose.yml -f compose.apache.yml up -d --no-deps --force-recreate app
curl -fsS http://127.0.0.1:18080/health
docker compose -f docker-compose.yml -f compose.apache.yml ps app
```

The migration is repeatable and preserves existing offers and groups. Stop if
the backup or migration fails. The `ps` output must include
`127.0.0.1:18080->8000/tcp`: always use **both** Compose files when recreating
the app, or Apache will return HTTP 503. New WAHA photo settings take effect
only when the WAHA service is separately recreated. That briefly interrupts
WhatsApp; plan it after the core app works, and be ready to relink if needed.

## 3. Connect WhatsApp and pick groups

1. In **/admin**, click **Connect / show QR**.
2. On your phone, go to WhatsApp → Settings → **Linked devices** → **Link a device**, then scan the QR code. The status changes to **WORKING**.
3. Click **Load groups from WhatsApp**. All groups are listed, and **all start OFF**.
4. Tick the trading groups. Use the filter box with **Enable visible** to switch on many at once. Leave family and friends groups off.
5. Optional: click **Import last 300** on a group to pull in its recent history.

New messages then flow in live. Give staff the STAFF_USER / STAFF_PASSWORD login for `https://feed.yourdomain.co.uk/`.

## Additional staff tools in v2

* `/advisor`: compare dealer asking prices and WTB demand for a reference;
  suggested buy prices are estimates, not guaranteed sale prices.
* `/dealers`: review dealer activity, ratings and notes; blocked dealers
  disappear from the default feed.
* `/alerts`: save feed filters and optionally send matching offers and daily
  summaries through Telegram or email. Delivery requires setting the relevant
  `.env` variables and recreating **only the app** using both Compose files.
  Never put bot tokens or SMTP passwords in Git or chat.
* `/stock`: record inventory and see matching WTB requests.
* Feed photos, market comparisons and staff follow-up status appear when
  enough data exists. WhatsApp remains read-only; configured Telegram/email
  alerts **do** send notifications.

Optional `STAFF_USERS` gives each team member an individual login; the existing
`STAFF_USER` and `STAFF_PASSWORD` still work.

The admin page has a **Feed auto-refresh** switch (off by default). It applies
only to the admin's market feed; staff use **Refresh now** instead. This switch
does not pause WhatsApp ingestion or AI parsing, and changing it does not affect
Anthropic API charges.

## 4. Staff feed

* **Search** looks across references, models, dial, dealer name and phone and the raw text. `126710 blnr`, `5711`, `daytona panda` and `+852` all work.
* **Filters:** WTS / WTB, period, brand, condition, set, location, group, price range (GBP), year and "priced only".
* **Sorting:** click a column header.
* **View** shows the original WhatsApp message with the offer's line highlighted.
* **Dealer phone** opens a WhatsApp chat.

## 5. Tuning and troubleshooting

Test the AI on any pasted message:
```bash
    docker compose exec app python -m app.try_parse "🇭🇰 WTS 126710BLNR 03/2024 N FS hkd128k"
```
To change the extraction rules (shorthand, currency habits of your groups), edit `SYSTEM_PROMPT` in `app/parser.py`. Then run `docker compose up -d --build app`.

| Problem | Fix |
|---|---|
| Status `SCAN_QR_CODE` again | Your phone logged the device out. Rescan in /admin. |
| Stats show "Failed" rising | Check the API key or credit, then click **Retry failed messages**. |
| Dealer shows no phone | WhatsApp hides some numbers in groups (privacy "LID"). The name still shows. |
| Too many "No offer found" | Normal for chatty groups. They cost one AI call each. Disable pure-chat groups. |

Backups: `docker compose exec db pg_dump -U watchfeed watchfeed > backup.sql`

## 6. Important

* This uses WhatsApp's **Linked devices** feature through an unofficial bridge (WAHA). That is against WhatsApp's terms. Read-only use is low risk, but not zero. Keep it read-only, and consider moving to a dedicated number later. To move, just scan with the new phone.
* Open WhatsApp on the phone at least every ~10 days, or linked devices log out.
* Dealer numbers and names are personal data. Keep the feed behind the login and use it internally only.
* The WhatsApp bridge is never exposed to the internet. Only the app is public, via HTTPS (Caddy).

## Files

```
app/main.py       web app + API + webhook
app/ingest.py     WhatsApp payload → stored group message (group filter)
app/parser.py     prefilter + AI extraction prompt/schema
app/worker.py     background parsing, cleaning, dedupe
app/fx.py         currency → GBP (ECB rates, refreshed 12-hourly)
app/waha.py       WhatsApp bridge client (QR, groups, history)
app/static/       staff feed + admin pages
tests/            end-to-end tests (docker compose run --rm app sh -c "pip install pytest && python -m pytest -q tests")
```
