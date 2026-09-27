"""Explicit, repeatable additive AI usage-table upgrade. Back up the database first.

Run with: python -m app.migrate_v3
"""
from sqlalchemy import inspect

from .db import AIUsage, AIUsageGroup, engine


def upgrade() -> None:
    if not inspect(engine).has_table("messages"):
        raise RuntimeError("Existing messages table missing; initialize or upgrade Watchfeed first.")
    AIUsage.__table__.create(engine, checkfirst=True)
    AIUsageGroup.__table__.create(engine, checkfirst=True)
    print("AI usage tables ready. Existing messages and offers were not changed.")


if __name__ == "__main__":
    upgrade()