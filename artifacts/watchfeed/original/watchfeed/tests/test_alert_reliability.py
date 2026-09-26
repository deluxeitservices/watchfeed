"""Focused durable-notification tests, isolated from the shared pipeline fixtures."""

import os
import subprocess
import sys
import textwrap
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1]


def run_isolated(tmp_path, source, *, suffix=""):
    env = os.environ.copy()
    env.update({
        "DATABASE_URL": f"sqlite:///{tmp_path / 'alerts.sqlite'}",
        "WATCHFEED_DEMO": "false",
        "STAFF_USER": "staff",
        "STAFF_PASSWORD": "test-staff",
        "ADMIN_USER": "admin",
        "ADMIN_PASSWORD": "test-admin",
        "WEBHOOK_SECRET": "test-webhook",
        "WORKER_ENABLED": "false",
        "MEDIA_DIR": str(tmp_path / "media"),
        "TELEGRAM_BOT_TOKEN": "test-token-must-not-leak",
        "TELEGRAM_CHAT_ID": "test-chat",
        "SMTP_HOST": "smtp.invalid",
        "SMTP_USER": "test-user",
        "SMTP_PASSWORD": "test-password",
        "SMTP_FROM": "watchfeed@example.test",
        "DIGEST_EMAILS": "staff@example.test",
        "DIGEST_HOUR": "8",
        "TIMEZONE": "UTC",
    })
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        cwd=APP_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    return result


def test_outbox_retries_only_failed_channel_after_restart(tmp_path):
    run_isolated(tmp_path, """
        import asyncio
        import json
        from app import alerts
        from app.db import Alert, AlertHit, Group, KV, Offer, SessionLocal, init_db, utcnow

        init_db()
        with SessionLocal() as s:
            s.add(Group(id="g1", name="Dealers", enabled=True))
            offer = Offer(message_id=1, group_id="g1", groups_seen="g1", direction="WTS",
                          brand="Rolex", family="Submariner", reference="124060",
                          reference_norm="124060", source_text="WTS Rolex 124060",
                          price=9000, currency="GBP", price_base=9000,
                          dealer_name="A dealer", dealer_key="dealer-1", dealer_phone="447000000001",
                          fingerprint="offer-fingerprint", first_seen_at=utcnow(), last_seen_at=utcnow())
            s.add(offer)
            s.flush()
            s.add(Alert(name="Rolex", query_json=json.dumps({"brand": "Rolex"}),
                        emails="staff@example.test", telegram=True, active=True))
            s.commit()
            offer_id = offer.id

        calls = {"telegram": 0, "email": 0}
        async def telegram(*args, **kwargs):
            calls["telegram"] += 1
            return True
        async def email(*args, **kwargs):
            calls["email"] += 1
            return False
        alerts.send_telegram, alerts.send_email = telegram, email
        assert asyncio.run(alerts.process_new_offers([offer_id])) == 1
        with SessionLocal() as s:
            states = [json.loads(row.value) for row in s.scalars(
                __import__("sqlalchemy").select(KV).where(KV.key.startswith(alerts.OUTBOX_PREFIX))).all()]
            assert sorted(state["status"] for state in states) == ["pending", "sent"]
            assert s.query(AlertHit).count() == 1
        assert calls == {"telegram": 1, "email": 1}
    """)

    # A new interpreter models an application restart. Telegram's acknowledged
    # delivery must remain deduplicated; only the failed email is retried.
    run_isolated(tmp_path, """
        import asyncio
        import json
        from datetime import timedelta
        from app import alerts
        from app.db import AlertHit, KV, SessionLocal, utcnow

        base = utcnow()
        alerts.utcnow = lambda: base + timedelta(seconds=31)
        calls = {"telegram": 0, "email": 0}
        async def telegram(*args, **kwargs):
            calls["telegram"] += 1
            return True
        async def email(*args, **kwargs):
            calls["email"] += 1
            return True
        alerts.send_telegram, alerts.send_email = telegram, email
        assert asyncio.run(alerts.retry_recent_offers()) == 1
        assert calls == {"telegram": 0, "email": 1}
        with SessionLocal() as s:
            assert s.query(AlertHit).count() == 1
            states = [json.loads(row.value) for row in s.scalars(
                __import__("sqlalchemy").select(KV).where(KV.key.startswith(alerts.OUTBOX_PREFIX))).all()]
            assert [state["status"] for state in states].count("sent") == 2
            assert not any(state["status"] == "pending" for state in states)
        assert asyncio.run(alerts.retry_recent_offers()) == 0
        assert calls == {"telegram": 0, "email": 1}
    """)


def test_retry_budget_is_bounded_and_sent_offer_recovery_is_time_limited(tmp_path):
    run_isolated(tmp_path, """
        import asyncio
        import json
        from datetime import timedelta
        from sqlalchemy import select
        from app import alerts
        from app.db import Alert, Group, KV, Offer, SessionLocal, init_db, utcnow

        init_db()
        base = utcnow()
        with SessionLocal() as s:
            s.add(Group(id="g1", name="Dealers", enabled=True))
            offer = Offer(message_id=1, group_id="g1", groups_seen="g1", direction="WTS",
                          brand="Rolex", reference="124060", reference_norm="124060",
                          source_text="WTS Rolex 124060", dealer_key="dealer-1",
                          fingerprint="offer-fingerprint", first_seen_at=base, last_seen_at=base)
            s.add(offer)
            s.flush()
            offer_id = offer.id
            s.add(Alert(name="Rolex", query_json=json.dumps({"brand": "Rolex"}),
                        emails="", telegram=True, active=True))
            s.commit()

        clock = [base]
        alerts.utcnow = lambda: clock[0]
        calls = {"n": 0}
        async def fail(*args, **kwargs):
            calls["n"] += 1
            return False
        alerts.send_telegram = fail
        asyncio.run(alerts.process_new_offers([offer_id]))
        # Advance past each exponential retry time while remaining inside the
        # 48-hour offer recovery window.
        for attempt in range(1, alerts.MAX_DELIVERY_ATTEMPTS):
            clock[0] += timedelta(seconds=min(3600, 30 * (2 ** (attempt - 1))) + 1)
            asyncio.run(alerts.dispatch_outbox())
        assert calls["n"] == alerts.MAX_DELIVERY_ATTEMPTS
        with SessionLocal() as s:
            states = [json.loads(row.value) for row in s.scalars(
                select(KV).where(KV.key.startswith(alerts.OUTBOX_PREFIX))).all()]
            assert len(states) == 1
            assert states[0]["status"] == "failed"
            assert states[0]["attempts"] == alerts.MAX_DELIVERY_ATTEMPTS
        asyncio.run(alerts.retry_recent_offers())
        assert calls["n"] == alerts.MAX_DELIVERY_ATTEMPTS

        clock[0] = base + timedelta(days=3)
        assert asyncio.run(alerts.retry_recent_offers()) == 0
        assert calls["n"] == alerts.MAX_DELIVERY_ATTEMPTS
    """)


def test_digest_retries_failed_channel_without_resending_success(tmp_path):
    run_isolated(tmp_path, """
        import asyncio
        import json
        from datetime import datetime, timezone, timedelta
        from sqlalchemy import select
        from app import alerts
        from app.db import KV, SessionLocal, init_db, utcnow

        init_db()
        calls = {"telegram": 0, "email": 0}
        async def telegram(*args, **kwargs):
            calls["telegram"] += 1
            return calls["telegram"] > 1
        async def email(*args, **kwargs):
            calls["email"] += 1
            return True
        alerts.send_telegram, alerts.send_email = telegram, email
        now = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
        assert asyncio.run(alerts.maybe_send_digest(now)) is True
        assert calls == {"telegram": 1, "email": 1}
        with SessionLocal() as s:
            states = [json.loads(row.value) for row in s.scalars(
                select(KV).where(KV.key.startswith(alerts.OUTBOX_PREFIX))).all()]
            assert sorted(state["status"] for state in states) == ["pending", "sent"]

        base = utcnow()
        alerts.utcnow = lambda: base + timedelta(seconds=31)
        assert asyncio.run(alerts.maybe_send_digest(now)) is True
        assert calls == {"telegram": 2, "email": 1}
        assert asyncio.run(alerts.maybe_send_digest(now)) is False
        assert calls == {"telegram": 2, "email": 1}
    """)


def test_telegram_exception_never_logs_the_bot_token(tmp_path):
    result = run_isolated(tmp_path, """
        import asyncio
        import httpx
        from app import alerts

        class FakeClient:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False
            async def post(self, url, **kwargs):
                raise httpx.ConnectError(f"request failed for {url}")

        alerts.httpx.AsyncClient = FakeClient
        assert asyncio.run(alerts.send_telegram("hello")) is False
        print("delivery failure returned safely")
    """)
    assert "delivery failure returned safely" in result.stdout
    assert "test-token-must-not-leak" not in result.stdout + result.stderr


def test_completed_rows_do_not_starve_due_rows_beyond_dispatch_limit(tmp_path):
    run_isolated(tmp_path, """
        import asyncio
        import json
        from sqlalchemy import select
        from app import alerts
        from app.db import KV, SessionLocal, init_db, utcnow

        init_db()
        now = utcnow().isoformat()
        with SessionLocal() as s:
            # These keys sort ahead of the due row and would consume a SQL LIMIT
            # if the dispatcher limited before checking delivery status.
            for i in range(125):
                s.add(KV(key=f"notify:{i:03d}", value=json.dumps({
                    "status": "sent", "created_at": now, "attempts": 1,
                })))
            s.add(KV(key="notify:zzz-due", value=json.dumps({
                "kind": "telegram",
                "telegram_text": "recover this offer",
                "identity": "recovery-test",
                "status": "pending",
                "created_at": now,
                "next_attempt_at": now,
                "attempts": 0,
            })))
            s.commit()

        calls = {"n": 0}
        async def deliver(item):
            calls["n"] += 1
            assert item["telegram_text"] == "recover this offer"
            return True
        alerts._deliver = deliver
        assert asyncio.run(alerts.dispatch_outbox(limit=100)) == 1
        assert calls["n"] == 1
        with SessionLocal() as s:
            item = json.loads(s.get(KV, "notify:zzz-due").value)
            assert item["status"] == "sent"
    """)


def test_concurrent_dispatchers_claim_a_job_only_once(tmp_path):
    run_isolated(tmp_path, """
        import asyncio
        import json
        from sqlalchemy import select
        from app import alerts
        from app.db import KV, SessionLocal, init_db, utcnow

        init_db()
        now = utcnow().isoformat()
        with SessionLocal() as s:
            s.add(KV(key="notify:one-job", value=json.dumps({
                "kind": "telegram",
                "telegram_text": "one delivery only",
                "status": "pending",
                "created_at": now,
                "next_attempt_at": now,
                "attempts": 0,
            })))
            s.commit()

        calls = {"n": 0}
        async def deliver(item):
            calls["n"] += 1
            await asyncio.sleep(0.05)
            return True
        alerts._deliver = deliver
        async def run_both():
            return await asyncio.gather(alerts.dispatch_outbox(), alerts.dispatch_outbox())
        results = asyncio.run(run_both())
        assert sum(results) == 1
        assert calls["n"] == 1
        with SessionLocal() as s:
            item = json.loads(s.get(KV, "notify:one-job").value)
            assert item["status"] == "sent"
            assert "lease_id" not in item and "lease_until" not in item
    """)