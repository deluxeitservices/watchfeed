"""Back up the database, then run: python -m app.migrate_v4.

Preserves existing messages/offers and upgrades either the previous request
meter or the uploaded v5 daily meter (retained as ai_usage_legacy_daily).
"""
from sqlalchemy import inspect, text

from .db import AIUsage, AIUsageGroup, Group, LineCache, engine


def upgrade() -> None:
    inspector = inspect(engine)
    if not inspector.has_table("messages") or not inspector.has_table("groups"):
        raise RuntimeError("Existing messages/groups missing; initialize or upgrade Watchfeed first.")
    if inspector.has_table("ai_usage"):
        columns = {col["name"] for col in inspector.get_columns("ai_usage")}
        if "day" in columns and "id" not in columns:
            if inspector.has_table("ai_usage_legacy_daily"):
                raise RuntimeError("Legacy daily usage already exists; resolve this schema before upgrading.")
            with engine.begin() as conn:
                if inspector.has_table("ai_usage_groups"):
                    count = conn.scalar(text("SELECT count(*) FROM ai_usage_groups"))
                    if count:
                        raise RuntimeError("Existing AI group allocations need manual review before migration.")
                    conn.execute(text("DROP TABLE ai_usage_groups"))
                conn.execute(text("ALTER TABLE ai_usage RENAME TO ai_usage_legacy_daily"))
            print("Preserved uploaded v5 daily usage as ai_usage_legacy_daily.")
        elif not {"id", "created_at", "estimated_usd"} <= columns:
            raise RuntimeError("Unrecognized ai_usage schema; no changes made.")
    AIUsage.__table__.create(engine, checkfirst=True)
    AIUsageGroup.__table__.create(engine, checkfirst=True)
    LineCache.__table__.create(engine, checkfirst=True)
    columns = {col["name"] for col in inspect(engine).get_columns("groups")}
    with engine.begin() as conn:
        for key in ("ai_paused", "ai_checked", "ai_hits"):
            if key not in columns:
                col = Group.__table__.c[key]
                type_sql = col.type.compile(dialect=engine.dialect)
                default = "false" if key == "ai_paused" else "0"
                conn.execute(text(f"ALTER TABLE groups ADD COLUMN {key} {type_sql} DEFAULT {default} NOT NULL"))
    print("Watchfeed v5 additive schema ready; existing offers and messages preserved.")


if __name__ == "__main__":
    upgrade()