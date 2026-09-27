import asyncio, base64, os, sqlite3, subprocess, sys, tempfile, time
from pathlib import Path

_TEST_TMP = tempfile.TemporaryDirectory(prefix="watchfeed-tests-")
_TEST_DIR = Path(_TEST_TMP.name)
APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
os.environ.update(DATABASE_URL=f"sqlite:///{_TEST_DIR / 'pipeline.db'}", STAFF_PASSWORD="s", ADMIN_PASSWORD="a",
                  WEBHOOK_SECRET="sec", WORKER_ENABLED="false", AUTO_ENABLE_NEW_GROUPS="false",
                  WATCHFEED_DEMO="false", STAFF_USERS="ali:pw1",
                  MEDIA_DIR=str(_TEST_DIR / "media"), WAHA_URL="http://localhost:3000",
                  TELEGRAM_BOT_TOKEN="test-token", TELEGRAM_CHAT_ID="test-chat")

import pytest
import httpx as _httpx
from fastapi.testclient import TestClient
from PIL import Image
from app import alerts as alerts_mod, main, media as media_mod, parser, waha, worker
from app.db import Message, Offer, SessionLocal, engine, utcnow

ADMIN = {"Authorization": "Basic " + base64.b64encode(b"admin:a").decode()}
STAFF = {"Authorization": "Basic " + base64.b64encode(b"staff:s").decode()}
ALI = {"Authorization": "Basic " + base64.b64encode(b"ali:pw1").decode()}
G1, G2, FAM = "120363001@g.us", "120363002@g.us", "120363999@g.us"
MSG = "🇭🇰 WTS\n126710BLNR 2024 full set hkd128k\n5711/1A 2019 used hkd1.05m"

FAKE = [
    {"direction": "WTS", "brand": "Rolex", "family": "GMT-Master II", "model": "Batman", "reference": "126710BLNR",
     "year": 2024, "set_type": "FULL_SET", "price": 128000, "currency": "HKD", "country": "HK",
     "source_text": "126710BLNR 2024 full set hkd128k"},
    {"direction": "WTS", "brand": "Patek Philippe", "family": "Nautilus", "reference": "5711/1A", "year": 2019,
     "condition": "USED", "price": 1050000, "currency": "hkd", "country": "hk", "source_text": "5711/1A 2019 used hkd1.05m"},
    {"direction": "WTS", "source_text": "garbage with no watch"},
]
calls = {"n": 0}
EXTRA = {}


@pytest.fixture(scope="session", autouse=True)
def _cleanup_test_storage():
    yield
    engine.dispose()
    _TEST_TMP.cleanup()

async def fake_extract(text, group_name=None, client=None):
    calls["n"] += 1
    if text in EXTRA:
        return EXTRA[text]
    return FAKE if "126710" in text else [{"direction": "WTB", "brand": "Rolex", "reference": "126500LN",
                                           "family": "Daytona", "source_text": text}]

def payload(i, chat, body, sender="447700900001@s.whatsapp.net", name="Ali HK", ts=None, photo=False):
    p = {"event": "message", "session": "default", "payload": {
        "id": f"false_{chat}_{i}", "timestamp": ts or int(time.time()), "from": chat, "fromMe": False,
        "participant": sender, "body": body, "hasMedia": photo, "_data": {"pushName": name}}}
    if photo:
        p["payload"]["media"] = {
            "url": f"http://localhost:3000/api/files/default/{i}.jpeg",
            "mimetype": "image/jpeg",
        }
    return p


@pytest.fixture(scope="module")
def client():
    parser_orig = worker.extract_offers_batch
    async def fake_batch(items, client=None, group_weights=None):
        return {item["id"]: await fake_extract(item["text"]) for item in items}
    worker.extract_offers_batch = fake_batch
    async def fake_groups():
        return [{"id": G1, "name": "HK Dealers", "participants": 200},
                {"id": G2, "name": "Dubai Watches", "participants": 90},
                {"id": FAM, "name": "Family", "participants": 8}]
    waha.list_groups = fake_groups
    async def fake_hist(chat_id, limit=300, **kwargs):
        return [payload(100, chat_id, "WTB 126500LN panda any year")["payload"],
                payload(101, chat_id, "thanks bro")["payload"],
                {"id": "x", "from": "447@c.us", "body": "private"}]
    waha.chat_messages = fake_hist
    with TestClient(main.app) as c:
        yield c
    worker.extract_offers_batch = parser_orig

def test_auth(client):
    assert client.get("/").status_code == 401
    assert client.get("/", headers=STAFF).status_code == 200
    assert client.get("/admin", headers=STAFF).status_code == 401
    assert client.get("/admin", headers=ADMIN).status_code == 200
    assert client.post("/webhook/waha?token=bad", json={}).status_code == 403


def test_feed_auto_refresh_is_admin_only(client):
    endpoint = "/admin/api/feed-refresh"
    assert client.get(endpoint).status_code == 401
    assert client.get(endpoint, headers=STAFF).status_code == 401
    assert client.put(endpoint, headers=ALI, json={"enabled": True}).status_code == 401
    assert client.get(endpoint, headers=ADMIN).json() == {"enabled": False}
    assert client.get("/api/me", headers=STAFF).json()["feed_auto_refresh"] is False
    assert client.get("/api/me", headers=STAFF).json()["is_admin"] is False
    assert client.put(endpoint, headers=ADMIN, json={"enabled": True}).json() == {"enabled": True}
    assert client.get(endpoint, headers=ADMIN).json() == {"enabled": True}
    assert client.get("/api/me", headers=ADMIN).json()["feed_auto_refresh"] is True
    assert client.get("/api/me", headers=ADMIN).json()["is_admin"] is True
    assert client.get("/api/me", headers=ALI).json()["feed_auto_refresh"] is False
    assert client.put(endpoint, headers=ADMIN, json={"enabled": False}).json() == {"enabled": False}
    assert client.get("/api/me", headers=ADMIN).json()["feed_auto_refresh"] is False

def test_groups_sync_and_toggle(client):
    r = client.post("/admin/api/groups/sync", headers=ADMIN).json()
    assert r == {"found": 3, "added": 3}
    gs = client.get("/admin/api/groups", headers=ADMIN).json()
    assert all(not g["enabled"] for g in gs)
    client.post("/admin/api/groups/toggle", headers=ADMIN, json={"ids": [G1, G2], "enabled": True})
    gs = {g["id"]: g for g in client.get("/admin/api/groups", headers=ADMIN).json()}
    assert gs[G1]["enabled"] and gs[G2]["enabled"] and not gs[FAM]["enabled"]

def test_webhook_filters(client):
    ok = lambda p: client.post("/webhook/waha?token=sec", json=p).json()
    assert ok(payload(1, G1, MSG))["stored"] is True
    assert ok(payload(1, G1, MSG)).get("stored") is False          # duplicate id
    assert ok(payload(2, FAM, "WTS 126710BLNR hkd128k")).get("stored") is False  # disabled group
    assert "stored" not in ok(payload(3, "447700900002@s.whatsapp.net", "private WTS 126710"))  # DM ignored
    assert ok(payload(4, G2, MSG, sender="447700900001@s.whatsapp.net"))["stored"]  # same text other group
    assert ok(payload(5, G2, "ok 👍"))["stored"]
    with SessionLocal() as s:
        assert s.query(Message).count() == 3

def test_worker_parses_dedupes_and_caches(client):
    calls["n"] = 0
    n, err = asyncio.run(worker.process_batch())
    assert (n, err) == (3, 0)
    assert calls["n"] == 1  # same text in 2 groups -> parsed once, "ok" skipped by prefilter
    with SessionLocal() as s:
        st = {m.text[:5]: m.status for m in s.query(Message)}
    assert st["ok 👍"] == "SKIPPED"
    d = client.get("/api/offers", headers=STAFF).json()
    assert d["total"] == 2  # garbage dropped; cross-group repost merged
    by = {o["reference"]: o for o in d["items"]}
    gmt = by["126710BLNR"]
    assert gmt["seen_count"] == 2 and sorted(gmt["groups"]) == ["Dubai Watches", "HK Dealers"]
    assert gmt["dealer_phone"] == "447700900001" and gmt["dealer_name"] == "Ali HK"
    assert gmt["currency"] == "HKD" and 10000 < gmt["price_base"] < 15000
    assert by["5711/1A"]["country"] == "HK" and by["5711/1A"]["condition"] == "USED"


def test_worker_processes_newest_pending_messages_first(monkeypatch):
    from datetime import datetime

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.db import Base

    isolated_engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                                    poolclass=StaticPool)
    Base.metadata.create_all(isolated_engine)
    sessions = sessionmaker(bind=isolated_engine, expire_on_commit=False)
    monkeypatch.setattr(worker, "SessionLocal", sessions)
    try:
        with sessions() as s:
            for wa_id, ts in (("old", datetime(2026, 9, 25, 12)),
                              ("new-a", datetime(2026, 9, 26, 12)),
                              ("new-b", datetime(2026, 9, 26, 12))):
                s.add(Message(wa_id=wa_id, group_id=G1, text="thanks", text_hash=wa_id, ts=ts))
            s.commit()

        for expected in ("new-b", "new-a", "old"):
            assert asyncio.run(worker.process_batch(limit=1)) == (1, 0)
            with sessions() as s:
                assert s.query(Message).filter_by(wa_id=expected).one().status == "SKIPPED"
    finally:
        isolated_engine.dispose()


def test_worker_batches_distinct_groups_and_reuses_cache(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from app.db import Base, Group, ParseCache

    isolated_engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                                    poolclass=StaticPool)
    Base.metadata.create_all(isolated_engine)
    sessions = sessionmaker(bind=isolated_engine, expire_on_commit=False)
    monkeypatch.setattr(worker, "SessionLocal", sessions)
    calls_made = []

    async def fake_batch(items, client=None, group_weights=None):
        calls_made.append((list(items), dict(group_weights)))
        return {item["id"]: [] for item in items}

    monkeypatch.setattr(worker, "extract_offers_batch", fake_batch)
    try:
        with sessions() as s:
            s.add_all([Group(id=G1, name="HK"), Group(id=G2, name="Dubai")])
            for wa_id, group, text, text_hash in (
                ("a", G1, "WTS Rolex 126710BLNR", "shared"),
                ("b", G2, "WTS Rolex 126710BLNR", "shared"),
                ("c", G2, "WTB AP 15510ST", "different"),
                ("d", G1, "job offer £20k", "chat"),
            ):
                s.add(Message(wa_id=wa_id, group_id=group, text=text,
                              text_hash=text_hash, ts=utcnow()))
            s.commit()
        assert asyncio.run(worker.process_batch()) == (4, 0)
        assert len(calls_made) == 1
        assert len(calls_made[0][0]) == 2
        assert set(calls_made[0][1]) == {G1, G2}
        with sessions() as s:
            assert {m.wa_id: m.status for m in s.query(Message)} == {
                "a": "NO_OFFERS", "b": "NO_OFFERS", "c": "NO_OFFERS", "d": "SKIPPED"}
            assert s.query(ParseCache).count() == 2
            s.add(Message(wa_id="e", group_id=G1, text="WTB AP 15510ST",
                          text_hash="different", ts=utcnow()))
            s.commit()
        assert asyncio.run(worker.process_batch()) == (1, 0)
        assert len(calls_made) == 1
    finally:
        isolated_engine.dispose()

def test_search_filters(client):
    q = lambda **p: client.get("/api/offers", headers=STAFF, params=p).json()["total"]
    assert q(q="126710 blnr") == 1
    assert q(q="126710-BLNR") == 1
    assert q(q="nautilus") == 1
    assert q(direction="WTB") == 0
    assert q(max_price=20000) == 1
    assert q(condition="USED") == 1
    assert q(set_type="FULL_SET") == 1
    assert q(group=G2) == 2  # the reposted message contains two separate watches
    assert q(sort="price", order="asc")== 2
    f = client.get("/api/facets", headers=STAFF).json()
    assert set(f["brands"]) == {"Rolex", "Patek Philippe"} and f["countries"] == ["HK"]
    oid = client.get("/api/offers", headers=STAFF).json()["items"][0]["id"]
    assert "126710BLNR" in client.get(f"/api/offers/{oid}", headers=STAFF).json()["original_message"]

def test_backfill(client):
    r = client.post("/admin/api/groups/backfill", headers=ADMIN, json={"group_id": G1}).json()
    assert r == {"fetched": 3, "stored": 2, "photos": 0}
    assert client.post("/admin/api/groups/backfill", headers=ADMIN, json={"group_id": FAM}).status_code == 400
    asyncio.run(worker.process_batch())
    assert client.get("/api/offers", headers=STAFF, params={"direction": "WTB"}).json()["total"] == 1
    s = client.get("/admin/api/stats", headers=ADMIN).json()
    assert s["messages_by_status"]["SKIPPED"] == 2 and s["offers_total"] == 3

def test_prefilter():
    yes = ["WTB 126500LN", "5711 hkd 1.2m", "RM 67-01 available", "AP 15510 blue fs",
           "Daytona panda new card 180k", "126710BLNR", "🇦🇪 Patek 5711/1A 2019 used",
           "Omega Speedmaster full set"]
    no = ["ok", "thanks bro 🙏", "good morning all", "who is in HK next week?", "😂😂",
          "selling my car for $500", "need a room for 2024", "available tomorrow at 3",
          "job offer USD 10k", "WTS", "the 2024 sales forecast is ready"]
    assert all(parser.looks_like_offer(t) for t in yes), [t for t in yes if not parser.looks_like_offer(t)]
    assert not any(parser.looks_like_offer(t) for t in no), [t for t in no if parser.looks_like_offer(t)]

def test_normalize_variants():
    from app.ingest import normalize
    lid = {"id": "a", "from": G1, "body": "x", "participant": "123@lid",
           "_data": {"key": {"participantAlt": "447700900009@s.whatsapp.net"}, "pushName": "Z"}}
    assert normalize(lid)["sender_phone"] == "447700900009"
    mine = {"id": "b", "from": "447@s.whatsapp.net", "to": G1, "fromMe": True, "body": "WTS"}
    assert normalize(mine)["group_id"] == G1
    assert normalize({"id": "c", "from": G1, "body": ""}) is None


# v2 feature regressions. All network and filesystem side effects stay inside
# the test directory or are replaced with in-process fakes.
import datetime as dt
import io

_image_buffer = io.BytesIO()
Image.new("RGB", (800, 600), (30, 90, 60)).save(_image_buffer, "PNG")
PNG = _image_buffer.getvalue()
SENT = []


class _FakeHttpx:
    @staticmethod
    def AsyncClient(**kwargs):
        return _httpx.AsyncClient(
            transport=_httpx.MockTransport(lambda request: _httpx.Response(200, content=PNG))
        )


@pytest.fixture(scope="module", autouse=True)
def _patch_external_services():
    original_httpx = media_mod.httpx
    original_telegram = alerts_mod.send_telegram
    media_mod.httpx = _FakeHttpx

    async def fake_telegram(text, photo_path=None, chat_id=None):
        SENT.append(text)
        return True

    alerts_mod.send_telegram = fake_telegram
    yield
    media_mod.httpx = original_httpx
    alerts_mod.send_telegram = original_telegram


def ref_offer(price, **kwargs):
    return [{
        "direction": "WTS", "brand": "Rolex", "family": "Submariner",
        "reference": "124060", "condition": "NEW", "set_type": "FULL_SET",
        "year": 2024, "price": price, "currency": "GBP",
        "source_text": f"124060 {price}", **kwargs,
    }]


def test_save_offers_returns_count_and_new_rows(client):
    timestamp = utcnow()
    with SessionLocal() as session:
        message = Message(
            wa_id="save-offers-return-contract", group_id=G1,
            sender_phone="447700900123", sender_name="Unit Dealer",
            text="WTS 124060 9000", text_hash="save-offers-return-contract",
            ts=timestamp,
        )
        session.add(message)
        session.flush()
        result = worker.save_offers(session, message, ref_offer(9000))
        assert isinstance(result, tuple) and len(result) == 2
        count, created = result
        assert count == 1 and len(created) == 1
        assert created[0].dealer_key == "447700900123"
        session.rollback()


def test_named_staff_login(client):
    assert client.get("/api/me", headers=ALI).json()["user"] == "ali"
    bad = {"Authorization": "Basic " + base64.b64encode(b"ali:bad").decode()}
    assert client.get("/api/me", headers=bad).status_code == 401
    assert client.get("/api/me", headers=STAFF).status_code == 200


def test_advisor_uses_recent_comps_and_handles_no_data(client):
    with SessionLocal() as session:
        for i, price in enumerate([13000, 13400, 13800, 14200, 14600, 1400]):
            session.add(Offer(
                message_id=100 + i, group_id=G1, groups_seen=G1, direction="WTS",
                brand="Rolex", reference="126710BLNR", reference_norm="126710BLNR",
                condition="NEW", set_type="FULL_SET", year=2023, price=price,
                currency="GBP", price_base=price, country="GB",
                dealer_phone=f"advisor-{i}", fingerprint=f"advisor-{i}",
                first_seen_at=utcnow(), last_seen_at=utcnow() - dt.timedelta(days=i),
            ))
        session.add(Offer(
            message_id=200, group_id=G1, groups_seen=G1, direction="WTB",
            reference="126710BLNR", reference_norm="126710BLNR", dealer_phone="advisor-buyer",
            fingerprint="advisor-buyer", first_seen_at=utcnow(), last_seen_at=utcnow(),
        ))
        session.commit()

    response = client.post("/api/advise", headers=STAFF, json={
        "reference": "126710-blnr", "condition": "USED",
        "set_type": "WATCH_ONLY", "asking_price": 20000,
    })
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["watch"]["family"] == "GMT-Master II"
    assert result["comparables"] >= 5 and result["market_low"] > 5000
    assert result["offer"] < result["max_price"] < result["quick_resale"] < 13000
    assert result["asking"]["code"] == "WALK_AWAY"

    no_data = client.post("/api/advise", headers=STAFF, json={"reference": "99999"})
    assert no_data.status_code == 200 and no_data.json()["verdict"] == "NO_DATA"
    assert client.post("/api/advise", json={"reference": "x"}).status_code == 401


def test_market_deal_photo_alert_and_reference_history(client):
    with SessionLocal() as session:
        for i, price in enumerate([10300, 10400, 10500, 10600, 10700]):
            session.add(Offer(
                message_id=300 + i, group_id=G1, groups_seen=G1, direction="WTS",
                brand="Rolex", family="Submariner", reference="124060",
                reference_norm="124060", condition="NEW", set_type="FULL_SET", year=2024,
                price=price, currency="GBP", price_base=price, dealer_key=f"market-{i}",
                dealer_phone=f"market-{i}", fingerprint=f"market-{i}",
                first_seen_at=utcnow(), last_seen_at=utcnow(),
            ))
        session.commit()

    alert_response = client.post("/api/alerts", headers=ALI, json={
        "name": "Cheap Subs", "query": {"q": "124060", "max_price": 9500, "period": "2w"},
    })
    assert alert_response.status_code == 200, alert_response.text
    alert_id = alert_response.json()["id"]

    timestamp = int(time.time())
    text = "WTS 124060 2024 N FS £9,000"
    EXTRA[text] = ref_offer(9000)
    client.post("/webhook/waha?token=sec", json=payload(300, G1, text, ts=timestamp))
    photo_result = client.post(
        "/webhook/waha?token=sec",
        json=payload(301, G1, "", ts=timestamp + 20, photo=True),
    ).json()
    assert photo_result["photo"] is True and photo_result["stored"] is False
    client.post(
        "/webhook/waha?token=sec",
        json=payload(302, G1, "", sender="447700900777@s.whatsapp.net",
                     ts=timestamp + 10, photo=True),
    )

    SENT.clear()
    assert asyncio.run(worker.process_batch()) == (1, 0)
    data = client.get("/api/offers", headers=STAFF, params={
        "q": "124060", "deals_only": "true",
    }).json()
    assert data["total"] == 1
    offer = data["items"][0]
    assert offer["deal"] == "DEAL" and -0.16 < offer["market_pct"] < -0.12
    assert offer["photo_id"] and offer["photo_kind"] == "EXACT"
    image = client.get(f"/media/{offer['photo_id']}/thumb", headers=STAFF)
    assert image.status_code == 200 and image.headers["content-type"] == "image/jpeg"
    assert client.get(f"/media/{offer['photo_id']}/thumb").status_code == 401
    assert any("Cheap Subs" in message and "124060" in message for message in SENT)

    alert = next(
        item for item in client.get("/api/alerts", headers=STAFF).json()["alerts"]
        if item["id"] == alert_id
    )
    assert alert["hit_count"] == 1 and alert["created_by"] == "ali"
    assert "period" not in alert["query"]
    matches = client.get(f"/api/alerts/{alert_id}/matches", headers=STAFF).json()
    assert matches[0]["id"] == offer["id"]
    detail = client.get(f"/api/offers/{offer['id']}", headers=STAFF).json()
    assert detail["photos"] == [offer["photo_id"]]
    history = client.get("/api/refs/124060", headers=STAFF).json()
    assert len(history["history"]) == 26 and history["history"][-1]["count"] >= 6
    assert history["family"] == "Submariner"


def test_captioned_photo_and_reference_photo_fallback(client):
    text = "WTS 124060 2023 watch only 8,800"
    EXTRA[text] = ref_offer(8800, set_type="WATCH_ONLY", year=2023)
    client.post(
        "/webhook/waha?token=sec",
        json=payload(310, G2, text, photo=True,
                     sender="447700900555@s.whatsapp.net", name="Bob"),
    )
    asyncio.run(worker.process_batch())
    items = client.get("/api/offers", headers=STAFF, params={"q": "124060"}).json()["items"]
    bob = next(item for item in items if item["dealer_name"] == "Bob")
    assert bob["photo_kind"] == "EXACT"
    comps = [
        item for item in items
        if item["dealer_key"] and item["dealer_key"].startswith("market-")
    ]
    assert comps and all(
        item["photo_kind"] == "REFERENCE" and item["photo_id"] for item in comps
    ), [(item["dealer_key"], item["photo_kind"], item["photo_id"]) for item in comps]


def test_staff_tracking_hide_handled(client):
    offers = client.get("/api/offers", headers=STAFF, params={
        "q": "124060", "deals_only": "true",
    }).json()["items"]
    offer_id = offers[0]["id"]
    response = client.post(
        f"/api/offers/{offer_id}/track", headers=ALI,
        json={"status": "contacted", "note": "offered 8.5k"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "CONTACTED" and response.json()["by"] == "ali"
    updated = client.get("/api/offers", headers=STAFF, params={"q": "124060"}).json()["items"]
    item = next(row for row in updated if row["id"] == offer_id)
    assert item["track"]["by"] == "ali" and item["track"]["note"] == "offered 8.5k"
    hidden = client.get("/api/offers", headers=STAFF, params={
        "q": "124060", "hide_handled": "true",
    }).json()["items"]
    assert offer_id not in [row["id"] for row in hidden]
    invalid = client.post(
        f"/api/offers/{offer_id}/track", headers=ALI,
        json={"status": "bogus"},
    )
    assert invalid.status_code == 400


def test_dealer_rating_blocks_default_feed(client):
    dealers = client.get("/api/dealers", headers=STAFF).json()
    ali = next(dealer for dealer in dealers if dealer["key"] == "447700900001")
    assert ali["listings"] >= 3 and ali["name"] == "Ali HK"

    before = client.get("/api/offers", headers=STAFF, params={"period": "all"}).json()["total"]
    update = client.put(
        "/api/dealers/447700900001", headers=ALI,
        json={"rating": "BLOCKED", "note": "test block"},
    )
    assert update.status_code == 200 and update.json()["ok"]
    profile = client.get("/api/dealers/447700900001", headers=STAFF).json()
    assert profile["rating"] == "BLOCKED" and profile["updated_by"] == "ali"
    after = client.get("/api/offers", headers=STAFF, params={"period": "all"}).json()["total"]
    assert after < before
    visible = client.get("/api/offers", headers=STAFF, params={
        "period": "all", "show_blocked": "true",
    }).json()["total"]
    assert visible == before
    client.put(
        "/api/dealers/447700900001", headers=ALI,
        json={"rating": "TRUSTED", "note": "unblocked"},
    )
    rows = client.get("/api/offers", headers=STAFF, params={"q": "126710"}).json()["items"]
    row = next(item for item in rows if item["dealer_key"] == "447700900001")
    assert row["dealer_rating"] == "TRUSTED"


def test_stock_matching_import_and_status(client):
    created = client.post("/api/stock", headers=ALI, json={
        "reference": "126500 LN", "description": "Daytona white",
        "condition": "new", "set_type": "full set", "asking_price": 24000,
    })
    assert created.status_code == 200, created.text
    stock_id = created.json()["id"]
    SENT.clear()
    client.post("/webhook/waha?token=sec", json=payload(
        320, G2, "WTB 126500LN any year cash",
        sender="447700900888@s.whatsapp.net", name="Buyer",
    ))
    asyncio.run(worker.process_batch())
    assert any("📦" in text for text in SENT)

    stock = client.get("/api/stock", headers=STAFF).json()["items"]
    item = next(row for row in stock if row["id"] == stock_id)
    assert item["reference_norm"] == "126500LN"
    assert item["condition"] == "NEW" and item["set_type"] == "FULL_SET"
    assert item["wtb_dealers_30d"] >= 1
    matches = client.get(f"/api/stock/{stock_id}/matches", headers=STAFF).json()
    assert matches[0]["direction"] == "WTB"
    wanted = client.get("/api/offers", headers=STAFF, params={
        "direction": "WTB", "q": "126500",
    }).json()["items"]
    assert wanted and all(row["in_stock"] for row in wanted)

    imported = client.post("/api/stock/import", headers=ALI, json={
        "csv": "reference,description,condition,set_type,year,asking_price\n"
               '5711/1A-010,Nautilus,used,full set,2019,"98,000"\n'
               ",missing ref,,,,\n",
    })
    assert imported.status_code == 200
    assert imported.json()["added"] == 1 and len(imported.json()["skipped"]) == 1
    updated = client.put(
        f"/api/stock/{stock_id}", headers=ALI,
        json={"reference": "126500LN", "status": "SOLD"},
    )
    assert updated.status_code == 200
    assert stock_id not in [
        row["id"] for row in client.get("/api/stock", headers=STAFF).json()["items"]
    ]


def test_digest_preview_and_v2_pages(client):
    digest = client.get("/api/digest/preview", headers=STAFF)
    assert digest.status_code == 200
    assert "Watch market summary" in digest.json()["text"]
    assert digest.json()["data"]["new_offers"] >= 1
    for page in ("/", "/advisor", "/alerts", "/dealers", "/stock"):
        assert client.get(page, headers=ALI).status_code == 200
    assert client.get("/static/common.js").status_code == 200
    assert client.post("/api/notify/test", headers=STAFF).status_code == 401


def test_demo_database_config_isolated_from_configured_live_url():
    """Demo config selects its own SQLite URL and never opens the configured DB."""
    code = (
        "import json; from app.config import settings; "
        "print(json.dumps({'demo': settings.DEMO_MODE, 'url': settings.DATABASE_URL}))"
    )
    env = os.environ.copy()
    env.update(
        WATCHFEED_DEMO="true",
        DATABASE_URL="sqlite:///guard-live-database.sqlite",
        PYTHONPATH=str(Path(__file__).resolve().parents[1]),
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        env=env, check=True, capture_output=True, text=True,
    )
    assert '"demo": true' in result.stdout
    assert '"url": "sqlite:///./watchfeed_preview.db"' in result.stdout
    assert not (Path(__file__).resolve().parents[1] / "guard-live-database.sqlite").exists()


def test_v2_migration_preserves_v1_data_on_temporary_database():
    """Exercise the additive migration only against a throwaway v1-format DB."""
    with tempfile.TemporaryDirectory(prefix="watchfeed-migration-test-") as folder:
        database = Path(folder) / "v1.sqlite"
        with sqlite3.connect(database) as connection:
            connection.execute(
                "CREATE TABLE offers (id INTEGER PRIMARY KEY, "
                "dealer_phone VARCHAR(40), dealer_name VARCHAR(200))"
            )
            connection.execute(
                "INSERT INTO offers (id, dealer_phone, dealer_name) VALUES (1, '4477009', 'Dealer')"
            )

        env = os.environ.copy()
        env.update(
            WATCHFEED_DEMO="false",
            DATABASE_URL=f"sqlite:///{database}",
            PYTHONPATH=str(Path(__file__).resolve().parents[1]),
        )
        result = subprocess.run(
            [sys.executable, "-m", "app.migrate_v2"],
            cwd=Path(__file__).resolve().parents[1],
            env=env, check=True, capture_output=True, text=True,
        )
        assert "schema upgrade complete" in result.stdout
        with sqlite3.connect(database) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(offers)")}
            saved = connection.execute(
                "SELECT dealer_phone, dealer_name, dealer_key FROM offers WHERE id=1"
            ).fetchone()
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        assert {
            "dealer_key", "photo_id", "photo_kind", "market_pct",
            "track_status", "track_by", "track_at", "track_note",
        } <= columns
        assert saved == ("4477009", "Dealer", "4477009")
        assert {"photos", "dealers", "alerts", "alert_hits", "stock", "kv"} <= tables


def test_ai_meter_batch_attribution_and_admin_access(client, monkeypatch):
    from decimal import Decimal
    from app.db import AIUsage, AIUsageGroup

    monkeypatch.setattr(parser.settings, "ANTHROPIC_API_KEY", "test-only")
    items = [{"id": "1", "group": "HK Dealers", "text": "WTS Rolex 126710BLNR 2024"},
             {"id": "2", "group": "Dubai Watches", "text": "WTB AP 15510ST"}]

    def answer(request):
        sent = __import__("json").loads(request.content)
        assert sent["tool_choice"]["name"] == "record_batch"
        assert sent["messages"][0]["content"].count("Message id:") == 2
        return _httpx.Response(200, json={
            "model": "claude-haiku-4-5-20251001", "usage": {
                "input_tokens": 1000, "output_tokens": 200,
                "cache_creation_input_tokens": 100, "cache_read_input_tokens": 50},
            "stop_reason": "tool_use", "content": [{"type": "tool_use", "name": "record_batch",
                "input": {"messages": [
                    {"id": "1", "offers": [{"direction": "WTS", "reference": "126710BLNR",
                                           "source_text": items[0]["text"]}]},
                    {"id": "2", "offers": []},
                ]}}],
        })

    async def run():
        async with _httpx.AsyncClient(transport=_httpx.MockTransport(answer)) as ai_client:
            return await parser.extract_offers_batch(items, ai_client, {G1: 3, G2: 1})

    parsed = asyncio.run(run())
    assert parsed["1"][0]["reference"] == "126710BLNR" and parsed["2"] == []
    with SessionLocal() as s:
        usage = s.query(AIUsage).order_by(AIUsage.id.desc()).first()
        allocations = s.query(AIUsageGroup).filter_by(usage_id=usage.id).all()
        assert usage.message_count == 2
        assert usage.estimated_usd == Decimal("0.00213000")
        assert sum((entry.estimated_usd for entry in allocations), Decimal("0")) == usage.estimated_usd
        assert {entry.group_id for entry in allocations} == {G1, G2}
    assert client.get("/admin/api/ai-usage", headers=STAFF).status_code == 401
    result = client.get("/admin/api/ai-usage", headers=ADMIN).json()
    assert result["windows"]["24h"]["estimated_usd"] >= .00213
    assert result["windows"]["7d"]["requests"] >= 1
    assert {group["id"] for group in result["groups_7d"]} >= {G1, G2}


def test_batch_invalid_response_is_metered_and_split(client, monkeypatch):
    from app.db import AIUsage

    monkeypatch.setattr(parser.settings, "ANTHROPIC_API_KEY", "test-only")
    monkeypatch.setattr(worker, "extract_offers_batch", parser.extract_offers_batch)
    seen = []

    def answer(request):
        sent = __import__("json").loads(request.content)
        content = sent["messages"][0]["content"]
        ids = __import__("re").findall(r"Message id: (\d+)", content)
        seen.append(ids)
        # Multi-message output is truncated and billable; single-message retries succeed.
        truncated = len(ids) > 1
        return _httpx.Response(200, json={
            "model": "claude-haiku-4-5-20251001",
            "usage": {"input_tokens": 50, "output_tokens": 10},
            "stop_reason": "max_tokens" if truncated else "tool_use",
            "content": [] if truncated else [{"type": "tool_use", "name": "record_batch",
                "input": {"messages": [{"id": ids[0], "offers": []}]}}],
        })

    async def run():
        async with _httpx.AsyncClient(transport=_httpx.MockTransport(answer)) as ai_client:
            messages = [Message(id=i, group_id=group, text=f"WTS Rolex {i} 126710BLNR",
                                text_hash=str(i), wa_id=str(i), ts=utcnow())
                        for i, group in ((1, G1), (2, G2))]
            return await worker._parse_chunk(messages, {G1: "HK", G2: "Dubai"},
                                             {"1": {G1}, "2": {G2}}, ai_client,
                                             asyncio.Semaphore(2))

    with SessionLocal() as s:
        before = s.query(AIUsage).count()
    result = asyncio.run(run())
    assert result == {"1": ("OK", []), "2": ("OK", [])}
    assert seen == [["1", "2"], ["1"], ["2"]]
    with SessionLocal() as s:
        assert s.query(AIUsage).count() - before == 3  # includes the paid truncated response


def test_bad_batch_mapping_counts_usage_and_rejects_misassigned_offer(client, monkeypatch):
    from app.db import AIUsage
    monkeypatch.setattr(parser.settings, "ANTHROPIC_API_KEY", "test-only")
    items = [{"id": "11", "group": "HK", "text": "Rolex 126710BLNR"},
             {"id": "12", "group": "Dubai", "text": "AP 15510ST"}]

    def wrong_id(request):
        return _httpx.Response(200, json={
            "model": "claude-haiku-4-5-20251001",
            "usage": {"input_tokens": 100, "output_tokens": 20},
            "stop_reason": "tool_use", "content": [{"type": "tool_use",
                "name": "record_batch", "input": {"messages": [
                    {"id": "11", "offers": []}, {"id": "99", "offers": []}]}}],
        })

    async def run():
        async with _httpx.AsyncClient(transport=_httpx.MockTransport(wrong_id)) as ai_client:
            return await parser.extract_offers_batch(items, ai_client, {G1: 1, G2: 1})

    with SessionLocal() as s:
        before = s.query(AIUsage).count()
    with pytest.raises(parser.InvalidBatch):
        asyncio.run(run())
    with SessionLocal() as s:
        assert s.query(AIUsage).count() == before + 1


def test_v3_usage_migration_requires_explicit_upgrade_and_preserves_data():
    with tempfile.TemporaryDirectory(prefix="watchfeed-v3-migration-") as folder:
        database = Path(folder) / "existing.sqlite"
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, text TEXT)")
            connection.execute("INSERT INTO messages VALUES (1, 'existing message')")
        env = os.environ.copy()
        env.update(WATCHFEED_DEMO="false", DATABASE_URL=f"sqlite:///{database}",
                   PYTHONPATH=str(APP_DIR))
        check = subprocess.run(
            [sys.executable, "-c", "from app.db import init_db; init_db()"],
            cwd=APP_DIR, env=env, capture_output=True, text=True)
        assert check.returncode != 0 and "migrate_v3" in check.stderr
        for _ in range(2):
            subprocess.run([sys.executable, "-m", "app.migrate_v3"], cwd=APP_DIR,
                           env=env, check=True, capture_output=True)
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT text FROM messages").fetchone()[0] == "existing message"
            assert {"ai_usage", "ai_usage_groups"} <= {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
