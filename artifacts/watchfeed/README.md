# Watch Trading Feed on Replit

This project preserves the uploaded FastAPI application under `original/watchfeed/`.
The managed web workflow serves that Python app directly; the React/Vite scaffold
created by the workspace is not used for the feed.

## Current preview

The development environment sets `WATCHFEED_DEMO=true`. The staff feed at `/`
shows four **fictional** watch offers, stored only in a separate local
`watchfeed_demo.db`. The preview cannot ingest WhatsApp messages, run AI extraction,
or access the original database. `/admin` remains password protected.

## To run live

1. Set `WATCHFEED_DEMO=false` in the intended environment. Do not turn it off
   before configuring all credentials.
2. Add `STAFF_PASSWORD`, `ADMIN_PASSWORD`, `WEBHOOK_SECRET`, and
   `ANTHROPIC_API_KEY` as secrets (never put them in source or chat). Set
   `STAFF_USER` and `ADMIN_USER` if the defaults `staff` and `admin` do not fit.
3. Provide a reachable WAHA bridge and set `WAHA_URL`, `WAHA_API_KEY`, and
   `APP_INTERNAL_URL` for webhook delivery. The original `docker-compose.yml`
   runs WAHA as a private container on a separate Docker host; Replit's web
   preview does **not** start that bridge or link a WhatsApp account.
4. Use durable PostgreSQL for live data and plan the schema migration before
   publishing. The imported app's SQLAlchemy `create_all()` is not a migration
   system. Do not put real dealer data in the demo SQLite database.
5. Keep HTTPS, staff/admin access controls, and the webhook secret in place.
   WAHA is an unofficial WhatsApp bridge and may violate WhatsApp's terms.

The original project's Docker setup and walkthrough are in
`original/watchfeed/README.md`. That Docker guide describes a *different*
deployment method; it does not automatically configure Replit.

## Code map

- `original/watchfeed/app/main.py`: HTTP Basic auth, feed/admin pages, API, webhook
- `original/watchfeed/app/ingest.py`: filters direct chats and disabled groups
- `original/watchfeed/app/parser.py`: prefilter + Anthropic extraction
- `original/watchfeed/app/worker.py`: background processing, cache, deduplication
- `original/watchfeed/app/fx.py`: converts prices to the base currency
- `original/watchfeed/app/waha.py`: bridge QR, groups, history, and session client
- `original/watchfeed/tests/test_pipeline.py`: mocked end-to-end flow

Run tests from `original/watchfeed/` with `python -m pytest -q tests`.