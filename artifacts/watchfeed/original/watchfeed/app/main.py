import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from sqlalchemy import func, or_, select, update

from . import advisor, extra, feed_settings, fx, ingest, market, media, pending_feed, refs, waha, worker
from .auth import is_admin as live_is_admin, staff_accounts
from .config import settings
from .db import AIUsage, AIUsageGroup, Dealer, Group, Message, Offer, SessionLocal, StockItem, init_db, utcnow
from .parser import ParseError, extract_offers

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")
STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not settings.DEMO_MODE and not (staff_accounts() and settings.ADMIN_PASSWORD and settings.WEBHOOK_SECRET):
        raise RuntimeError("Set STAFF_USERS (or STAFF_PASSWORD), ADMIN_PASSWORD and WEBHOOK_SECRET in .env")
    init_db()
    if settings.DEMO_MODE:
        from .demo import seed_demo
        seed_demo()
    task = asyncio.create_task(worker.run_forever()) if settings.WORKER_ENABLED and not settings.DEMO_MODE else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="Watch Trading Feed", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
basic = HTTPBasic(auto_error=False)


def _check(creds: HTTPBasicCredentials, user: str, pw: str) -> bool:
    return (secrets.compare_digest(creds.username.encode(), user.encode())
            and secrets.compare_digest(creds.password.encode(), pw.encode()))


def _deny():
    raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Basic"})


def is_admin(creds: Optional[HTTPBasicCredentials] = Depends(basic)):
    if not creds:
        _deny()
    return live_is_admin(creds)


def is_staff(creds: Optional[HTTPBasicCredentials] = Depends(basic)):
    if settings.DEMO_MODE:
        return "demo"
    if not creds:
        _deny()
    if _check(creds, settings.ADMIN_USER, settings.ADMIN_PASSWORD):
        return creds.username
    if any(_check(creds, username, password) for username, password in staff_accounts().items()):
        return creds.username
    _deny()


# ------------------------------------------------------------------ webhook
@app.post("/webhook/waha")
async def waha_webhook(request: Request, token: str = ""):
    if settings.DEMO_MODE:
        raise HTTPException(403, "Demo mode does not accept messages")
    if not secrets.compare_digest(token.encode(), settings.WEBHOOK_SECRET.encode()):
        raise HTTPException(403)
    body = await request.json()
    if body.get("event") not in ("message", "message.any"):
        return {"ok": True}
    m = ingest.normalize(body.get("payload") or {})
    if not m:
        return {"ok": True}
    m = await ingest.resolve_phone(m)
    return {"ok": True, **(await _ingest_one(m))}


async def _ingest_one(m: dict) -> dict:
    """Store an enabled-group message and any supported photo."""
    with SessionLocal() as s:
        if not ingest.group_enabled(s, m["group_id"]):
            return {"stored": False}
        photo = await media.save_photo(s, m) if m.get("_media") else None
        stored = ingest.store(s, m)
        if photo and not stored:
            if photo.caption.strip():
                media.link_backfilled_photo(s, photo, m["wa_id"], m["group_id"])
            else:
                media.link_late_photo(s, photo)
    return {"stored": bool(stored), "photo": bool(photo)}


@app.get("/health")
def health():
    return {"ok": True, "demo": settings.DEMO_MODE}


# ------------------------------------------------------------------ pages
@app.get("/", dependencies=[Depends(is_staff)])
def feed_page():
    return FileResponse(STATIC / "index.html")


@app.get("/advisor", dependencies=[Depends(is_staff)])
def advisor_page():
    return FileResponse(STATIC / "advisor.html")


@app.get("/alerts", dependencies=[Depends(is_staff)])
def alerts_page():
    return FileResponse(STATIC / "alerts.html")


@app.get("/dealers", dependencies=[Depends(is_staff)])
def dealers_page():
    return FileResponse(STATIC / "dealers.html")


@app.get("/stock", dependencies=[Depends(is_staff)])
def stock_page():
    return FileResponse(STATIC / "stock.html")


@app.get("/admin", dependencies=[Depends(is_admin)])
def admin_page():
    return FileResponse(STATIC / "admin.html")


# ------------------------------------------------------------------ staff API
PERIODS = {"6h": 0.25, "1d": 1, "3d": 3, "1w": 7, "2w": 14, "1m": 30, "3m": 90, "6m": 180, "all": None}
SORTS = {"last_seen": Offer.last_seen_at, "first_seen": Offer.first_seen_at, "price": Offer.price_base,
         "year": Offer.year, "seen_count": Offer.duplicate_count, "brand": Offer.brand, "deal": Offer.market_pct}


def offer_json(o: Offer, groups: dict, ctx: Optional[dict] = None,
               include_message: Optional[str] = None) -> dict:
    ctx = ctx or {}
    photo_id, photo_kind = o.photo_id, o.photo_kind
    if not photo_id and o.reference_norm in ctx.get("ref_photos", {}):
        photo_id, photo_kind = ctx["ref_photos"][o.reference_norm], "REFERENCE"
    return {
        "id": o.id, "direction": o.direction, "brand": o.brand, "family": o.family, "model": o.model,
        "reference": o.reference, "reference_norm": o.reference_norm, "dial_color": o.dial_color,
        "case_material": o.case_material, "bracelet": o.bracelet, "diamond_indices": o.diamond_indices,
        "condition": o.condition, "set_type": o.set_type, "year": o.year, "month": o.month,
        "price": o.price, "currency": o.currency, "price_base": o.price_base,
        "base_currency": settings.BASE_CURRENCY, "discount_pct": o.discount_pct,
        "country": o.country, "city": o.city, "notes": o.notes, "source_text": o.source_text,
        "dealer_name": o.dealer_name, "dealer_phone": o.dealer_phone, "dealer_key": o.dealer_key,
        "dealer_rating": ctx.get("ratings", {}).get(o.dealer_key),
        "groups": [groups.get(g, g) for g in o.groups_seen.split(",") if g],
        "seen_count": o.duplicate_count,
        "first_seen_at": o.first_seen_at.isoformat() + "Z", "last_seen_at": o.last_seen_at.isoformat() + "Z",
        "photo_id": photo_id, "photo_kind": photo_kind,
        "market_pct": o.market_pct, "deal": market.deal_label(o.market_pct, settings.DEAL_THRESHOLD_PCT),
        "in_stock": o.direction == "WTB" and o.reference_norm in ctx.get("stock", set()),
        "track": {"status": o.track_status, "by": o.track_by, "note": o.track_note,
                  "at": o.track_at.isoformat() + "Z" if o.track_at else None} if o.track_status else None,
        **({"original_message": include_message} if include_message is not None else {}),
    }


def feed_ctx(s, rows: list) -> dict:
    keys = {o.dealer_key for o in rows if o.dealer_key}
    ratings = dict(s.execute(select(Dealer.key, Dealer.rating).where(Dealer.key.in_(keys))).all()) if keys else {}
    need = {o.reference_norm for o in rows if not o.photo_id and o.reference_norm}
    stock = set(s.scalars(select(StockItem.reference_norm).where(StockItem.status == "ACTIVE")).all())
    return {"ratings": ratings, "ref_photos": media.reference_photos(s, need), "stock": stock}


@app.get("/api/offers", dependencies=[Depends(is_staff)])
def list_offers(q: str = "", direction: str = "", brand: str = "", condition: str = "", set_type: str = "",
                country: str = "", group: str = "", period: str = "2w", min_price: Optional[float] = None,
                max_price: Optional[float] = None, year_from: Optional[int] = None,
                year_to: Optional[int] = None, priced_only: bool = False, deals_only: bool = False,
                hide_handled: bool = False, show_blocked: bool = False, track: str = "",
                 sort: str = "last_seen", order: str = "desc", page: int = 1, page_size: int = 50,
                 include_messages: bool = False):
    page_size = max(1, min(page_size, 200))
    stmt = select(Offer).where(Offer.archived.is_(False))
    days = PERIODS.get(period, 14)
    if days:
        stmt = stmt.where(Offer.last_seen_at >= utcnow() - timedelta(days=days))
    if direction in ("WTS", "WTB"):
        stmt = stmt.where(Offer.direction == direction)
    if brand:
        stmt = stmt.where(Offer.brand == brand)
    if condition:
        stmt = stmt.where(Offer.condition.in_(condition.split(",")))
    if set_type:
        stmt = stmt.where(Offer.set_type.in_(set_type.split(",")))
    if country:
        stmt = stmt.where(Offer.country.in_(country.upper().split(",")))
    if group:
        stmt = stmt.where(Offer.groups_seen.contains(group))
    if min_price is not None:
        stmt = stmt.where(Offer.price_base >= min_price)
    if max_price is not None:
        stmt = stmt.where(Offer.price_base <= max_price)
    if priced_only:
        stmt = stmt.where(Offer.price_base.is_not(None))
    if year_from:
        stmt = stmt.where(Offer.year >= year_from)
    if year_to:
        stmt = stmt.where(Offer.year <= year_to)
    if deals_only:
        stmt = stmt.where(Offer.market_pct <= -settings.DEAL_THRESHOLD_PCT / 100,
                          Offer.market_pct > market.SUSPECT_PCT)
    if hide_handled:
        stmt = stmt.where(Offer.track_status.is_(None))
    if track:
        stmt = stmt.where(Offer.track_status == track.upper())
    if not show_blocked:
        blocked = select(Dealer.key).where(Dealer.rating == "BLOCKED")
        stmt = stmt.where(or_(Offer.dealer_key.is_(None), Offer.dealer_key.not_in(blocked)))
    for tok in q.split():
        like = f"%{tok}%"
        ref = worker.norm_ref(tok)
        conds = [Offer.brand.ilike(like), Offer.family.ilike(like), Offer.model.ilike(like),
                 Offer.dial_color.ilike(like), Offer.case_material.ilike(like), Offer.source_text.ilike(like),
                 Offer.dealer_name.ilike(like), Offer.dealer_phone.ilike(like)]
        if ref:
            conds.append(Offer.reference_norm.contains(ref))
        stmt = stmt.where(or_(*conds))
    col = SORTS.get(sort, Offer.last_seen_at)
    with SessionLocal() as s:
        show_messages = include_messages and not any((
            brand, condition, set_type, country, min_price is not None,
            max_price is not None, year_from is not None, year_to is not None,
            priced_only, deals_only, track,
        ))
        total, ids, offers, messages = pending_feed.page(
            s, stmt, col, sort=sort, order=order, page_number=max(1, page),
            page_size=page_size, days=days, group=group, query=q, direction=direction,
            include_messages=show_messages, show_blocked=show_blocked,
        )
        groups = {g.id: g.name for g in s.scalars(select(Group)).all()}
        ctx = feed_ctx(s, list(offers.values()))
        group_rows = {g.id: g for g in s.scalars(select(Group).where(
            Group.id.in_({m.group_id for m in messages.values()}))).all()} if messages else {}
        budget_blocked = worker.budget_status(s)["blocked"] if messages else False
        items = []
        for kind, item_id in ids:
            if kind == "offer":
                offer = offer_json(offers[item_id], groups, ctx)
                items.append({"kind": "offer", **offer} if include_messages else offer)
            else:
                msg = messages[item_id]
                items.append(pending_feed.message_preview(msg, group_rows[msg.group_id], budget_blocked))
    return {"total": total, "page": page, "page_size": page_size,
            "items": items}


@app.get("/api/messages/{message_id}", dependencies=[Depends(is_staff)])
def message_detail(message_id: int):
    with SessionLocal() as s:
        msg = s.get(Message, message_id)
        group = s.get(Group, msg.group_id) if msg else None
        if not msg or not group or not group.enabled:
            raise HTTPException(404, "Message not found")
        return {"id": msg.id, "text": msg.text, "sender_name": msg.sender_name,
                "sender_phone": msg.sender_phone, "group": group.name,
                "timestamp": msg.ts.isoformat() + "Z", "status": msg.status,
                "display_status": pending_feed.message_status(
                    msg, group, worker.budget_status(s)["blocked"])}


@app.get("/api/offers/{offer_id}", dependencies=[Depends(is_staff)])
def get_offer(offer_id: int):
    with SessionLocal() as s:
        o = s.get(Offer, offer_id)
        if not o:
            raise HTTPException(404)
        msg = s.get(Message, o.message_id)
        groups = {g.id: g.name for g in s.scalars(select(Group)).all()}
        out = offer_json(o, groups, feed_ctx(s, [o]), include_message=msg.text if msg else "")
        out["photos"] = [p.id for p in media.photos_near(s, o.photo_id)] if o.photo_id else (
            [out["photo_id"]] if out["photo_id"] else [])
        d = s.get(Dealer, o.dealer_key) if o.dealer_key else None
        out["dealer_note"] = d.note if d else None
        return out


@app.get("/api/facets", dependencies=[Depends(is_staff)])
def facets():
    since = utcnow() - timedelta(days=90)
    with SessionLocal() as s:
        brands = s.execute(select(Offer.brand, func.count()).where(Offer.brand.is_not(None),
                           Offer.last_seen_at >= since).group_by(Offer.brand)
                           .order_by(func.count().desc()).limit(80)).all()
        countries = s.execute(select(Offer.country, func.count()).where(Offer.country.is_not(None),
                              Offer.last_seen_at >= since).group_by(Offer.country)
                              .order_by(func.count().desc()).limit(60)).all()
        groups = s.scalars(select(Group).where(Group.enabled.is_(True)).order_by(Group.name)).all()
    return {"brands": [b for b, _ in brands], "countries": [c for c, _ in countries],
            "groups": [{"id": g.id, "name": g.name} for g in groups],
            "base_currency": settings.BASE_CURRENCY}


class AdviseIn(BaseModel):
    reference: str
    dial_color: Optional[str] = None
    condition: Optional[str] = None
    set_type: Optional[str] = None
    year: Optional[int] = None
    asking_price: Optional[float] = None
    margin_pct: Optional[float] = None


@app.get("/api/advise/config", dependencies=[Depends(is_staff)])
def advise_config():
    return {"margin_pct": settings.TARGET_MARGIN_PCT, "base_currency": settings.BASE_CURRENCY}


@app.post("/api/advise", dependencies=[Depends(is_staff)])
def advise(body: AdviseIn):
    ref = worker.norm_ref(body.reference)
    if not ref or len(ref) < 4:
        raise HTTPException(400, "Enter a reference number, e.g. 126710BLNR")
    since = utcnow() - timedelta(days=90)
    blocked = select(Dealer.key).where(Dealer.rating == "BLOCKED")
    base = select(Offer).where(Offer.archived.is_(False), Offer.last_seen_at >= since,
                               or_(Offer.dealer_key.is_(None), Offer.dealer_key.not_in(blocked)))
    broader = False
    with SessionLocal() as s:
        rows = s.scalars(base.where(Offer.reference_norm == ref)).all()
        if sum(1 for o in rows if o.direction == "WTS" and o.price_base) < 3 and len(ref) > 6:
            rows = s.scalars(base.where(Offer.reference_norm.startswith(ref[:6]))).all()
            broader = True
    if body.dial_color:
        dial = body.dial_color.lower()
        same = [o for o in rows if o.dial_color and dial in o.dial_color.lower()]
        if sum(1 for o in same if o.direction == "WTS" and o.price_base) >= 3:
            rows = same
    comps = [{
        "id": o.id, "direction": o.direction, "price_base": o.price_base, "price": o.price, "currency": o.currency,
        "condition": o.condition, "set_type": o.set_type, "year": o.year, "country": o.country,
        "dial_color": o.dial_color, "reference": o.reference, "last_seen_at": o.last_seen_at,
        "dealer": o.dealer_phone or o.dealer_name, "dealer_name": o.dealer_name, "dealer_phone": o.dealer_phone,
        "dealer_key": o.dealer_key,
    } for o in rows]
    res = advisor.analyse(body.model_dump(), comps, utcnow(),
                          body.margin_pct if body.margin_pct is not None else settings.TARGET_MARGIN_PCT,
                          settings.IMPORT_UPLIFT_PCT)
    brand, family = refs.lookup(ref, None)
    if not brand:
        named = [o for o in rows if o.brand]
        if named:
            brand, family = named[0].brand, named[0].family
    res["watch"] = {"reference": body.reference, "brand": brand, "family": family, "reference_norm": ref}
    with SessionLocal() as s:
        res["history"] = extra.history_for(s, ref)
    res["broader_match"] = broader
    for c in res["samples"]:
        c["last_seen_at"] = c["last_seen_at"].isoformat() + "Z"
        c["adjusted"] = round(c["adjusted"])
    return res


class ParseIn(BaseModel):
    text: str


@app.post("/api/advise/parse", dependencies=[Depends(is_staff)])
async def advise_parse(body: ParseIn):
    """Fill the advisor form from a customer's message."""
    if settings.DEMO_MODE:
        raise HTTPException(403, "Message parsing is disabled in demo mode")
    with SessionLocal() as s:
        if worker.budget_status(s)["blocked"]:
            raise HTTPException(429, "AI limit reached or usage unpriced; enter watch details manually")
    try:
        offers = await extract_offers(
            body.text[:4000],
            f"Private customer selling to us in the UK. Amounts with no currency symbol are {settings.BASE_CURRENCY}.",
        )
    except ParseError as e:
        raise HTTPException(502, str(e))
    cleaned = [o for o in (worker.clean_offer(x) for x in offers) if o]
    if not cleaned:
        raise HTTPException(422, "Couldn't find a watch in that message")
    o = cleaned[0]
    price_gbp = o["price"] if o["currency"] == settings.BASE_CURRENCY else o["price_base"]
    return {"reference": o["reference"], "brand": o["brand"], "family": o["family"],
            "dial_color": o["dial_color"], "condition": o["condition"], "set_type": o["set_type"],
            "year": o["year"], "asking_price": round(price_gbp) if price_gbp else None,
            "others": len(cleaned) - 1}


# ------------------------------------------------------------------ admin API
@app.get("/admin/api/status", dependencies=[Depends(is_admin)])
async def admin_status():
    try:
        st = await waha.session_status()
    except Exception as e:  # noqa: BLE001
        st = {"status": "BRIDGE_UNREACHABLE", "error": str(e)}
    me = st.get("me") or {}
    return {"status": st.get("status"), "me": {"id": me.get("id"), "name": me.get("pushName")},
            "error": st.get("error")}


@app.post("/admin/api/connect", dependencies=[Depends(is_admin)])
async def admin_connect():
    try:
        st = await waha.start_session()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"WhatsApp bridge error: {e}")
    return {"status": st.get("status")}


@app.post("/admin/api/restart", dependencies=[Depends(is_admin)])
async def admin_restart():
    await waha.restart_session()
    return {"ok": True}


@app.get("/admin/api/qr", dependencies=[Depends(is_admin)])
async def admin_qr():
    png = await waha.qr_png()
    if not png:
        raise HTTPException(404, "No QR available")
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.get("/admin/api/groups", dependencies=[Depends(is_admin)])
def admin_groups():
    with SessionLocal() as s:
        gs = s.scalars(select(Group).order_by(Group.enabled.desc(), Group.name)).all()
        offers = dict(s.execute(select(Offer.group_id, func.count()).group_by(Offer.group_id)).all())
        group_cost = dict(s.execute(
            select(AIUsageGroup.group_id, func.sum(AIUsageGroup.estimated_usd))
            .join(AIUsage, AIUsage.id == AIUsageGroup.usage_id)
            .where(AIUsage.created_at >= utcnow() - timedelta(days=7))
            .group_by(AIUsageGroup.group_id)
        ).all())
    return [{"id": g.id, "name": g.name, "enabled": g.enabled, "participants": g.participants,
             "messages": g.message_count, "offers": offers.get(g.id, 0),
             "ai_paused": g.ai_paused, "ai_checked": g.ai_checked, "ai_hits": g.ai_hits,
             "cost_7d_usd": float(group_cost.get(g.id) or 0),
             "last_message_at": g.last_message_at.isoformat() + "Z" if g.last_message_at else None}
            for g in gs]


@app.post("/admin/api/groups/sync", dependencies=[Depends(is_admin)])
async def admin_sync_groups():
    try:
        found = await waha.list_groups()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Could not load groups: {e}")
    with SessionLocal() as s:
        added = 0
        for g in found:
            row = s.get(Group, g["id"])
            if row is None:
                s.add(Group(id=g["id"], name=g["name"], participants=g["participants"],
                            enabled=settings.AUTO_ENABLE_NEW_GROUPS))
                added += 1
            else:
                row.name, row.participants = g["name"], g["participants"] or row.participants
        s.commit()
    return {"found": len(found), "added": added}


class GroupToggle(BaseModel):
    ids: list
    enabled: bool


@app.post("/admin/api/groups/toggle", dependencies=[Depends(is_admin)])
def admin_toggle(body: GroupToggle):
    with SessionLocal() as s:
        s.execute(update(Group).where(Group.id.in_(body.ids)).values(enabled=body.enabled))
        s.commit()
    return {"ok": True}


class AIPause(BaseModel):
    paused: bool


@app.post("/admin/api/groups/{group_id}/ai", dependencies=[Depends(is_admin)])
def admin_group_ai(group_id: str, body: AIPause):
    with SessionLocal() as s:
        group = s.get(Group, group_id)
        if not group:
            raise HTTPException(404, "Group not found")
        group.ai_paused = body.paused
        if not body.paused:
            group.ai_checked = 0
            group.ai_hits = 0
        s.commit()
    return {"ok": True, "paused": body.paused}


class Backfill(BaseModel):
    group_id: str
    limit: int = 300


@app.post("/admin/api/groups/backfill", dependencies=[Depends(is_admin)])
async def admin_backfill(body: Backfill):
    try:
        msgs = await waha.chat_messages(body.group_id, min(max(body.limit, 1), 2000), download_media=True)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Could not load history: {e}")
    with SessionLocal() as s:
        g = s.get(Group, body.group_id)
        if not g or not g.enabled:
            raise HTTPException(400, "Enable the group first")
    stored = photos = 0
    for p in msgs:
        m = ingest.normalize(p)
        if not m:
            continue
        m = await ingest.resolve_phone(m)
        result = await _ingest_one(m)
        stored += result["stored"]
        photos += result.get("photo", False)
    return {"fetched": len(msgs), "stored": stored, "photos": photos}


@app.get("/admin/api/stats", dependencies=[Depends(is_admin)])
def admin_stats():
    day = utcnow() - timedelta(days=1)
    with SessionLocal() as s:
        by_status = dict(s.execute(select(Message.status, func.count()).group_by(Message.status)).all())
        return {
            "messages_24h": s.scalar(select(func.count()).where(Message.ts >= day)),
            "messages_by_status": by_status,
            "offers_total": s.scalar(select(func.count(Offer.id))),
            "offers_24h": s.scalar(select(func.count()).where(Offer.last_seen_at >= day)),
            "groups_enabled": s.scalar(select(func.count()).where(Group.enabled.is_(True))),
            "last_message_at": (lambda t: t.isoformat() + "Z" if t else None)(s.scalar(select(func.max(Message.ts)))),
            "fx_updated_at": fx.updated_at.isoformat() + "Z" if fx.updated_at else None,
            "ai_configured": bool(settings.ANTHROPIC_API_KEY),
            "ai_spend": worker.budget_status(s),
        }


@app.get("/admin/api/ai-usage", dependencies=[Depends(is_admin)])
def admin_ai_usage():
    now = utcnow()
    with SessionLocal() as s:
        windows = {}
        for label, days in (("24h", 1), ("7d", 7)):
            since = now - timedelta(days=days)
            requests, priced, total, input_tokens, output_tokens = s.execute(
                select(func.count(AIUsage.id), func.count(AIUsage.estimated_usd),
                       func.sum(AIUsage.estimated_usd), func.sum(AIUsage.input_tokens),
                       func.sum(AIUsage.output_tokens))
                .where(AIUsage.created_at >= since)
            ).one()
            skipped = s.scalar(select(func.count(Message.id)).where(
                Message.created_at >= since, Message.status == "SKIPPED"
            ))
            windows[label] = {
                "estimated_usd": float(total or 0),
                "requests": requests,
                "unpriced_requests": requests - priced,
                "input_tokens": input_tokens or 0,
                "output_tokens": output_tokens or 0,
                "skipped_received_messages": skipped,
            }
        rows = s.execute(
            select(AIUsageGroup.group_id, Group.name, func.sum(AIUsageGroup.estimated_usd),
                   func.count(AIUsageGroup.id),
                   func.count(AIUsageGroup.estimated_usd))
            .join(AIUsage, AIUsage.id == AIUsageGroup.usage_id)
            .outerjoin(Group, Group.id == AIUsageGroup.group_id)
            .where(AIUsage.created_at >= now - timedelta(days=7))
            .group_by(AIUsageGroup.group_id, Group.name)
            .order_by(func.sum(AIUsageGroup.estimated_usd).desc())
        ).all()
        groups = [{"id": group_id, "name": name or group_id,
                   "estimated_usd": float(amount or 0), "requests": requests,
                   "unpriced_requests": requests - priced}
                  for group_id, name, amount, requests, priced in rows]
    return {"windows": windows, "groups_7d": groups, "worker_enabled": settings.WORKER_ENABLED,
            "model": settings.ANTHROPIC_MODEL}


@app.get("/admin/api/errors", dependencies=[Depends(is_admin)])
def admin_errors():
    with SessionLocal() as s:
        rows = s.scalars(select(Message).where(Message.status == "ERROR").order_by(Message.id.desc()).limit(50)).all()
        return [{"id": m.id, "error": m.error, "text": m.text[:300]} for m in rows]


@app.post("/admin/api/retry-errors", dependencies=[Depends(is_admin)])
def admin_retry():
    with SessionLocal() as s:
        n = s.execute(update(Message).where(Message.status == "ERROR").values(status="NEW", attempts=0)).rowcount
        s.commit()
    return {"requeued": n}


app.include_router(extra.router)
app.include_router(feed_settings.router)


@app.exception_handler(HTTPException)
async def http_exc(request: Request, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
