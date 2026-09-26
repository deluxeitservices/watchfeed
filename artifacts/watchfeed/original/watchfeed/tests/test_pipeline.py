import asyncio, base64, os, json, time
os.environ.update(DATABASE_URL="sqlite:///./test.db", STAFF_PASSWORD="s", ADMIN_PASSWORD="a",
                  WEBHOOK_SECRET="sec", WORKER_ENABLED="false", AUTO_ENABLE_NEW_GROUPS="false",
                  WATCHFEED_DEMO="false")
if os.path.exists("test.db"): os.remove("test.db")

import pytest
from fastapi.testclient import TestClient
from app import main, worker, parser, waha
from app.db import SessionLocal, Group, Message

ADMIN = {"Authorization": "Basic " + base64.b64encode(b"admin:a").decode()}
STAFF = {"Authorization": "Basic " + base64.b64encode(b"staff:s").decode()}
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

async def fake_extract(text, group_name=None, client=None):
    calls["n"] += 1
    return FAKE if "126710" in text else [{"direction": "WTB", "brand": "Rolex", "reference": "126500LN",
                                           "family": "Daytona", "source_text": text}]

def payload(i, chat, body, sender="447700900001@s.whatsapp.net", name="Ali HK", ts=None):
    return {"event": "message", "session": "default", "payload": {
        "id": f"false_{chat}_{i}", "timestamp": ts or int(time.time()), "from": chat, "fromMe": False,
        "participant": sender, "body": body, "hasMedia": False, "_data": {"pushName": name}}}

@pytest.fixture(scope="module")
def client():
    parser_orig = worker.extract_offers
    worker.extract_offers = fake_extract
    async def fake_groups():
        return [{"id": G1, "name": "HK Dealers", "participants": 200},
                {"id": G2, "name": "Dubai Watches", "participants": 90},
                {"id": FAM, "name": "Family", "participants": 8}]
    waha.list_groups = fake_groups
    async def fake_hist(chat_id, limit=300):
        return [payload(100, chat_id, "WTB 126500LN panda any year")["payload"],
                payload(101, chat_id, "thanks bro")["payload"],
                {"id": "x", "from": "447@c.us", "body": "private"}]
    waha.chat_messages = fake_hist
    with TestClient(main.app) as c:
        yield c
    worker.extract_offers = parser_orig

def test_auth(client):
    assert client.get("/").status_code == 401
    assert client.get("/", headers=STAFF).status_code == 200
    assert client.get("/admin", headers=STAFF).status_code == 401
    assert client.get("/admin", headers=ADMIN).status_code == 200
    assert client.post("/webhook/waha?token=bad", json={}).status_code == 403

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
    assert r == {"fetched": 3, "stored": 2}
    assert client.post("/admin/api/groups/backfill", headers=ADMIN, json={"group_id": FAM}).status_code == 400
    asyncio.run(worker.process_batch())
    assert client.get("/api/offers", headers=STAFF, params={"direction": "WTB"}).json()["total"] == 1
    s = client.get("/admin/api/stats", headers=ADMIN).json()
    assert s["messages_by_status"]["SKIPPED"] == 2 and s["offers_total"] == 3

def test_prefilter():
    yes = ["WTB 126500LN", "5711 hkd 1.2m", "RM 67-01 available", "AP 15510 blue fs", "Daytona panda new card 180k"]
    no = ["ok", "thanks bro 🙏", "good morning all", "who is in HK next week?", "😂😂"]
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
