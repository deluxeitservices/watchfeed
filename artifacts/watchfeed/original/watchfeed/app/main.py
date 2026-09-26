import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from sqlalchemy import func, or_, select, update

from . import fx, ingest, waha, worker
from .config import settings
from .db import Group, Message, Offer, SessionLocal, init_db, utcnow

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")
STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not settings.DEMO_MODE and not (settings.STAFF_PASSWORD and settings.ADMIN_PASSWORD and settings.WEBHOOK_SECRET):
        raise RuntimeError("Set STAFF_PASSWORD, ADMIN_PASSWORD and WEBHOOK_SECRET in .env")
    init_db()
    if settings.DEMO_MODE:
        from .demo import seed_demo
        seed_demo()
    task = asyncio.create_task(worker.run_forever()) if settings.WORKER_ENABLED and not settings.DEMO_MODE else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="Watch Trading Feed", lifespan=lifespan, docs_url=None, redoc_url=None)
basic = HTTPBasic(auto_error=False)


def _check(creds: HTTPBasicCredentials, user: str, pw: str) -> bool:
    return (secrets.compare_digest(creds.username.encode(), user.encode())
            and secrets.compare_digest(creds.password.encode(), pw.encode()))


def _deny():
    raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Basic"})


def is_admin(creds: Optional[HTTPBasicCredentials] = Depends(basic)):
    if not creds or not _check(creds, settings.ADMIN_USER, settings.ADMIN_PASSWORD):
        _deny()


def is_staff(creds: Optional[HTTPBasicCredentials] = Depends(basic)):
    if settings.DEMO_MODE:
        return
    if not creds or not (_check(creds, settings.STAFF_USER, settings.STAFF_PASSWORD)
            or _check(creds, settings.ADMIN_USER, settings.ADMIN_PASSWORD)):
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
    with SessionLocal() as s:
        stored = ingest.store(s, m)
    return {"ok": True, "stored": bool(stored)}


@app.get("/health")
def health():
    return {"ok": True, "demo": settings.DEMO_MODE}


# ------------------------------------------------------------------ pages
@app.get("/", dependencies=[Depends(is_staff)])
def feed_page():
    return FileResponse(STATIC / "index.html")


@app.get("/admin", dependencies=[Depends(is_admin)])
def admin_page():
    return FileResponse(STATIC / "admin.html")


# ------------------------------------------------------------------ staff API
PERIODS = {"6h": 0.25, "1d": 1, "3d": 3, "1w": 7, "2w": 14, "1m": 30, "3m": 90, "6m": 180, "all": None}
SORTS = {"last_seen": Offer.last_seen_at, "first_seen": Offer.first_seen_at, "price": Offer.price_base,
         "year": Offer.year, "seen_count": Offer.duplicate_count, "brand": Offer.brand}


def offer_json(o: Offer, groups: dict, include_message: Optional[str] = None) -> dict:
    return {
        "id": o.id, "direction": o.direction, "brand": o.brand, "family": o.family, "model": o.model,
        "reference": o.reference, "dial_color": o.dial_color, "case_material": o.case_material,
        "bracelet": o.bracelet, "diamond_indices": o.diamond_indices, "condition": o.condition,
        "set_type": o.set_type, "year": o.year, "month": o.month, "price": o.price, "currency": o.currency,
        "price_base": o.price_base, "base_currency": settings.BASE_CURRENCY, "discount_pct": o.discount_pct,
        "country": o.country, "city": o.city, "notes": o.notes, "source_text": o.source_text,
        "dealer_name": o.dealer_name, "dealer_phone": o.dealer_phone,
        "groups": [groups.get(g, g) for g in o.groups_seen.split(",") if g],
        "seen_count": o.duplicate_count,
        "first_seen_at": o.first_seen_at.isoformat() + "Z", "last_seen_at": o.last_seen_at.isoformat() + "Z",
        **({"original_message": include_message} if include_message is not None else {}),
    }


@app.get("/api/offers", dependencies=[Depends(is_staff)])
def list_offers(q: str = "", direction: str = "", brand: str = "", condition: str = "", set_type: str = "",
                country: str = "", group: str = "", period: str = "2w", min_price: Optional[float] = None,
                max_price: Optional[float] = None, year_from: Optional[int] = None,
                year_to: Optional[int] = None, priced_only: bool = False, sort: str = "last_seen",
                order: str = "desc", page: int = 1, page_size: int = 50):
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
    ordering = [col.is_(None), col.asc() if order == "asc" else col.desc(), Offer.id.desc()]
    with SessionLocal() as s:
        total = s.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = s.scalars(stmt.order_by(*ordering).offset((page - 1) * page_size).limit(page_size)).all()
        groups = {g.id: g.name for g in s.scalars(select(Group)).all()}
    return {"total": total, "page": page, "page_size": page_size,
            "items": [offer_json(o, groups) for o in rows]}


@app.get("/api/offers/{offer_id}", dependencies=[Depends(is_staff)])
def get_offer(offer_id: int):
    with SessionLocal() as s:
        o = s.get(Offer, offer_id)
        if not o:
            raise HTTPException(404)
        msg = s.get(Message, o.message_id)
        groups = {g.id: g.name for g in s.scalars(select(Group)).all()}
        return offer_json(o, groups, include_message=msg.text if msg else "")


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
    return [{"id": g.id, "name": g.name, "enabled": g.enabled, "participants": g.participants,
             "messages": g.message_count, "offers": offers.get(g.id, 0),
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


class Backfill(BaseModel):
    group_id: str
    limit: int = 300


@app.post("/admin/api/groups/backfill", dependencies=[Depends(is_admin)])
async def admin_backfill(body: Backfill):
    try:
        msgs = await waha.chat_messages(body.group_id, min(max(body.limit, 1), 2000))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Could not load history: {e}")
    stored = 0
    with SessionLocal() as s:
        g = s.get(Group, body.group_id)
        if not g or not g.enabled:
            raise HTTPException(400, "Enable the group first")
        for p in msgs:
            m = ingest.normalize(p)
            if not m:
                continue
            m = await ingest.resolve_phone(m)
            if ingest.store(s, m):
                stored += 1
    return {"fetched": len(msgs), "stored": stored}


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
        }


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


@app.exception_handler(HTTPException)
async def http_exc(request: Request, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
