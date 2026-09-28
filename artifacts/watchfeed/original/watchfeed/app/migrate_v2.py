"""Additive upgrade for existing Watchfeed databases; run before starting the new app.

Back up the database first. This keeps all existing messages, offers and groups.
Run with: python -m app.migrate_v2
"""

from sqlalchemy import inspect, text

from .db import AIUsage, AIUsageGroup, Base, LineCache, Offer, engine


OFFER_COLUMNS = (
    "dealer_key",
    "photo_id",
    "photo_kind",
    "market_pct",
    "track_status",
    "track_by",
    "track_at",
    "track_note",
)


def upgrade() -> None:
    # create_all adds the new feature tables, but does not alter existing tables.
    Base.metadata.create_all(engine, tables=[table for table in Base.metadata.sorted_tables
                                               if table not in (AIUsage.__table__, AIUsageGroup.__table__,
                                                                LineCache.__table__)])
    existing = {column["name"] for column in inspect(engine).get_columns("offers")}
    with engine.begin() as conn:
        for name in OFFER_COLUMNS:
            if name not in existing:
                column_type = Offer.__table__.c[name].type.compile(dialect=engine.dialect)
                conn.execute(text(f"ALTER TABLE offers ADD COLUMN {name} {column_type}"))
                print(f"Added offers.{name}")
        conn.execute(text(
            "UPDATE offers SET dealer_key = COALESCE(NULLIF(dealer_phone, ''), "
            "LOWER(NULLIF(TRIM(dealer_name), ''))) WHERE dealer_key IS NULL"
        ))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_offers_dealer_key ON offers (dealer_key)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_offers_market_pct ON offers (market_pct)"))
    print("Watchfeed schema upgrade complete. Existing records were preserved.")


if __name__ == "__main__":
    upgrade()