"""Background loop: parse NEW messages into offers."""
import asyncio
import hashlib
import json
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Optional

import httpx
from sqlalchemy import inspect, select, text

from . import alerts, cost, fx, market, media, refs
from .ai_usage import UsageMeterError
from .config import settings
from .db import AIUsage, Group, LineCache, Message, Offer, ParseCache, SessionLocal, utcnow
from .parser import InvalidBatch, OversizedBatch, ParseError, extract_offers_batch, looks_like_offer
from .worker_utils import norm_ref  # noqa: F401  (re-exported for existing callers)

log = logging.getLogger("worker")

CONDITIONS = {"NEW", "LIKE_NEW", "USED", "VINTAGE"}
SET_TYPES = {"FULL_SET", "WATCH_ONLY", "BOX_ONLY", "PAPERS_ONLY"}


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
    known_brand, known_family = refs.lookup(norm_ref(ref), brand)
    if known_family:
        brand, family = known_brand, known_family
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


def compute_market_pct(s, o: dict, dealer_key: str, ts) -> Optional[float]:
    """Compare a priced WTS against recent asks by other dealers for this reference."""
    if o["direction"] != "WTS" or not o["price_base"] or not o["reference_norm"]:
        return None
    rows = s.execute(select(Offer.price_base, Offer.condition, Offer.set_type).where(
        Offer.reference_norm == o["reference_norm"], Offer.direction == "WTS", Offer.price_base.is_not(None),
        Offer.last_seen_at >= ts - timedelta(days=30), Offer.dealer_key != dealer_key)).all()
    comps = [market.normalize(price, condition, set_type) for price, condition, set_type in rows]
    return market.market_pct(market.normalize(o["price_base"], o["condition"], o["set_type"]), comps)


def save_offers(s, msg: Message, raw_offers: list) -> tuple:
    """Save parsed offers; return (offers found, newly created rows)."""
    dealer_key = media.sender_key(msg.sender_phone, msg.sender_name) or ""
    window = msg.ts - timedelta(days=settings.DUPLICATE_WINDOW_DAYS)
    count, new, touched = 0, [], []
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
            touched.append(existing)
        else:
            row = Offer(message_id=msg.id, group_id=msg.group_id, groups_seen=msg.group_id,
                        dealer_name=msg.sender_name, dealer_phone=msg.sender_phone, dealer_key=dealer_key or None,
                        market_pct=compute_market_pct(s, o, dealer_key, msg.ts),
                        fingerprint=fp, first_seen_at=msg.ts, last_seen_at=msg.ts, **o)
            s.add(row)
            new.append(row)
            touched.append(row)
        count += 1
    if touched:
        s.flush()
        media.link_for_message(s, msg, touched)
    return count, new


def _chunks(candidates: list[Message]) -> list[list[Message]]:
    """Bound both the message count and the prompt text in each paid request."""
    chunks, current, chars = [], [], 0
    for msg in candidates:
        size = min(len(msg.text), settings.MAX_MESSAGE_CHARS) + 150
        if current and (len(current) >= settings.AI_BATCH_SIZE or chars + size > 16000):
            chunks.append(current)
            current, chars = [], 0
        current.append(msg)
        chars += size
    if current:
        chunks.append(current)
    return chunks


def spend(s) -> dict:
    """Recorded estimates in USD, including retained daily usage from a v5 ZIP."""
    now = utcnow()
    day = datetime(now.year, now.month, now.day)
    month = datetime(now.year, now.month, 1)
    rows = s.scalars(select(AIUsage).where(AIUsage.created_at >= month)).all()
    today = sum((r.estimated_usd or Decimal(0) for r in rows if r.created_at >= day), Decimal(0))
    monthly = sum((r.estimated_usd or Decimal(0) for r in rows), Decimal(0))
    unknown = sum(r.estimated_usd is None for r in rows)
    calls_today = sum(r.created_at >= day for r in rows)
    if inspect(s.bind).has_table("ai_usage_legacy_daily"):
        legacy = s.execute(text("SELECT day, tokens_in, tokens_out, calls FROM ai_usage_legacy_daily "
                                "WHERE day >= :month"), {"month": month.strftime("%Y-%m-%d")}).all()
        for date, tin, tout, calls in legacy:
            amount = Decimal(tin or 0) / Decimal(1000000) + Decimal(tout or 0) * 5 / Decimal(1000000)
            monthly += amount
            if date == day.strftime("%Y-%m-%d"):
                today += amount
                calls_today += calls or 0
    return {"today": float(today), "month": float(monthly), "calls_today": calls_today,
            "unpriced_month": unknown}


def budget_status(s) -> dict:
    usage = spend(s)
    daily = settings.AI_DAILY_BUDGET_USD or settings.AI_MONTHLY_BUDGET_USD / 30
    usage.update(daily_budget=daily, monthly_budget=settings.AI_MONTHLY_BUDGET_USD,
                 remaining=min(daily - usage["today"], settings.AI_MONTHLY_BUDGET_USD - usage["month"]))
    # An unknown price cannot safely be treated as a zero-dollar charge.
    usage["blocked"] = usage["remaining"] <= 0 or usage["unpriced_month"] > 0
    usage["interval_minutes"] = settings.AI_INTERVAL_MINUTES
    usage["max_age_hours"] = settings.AI_MAX_AGE_HOURS
    usage["auto_pause_after"] = settings.AUTO_PAUSE_AFTER
    usage["next_run"] = (STATE["next_ai_run"].isoformat() + "Z"
                         if STATE["next_ai_run"] else None)
    return usage


async def _parse_chunk(chunk: list[Message], names: dict, group_ids: dict,
                       client: httpx.AsyncClient, sem: asyncio.Semaphore) -> dict:
    with SessionLocal() as s:
        if budget_status(s)["blocked"]:
            return {msg.text_hash: ("BUDGET", None) for msg in chunk}
    items = [{"id": str(msg.id), "group": names.get(msg.group_id, ""),
              "text": msg.text} for msg in chunk]
    weights = {}
    for msg in chunk:
        groups = group_ids[msg.text_hash]
        length = max(1, min(len(msg.text), settings.MAX_MESSAGE_CHARS))
        for group in groups:
            weights[group] = weights.get(group, 0) + max(1, length // len(groups))
    try:
        async with sem:
            parsed = await extract_offers_batch(items, client, group_weights=weights)
        return {msg.text_hash: ("OK", parsed[str(msg.id)]) for msg in chunk}
    except OversizedBatch as exc:
        if len(chunk) == 1:
            return {chunk[0].text_hash: ("ERROR", str(exc))}
        log.warning("splitting %d-message AI batch after %s", len(chunk), type(exc).__name__)
        mid = len(chunk) // 2
        halves = []
        for part in (chunk[:mid], chunk[mid:]):
            halves.append(await _parse_chunk(part, names, group_ids, client, sem))
        return {key: val for half in halves for key, val in half.items()}
    except UsageMeterError:
        raise  # Never continue paid parsing when metering failed.
    except ParseError as exc:
        transient = any(x in str(exc) for x in ("HTTP 429", "HTTP 5", "transport", "unavailable"))
        return {msg.text_hash: ("ERROR", str(exc), transient) for msg in chunk}
    except Exception as exc:  # noqa: BLE001
        log.exception("batch parsing failed")
        return {msg.text_hash: ("ERROR", repr(exc)) for msg in chunk}


async def process_batch(limit: int = 25) -> tuple:
    """Returns (processed, errors)."""
    with SessionLocal() as s:
        msgs = s.scalars(select(Message).where(Message.status == "NEW")
                         .order_by(Message.ts.desc(), Message.id.desc()).limit(limit)).all()
        groups = {g.id: g for g in s.scalars(select(Group)).all()}
    if not msgs:
        return 0, 0
    names = {key: group.name for key, group in groups.items()}
    sem = asyncio.Semaphore(settings.LLM_CONCURRENCY)
    unique = {}  # same text pasted into several active groups -> one AI call
    group_ids = {}
    for m in msgs:
        if groups.get(m.group_id) and groups[m.group_id].ai_paused:
            continue
        unique.setdefault(m.text_hash, m)
        group_ids.setdefault(m.text_hash, set()).add(m.group_id)
    by_hash, work = {}, {}
    oldest = utcnow() - timedelta(hours=settings.AI_MAX_AGE_HOURS)
    with SessionLocal() as s:
        for msg in unique.values():
            if not looks_like_offer(msg.text):
                by_hash[msg.text_hash] = ("SKIPPED", None)
                continue
            cached = s.get(ParseCache, msg.text_hash)
            if cached:
                by_hash[msg.text_hash] = ("OK", json.loads(cached.result_json))
                continue
            p = cost.plan(msg.text[:settings.MAX_MESSAGE_CHARS])
            hits = ({r.key: json.loads(r.offers_json) for r in s.scalars(
                select(LineCache).where(LineCache.key.in_(list(p.keys.values())))).all()}
                if p.keys else {})
            have = {i: hits[key] for i, key in p.keys.items() if key in hits}
            if p.cand and len(have) == len(p.cand):
                by_hash[msg.text_hash] = ("OK", [o for i in p.cand for o in have[i]])
            elif msg.ts < oldest:
                by_hash[msg.text_hash] = ("OLD", None)
            else:
                work[msg.text_hash] = (msg, p, have)
    pieces, piece_info, piece_groups = [], {}, {}
    for h, (msg, p, have) in work.items():
        todo = [i for i in p.cand if i not in have]
        items = cost.make_items(p, todo, settings.AI_MAX_LINES_PER_CALL) if todo else [
            cost.Item(msg.text[:settings.MAX_MESSAGE_CHARS], [], 1)]
        for n, item in enumerate(items):
            key = f"{h}:{n}"
            piece = SimpleNamespace(id=f"{msg.id}-{n}", group_id=msg.group_id,
                                    text=item.text, text_hash=key)
            pieces.append((piece, item))
            piece_info[key] = (h, p, item)
            piece_groups[key] = group_ids[h]
    answers = {}
    chunks = cost.batches(pieces, settings.AI_MAX_LINES_PER_CALL, settings.AI_BATCH_SIZE)
    if chunks:
        async with httpx.AsyncClient(timeout=180) as client:
            for chunk in chunks:
                answers.update(await _parse_chunk(
                    [piece for piece, _ in chunk], names, client=client,
                    group_ids=piece_groups, sem=sem,
                ))
    if work:
        try:
            with SessionLocal() as s:
                for h, (msg, p, have) in work.items():
                    mine = [(piece.text_hash, item) for piece, item in pieces
                            if piece_info[piece.text_hash][0] == h]
                    outcomes = [answers.get(key, ("BUDGET", None)) for key, _ in mine]
                    per_line, loose = dict(have), []
                    for (key, item), outcome in zip(mine, outcomes):
                        if outcome[0] != "OK":
                            continue
                        mapping, extra, safe = cost.assign(item, outcome[1])
                        if safe:
                            for index, offers in mapping.items():
                                per_line[index] = offers
                                if not s.get(LineCache, p.keys[index]):
                                    s.add(LineCache(key=p.keys[index], offers_json=json.dumps(offers)))
                        else:
                            # Ambiguous source-line mapping must not poison the line cache.
                            loose += [o for offers in mapping.values() for o in offers]
                        loose += extra
                    if any(outcome[0] == "BUDGET" for outcome in outcomes):
                        by_hash[h] = ("BUDGET", None)
                    elif any(outcome[0] == "ERROR" for outcome in outcomes):
                        error = next(outcome for outcome in outcomes if outcome[0] == "ERROR")
                        by_hash[h] = error
                    else:
                        offers = [o for i in p.cand for o in per_line.get(i, [])] + loose
                        by_hash[h] = ("OK", offers)
                        if not s.get(ParseCache, h):
                            s.add(ParseCache(text_hash=h, result_json=json.dumps(offers)))
                s.commit()
        except Exception as exc:
            raise UsageMeterError("AI results could not be cached; paid worker stopped") from exc
    errors, new_ids, checked = 0, [], set()
    with SessionLocal() as s:
        for m in msgs:
            outcome = (("PAUSED", None) if groups.get(m.group_id) and groups[m.group_id].ai_paused
                       else by_hash[m.text_hash])
            status, data = outcome[:2]
            msg, created = s.get(Message, m.id), []
            if status in ("SKIPPED", "PAUSED", "OLD", "BUDGET"):
                msg.status = status
            elif status == "ERROR":
                errors += 1
                msg.attempts += 1
                msg.error = (data or "")[:1000]
                if msg.attempts >= 3 or not (len(outcome) > 2 and outcome[2]):
                    msg.status = "ERROR"
            else:
                n, created = save_offers(s, msg, data or [])
                msg.offers_count = n
                msg.status = "PARSED" if n else "NO_OFFERS"
                msg.error = None
                if m.text_hash in work and (m.group_id, m.text_hash) not in checked:
                    checked.add((m.group_id, m.text_hash))
                    g = s.get(Group, m.group_id)
                    if g:
                        g.ai_checked = (g.ai_checked or 0) + 1
                        g.ai_hits = (g.ai_hits or 0) + (1 if n else 0)
                        if (settings.AUTO_PAUSE_AFTER and g.ai_checked >= settings.AUTO_PAUSE_AFTER
                                and not g.ai_hits):
                            g.ai_paused = True
                            log.info("AI paused for group %s after %d no-offer checks",
                                     g.name, g.ai_checked)
            s.commit()
            if status == "OK":
                new_ids += [o.id for o in created]
    if new_ids:
        try:
            await alerts.process_new_offers(new_ids)
        except Exception:  # noqa: BLE001 - alert failures must not stop parsing
            log.exception("alert processing failed")
    return len(msgs), errors


STATE = {"next_ai_run": None}


async def run_forever() -> None:
    log.info("worker started")
    last_fx = last_house = None
    last_clean = None
    last_outbox = None
    while True:
        try:
            if last_fx is None or utcnow() - last_fx > timedelta(hours=12):
                await fx.refresh()
                last_fx = utcnow()
            if last_house is None or utcnow() - last_house > timedelta(minutes=5):
                last_house = utcnow()
                try:
                    await alerts.maybe_send_digest()
                except Exception:  # noqa: BLE001 - digest failure must not stop parsing
                    log.exception("digest processing failed")
                try:
                    # Recover work committed just before a restart and retry
                    # pending deliveries from the DB-backed outbox.
                    await alerts.retry_recent_offers()
                except Exception:  # noqa: BLE001 - retries must not stop parsing
                    log.exception("notification retry failed")
                if utcnow().hour == 3 and last_clean != utcnow().date():
                    last_clean = utcnow().date()
                    try:
                        with SessionLocal() as s:
                            media.cleanup(s)
                    except Exception:  # noqa: BLE001 - cleanup failure must not stop parsing
                        log.exception("photo cleanup failed")
            if last_outbox is None or utcnow() - last_outbox >= timedelta(seconds=15):
                last_outbox = utcnow()
                try:
                    await alerts.dispatch_outbox()
                except Exception:  # noqa: BLE001 - retry failures must not stop parsing
                    log.exception("notification outbox delivery failed")
            if STATE["next_ai_run"] is not None and utcnow() < STATE["next_ai_run"]:
                await asyncio.sleep(min(5, max(0, (STATE["next_ai_run"] - utcnow()).total_seconds())))
                continue
            n, errors = await process_batch()
            if settings.AI_INTERVAL_MINUTES:
                STATE["next_ai_run"] = utcnow() + timedelta(minutes=settings.AI_INTERVAL_MINUTES)
            if errors:
                await asyncio.sleep(30)
            elif n == 0:
                await asyncio.sleep(3)
        except asyncio.CancelledError:
            raise
        except UsageMeterError:
            log.exception("AI usage or result persistence failed; stopping paid worker")
            return
        except Exception:  # noqa: BLE001
            log.exception("worker loop error")
            await asyncio.sleep(10)
