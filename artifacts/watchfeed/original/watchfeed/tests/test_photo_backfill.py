import os
import subprocess
import sys
from pathlib import Path


def test_captioned_backfill_photo_only_attaches_to_matching_wa_message(tmp_path):
    app_dir = Path(__file__).resolve().parents[1]
    script = r'''
import base64
from fastapi.testclient import TestClient
from app import main, media, waha
from app.db import Group, Message, Offer, Photo, SessionLocal, init_db, utcnow
from app.parser import text_hash
from app.worker import save_offers

GROUP = "120363001@g.us"
SENDER = "447700900001"
CAPTIONED_ID = "backfill-captioned-message"
NEARBY_ID = "nearby-unrelated-message"

init_db()
now = utcnow()
with SessionLocal() as s:
    s.add(Group(id=GROUP, name="Dealers", enabled=True))
    s.flush()
    target = Message(
        wa_id=CAPTIONED_ID, group_id=GROUP, sender_phone=SENDER,
        sender_name="Same dealer", text="WTS 126710BLNR 2024",
        text_hash=text_hash("WTS 126710BLNR 2024"), ts=now, status="PARSED",
    )
    nearby = Message(
        wa_id=NEARBY_ID, group_id=GROUP, sender_phone=SENDER,
        sender_name="Same dealer", text="WTS 124060 2022",
        text_hash=text_hash("WTS 124060 2022"), ts=now, status="PARSED",
    )
    s.add_all([target, nearby])
    s.flush()
    save_offers(s, target, [{
        "direction": "WTS", "brand": "Rolex", "reference": "126710BLNR",
        "price": 12000, "currency": "GBP", "source_text": target.text,
    }])
    save_offers(s, nearby, [{
        "direction": "WTS", "brand": "Rolex", "reference": "124060",
        "price": 9000, "currency": "GBP", "source_text": nearby.text,
    }])
    s.commit()

async def backfill(group_id, limit=300, download_media=False):
    return [{
        "id": CAPTIONED_ID, "timestamp": int(now.timestamp()), "from": GROUP,
        "fromMe": False, "participant": SENDER + "@s.whatsapp.net",
        "body": "WTS 126710BLNR 2024 blue dial", "hasMedia": True,
        "media": {"url": "http://localhost:3000/photo.jpg", "mimetype": "image/jpeg"},
    }]

async def save_captioned_photo(session, message):
    photo = Photo(
        wa_id=message["wa_id"], group_id=message["group_id"],
        sender_key=media.sender_key(message.get("sender_phone"), message.get("sender_name")),
        ts=message["ts"], file="202601/test.jpg", thumb="202601/test_t.jpg",
        caption=message["text"],
    )
    session.add(photo)
    session.commit()
    return photo

waha.chat_messages = backfill
media.save_photo = save_captioned_photo
admin = {"Authorization": "Basic " + base64.b64encode(b"admin:admin-pass").decode()}
with TestClient(main.app) as client:
    response = client.post(
        "/admin/api/groups/backfill", headers=admin,
        json={"group_id": GROUP, "limit": 10},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"fetched": 1, "stored": 0, "photos": 1}

with SessionLocal() as s:
    rows = {o.reference: o for o in s.query(Offer).all()}
    assert rows["126710BLNR"].photo_id is not None
    assert rows["126710BLNR"].photo_kind == "EXACT"
    assert rows["124060"].photo_id is None
'''
    env = os.environ.copy()
    env.update({
        "DATABASE_URL": f"sqlite:///{tmp_path / 'photo-backfill.sqlite'}",
        "WATCHFEED_DEMO": "false",
        "STAFF_USER": "staff",
        "STAFF_PASSWORD": "staff-pass",
        "ADMIN_USER": "admin",
        "ADMIN_PASSWORD": "admin-pass",
        "WEBHOOK_SECRET": "test-webhook-secret",
        "WORKER_ENABLED": "false",
        "MEDIA_DIR": str(tmp_path / "media"),
    })
    subprocess.run(
        [sys.executable, "-c", script],
        cwd=app_dir,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )