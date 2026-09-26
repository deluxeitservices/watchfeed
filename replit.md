# Watch Trading Feed

Searchable staff feed of WTS/WTB watch offers from selected WhatsApp groups, with a separate protected admin interface.

## Run & Operate

- `pnpm --filter @workspace/watchfeed run dev` — start the imported FastAPI app through its managed workflow
- `cd artifacts/watchfeed/original/watchfeed && python -m pytest -q tests` — run mocked pipeline tests
- `artifacts/watchfeed/README.md` — Replit preview and live-service setup details
- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- Development preview: `WATCHFEED_DEMO=true` uses an isolated local SQLite database with fictional offers; no WhatsApp or AI access.
- Live mode: requires staff/admin passwords, webhook secret, PostgreSQL, Anthropic access, and a separate WAHA bridge.

## Stack

- Imported app: FastAPI, SQLAlchemy, Python 3; no React rewrite of the uploaded app.
- The pnpm/Express/Drizzle scaffold remains in the workspace but is not used by this feed.

## Where things live

- Application and tests: `artifacts/watchfeed/original/watchfeed/`
- Workflow wrapper: `artifacts/watchfeed/package.json`

## Architecture decisions

- Preserve the user's FastAPI code instead of porting it to TypeScript; the generated web artifact is used only as a managed preview wrapper.
- Isolate demo data from the managed database to avoid accidental exposure of personal dealer details in a public preview.

## Product

- Staff can search, sort, and filter watch offers and view the source message; admins can manage groups when live services are configured.

## User preferences

- User asked to check and set up the uploaded code and explain what works and how it works.

## Gotchas

- Demo mode never starts the background worker or accepts WAHA webhooks. Do not activate live mode before configuring auth and the bridge.
- The imported Docker Compose stack does not automatically run inside the Replit artifact.

## Pointers

- `artifacts/watchfeed/README.md` for the actual imported app setup.
