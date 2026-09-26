"""API for photos, staff tracking, reference history, dealer profiles, alerts and our stock."""
import csv
import io
import json
import os
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import case, func, select

from . import advisor, alerts, market, refs
from .auth import is_admin, is_staff
from .config import settings
from .db import Alert, AlertHit, Dealer, Group, Offer, Photo, SessionLocal, StockItem, utcnow
from .feed_settings import admin_auto_refresh_enabled
from .worker_utils import norm_ref

router = APIRouter()
TRACK = {"CONTACTED", "OFFERED", "BOUGHT", "PASSED"}
RATINGS = {"TRUSTED", "OK", "CAUTION", "BLOCKED"}


def _iso(dt):
    return dt.isoformat() + "Z" if dt else None


def _offer_brief(o: Offer, groups: dict) -> dict:
    return {"id": o.id, "direction": o.direction, "brand": o.brand, "family": o.family, "reference": o.reference,
            "dial_color": o.dial_color, "condition": o.condition, "set_type": o.set_type, "year": o.year,
            "price": o.price, "currency": o.currency, "price_base": o.price_base, "country": o.country,
            "dealer_name": o.dealer_name, "dealer_phone": o.dealer_phone, "dealer_key": o.dealer_key,
            "market_pct": o.market_pct, "deal": market.deal_label(o.market_pct, settings.DEAL_THRESHOLD_PCT),
            "photo_id": o.photo_id, "last_seen_at": _iso(o.last_seen_at),
            "groups": [groups.get(g, g) for g in o.groups_seen.split(",") if g]}


def _groups(s) -> dict:
    return {g.id: g.name for g in s.scalars(select(Group)).all()}


@router.get("/api/me")
def me(request: Request, user: str = Depends(is_staff)):
    admin = bool(request.state.is_admin)
    with SessionLocal() as session:
        auto_refresh = admin and admin_auto_refresh_enabled(session)
    return {"user": user, "is_admin": admin, "feed_auto_refresh": auto_refresh,
            "base_currency": settings.BASE_CURRENCY, "deal_threshold_pct": settings.DEAL_THRESHOLD_PCT}


# ------------------------------------------------------------------ photos
@router.get("/media/{photo_id}/{size}", dependencies=[Depends(is_staff)])
def photo_file(photo_id: int, size: str):
    with SessionLocal() as s:
        p = s.get(Photo, photo_id)
    if not p:
        raise HTTPException(404)
    path = os.path.join(settings.MEDIA_DIR, p.thumb if size == "thumb" else p.file)
    if not os.path.exists(path):
        raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=604800"})


# ------------------------------------------------------------------ staff tracking
class TrackIn(BaseModel):
    status: Optional[str] = None  # None/"" clears
    note: Optional[str] = None


@router.post("/api/offers/{offer_id}/track")
def track_offer(offer_id: int, body: TrackIn, user: str = Depends(is_staff)):
    st = (body.status or "").upper() or None
    if st and st not in TRACK:
        raise HTTPException(400, "Unknown status")
    with SessionLocal() as s:
        o = s.get(Offer, offer_id)
        if not o:
            raise HTTPException(404)
        o.track_status, o.track_note = st, (body.note or None)
        o.track_by, o.track_at = (user, utcnow()) if st else (None, None)
        s.commit()
        return {"status": o.track_status, "by": o.track_by, "at": _iso(o.track_at), "note": o.track_note}


# ------------------------------------------------------------------ reference history
def history_for(s, ref_norm: str, weeks: int = 26) -> list:
    since = utcnow() - timedelta(weeks=weeks)
    rows = s.execute(select(Offer.last_seen_at, Offer.price_base, Offer.condition, Offer.set_type).where(
        Offer.reference_norm == ref_norm, Offer.direction == "WTS", Offer.price_base.is_not(None),
        Offer.last_seen_at >= since, Offer.market_pct.is_(None) | (Offer.market_pct > market.SUSPECT_PCT))).all()
    return market.weekly_history([(ts, market.normalize(p, c, st)) for ts, p, c, st in rows], utcnow(), weeks)


@router.get("/api/refs/{ref}", dependencies=[Depends(is_staff)])
def reference_summary(ref: str):
    r = norm_ref(ref)
    if not r:
        raise HTTPException(400)
    month = utcnow() - timedelta(days=30)
    with SessionLocal() as s:
        recent = s.scalars(select(Offer).where(Offer.reference_norm == r, Offer.last_seen_at >= month)
                           .order_by(Offer.last_seen_at.desc()).limit(40)).all()
        groups = _groups(s)
        stock = s.scalars(select(StockItem).where(StockItem.reference_norm == r, StockItem.status == "ACTIVE")).all()
        hist = history_for(s, r)
    brand, family = refs.lookup(r, None)
    if not brand and recent:
        brand, family = recent[0].brand, recent[0].family
    wts = [o for o in recent if o.direction == "WTS"]
    norms = [market.normalize(o.price_base, o.condition, o.set_type) for o in wts if o.price_base]
    norms = [n for n in norms if n]
    return {"reference": recent[0].reference if recent else ref, "reference_norm": r, "brand": brand, "family": family,
            "typical_new_full_set": round(advisor._q(norms, .5)) if norms else None,
            "wts_dealers_30d": len({o.dealer_key for o in wts}),
            "wtb_dealers_30d": len({o.dealer_key for o in recent if o.direction == "WTB"}),
            "history": hist, "recent": [_offer_brief(o, groups) for o in recent],
            "in_stock": [{"id": i.id, "description": i.description, "asking_price": i.asking_price} for i in stock],
            "base_currency": settings.BASE_CURRENCY}


# ------------------------------------------------------------------ dealers
def _dealer_stats_query(since):
    return (select(Offer.dealer_key, func.max(Offer.dealer_name), func.max(Offer.dealer_phone),
                   func.count(Offer.id),
                   func.sum(case((Offer.direction == "WTS", 1), else_=0)),
                   func.sum(case((Offer.direction == "WTB", 1), else_=0)),
                   func.max(Offer.last_seen_at),
                   func.avg(case((Offer.market_pct > market.SUSPECT_PCT, Offer.market_pct), else_=None)),
                   func.count(func.distinct(Offer.group_id)))
            .where(Offer.dealer_key.is_not(None), Offer.last_seen_at >= since)
            .group_by(Offer.dealer_key))


def _stat_row(r, d: Optional[Dealer]) -> dict:
    key, name, phone, n, wts, wtb, last, avg_mkt, ngroups = r
    return {"key": key, "name": (d.name if d and d.name else name), "phone": phone, "listings": n,
            "wts": int(wts or 0), "wtb": int(wtb or 0), "last_seen_at": _iso(last),
            "vs_market_pct": round(avg_mkt * 100, 1) if avg_mkt is not None else None, "groups": ngroups,
            "rating": d.rating if d else None, "note": d.note if d else None}


@router.get("/api/dealers", dependencies=[Depends(is_staff)])
def list_dealers(q: str = "", rating: str = "", sort: str = "listings", days: int = 90):
    since = utcnow() - timedelta(days=max(1, min(days, 365)))
    with SessionLocal() as s:
        rows = s.execute(_dealer_stats_query(since)).all()
        notes = {d.key: d for d in s.scalars(select(Dealer)).all()}
    out = [_stat_row(r, notes.get(r[0])) for r in rows]
    if q:
        ql = q.lower()
        out = [d for d in out if ql in (d["name"] or "").lower() or ql in (d["phone"] or "") or ql in (d["note"] or "").lower()]
    if rating:
        out = [d for d in out if (d["rating"] or "") == rating.upper()]
    key = {"listings": lambda d: -d["listings"], "recent": lambda d: d["last_seen_at"] or "",
           "cheap": lambda d: d["vs_market_pct"] if d["vs_market_pct"] is not None else 99}.get(sort)
    out.sort(key=key or (lambda d: -d["listings"]), reverse=(sort == "recent"))
    return out[:500]


@router.get("/api/dealers/{key}", dependencies=[Depends(is_staff)])
def dealer_profile(key: str):
    since = utcnow() - timedelta(days=90)
    with SessionLocal() as s:
        row = s.execute(_dealer_stats_query(since).where(Offer.dealer_key == key)).first()
        d = s.get(Dealer, key)
        if not row and not d:
            raise HTTPException(404)
        groups = _groups(s)
        recent = s.scalars(select(Offer).where(Offer.dealer_key == key).order_by(Offer.last_seen_at.desc()).limit(40)).all()
        brands = s.execute(select(Offer.brand, func.count()).where(Offer.dealer_key == key, Offer.brand.is_not(None))
                           .group_by(Offer.brand).order_by(func.count().desc()).limit(6)).all()
        gids = s.scalars(select(func.distinct(Offer.group_id)).where(Offer.dealer_key == key)).all()
    prof = _stat_row(row, d) if row else {"key": key, "name": d.name, "phone": d.phone, "listings": 0, "wts": 0,
                                          "wtb": 0, "last_seen_at": None, "vs_market_pct": None, "groups": 0,
                                          "rating": d.rating, "note": d.note}
    prof.update(top_brands=[{"brand": b, "count": n} for b, n in brands], group_names=[groups.get(g, g) for g in gids],
                recent=[_offer_brief(o, groups) for o in recent],
                updated_by=d.updated_by if d else None, updated_at=_iso(d.updated_at) if d else None)
    return prof


class DealerIn(BaseModel):
    rating: Optional[str] = None
    note: Optional[str] = None


@router.put("/api/dealers/{key}")
def update_dealer(key: str, body: DealerIn, user: str = Depends(is_staff)):
    rating = (body.rating or "").upper() or None
    if rating and rating not in RATINGS:
        raise HTTPException(400, "Unknown rating")
    with SessionLocal() as s:
        d = s.get(Dealer, key)
        if d is None:
            last = s.scalar(select(Offer).where(Offer.dealer_key == key).order_by(Offer.last_seen_at.desc()).limit(1))
            d = Dealer(key=key, phone=last.dealer_phone if last else None, name=last.dealer_name if last else None)
            s.add(d)
        d.rating, d.note = rating, (body.note or "").strip() or None
        d.updated_by, d.updated_at = user, utcnow()
        s.commit()
    return {"ok": True}


# ------------------------------------------------------------------ alerts
class AlertIn(BaseModel):
    name: str
    query: dict
    emails: str = ""
    telegram: bool = True


ALERT_KEYS = {"q", "direction", "brand", "condition", "set_type", "country", "group", "min_price", "max_price",
              "year_from", "year_to", "priced_only", "deals_only"}


@router.get("/api/alerts", dependencies=[Depends(is_staff)])
def list_alerts():
    with SessionLocal() as s:
        rows = s.scalars(select(Alert).order_by(Alert.created_at.desc())).all()
        week = utcnow() - timedelta(days=7)
        recent = dict(s.execute(select(AlertHit.alert_id, func.count()).where(AlertHit.created_at >= week)
                                .group_by(AlertHit.alert_id)).all())
    return {"channels": alerts.channels_configured(),
            "alerts": [{"id": a.id, "name": a.name, "query": json.loads(a.query_json), "emails": a.emails,
                        "telegram": a.telegram, "active": a.active, "created_by": a.created_by,
                        "created_at": _iso(a.created_at), "last_hit_at": _iso(a.last_hit_at),
                        "hit_count": a.hit_count, "hits_7d": recent.get(a.id, 0)} for a in rows]}


@router.post("/api/alerts")
def create_alert(body: AlertIn, user: str = Depends(is_staff)):
    q = {k: v for k, v in body.query.items() if k in ALERT_KEYS and v not in (None, "", False)}
    q.pop("period", None)
    if not q:
        raise HTTPException(400, "Choose at least one filter or search term")
    with SessionLocal() as s:
        a = Alert(name=body.name.strip()[:120] or "Alert", query_json=json.dumps(q), emails=body.emails.strip(),
                  telegram=body.telegram, created_by=user)
        s.add(a)
        s.commit()
        return {"id": a.id}


class AlertPatch(BaseModel):
    active: Optional[bool] = None
    name: Optional[str] = None
    emails: Optional[str] = None
    telegram: Optional[bool] = None


@router.patch("/api/alerts/{alert_id}", dependencies=[Depends(is_staff)])
def patch_alert(alert_id: int, body: AlertPatch):
    with SessionLocal() as s:
        a = s.get(Alert, alert_id)
        if not a:
            raise HTTPException(404)
        for k, v in body.model_dump(exclude_none=True).items():
            setattr(a, k, v)
        s.commit()
    return {"ok": True}


@router.delete("/api/alerts/{alert_id}", dependencies=[Depends(is_staff)])
def delete_alert(alert_id: int):
    with SessionLocal() as s:
        a = s.get(Alert, alert_id)
        if a:
            s.delete(a)
            s.commit()
    return {"ok": True}


@router.get("/api/alerts/{alert_id}/matches", dependencies=[Depends(is_staff)])
def alert_matches(alert_id: int):
    with SessionLocal() as s:
        ids = s.scalars(select(AlertHit.offer_id).where(AlertHit.alert_id == alert_id)
                        .order_by(AlertHit.id.desc()).limit(50)).all()
        groups = _groups(s)
        offers = {o.id: o for o in s.scalars(select(Offer).where(Offer.id.in_(ids))).all()} if ids else {}
        return [_offer_brief(offers[i], groups) for i in ids if i in offers]


@router.post("/api/notify/test", dependencies=[Depends(is_admin)])
async def notify_test():
    msg = "✅ Watch feed test notification — alerts are working."
    return {"telegram": await alerts.send_telegram(msg),
            "email": await alerts.send_email(settings.DIGEST_EMAILS.split(","), "Watch feed test", msg)}


@router.get("/api/digest/preview", dependencies=[Depends(is_staff)])
def digest_preview():
    with SessionLocal() as s:
        d = alerts.build_digest(s, utcnow())
    return {"text": alerts.render_digest(d, utcnow().strftime("%a %d %b")), "data": d}


# ------------------------------------------------------------------ our stock
COND_WORDS = {"new": "NEW", "unworn": "NEW", "like new": "LIKE_NEW", "mint": "LIKE_NEW", "used": "USED",
              "pre-owned": "USED", "preowned": "USED", "vintage": "VINTAGE"}
SET_WORDS = {"full set": "FULL_SET", "fs": "FULL_SET", "box and papers": "FULL_SET", "papers only": "PAPERS_ONLY",
             "watch + papers": "PAPERS_ONLY", "box only": "BOX_ONLY", "watch + box": "BOX_ONLY",
             "watch only": "WATCH_ONLY", "naked": "WATCH_ONLY"}


def _enum(v, words: dict, allowed: set):
    if not v:
        return None
    u = str(v).strip().upper().replace(" ", "_")
    return u if u in allowed else words.get(str(v).strip().lower())


class StockIn(BaseModel):
    reference: str
    description: Optional[str] = None
    condition: Optional[str] = None
    set_type: Optional[str] = None
    year: Optional[int] = None
    asking_price: Optional[float] = None
    status: Optional[str] = "ACTIVE"
    notes: Optional[str] = None


def _stock_fields(b: StockIn) -> dict:
    r = norm_ref(b.reference)
    if not r:
        raise HTTPException(400, "Reference required")
    return {"reference": b.reference.strip(), "reference_norm": r, "description": b.description,
            "condition": _enum(b.condition, COND_WORDS, set(advisor.COND_FACTOR) - {None}),
            "set_type": _enum(b.set_type, SET_WORDS, set(advisor.SET_FACTOR) - {None}),
            "year": b.year, "asking_price": b.asking_price,
            "status": (b.status or "ACTIVE").upper() if (b.status or "").upper() in ("ACTIVE", "SOLD") else "ACTIVE",
            "notes": b.notes}


@router.get("/api/stock", dependencies=[Depends(is_staff)])
def list_stock(status: str = "ACTIVE"):
    month = utcnow() - timedelta(days=30)
    with SessionLocal() as s:
        q = select(StockItem).order_by(StockItem.created_at.desc())
        if status:
            q = q.where(StockItem.status == status.upper())
        items = s.scalars(q).all()
        refs_ = {i.reference_norm for i in items}
        offers = s.scalars(select(Offer).where(Offer.reference_norm.in_(refs_), Offer.last_seen_at >= month)).all() if refs_ else []
    by_ref = {}
    for o in offers:
        by_ref.setdefault(o.reference_norm, []).append(o)
    out = []
    for i in items:
        os_ = by_ref.get(i.reference_norm, [])
        wtb = {o.dealer_key for o in os_ if o.direction == "WTB"}
        norms = [market.normalize(o.price_base, o.condition, o.set_type) for o in os_ if o.direction == "WTS" and o.price_base]
        norms = [n for n in norms if n]
        typical = (advisor._q(norms, .5) * advisor.COND_FACTOR.get(i.condition, .95) * advisor.SET_FACTOR.get(i.set_type, .95)
                   if len(norms) >= 3 else None)
        out.append({"id": i.id, "reference": i.reference, "reference_norm": i.reference_norm, "description": i.description,
                    "condition": i.condition, "set_type": i.set_type, "year": i.year, "asking_price": i.asking_price,
                    "status": i.status, "notes": i.notes, "created_by": i.created_by, "created_at": _iso(i.created_at),
                    "wtb_dealers_30d": len(wtb), "market_typical": round(typical) if typical else None,
                    "vs_market_pct": round((i.asking_price - typical) / typical * 100, 1)
                    if typical and i.asking_price else None})
    return {"items": out, "base_currency": settings.BASE_CURRENCY}


@router.post("/api/stock")
def add_stock(body: StockIn, user: str = Depends(is_staff)):
    with SessionLocal() as s:
        it = StockItem(created_by=user, **_stock_fields(body))
        s.add(it)
        s.commit()
        return {"id": it.id}


@router.put("/api/stock/{item_id}", dependencies=[Depends(is_staff)])
def edit_stock(item_id: int, body: StockIn):
    with SessionLocal() as s:
        it = s.get(StockItem, item_id)
        if not it:
            raise HTTPException(404)
        for k, v in _stock_fields(body).items():
            setattr(it, k, v)
        s.commit()
    return {"ok": True}


@router.delete("/api/stock/{item_id}", dependencies=[Depends(is_staff)])
def delete_stock(item_id: int):
    with SessionLocal() as s:
        it = s.get(StockItem, item_id)
        if it:
            s.delete(it)
            s.commit()
    return {"ok": True}


class CsvIn(BaseModel):
    csv: str


@router.post("/api/stock/import")
def import_stock(body: CsvIn, user: str = Depends(is_staff)):
    """CSV with header: reference,description,condition,set_type,year,asking_price (only reference required)."""
    reader = csv.DictReader(io.StringIO(body.csv.strip()))
    if not reader.fieldnames or "reference" not in [f.strip().lower() for f in reader.fieldnames]:
        raise HTTPException(400, "First line must be a header including 'reference'")
    added, skipped = 0, []
    with SessionLocal() as s:
        for n, row in enumerate(reader, start=2):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            try:
                b = StockIn(reference=row.get("reference", ""), description=row.get("description") or None,
                            condition=row.get("condition") or None, set_type=row.get("set_type") or row.get("set") or None,
                            year=int(row["year"]) if row.get("year") else None,
                            asking_price=float(row["asking_price"].replace(",", "").replace("£", ""))
                            if row.get("asking_price") else None)
                s.add(StockItem(created_by=user, **_stock_fields(b)))
                added += 1
            except (HTTPException, ValueError) as e:
                skipped.append(f"line {n}: {getattr(e, 'detail', e)}")
        s.commit()
    return {"added": added, "skipped": skipped}


@router.get("/api/stock/{item_id}/matches", dependencies=[Depends(is_staff)])
def stock_matches(item_id: int):
    month = utcnow() - timedelta(days=30)
    with SessionLocal() as s:
        it = s.get(StockItem, item_id)
        if not it:
            raise HTTPException(404)
        groups = _groups(s)
        wtb = s.scalars(select(Offer).where(Offer.reference_norm == it.reference_norm, Offer.direction == "WTB",
                                            Offer.last_seen_at >= month).order_by(Offer.last_seen_at.desc())).all()
        return [_offer_brief(o, groups) for o in wtb]
