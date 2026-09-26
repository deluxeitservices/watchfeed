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

## 3. Connect WhatsApp and pick groups

1. In **/admin**, click **Connect / show QR**.
2. On your phone, go to WhatsApp → Settings → **Linked devices** → **Link a device**, then scan the QR code. The status changes to **WORKING**.
3. Click **Load groups from WhatsApp**. All groups are listed, and **all start OFF**.
4. Tick the trading groups. Use the filter box with **Enable visible** to switch on many at once. Leave family and friends groups off.
5. Optional: click **Import last 300** on a group to pull in its recent history.

New messages then flow in live. Give staff the STAFF_USER / STAFF_PASSWORD login for `https://feed.yourdomain.co.uk/`.

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
