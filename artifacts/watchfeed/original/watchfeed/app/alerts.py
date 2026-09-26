"""Saved-search alerts, stock matches and the morning summary. Sent by email and/or Telegram.

The WhatsApp number is never used to send anything - the tool stays read-only."""
import asyncio
import hashlib
import html
import json
import logging
import os
import secrets
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import delete, func, select, update

from . import market, worker_utils
from .config import settings
from .db import KV, Alert, AlertHit, Dealer, Offer, Photo, SessionLocal, StockItem, utcnow

log = logging.getLogger("alerts")
OUTBOX_PREFIX = "notify:"
OFFER_RECOVERY_WINDOW = timedelta(hours=48)
OUTBOX_MARKER_RETENTION = timedelta(days=4)
MAX_DELIVERY_ATTEMPTS = 8
DELIVERY_TIMEOUT = 30
OUTBOX_LEASE = timedelta(minutes=5)

COND = {"NEW": "New", "LIKE_NEW": "Like new", "USED": "Pre-owned", "VINTAGE": "Vintage"}
SETS = {"FULL_SET": "Full set", "WATCH_ONLY": "Watch only", "BOX_ONLY": "Watch + box", "PAPERS_ONLY": "Watch + papers"}
SYMBOL = {"GBP": "£", "USD": "$", "EUR": "€", "HKD": "HK$"}


# ------------------------------------------------------------------ matching
def offer_matches(o: dict, q: dict) -> bool:
    """o: offer dict (see offer_dict). q: the same filters the feed uses."""
    if q.get("direction") and o["direction"] != q["direction"]:
        return False
    if q.get("brand") and o.get("brand") != q["brand"]:
        return False
    for k in ("condition", "set_type", "country"):
        if q.get(k) and (o.get(k) or "") not in str(q[k]).upper().split(","):
            return False
    pb = o.get("price_base")
    if q.get("min_price") not in (None, "") and (pb is None or pb < float(q["min_price"])):
        return False
    if q.get("max_price") not in (None, "") and (pb is None or pb > float(q["max_price"])):
        return False
    if q.get("priced_only") and pb is None:
        return False
    y = o.get("year")
    if q.get("year_from") not in (None, "") and (y is None or y < int(q["year_from"])):
        return False
    if q.get("year_to") not in (None, "") and (y is None or y > int(q["year_to"])):
        return False
    if q.get("deals_only") and market.deal_label(o.get("market_pct"), settings.DEAL_THRESHOLD_PCT) != "DEAL":
        return False
    if q.get("group") and q["group"] not in (o.get("groups_seen") or ""):
        return False
    hay = " ".join(str(o.get(k) or "") for k in ("brand", "family", "model", "dial_color", "case_material",
                                                 "source_text", "dealer_name", "dealer_phone")).lower()
    for tok in str(q.get("q") or "").split():
        ref = worker_utils.norm_ref(tok)
        if tok.lower() not in hay and not (ref and ref in (o.get("reference_norm") or "")):
            return False
    return True


def offer_dict(o: Offer) -> dict:
    return {c: getattr(o, c) for c in (
        "id", "direction", "brand", "family", "model", "reference", "reference_norm", "dial_color", "case_material",
        "condition", "set_type", "year", "price", "currency", "price_base", "country", "source_text",
        "dealer_name", "dealer_phone", "dealer_key", "groups_seen", "market_pct", "photo_id")}


# ------------------------------------------------------------------ formatting
def money(v: Optional[float], cur: Optional[str]) -> str:
    if v is None:
        return ""
    return f"{SYMBOL.get(cur or '', (cur or '') + ' ')}{v:,.0f}"


def offer_line(o: dict) -> str:
    title = " ".join(x for x in (o.get("brand"), o.get("family")) if x)
    bits = [f"{o['direction']} {title} {o.get('reference') or ''}".strip(),
            COND.get(o.get("condition"), ""), SETS.get(o.get("set_type"), ""), str(o.get("year") or "")]
    if o.get("price") is not None:
        p = money(o["price"], o.get("currency"))
        if o.get("currency") != settings.BASE_CURRENCY and o.get("price_base"):
            p += f" ({money(o['price_base'], settings.BASE_CURRENCY)})"
        bits.append(p)
    lab = market.deal_label(o.get("market_pct"), settings.DEAL_THRESHOLD_PCT)
    if lab == "DEAL":
        bits.append(f"DEAL {o['market_pct'] * 100:.0f}% vs market")
    who = " ".join(x for x in (o.get("dealer_name"), f"+{o['dealer_phone']}" if o.get("dealer_phone") else None) if x)
    if who:
        bits.append(who)
    return " · ".join(b for b in bits if b)


def link(path: str) -> str:
    return f"{settings.PUBLIC_URL}{path}" if settings.PUBLIC_URL else ""


# ------------------------------------------------------------------ senders
def _send_email_sync(to: list, subject: str, text: str) -> None:
    msg = EmailMessage()
    msg["From"] = settings.SMTP_FROM or settings.SMTP_USER
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg.set_content(text)
    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=30) as smtp:
        smtp.starttls()
        if settings.SMTP_USER:
            smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        refused = smtp.send_message(msg)
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)


async def send_email(to: list, subject: str, text: str) -> bool:
    to = [t.strip() for t in to if t and t.strip()]
    if not (to and settings.SMTP_HOST):
        return False
    try:
        await asyncio.to_thread(_send_email_sync, to, subject, text)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("email delivery failed (%s)", type(e).__name__)
        return False


async def send_telegram(text: str, photo_path: Optional[str] = None, chat_id: Optional[str] = None) -> bool:
    chat = chat_id or settings.TELEGRAM_CHAT_ID
    if not (settings.TELEGRAM_BOT_TOKEN and chat):
        return False
    base = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}"
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            if photo_path:
                with open(photo_path, "rb") as f:
                    r = await c.post(f"{base}/sendPhoto", data={"chat_id": chat, "caption": text[:1000], "parse_mode": "HTML"},
                                     files={"photo": ("watch.jpg", f, "image/jpeg")})
            else:
                r = await c.post(f"{base}/sendMessage", json={"chat_id": chat, "text": text[:4000], "parse_mode": "HTML",
                                                              "disable_web_page_preview": True})
            r.raise_for_status()
        return True
    except httpx.HTTPStatusError as e:
        # Never log the request/response: Telegram URLs contain the bot token.
        log.warning("telegram delivery failed (HTTP %s)", e.response.status_code)
        return False
    except Exception as e:  # noqa: BLE001
        # Client exception strings can include the full bot URL and credential.
        log.warning("telegram delivery failed (%s)", type(e).__name__)
        return False


def channels_configured() -> dict:
    return {"email": bool(settings.SMTP_HOST), "telegram": bool(settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID)}


# ------------------------------------------------------------------ new offers -> alerts + stock matches
def _outbox_key(identity: str) -> str:
    # KV.key is VARCHAR(60); a fixed digest also keeps dealer data out of keys.
    return OUTBOX_PREFIX + hashlib.sha256(identity.encode()).hexdigest()[:48]


def _now_text(value: Optional[datetime] = None) -> str:
    return (value or utcnow()).isoformat()


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=None)


def _enqueue(s, identity: str, item: dict) -> bool:
    key = _outbox_key(identity)
    if s.get(KV, key) is not None:
        return False
    now = utcnow()
    item.update({
        "delivery_id": hashlib.sha256(identity.encode()).hexdigest(),
        "status": "pending",
        "created_at": _now_text(now),
        "next_attempt_at": _now_text(now),
        "attempts": 0,
    })
    s.add(KV(key=key, value=json.dumps(item, separators=(",", ":"))))
    return True


def _prune_outbox(s, now: datetime) -> None:
    cutoff = now - OUTBOX_MARKER_RETENTION
    for row in s.scalars(select(KV).where(KV.key.startswith(OUTBOX_PREFIX))).all():
        try:
            item = json.loads(row.value)
            created = _parse_time(item["created_at"])
        except (ValueError, KeyError, TypeError):
            # Invalid state must not poison every later worker iteration.
            s.delete(row)
            continue
        if item.get("status") != "pending" and created < cutoff:
            s.delete(row)


def _format_notification(title: str, offers: list, photo: Optional[str]) -> tuple:
    line = offer_line(offers[0])
    url = link("/")
    text = f"{title}\n\n{line}" + (f"\n\n{url}" if url else "")
    tg_text = f"<b>{html.escape(title)}</b>\n\n{html.escape(line)}"
    if url:
        tg_text += f'\n\n<a href="{html.escape(url, quote=True)}">Open the feed</a>'
    return text, tg_text, photo


def _queue_offer_notifications(s, identity: str, title: str, offers: list, *,
                               emails: list, telegram: bool, photo: Optional[str],
                               event_kind: str, event_id: int, offer_id: int) -> int:
    if not offers:
        return 0
    text, tg_text, photo = _format_notification(title, offers, photo)
    created = 0
    if telegram and settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID:
        created += _enqueue(s, identity + ":telegram", {
            "kind": "telegram",
            "telegram_text": tg_text,
            "photo_path": photo,
            "event_kind": event_kind,
            "event_id": event_id,
            "offer_id": offer_id,
        })
    email_to = [address.strip() for address in emails if address and address.strip()]
    if settings.SMTP_HOST:
        for address in email_to:
            created += _enqueue(s, identity + ":email:" + address.lower(), {
                "kind": "email",
                "emails": [address],
                "subject": title,
                "text": text,
                "event_kind": event_kind,
                "event_id": event_id,
                "offer_id": offer_id,
            })
    return created


def _record_delivery(s, item: dict) -> None:
    kind = item.get("event_kind")
    offer_id = item.get("offer_id")
    if not offer_id or kind not in ("alert", "stock"):
        return
    alert_id = int(item["event_id"]) if kind == "alert" else -int(item["event_id"])
    existing = s.scalar(select(AlertHit.id).where(
        AlertHit.alert_id == alert_id, AlertHit.offer_id == offer_id).limit(1))
    if existing is not None:
        return
    s.add(AlertHit(alert_id=alert_id, offer_id=offer_id))
    if kind == "alert":
        alert = s.get(Alert, alert_id)
        if alert is not None:
            alert.hit_count += 1
            alert.last_hit_at = utcnow()


async def _deliver(item: dict) -> bool:
    if item.get("kind") == "telegram":
        return await send_telegram(item.get("telegram_text", ""), item.get("photo_path"))
    if item.get("kind") == "email":
        return await send_email(item.get("emails") or [], item.get("subject", ""), item.get("text", ""))
    log.error("discarding notification with unknown delivery channel")
    return False


def _claim_outbox(key: str, raw_value: str, item: dict, now: datetime) -> Optional[dict]:
    """Atomically reserve one due row so overlapping workers cannot send it twice."""
    claim_id = secrets.token_hex(16)
    item["attempts"] = int(item.get("attempts", 0)) + 1
    delay = min(3600, 30 * (2 ** (item["attempts"] - 1)))
    item["next_attempt_at"] = _now_text(now + timedelta(seconds=delay))
    item["lease_id"] = claim_id
    item["lease_until"] = _now_text(now + OUTBOX_LEASE)
    serialized = json.dumps(item, separators=(",", ":"))
    with SessionLocal() as s:
        result = s.execute(
            update(KV).where(KV.key == key, KV.value == raw_value).values(value=serialized)
        )
        s.commit()
        if result.rowcount != 1:
            return None
    return item


def _settle_outbox_claim(key: str, claim_id: str, item: dict, delivered: bool, now: datetime) -> bool:
    """Persist an acknowledgement/retry only while this process still owns the lease."""
    with SessionLocal() as s:
        row = s.get(KV, key)
        if row is None:
            return False
        try:
            current = json.loads(row.value)
        except ValueError:
            s.delete(row)
            s.commit()
            return False
        if current.get("lease_id") != claim_id:
            return False
        current.pop("lease_id", None)
        current.pop("lease_until", None)
        if delivered:
            current["status"] = "sent"
            current["sent_at"] = _now_text(now)
            # Keep only the deduplication marker, not message content or recipient data.
            current["telegram_text"] = current["text"] = current["photo_path"] = ""
            current["emails"] = []
            _record_delivery(s, current)
        elif int(current.get("attempts", 0)) >= MAX_DELIVERY_ATTEMPTS or now - _parse_time(current["created_at"]) >= OFFER_RECOVERY_WINDOW:
            current["status"] = "failed"
            current["telegram_text"] = current["text"] = current["photo_path"] = ""
            current["emails"] = []
        row.value = json.dumps(current, separators=(",", ":"))
        s.commit()
        return delivered


def _expire_outbox_row(key: str, raw_value: str, item: dict) -> None:
    item["status"] = "failed"
    item.pop("lease_id", None)
    item.pop("lease_until", None)
    item["telegram_text"] = item["text"] = item["photo_path"] = ""
    item["emails"] = []
    serialized = json.dumps(item, separators=(",", ":"))
    with SessionLocal() as s:
        s.execute(update(KV).where(KV.key == key, KV.value == raw_value).values(value=serialized))
        s.commit()


async def dispatch_outbox(limit: int = 100) -> int:
    """Send due outbox jobs; persist each attempt/result so restarts can retry safely."""
    now = utcnow()
    with SessionLocal() as s:
        _prune_outbox(s, now)
        s.commit()
        rows = list(s.execute(select(KV.key, KV.value).where(KV.key.startswith(OUTBOX_PREFIX))).all())

    sent = 0
    due_rows = []
    for key, raw_value in rows:
        try:
            item = json.loads(raw_value)
            created = _parse_time(item["created_at"])
            due = _parse_time(item["next_attempt_at"])
            lease_until = _parse_time(item["lease_until"]) if item.get("lease_until") else None
        except (ValueError, KeyError, TypeError):
            # Delete malformed rows conditionally; don't let stale data starve the queue.
            with SessionLocal() as s:
                s.execute(delete(KV).where(KV.key == key, KV.value == raw_value))
                s.commit()
            continue
        if item.get("status") != "pending":
            continue
        if lease_until and lease_until > now:
            continue
        if now - created >= OFFER_RECOVERY_WINDOW or int(item.get("attempts", 0)) >= MAX_DELIVERY_ATTEMPTS:
            _expire_outbox_row(key, raw_value, item)
            continue
        if due <= now:
            due_rows.append((key, raw_value, item))

    # Apply the limit after filtering complete, leased, future and expired rows.
    # Old sent markers therefore cannot consume the per-pass delivery budget.
    for key, raw_value, item in due_rows[:max(0, limit)]:
        claimed = _claim_outbox(key, raw_value, item, now)
        if claimed is None:
            continue
        try:
            delivered = await _deliver(claimed)
        except Exception:  # noqa: BLE001 - leave a durable retry after unexpected sender errors
            log.exception("notification delivery raised unexpectedly")
            delivered = False

        if _settle_outbox_claim(key, claimed["lease_id"], claimed, delivered, utcnow()) and delivered:
            sent += 1
    return sent


def _photo_path(s, photo_id: Optional[int]) -> Optional[str]:
    if not photo_id:
        return None
    p = s.get(Photo, photo_id)
    return os.path.join(settings.MEDIA_DIR, p.thumb) if p else None


async def process_new_offers(offer_ids: list) -> int:
    """Queue per-channel notifications durably, then deliver due jobs."""
    if not offer_ids:
        return await dispatch_outbox()
    now = utcnow()
    with SessionLocal() as s:
        _prune_outbox(s, now)
        blocked = set(s.scalars(select(Dealer.key).where(Dealer.rating == "BLOCKED")).all())
        rows = s.scalars(select(Offer).where(
            Offer.id.in_(offer_ids),
            Offer.first_seen_at >= now - OFFER_RECOVERY_WINDOW,
            Offer.archived.is_(False),
        )).all()
        offers = {o.id: offer_dict(o) for o in rows if o.dealer_key not in blocked}
        alert_rows = s.scalars(select(Alert).where(Alert.active.is_(True))).all()
        sent_alert_hits = set()
        if offers and alert_rows:
            sent_alert_hits = set(s.execute(select(AlertHit.alert_id, AlertHit.offer_id).where(
                AlertHit.alert_id.in_([a.id for a in alert_rows]),
                AlertHit.offer_id.in_(list(offers)),
            )).all())
        for a in alert_rows:
            try:
                q = json.loads(a.query_json)
            except (ValueError, TypeError):
                continue
            for offer_id, offer in offers.items():
                if (a.id, offer_id) in sent_alert_hits or not offer_matches(offer, q):
                    continue
                title = f"🔔 {a.name}"
                photo = _photo_path(s, offer.get("photo_id"))
                _queue_offer_notifications(
                    s, f"alert:{a.id}:{offer_id}", title, [offer],
                    emails=a.emails.split(","), telegram=a.telegram, photo=photo,
                    event_kind="alert", event_id=a.id, offer_id=offer_id,
                )

        # WTB requests for watches we hold. Negative alert_id values reserve
        # AlertHit as a sent marker without adding a schema change.
        stocks = s.scalars(select(StockItem).where(StockItem.status == "ACTIVE")).all()
        for stock in stocks:
            for offer_id, offer in offers.items():
                if offer["direction"] != "WTB" or offer.get("reference_norm") != stock.reference_norm:
                    continue
                stock_marker = -stock.id
                if (stock_marker, offer_id) in sent_alert_hits:
                    continue
                title = f"📦 Dealer wants a watch in our stock ({stock.reference})"
                _queue_offer_notifications(
                    s, f"stock:{stock.id}:{offer_id}", title, [offer],
                    emails=settings.DIGEST_EMAILS.split(","), telegram=True, photo=None,
                    event_kind="stock", event_id=stock.id, offer_id=offer_id,
                )
        s.commit()
    return await dispatch_outbox()


async def retry_recent_offers(limit: int = 500) -> int:
    """Recover the offer-commit/alert-enqueue crash window without replaying old posts."""
    cutoff = utcnow() - OFFER_RECOVERY_WINDOW
    with SessionLocal() as s:
        ids = list(s.scalars(select(Offer.id).where(
            Offer.first_seen_at >= cutoff,
            Offer.archived.is_(False),
        ).order_by(Offer.first_seen_at.desc()).limit(limit)).all())
    return await process_new_offers(ids) if ids else await dispatch_outbox()


# ------------------------------------------------------------------ morning summary
def build_digest(s, now: datetime) -> dict:
    day, week = now - timedelta(days=1), now - timedelta(days=7)
    blocked = set(s.scalars(select(Dealer.key).where(Dealer.rating == "BLOCKED")).all())
    recent = [o for o in s.scalars(select(Offer).where(Offer.first_seen_at >= day, Offer.archived.is_(False))).all()
              if o.dealer_key not in blocked]
    deals = sorted([o for o in recent if market.deal_label(o.market_pct, settings.DEAL_THRESHOLD_PCT) == "DEAL"],
                   key=lambda o: o.market_pct)[:10]
    wanted = s.execute(select(Offer.reference, func.count(func.distinct(Offer.dealer_key)).label("n"))
                       .where(Offer.direction == "WTB", Offer.last_seen_at >= week, Offer.reference.is_not(None))
                       .group_by(Offer.reference).order_by(func.count(func.distinct(Offer.dealer_key)).desc())
                       .limit(10)).all()
    rows = s.execute(select(Offer.reference_norm, Offer.reference, Offer.brand, Offer.family, Offer.last_seen_at,
                            Offer.price_base, Offer.condition, Offer.set_type)
                     .where(Offer.direction == "WTS", Offer.last_seen_at >= now - timedelta(days=28),
                            Offer.price_base.is_not(None))).all()
    mv = market.movers([(r[0], r[1], r[2], r[3], r[4], market.normalize(r[5], r[6], r[7])) for r in rows], now)
    stock_refs = set(s.scalars(select(StockItem.reference_norm).where(StockItem.status == "ACTIVE")).all())
    stock_wtb = [o for o in recent if o.direction == "WTB" and o.reference_norm in stock_refs]
    return {"new_offers": len(recent), "wts": sum(o.direction == "WTS" for o in recent),
            "wtb": sum(o.direction == "WTB" for o in recent),
            "deals": [offer_dict(o) for o in deals], "wanted": [(r, n) for r, n in wanted],
            "movers": mv, "stock_wtb": [offer_dict(o) for o in stock_wtb]}


def render_digest(d: dict, date_str: str) -> str:
    out = [f"Watch market summary — {date_str}",
           f"{d['new_offers']} new offers in 24h ({d['wts']} for sale, {d['wtb']} wanted)", ""]
    if d["deals"]:
        out.append("BEST DEALS (priced below other dealers)")
        out += [f"• {offer_line(o)}" for o in d["deals"]] + [""]
    if d["stock_wtb"]:
        out.append("DEALERS WANTING WATCHES WE HOLD")
        out += [f"• {offer_line(o)}" for o in d["stock_wtb"]] + [""]
    if d["wanted"]:
        out.append("MOST WANTED THIS WEEK (dealers asking to buy)")
        out += [f"• {ref}: {n} dealers" for ref, n in d["wanted"]] + [""]
    if d["movers"]:
        out.append("PRICE MOVES (last 7 days vs previous 3 weeks)")
        out += [f"• {m['reference']} {m['family'] or ''}: {m['change_pct']:+.1f}% "
                f"(typical {money(m['now'], settings.BASE_CURRENCY)})" for m in d["movers"]] + [""]
    url = link("/")
    if url:
        out.append(f"Open the feed: {url}")
    return "\n".join(out)


async def maybe_send_digest(now_utc: Optional[datetime] = None) -> bool:
    tz = ZoneInfo(settings.TIMEZONE)
    local = (now_utc or datetime.utcnow()).replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
    if local.hour < settings.DIGEST_HOUR:
        return False
    today = local.date().isoformat()
    identities = []
    with SessionLocal() as s:
        # A legacy digest_last value was written before delivery in v1. Treat
        # today's value as a completed legacy send to avoid duplicate upgrades.
        legacy = s.get(KV, "digest_last")
        if legacy and legacy.value == today:
            return False
        d = build_digest(s, utcnow())
        text = render_digest(d, local.strftime("%a %d %b"))
        if settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID:
            identity = f"digest:{today}:telegram"
            key = _outbox_key(identity)
            created = _enqueue(s, identity, {
                "kind": "telegram",
                "telegram_text": html.escape(text),
            })
            if created:
                identities.append(key)
            marker = s.get(KV, key)
            if marker is not None:
                try:
                    queued = json.loads(marker.value)
                    if (not created and queued.get("status") == "pending"
                            and _parse_time(queued["next_attempt_at"]) <= utcnow()):
                        identities.append(key)
                except (ValueError, KeyError, TypeError):
                    pass
        if settings.DIGEST_EMAILS.strip() and settings.SMTP_HOST:
            for address in (part.strip() for part in settings.DIGEST_EMAILS.split(",")):
                if not address:
                    continue
                identity = f"digest:{today}:email:{address.lower()}"
                key = _outbox_key(identity)
                created = _enqueue(s, identity, {
                    "kind": "email",
                    "emails": [address],
                    "subject": f"Watch market summary {local:%d %b}",
                    "text": text,
                })
                if created:
                    identities.append(key)
                marker = s.get(KV, key)
                if marker is not None:
                    try:
                        queued = json.loads(marker.value)
                        if (not created and queued.get("status") == "pending"
                                and _parse_time(queued["next_attempt_at"]) <= utcnow()):
                            identities.append(key)
                    except (ValueError, KeyError, TypeError):
                        pass
        s.commit()

    await dispatch_outbox()
    if not identities:
        return False
    with SessionLocal() as s:
        for key in identities:
            row = s.get(KV, key)
            if row is None:
                continue
            try:
                item = json.loads(row.value)
                if item.get("status") == "sent":
                    return True
            except (ValueError, KeyError, TypeError):
                continue
    return False
