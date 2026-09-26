"""Background loop: parse NEW messages into offers."""
import asyncio
import hashlib
import json
import logging
import re
from datetime import timedelta
from typing import Optional

import httpx
from sqlalchemy import select

from . import fx
from .config import settings
from .db import Group, Message, Offer, ParseCache, SessionLocal, utcnow
from .parser import ParseError, extract_offers, looks_like_offer

log = logging.getLogger("worker")

CONDITIONS = {"NEW", "LIKE_NEW", "USED", "VINTAGE"}
SET_TYPES = {"FULL_SET", "WATCH_ONLY", "BOX_ONLY", "PAPERS_ONLY"}


def norm_ref(ref: Optional[str]) -> Optional[str]:
    if not ref:
        return None
    r = re.sub(r"[^A-Z0-9]", "", ref.upper())
    return r or None


def _s(v, n=200) -> Optional[str]:
    if v is None:
        return None
    v = str(v).strip()
    return v[:n] or None


def clean_offer(o: dict) -> Optional[dict]:
    if not isinstance(o, dict):
        return None
    direction = str(o.get("direction") or "WTS").upper()
    if direction not in ("WTS", "WTB"):
        direction = "WTS"
    brand, ref = _s(o.get("brand"), 80), _s(o.get("reference"), 80)
    family, model = _s(o.get("family"), 120), _s(o.get("model"), 200)
    if not (brand or ref or family or model):
        return None
    year = o.get("year")
    try:
        year = int(year) if year is not None else None
        if year is not None and year < 100:
            year += 2000
        if year is not None and not (1900 <= year <= utcnow().year + 1):
            year = None
    except (TypeError, ValueError):
        year = None
    month = o.get("month")
    try:
        month = int(month) if month is not None and 1 <= int(month) <= 12 else None
    except (TypeError, ValueError):
        month = None
    try:
        price = float(o["price"]) if o.get("price") not in (None, "") else None
        if price is not None and price <= 0:
            price = None
    except (TypeError, ValueError):
        price = None
    currency = fx.normalize_currency(o.get("currency")) if price is not None else None
    try:
        disc = float(o["discount_pct"]) if o.get("discount_pct") is not None else None
    except (TypeError, ValueError):
        disc = None
    cond = str(o.get("condition") or "").upper() or None
    st = str(o.get("set_type") or "").upper() or None
    country = str(o.get("country") or "").upper()[:2] or None
    return {
        "direction": direction, "brand": brand, "family": family, "model": model,
        "reference": ref, "reference_norm": norm_ref(ref),
        "dial_color": _s(o.get("dial_color"), 80), "case_material": _s(o.get("case_material"), 80),
        "bracelet": _s(o.get("bracelet"), 80), "diamond_indices": bool(o.get("diamond_indices")),
        "condition": cond if cond in CONDITIONS else None,
        "set_type": st if st in SET_TYPES else None,
        "year": year, "month": month, "price": price, "currency": currency,
        "price_base": fx.to_base(price, currency), "discount_pct": disc,
        "country": country if country and country.isalpha() else None,
        "city": _s(o.get("city"), 80), "notes": _s(o.get("notes"), 500),
        "source_text": (_s(o.get("source_text"), 2000) or ""),
    }


def fingerprint(o: dict, dealer: str) -> str:
    parts = [o["direction"], o["reference_norm"] or (o["brand"] or "") + (o["model"] or ""),
             o["dial_color"] or "", str(o["price"] or ""), o["currency"] or "",
             o["condition"] or "", o["set_type"] or "", str(o["year"] or ""), dealer]
    return hashlib.sha1("|".join(p.lower() for p in parts).encode()).hexdigest()


def save_offers(s, msg: Message, raw_offers: list) -> int:
    dealer_key = msg.sender_phone or (msg.sender_name or "").lower()
    window = msg.ts - timedelta(days=settings.DUPLICATE_WINDOW_DAYS)
    count = 0
    for raw in raw_offers:
        o = clean_offer(raw)
        if not o:
            continue
        fp = fingerprint(o, dealer_key)
        existing = s.scalar(select(Offer).where(Offer.fingerprint == fp, Offer.last_seen_at >= window)
                            .order_by(Offer.last_seen_at.desc()).limit(1))
        if existing:
            existing.duplicate_count += 1
            if msg.ts > existing.last_seen_at:
                existing.last_seen_at = msg.ts
            if msg.ts < existing.first_seen_at:
                existing.first_seen_at = msg.ts
            seen = set(filter(None, existing.groups_seen.split(",")))
            seen.add(msg.group_id)
            existing.groups_seen = ",".join(sorted(seen))
        else:
            s.add(Offer(message_id=msg.id, group_id=msg.group_id, groups_seen=msg.group_id,
                        dealer_name=msg.sender_name, dealer_phone=msg.sender_phone,
                        fingerprint=fp, first_seen_at=msg.ts, last_seen_at=msg.ts, **o))
        count += 1
    return count


async def _parse(msg: Message, group_name: str, client: httpx.AsyncClient, sem: asyncio.Semaphore):
    if not looks_like_offer(msg.text):
        return "SKIPPED", None
    with SessionLocal() as s:
        cached = s.get(ParseCache, msg.text_hash)
    if cached:
        return "OK", json.loads(cached.result_json)
    async with sem:
        try:
            offers = await extract_offers(msg.text, group_name, client)
        except ParseError as e:
            return "ERROR", str(e)
        except Exception as e:  # noqa: BLE001
            log.exception("parse failed")
            return "ERROR", repr(e)
    with SessionLocal() as s:
        if not s.get(ParseCache, msg.text_hash):
            s.add(ParseCache(text_hash=msg.text_hash, result_json=json.dumps(offers)))
            s.commit()
    return "OK", offers


async def process_batch(limit: int = 25) -> tuple:
    """Returns (processed, errors)."""
    with SessionLocal() as s:
        msgs = s.scalars(select(Message).where(Message.status == "NEW")
                         .order_by(Message.ts).limit(limit)).all()
        names = {g.id: g.name for g in s.scalars(select(Group)).all()}
    if not msgs:
        return 0, 0
    sem = asyncio.Semaphore(settings.LLM_CONCURRENCY)
    unique = {}  # same text pasted into several groups -> one AI call
    for m in msgs:
        unique.setdefault(m.text_hash, m)
    async with httpx.AsyncClient(timeout=180) as client:
        parsed = await asyncio.gather(*[_parse(m, names.get(m.group_id, ""), client, sem) for m in unique.values()])
    by_hash = dict(zip(unique.keys(), parsed))
    results = [by_hash[m.text_hash] for m in msgs]
    errors = 0
    with SessionLocal() as s:
        for m, (status, data) in zip(msgs, results):
            msg = s.get(Message, m.id)
            if status == "SKIPPED":
                msg.status = "SKIPPED"
            elif status == "ERROR":
                errors += 1
                msg.attempts += 1
                msg.error = (data or "")[:1000]
                if msg.attempts >= 3:
                    msg.status = "ERROR"
            else:
                n = save_offers(s, msg, data or [])
                msg.offers_count = n
                msg.status = "PARSED" if n else "NO_OFFERS"
                msg.error = None
            s.commit()
    return len(msgs), errors


async def run_forever() -> None:
    log.info("worker started")
    last_fx = None
    while True:
        try:
            if last_fx is None or utcnow() - last_fx > timedelta(hours=12):
                await fx.refresh()
                last_fx = utcnow()
            n, errors = await process_batch()
            if errors:
                await asyncio.sleep(30)
            elif n == 0:
                await asyncio.sleep(3)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("worker loop error")
            await asyncio.sleep(10)
