"""Test the AI extraction on a pasted message without WhatsApp:

    docker compose exec app python -m app.try_parse "WTS 🇭🇰 126710BLNR 2024 full set hkd128k"
    docker compose exec app python -m app.try_parse < message.txt
"""
import asyncio
import json
import sys

from .parser import extract_offers, looks_like_offer
from .worker import clean_offer


async def main() -> None:
    text = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else sys.stdin.read()
    print(f"prefilter: {'would parse' if looks_like_offer(text) else 'would SKIP (no offer detected)'}\n")
    offers = await extract_offers(text)
    print(json.dumps([clean_offer(o) for o in offers], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
