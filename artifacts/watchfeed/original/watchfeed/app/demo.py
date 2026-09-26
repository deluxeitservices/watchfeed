"""Clearly fictional, isolated records for the public development preview."""

from .db import Group, Message, SessionLocal, utcnow
from .parser import text_hash
from .worker import save_offers


EXAMPLES = [
    ("120000001@g.us", "Example HK Dealers", "Sample Dealer A",
     "WTS Rolex GMT-Master II 126710BLNR 2024 full set HKD 128k",
     {"direction": "WTS", "brand": "Rolex", "family": "GMT-Master II",
      "model": "Batman", "reference": "126710BLNR", "year": 2024,
      "set_type": "FULL_SET", "price": 128000, "currency": "HKD", "country": "HK"}),
    ("120000001@g.us", "Example HK Dealers", "Sample Dealer B",
     "WTS Patek Philippe Nautilus 5711/1A 2019 used HKD 1.05m",
     {"direction": "WTS", "brand": "Patek Philippe", "family": "Nautilus",
      "reference": "5711/1A", "condition": "USED", "year": 2019,
      "price": 1050000, "currency": "HKD", "country": "HK"}),
    ("120000002@g.us", "Example London Traders", "Sample Dealer C",
     "WTB Rolex Daytona 126500LN panda, new, 2025",
     {"direction": "WTB", "brand": "Rolex", "family": "Daytona",
      "model": "Panda", "reference": "126500LN", "condition": "NEW",
      "year": 2025, "country": "GB", "city": "London"}),
    ("120000002@g.us", "Example London Traders", "Sample Dealer D",
     "WTS Omega Speedmaster 310.30.42.50.01.001 2023 full set GBP 5,800",
     {"direction": "WTS", "brand": "Omega", "family": "Speedmaster",
      "reference": "310.30.42.50.01.001", "year": 2023,
      "set_type": "FULL_SET", "price": 5800, "currency": "GBP",
      "country": "GB", "city": "London"}),
]


def seed_demo() -> None:
    with SessionLocal() as s:
        if s.get(Group, EXAMPLES[0][0]):
            return
        now = utcnow()
        for idx, (gid, group_name, dealer, text, offer) in enumerate(EXAMPLES):
            group = s.get(Group, gid)
            if group is None:
                group = Group(id=gid, name=group_name, enabled=True,
                              message_count=0, last_message_at=now)
                s.add(group)
                s.flush()
            msg = Message(wa_id=f"demo-{idx}", group_id=gid, sender_name=dealer,
                          text=text, text_hash=text_hash(text), has_media=False,
                          ts=now, status="PARSED", offers_count=1)
            s.add(msg)
            s.flush()
            save_offers(s, msg, [{**offer, "source_text": text}])
            group.message_count += 1
        s.commit()