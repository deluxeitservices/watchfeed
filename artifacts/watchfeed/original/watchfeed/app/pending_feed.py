"""Combine staff-visible offers and unparsed group messages in one paginated feed."""
from datetime import timedelta
import re

from sqlalchemy import and_, func, literal, or_, select, union_all

from .config import settings
from .db import Dealer, Group, Message, Offer, utcnow
from .parser import looks_like_offer

# Use the same portable regular expressions for Python display and SQL filtering.
# A direction inferred from text is deliberately not an AI-parsed offer.
_WATCH = (r"(?i)(^|[^a-z0-9_])(rolex|patek|audemars|richard[ ]?mille|"
          r"omega|cartier|tudor|vacheron|breitling|panerai|iwc|hublot|"
          r"daytona|submariner|gmt|nautilus|royal[ ]?oak|speedmaster|"
          r"watch(es)?|timepiece|full[ ]?set|dial|bezel|bracelet|"
          r"[0-9]{5,6}[a-z]{0,6}|[0-9]{4,5}/[a-z0-9][a-z0-9/-]*)([^a-z0-9_]|$)")
_BUY = (r"(?i)(^|[^a-z0-9_])(wtb|lf|ltb|looking[ ]+for|want[ ]+to[ ]+buy|"
        r"need|searching[ ]+for|who[ ]+has|anyone[ ]+have|buying)([^a-z0-9_]|$)")
_SELL = (r"(?i)(^|[^a-z0-9_])(wts|for[ ]+sale|selling|sell|in[ ]+stock)([^a-z0-9_]|$)")
_AVAILABLE = r"(?i)(^|[^a-z0-9_])(available|ready)([^a-z0-9_]|$)"
_PRICE = (r"(?i)([$£€¥][ ]*[0-9]|(hkd|usd|usdt|aed|eur|gbp|chf|sgd|dhs)"
          r"[ ]*[0-9]|(^|[^a-z0-9_])[0-9]+([.,][0-9]+)?[ ]*[km]([^a-z0-9_]|$)|"
          r"(^|[^a-z0-9_])[0-9]{1,3}[,.][0-9]{3}([^a-z0-9_]|$))")


def provisional_directions(text: str) -> list[str]:
    """Conservative, free text hints; empty means unknown rather than a guessed sale."""
    if not re.search(_WATCH, text or ""):
        return []
    buying = bool(re.search(_BUY, text))
    selling = bool(re.search(_SELL, text))
    return (["WTS"] if selling or (not buying and (
        re.search(_AVAILABLE, text) or re.search(_PRICE, text))) else []) + (
        ["WTB"] if buying else [])


def _direction_clause(direction: str):
    """Match provisional_directions before SQL pagination without storing a guess."""
    text = Message.text
    watch, buy = text.regexp_match(_WATCH), text.regexp_match(_BUY)
    if direction == "WTB":
        return and_(watch, buy)
    return and_(watch, or_(text.regexp_match(_SELL),
                           and_(~buy, or_(text.regexp_match(_AVAILABLE),
                                          text.regexp_match(_PRICE)))))


def page(s, offer_stmt, sort_column, *, sort, order, page_number, page_size,
         days, group, query, direction, include_messages, show_blocked):
    """Return one ordered page without paginating offers and messages separately."""
    offers = offer_stmt.with_only_columns(
        literal("offer").label("kind"), Offer.id.label("item_id"),
        Offer.last_seen_at.label("seen_at"), sort_column.label("sort_value"),
        maintain_column_froms=True,
    )
    if include_messages:
        messages = select(
            literal("message").label("kind"), Message.id.label("item_id"),
            Message.ts.label("seen_at"),
            (Message.ts if sort in ("last_seen", "first_seen") else literal(None)).label("sort_value"),
        ).join(Group, Group.id == Message.group_id).where(
            Group.enabled.is_(True), Message.status != "PARSED",
        )
        if days:
            messages = messages.where(Message.ts >= utcnow() - timedelta(days=days))
        if group:
            messages = messages.where(Message.group_id == group)
        if direction in ("WTS", "WTB"):
            messages = messages.where(_direction_clause(direction))
        for token in query.split():
            pattern = f"%{token}%"
            messages = messages.where(or_(Message.text.ilike(pattern), Message.sender_name.ilike(pattern),
                                          Message.sender_phone.ilike(pattern), Group.name.ilike(pattern)))
        if not show_blocked:
            dealer_key = func.coalesce(func.nullif(Message.sender_phone, ""),
                                       func.nullif(func.lower(func.trim(Message.sender_name)), ""))
            blocked = select(Dealer.key).where(Dealer.rating == "BLOCKED")
            messages = messages.where(or_(dealer_key.is_(None), dealer_key.not_in(blocked)))
        items = union_all(offers, messages).subquery()
    else:
        items = offers.subquery()
    total = s.scalar(select(func.count()).select_from(items))
    ordered = select(items.c.kind, items.c.item_id).order_by(
        items.c.sort_value.is_(None),
        items.c.sort_value.asc() if order == "asc" else items.c.sort_value.desc(),
        items.c.seen_at.desc(), items.c.item_id.desc(), items.c.kind,
    ).offset((page_number - 1) * page_size).limit(page_size)
    ids = s.execute(ordered).all()
    offer_ids = [item_id for kind, item_id in ids if kind == "offer"]
    message_ids = [item_id for kind, item_id in ids if kind == "message"]
    offer_rows = {o.id: o for o in s.scalars(select(Offer).where(Offer.id.in_(offer_ids))).all()} if offer_ids else {}
    message_rows = ({m.id: m for m in s.scalars(select(Message).where(Message.id.in_(message_ids))).all()}
                    if message_ids else {})
    return total, ids, offer_rows, message_rows


def message_status(msg, group, budget_blocked):
    if msg.status != "NEW":
        return {
            "BUDGET": "Budget skipped · not queued",
            "PAUSED": "AI paused · not queued", "OLD": "History skipped",
            "SKIPPED": "Not a watch offer", "NO_OFFERS": "No offer found", "ERROR": "AI failed",
        }.get(msg.status, msg.status)
    if group.ai_paused:
        return "AI paused"
    if not settings.WORKER_ENABLED:
        return "AI worker off"
    if not looks_like_offer(msg.text):
        return "Waiting for check"
    if budget_blocked:
        return "AI budget blocked"
    if msg.ts < utcnow() - timedelta(hours=settings.AI_MAX_AGE_HOURS):
        return "Outside AI window"
    return "Waiting for AI"


def message_preview(msg, group, budget_blocked):
    return {
        "kind": "message", "id": msg.id, "group": group.name,
        "last_seen_at": msg.ts.isoformat() + "Z",
        "preview": " ".join(msg.text.split())[:220],
        "directions": provisional_directions(msg.text),
        "sender_name": msg.sender_name, "sender_phone": msg.sender_phone,
        "status": msg.status, "display_status": message_status(msg, group, budget_blocked),
    }