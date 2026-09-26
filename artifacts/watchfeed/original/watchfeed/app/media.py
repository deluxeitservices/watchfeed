"""Dealer photos: download from the WhatsApp bridge, make thumbnails, link to offers, clean up."""
import hashlib
import io
import logging
import os
from datetime import timedelta
from typing import Optional
from urllib.parse import urlsplit

import httpx
from sqlalchemy import func, select, update

from .config import settings
from .db import Message, Offer, Photo, utcnow

log = logging.getLogger("media")

try:
    from PIL import Image, ImageOps
except ImportError:  # thumbnails are optional
    Image = None

FULL_PX, THUMB_PX = 1600, 360
MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
SUPPORTED_IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


def sender_key(phone: Optional[str], name: Optional[str]) -> Optional[str]:
    return phone or ((name or "").strip().lower() or None)


def _bridge_url(url: str) -> str:
    """WAHA may report files as http://localhost:3000/...; always fetch via the internal bridge address."""
    parts = urlsplit(url)
    return settings.WAHA_URL + parts.path + (f"?{parts.query}" if parts.query else "")


def process_image(data: bytes) -> tuple:
    """Return (full_jpeg, thumb_jpeg). Falls back to the original bytes if Pillow is missing."""
    if Image is None:
        return data, data
    source = Image.open(io.BytesIO(data))
    if source.width * source.height > MAX_IMAGE_PIXELS:
        raise ValueError("image dimensions exceed the processing limit")
    img = ImageOps.exif_transpose(source).convert("RGB")
    out = []
    for px, q in ((FULL_PX, 82), (THUMB_PX, 78)):
        im = img.copy()
        im.thumbnail((px, px))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=q, optimize=True)
        out.append(buf.getvalue())
    return out[0], out[1]


def store_files(data: bytes, when) -> tuple:
    full, thumb = process_image(data)
    h = hashlib.sha1(data).hexdigest()[:20]
    sub = when.strftime("%Y%m")
    os.makedirs(os.path.join(settings.MEDIA_DIR, sub), exist_ok=True)
    rel_full, rel_thumb = f"{sub}/{h}.jpg", f"{sub}/{h}_t.jpg"
    for rel, blob in ((rel_full, full), (rel_thumb, thumb)):
        path = os.path.join(settings.MEDIA_DIR, rel)
        if not os.path.exists(path):
            with open(path, "wb") as f:
                f.write(blob)
    return rel_full, rel_thumb


async def save_photo(s, m: dict) -> Optional[Photo]:
    media = m.get("_media") or {}
    url = media.get("url")
    mime = (media.get("mimetype") or "").split(";", 1)[0].strip().lower()
    if not url or mime not in SUPPORTED_IMAGE_MIMES:
        return None
    # Without Pillow, retain only JPEGs so the stored .jpg extension and response
    # content type remain truthful.
    if Image is None and mime != "image/jpeg":
        return None
    existing = s.scalar(select(Photo).where(Photo.wa_id == m["wa_id"]))
    if existing:
        return existing
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            async with c.stream("GET", _bridge_url(url),
                                headers={"X-Api-Key": settings.WAHA_API_KEY} if settings.WAHA_API_KEY else {}) as r:
                r.raise_for_status()
                content_length = r.headers.get("content-length")
                if content_length:
                    try:
                        if int(content_length) > MAX_IMAGE_BYTES:
                            log.warning("photo too large for %s", m.get("wa_id"))
                            return None
                    except ValueError:
                        pass
                chunks, size = [], 0
                async for chunk in r.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_IMAGE_BYTES:
                        log.warning("photo too large for %s", m.get("wa_id"))
                        return None
                    chunks.append(chunk)
                data = b"".join(chunks)
            if not data:
                return None
        rel_full, rel_thumb = store_files(data, m["ts"])
    except Exception as e:  # noqa: BLE001 - a missing photo must never block the message
        log.warning("photo download failed for %s: %s", m.get("wa_id"), e)
        return None
    photo = Photo(wa_id=m["wa_id"], group_id=m["group_id"], sender_key=sender_key(m.get("sender_phone"), m.get("sender_name")),
                  ts=m["ts"], file=rel_full, thumb=rel_thumb, caption=(m.get("text") or "")[:2000])
    s.add(photo)
    s.commit()
    return photo


def _attach(s, photo: Photo, offers: list) -> None:
    kind = "EXACT" if len(offers) == 1 else "MESSAGE"
    for o in offers:
        if o.photo_id is None or (kind == "EXACT" and o.photo_kind != "EXACT"):
            o.photo_id, o.photo_kind = photo.id, kind
    if kind == "EXACT" and offers[0].reference_norm:
        photo.reference_norm = offers[0].reference_norm


def link_for_message(s, msg: Message, offers: list) -> None:
    """Called after a message's offers are created: find its photo (same message, else same dealer nearby in time)."""
    if not offers:
        return
    photo = s.scalar(select(Photo).where(Photo.wa_id == msg.wa_id))
    if photo is None:
        key = sender_key(msg.sender_phone, msg.sender_name)
        if not key:
            return
        w = timedelta(seconds=settings.PHOTO_LINK_SECONDS)
        cands = s.scalars(select(Photo).where(Photo.group_id == msg.group_id, Photo.sender_key == key,
                                              Photo.ts >= msg.ts - w, Photo.ts <= msg.ts + w)).all()
        # only photos without their own caption-offers (a captioned photo belongs to its caption)
        cands = [p for p in cands if not p.caption.strip()]
        if not cands:
            return
        photo = min(cands, key=lambda p: abs((p.ts - msg.ts).total_seconds()))
    _attach(s, photo, offers)


def link_late_photo(s, photo: Photo) -> None:
    """A photo-only message arriving just after its text: attach to that text's offers."""
    if photo.caption.strip() or not photo.sender_key:
        return
    w = timedelta(seconds=settings.PHOTO_LINK_SECONDS)
    msgs = s.scalars(select(Message).where(Message.group_id == photo.group_id,
                                           Message.ts >= photo.ts - w, Message.ts <= photo.ts + w)).all()
    msgs = [m for m in msgs if sender_key(m.sender_phone, m.sender_name) == photo.sender_key]
    if not msgs:
        return
    msg = min(msgs, key=lambda m: abs((m.ts - photo.ts).total_seconds()))
    offers = s.scalars(select(Offer).where(Offer.message_id == msg.id)).all()
    if offers:
        _attach(s, photo, offers)
        s.commit()


def link_backfilled_photo(s, photo: Photo, wa_id: str, group_id: str) -> bool:
    """Link a captioned backfill image only to offers from that exact WA message."""
    if not wa_id or not group_id or photo.wa_id != wa_id or photo.group_id != group_id:
        return False
    msg = s.scalar(select(Message).where(Message.wa_id == wa_id, Message.group_id == group_id))
    if not msg:
        return False
    offers = s.scalars(select(Offer).where(Offer.message_id == msg.id)).all()
    if not offers:
        return False
    _attach(s, photo, offers)
    s.commit()
    return True


def photos_near(s, photo_id: int) -> list:
    """All photos the same dealer sent around the same time (album) - for the detail view."""
    p = s.get(Photo, photo_id)
    if not p:
        return []
    w = timedelta(seconds=settings.PHOTO_LINK_SECONDS)
    q = select(Photo).where(Photo.group_id == p.group_id, Photo.ts >= p.ts - w, Photo.ts <= p.ts + w)
    q = q.where(Photo.sender_key == p.sender_key) if p.sender_key else q.where(Photo.id == p.id)
    return s.scalars(q.order_by(Photo.ts)).all()


def reference_photos(s, ref_norms: set) -> dict:
    """Latest EXACT photo per reference, used when a listing has no photo of its own."""
    if not ref_norms:
        return {}
    latest = (select(Photo.reference_norm, func.max(Photo.id).label("pid"))
              .where(Photo.reference_norm.in_(ref_norms)).group_by(Photo.reference_norm))
    return {r: pid for r, pid in s.execute(latest).all()}


def cleanup(s) -> int:
    """Delete photos older than retention, but keep the newest photo of each reference (used as fallback)."""
    cutoff = utcnow() - timedelta(days=settings.PHOTO_RETENTION_DAYS)
    keep = {pid for _, pid in s.execute(select(Photo.reference_norm, func.max(Photo.id))
                                        .where(Photo.reference_norm.is_not(None))
                                        .group_by(Photo.reference_norm)).all()}
    old = [p for p in s.scalars(select(Photo).where(Photo.ts < cutoff)).all() if p.id not in keep]
    for p in old:
        for rel in (p.file, p.thumb):
            try:
                os.remove(os.path.join(settings.MEDIA_DIR, rel))
            except OSError:
                pass
        s.execute(update(Offer).where(Offer.photo_id == p.id).values(photo_id=None, photo_kind=None))
        s.delete(p)
    s.commit()
    return len(old)
