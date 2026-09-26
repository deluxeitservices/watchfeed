"""Turn WAHA message payloads into stored group messages."""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import waha
from .config import settings
from .db import Group, Message, utcnow
from .parser import text_hash


def _phone_from_jid(jid: Optional[str]) -> Optional[str]:
    if not jid or "@" not in jid:
        return None
    user, domain = jid.split("@", 1)
    if domain in ("s.whatsapp.net", "c.us"):
        return user.split(":")[0]
    return None


def normalize(p: dict) -> Optional[dict]:
    """Return a normalized dict for group messages, None for anything else."""
    if not isinstance(p, dict):
        return None
    chat = p.get("from") or ""
    if p.get("fromMe") and str(p.get("to", "")).endswith("@g.us"):
        chat = p["to"]
    if not str(chat).endswith("@g.us"):
        return None
    text = (p.get("body") or "").strip()
    if not text or not p.get("id"):
        return None
    data = p.get("_data") if isinstance(p.get("_data"), dict) else {}
    key = data.get("key") if isinstance(data.get("key"), dict) else {}
    participant = p.get("participant") or key.get("participant") or p.get("author") or data.get("author")
    if isinstance(participant, dict):
        participant = participant.get("_serialized")
    phone = _phone_from_jid(participant)
    for alt in (key.get("participantAlt"), key.get("participantPn"), p.get("participantPn")):
        if not phone:
            phone = _phone_from_jid(alt)
    name = data.get("pushName") or data.get("notifyName") or p.get("notifyName") or p.get("pushName")
    ts_raw = p.get("timestamp")
    try:
        ts = datetime.fromtimestamp(int(ts_raw), tz=timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError):
        ts = utcnow()
    return {
        "wa_id": str(p["id"]) if not isinstance(p["id"], dict) else p["id"].get("_serialized"),
        "group_id": chat,
        "sender_jid": participant,
        "sender_phone": phone,
        "sender_name": name,
        "text": text,
        "has_media": bool(p.get("hasMedia")),
        "ts": ts,
    }


async def resolve_phone(m: dict) -> dict:
    jid = m.get("sender_jid") or ""
    if not m.get("sender_phone") and jid.endswith("@lid"):
        m["sender_phone"] = await waha.resolve_lid(jid)
    return m


def store(s: Session, m: dict) -> Optional[Message]:
    """Store message if its group is enabled. Returns Message or None (skipped/duplicate)."""
    group = s.get(Group, m["group_id"])
    if group is None:
        group = Group(id=m["group_id"], name=m["group_id"], enabled=settings.AUTO_ENABLE_NEW_GROUPS)
        s.add(group)
        s.flush()
    if not group.enabled:
        s.commit()
        return None
    if s.scalar(select(Message.id).where(Message.wa_id == m["wa_id"])):
        return None
    msg = Message(text_hash=text_hash(m["text"]), **m)
    s.add(msg)
    group.message_count = (group.message_count or 0) + 1
    if not group.last_message_at or m["ts"] > group.last_message_at:
        group.last_message_at = m["ts"]
    try:
        s.commit()
    except IntegrityError:  # same message delivered twice at once
        s.rollback()
        return None
    return msg
