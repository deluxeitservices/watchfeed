"""Admin-only display preferences. These never change AI parsing or WhatsApp ingestion."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from .auth import is_admin
from .db import KV, SessionLocal

router = APIRouter()
FEED_AUTO_REFRESH_KEY = "admin_feed_auto_refresh"


def admin_auto_refresh_enabled(session) -> bool:
    row = session.get(KV, FEED_AUTO_REFRESH_KEY)
    return row is not None and row.value == "1"


class FeedRefreshIn(BaseModel):
    enabled: bool


@router.get("/admin/api/feed-refresh", dependencies=[Depends(is_admin)])
def get_feed_refresh():
    with SessionLocal() as session:
        return {"enabled": admin_auto_refresh_enabled(session)}


@router.put("/admin/api/feed-refresh", dependencies=[Depends(is_admin)])
def set_feed_refresh(body: FeedRefreshIn):
    with SessionLocal() as session:
        row = session.get(KV, FEED_AUTO_REFRESH_KEY)
        if row is None:
            session.add(KV(key=FEED_AUTO_REFRESH_KEY, value="1" if body.enabled else "0"))
        else:
            row.value = "1" if body.enabled else "0"
        session.commit()
    return {"enabled": body.enabled}