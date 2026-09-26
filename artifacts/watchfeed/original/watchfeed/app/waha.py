"""Thin client for WAHA (WhatsApp HTTP API) — https://waha.devlike.pro"""
import logging
from typing import Optional

import httpx

from .config import settings

log = logging.getLogger("waha")


def _headers() -> dict:
    return {"X-Api-Key": settings.WAHA_API_KEY} if settings.WAHA_API_KEY else {}


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=settings.WAHA_URL, headers=_headers(), timeout=60)


def webhook_url() -> str:
    return f"{settings.APP_INTERNAL_URL}/webhook/waha?token={settings.WEBHOOK_SECRET}"


def session_config() -> dict:
    return {
        "webhooks": [{"url": webhook_url(), "events": ["message.any"]}],
        # NOWEB store lets us list groups and pull message history
        "noweb": {"store": {"enabled": True, "fullSync": True}},
    }


async def session_status() -> dict:
    async with _client() as c:
        r = await c.get(f"/api/sessions/{settings.WAHA_SESSION}")
        if r.status_code == 404:
            return {"status": "NOT_CREATED"}
        r.raise_for_status()
        return r.json()


async def start_session() -> dict:
    name = settings.WAHA_SESSION
    async with _client() as c:
        r = await c.get(f"/api/sessions/{name}")
        if r.status_code == 404:
            r = await c.post("/api/sessions", json={"name": name, "start": True, "config": session_config()})
            r.raise_for_status()
            return r.json()
        r.raise_for_status()
        await c.put(f"/api/sessions/{name}", json={"name": name, "config": session_config()})
        if r.json().get("status") in ("STOPPED", "FAILED"):
            await c.post(f"/api/sessions/{name}/start")
        return (await c.get(f"/api/sessions/{name}")).json()


async def restart_session() -> None:
    async with _client() as c:
        await c.post(f"/api/sessions/{settings.WAHA_SESSION}/restart")


async def qr_png() -> Optional[bytes]:
    async with _client() as c:
        r = await c.get(f"/api/{settings.WAHA_SESSION}/auth/qr", params={"format": "image"},
                        headers={"Accept": "image/png"})
        if r.status_code != 200:
            return None
        return r.content


async def list_groups() -> list:
    """Returns [{id, name, participants}] regardless of engine response shape."""
    async with _client() as c:
        r = await c.get(f"/api/{settings.WAHA_SESSION}/groups")
        r.raise_for_status()
        data = r.json()
    items = data.values() if isinstance(data, dict) else data
    out = []
    for g in items:
        gid = g.get("id")
        if isinstance(gid, dict):
            gid = gid.get("_serialized")
        if not gid or not str(gid).endswith("@g.us"):
            continue
        name = g.get("subject") or g.get("name") or (g.get("groupMetadata") or {}).get("subject") or gid
        parts = g.get("participants") or (g.get("groupMetadata") or {}).get("participants")
        out.append({"id": gid, "name": name, "participants": len(parts) if isinstance(parts, list) else None})
    return out


async def chat_messages(chat_id: str, limit: int = 300) -> list:
    async with _client() as c:
        r = await c.get(f"/api/{settings.WAHA_SESSION}/chats/{chat_id}/messages",
                        params={"limit": limit, "downloadMedia": "false"})
        r.raise_for_status()
        return r.json()


_lid_cache: dict = {}


async def resolve_lid(lid: str) -> Optional[str]:
    """WhatsApp hides numbers behind '@lid' ids in some groups; ask WAHA for the phone number."""
    if lid in _lid_cache:
        return _lid_cache[lid]
    phone = None
    try:
        async with _client() as c:
            r = await c.get(f"/api/{settings.WAHA_SESSION}/lids/{lid}")
            if r.status_code == 200:
                pn = (r.json() or {}).get("pn")
                if pn:
                    phone = pn.split("@")[0].split(":")[0]
    except Exception as e:
        log.debug("lid resolve failed: %s", e)
    _lid_cache[lid] = phone
    return phone
