"""AI extraction of WTS / WTB watch offers from raw WhatsApp dealer messages."""
import asyncio
import hashlib
import logging
import re
from typing import Optional

import httpx

from .config import settings

log = logging.getLogger("parser")

# ---------------------------------------------------------------- prefilter
_KEYWORDS = re.compile(
    r"\b(wts|wtb|fs|lf|ltb|sell|selling|buy|buying|looking|need|wanted|avail|available|stock|"
    r"full ?set|box|papers|card|unworn|lnib|bnib|new|used|mint|net|"
    r"rolex|patek|pp|audemars|ap|richard ?mille|rm|vacheron|vc|cartier|omega|tudor|"
    r"iwc|breitling|hublot|panerai|jlc|jaeger|lange|fp ?journe|mb&f|tag|zenith|"
    r"daytona|submariner|sub|gmt|datejust|dj|day-?date|dd|yacht|explorer|nautilus|aquanaut|"
    r"royal ?oak|offshore|santos|panthere|speedmaster|batman|pepsi|sprite|hulk|"
    r"hkd|usd|usdt|aed|eur|gbp|chf|sgd|dhs)\b",
    re.I,
)
_REF = re.compile(r"\b\d{4,6}[a-z]{0,6}\b|\b[a-z]{1,4}[\-\s]?\d{2,}[a-z0-9\.\-]*\b", re.I)
_PRICE = re.compile(r"[$£€¥]|\d+(?:[.,]\d+)?\s?[km]\b|\b\d{1,3}(?:[,.]\d{3})+\b", re.I)


def looks_like_offer(text: str) -> bool:
    t = (text or "").strip()
    if len(t) < 5:
        return False
    has_kw = bool(_KEYWORDS.search(t))
    has_ref = bool(_REF.search(t))
    has_price = bool(_PRICE.search(t))
    return (has_ref and (has_kw or has_price)) or (has_kw and has_price)


def text_hash(text: str) -> str:
    norm = re.sub(r"\s+", " ", (text or "").strip().lower())
    return hashlib.sha256(norm.encode()).hexdigest()


# ---------------------------------------------------------------- LLM
SYSTEM_PROMPT = """You extract structured watch trading offers from WhatsApp messages posted in \
grey-market luxury watch dealer groups (Hong Kong, Dubai, Europe, US, UK dealers).

Return every individual WATCH offered for sale (WTS) or wanted (WTB) by calling record_offers.
One offer per distinct watch line. A message with 30 lines of watches = 30 offers.
If the message contains no concrete watch offer or request (chat, greetings, "ok", news, \
questions about shipping, jewellery/bags only), return an empty list.

DIRECTION
- WTB: "wtb", "want to buy", "looking for", "LF", "need", "searching", "buying", "who has", "anyone have".
- WTS: "wts", "for sale", "available", "in stock", price lists, "ready", or any listing with prices.
- A message can contain both sections; headers apply to the lines below them.

FIELDS
- brand: canonical name (Rolex, Patek Philippe, Audemars Piguet, Richard Mille, Vacheron Constantin, \
Cartier, Omega, Tudor, A. Lange & Söhne, F.P. Journe, IWC, Jaeger-LeCoultre, Hublot, Panerai, Breitling...). \
Infer from reference or nicknames when clear (126610LN -> Rolex; 5711 -> Patek Philippe; 15500ST -> Audemars Piguet; \
"AP" = Audemars Piguet; "PP" = Patek Philippe; "RM" = Richard Mille; "VC" = Vacheron Constantin).
- family: collection (Daytona, Submariner, GMT-Master II, Datejust, Day-Date, Nautilus, Aquanaut, Royal Oak, \
Royal Oak Offshore, Overseas, Santos, ...). model: nickname/variant if given (Batman, Pepsi, Panda, Hulk, Jubilee...).
- reference: the manufacturer reference as written, cleaned (e.g. "126710BLNR", "5711/1A-010", "15510ST.OO.1320ST.06"). \
Do not invent a reference that is not in the text.
- dial_color, case_material ("stainless steel", "yellow gold", "rose gold", "white gold", "platinum", "titanium", \
"two-tone", "ceramic"...), bracelet (Oyster, Jubilee, President, rubber, leather...). Rolex suffixes: LN/LV/BLNR/BLRO/\
GRNR/LB are bezel codes; "tt"/"rolesor" = two-tone.
- diamond_indices: true if diamond markers/dial ("dia", "diamond dial", "G dial", "baguette") are stated.
- condition: NEW ("new", "N", "BNIB", "unworn", "brand new", "NOS", "sticker"), LIKE_NEW ("LNIB", "like new", \
"mint", "99%", "worn once"), USED ("used", "pre-owned", "U", "good condition", "polished"), VINTAGE (pre-1990 watches). \
null if not stated.
- set_type: FULL_SET ("full set", "FS", "box and papers", "B&P", "complete"), WATCH_ONLY ("watch only", "naked", \
"no box no papers"), BOX_ONLY (watch + box, no papers), PAPERS_ONLY (watch + papers/card, no box). null if not stated.
- year / month: warranty-card or production date. "2023", "23y", "03/2024", "card 2022", "new card" (= current year). \
Two-digit years mean 20xx.
- price: the number in full units. "hkd541k" = 541000, "$23.5k" = 23500, "1.2m" = 1200000, "45,500" = 45500. \
Never guess a price that is not written. If only a discount vs retail is given ("-12%", "retail+15%"), leave price null \
and set discount_pct (negative for below retail, positive for premium).
- currency: ISO code (USD, HKD, EUR, GBP, AED, CHF, SGD, JPY, CNY, USDT). "$" alone = USD unless context is HK \
(then usually HKD if the amounts are large, e.g. $250,000 for a Submariner). "k" is a multiplier, not a currency. \
Currency written in a header applies to all lines below it.
- country: ISO-3166 alpha-2 of the watch location from flags (🇭🇰=HK, 🇦🇪=AE, 🇬🇧=GB, 🇺🇸=US, 🇨🇭=CH, 🇸🇬=SG, 🇯🇵=JP, \
🇩🇪=DE, 🇫🇷=FR, 🇮🇹=IT, 🇨🇳=CN) or city/country names ("HK", "Dubai" -> AE, "London" -> GB). city if stated.
- notes: short extra info worth keeping (e.g. "tag attached", "stickers", "sold out", "can deliver London").
- source_text: the exact line(s) from the message that describe this offer, copied verbatim.

Shared context applies to every line under it ONLY when written as a header/footer line on its own (brand headers, \
flags, "all HKD", "all full set", "all 2024", "prices in USD"). A condition, set or year written on one watch's line \
belongs to that line only — never copy it to the next watch. If a line does not state condition or set, leave it null.
Keep one offer per line even if lines repeat the same reference with different dials/prices.
Output only via the tool."""

_nullable = lambda t: {"type": [t, "null"]}  # noqa: E731

OFFER_TOOL = {
    "name": "record_offers",
    "description": "Record every watch WTS/WTB offer found in the message (empty list if none).",
    "input_schema": {
        "type": "object",
        "properties": {
            "offers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "direction": {"type": "string", "enum": ["WTS", "WTB"]},
                        "brand": _nullable("string"),
                        "family": _nullable("string"),
                        "model": _nullable("string"),
                        "reference": _nullable("string"),
                        "dial_color": _nullable("string"),
                        "case_material": _nullable("string"),
                        "bracelet": _nullable("string"),
                        "diamond_indices": {"type": "boolean"},
                        "condition": {"type": ["string", "null"],
                                      "enum": ["NEW", "LIKE_NEW", "USED", "VINTAGE", None]},
                        "set_type": {"type": ["string", "null"],
                                     "enum": ["FULL_SET", "WATCH_ONLY", "BOX_ONLY", "PAPERS_ONLY", None]},
                        "year": _nullable("integer"),
                        "month": _nullable("integer"),
                        "price": _nullable("number"),
                        "currency": _nullable("string"),
                        "discount_pct": _nullable("number"),
                        "country": _nullable("string"),
                        "city": _nullable("string"),
                        "notes": _nullable("string"),
                        "source_text": {"type": "string"},
                    },
                    "required": ["direction", "source_text"],
                },
            }
        },
        "required": ["offers"],
    },
}

API_URL = "https://api.anthropic.com/v1/messages"


class ParseError(Exception):
    pass


async def extract_offers(text: str, group_name: Optional[str] = None,
                         client: Optional[httpx.AsyncClient] = None) -> list:
    if not settings.ANTHROPIC_API_KEY:
        raise ParseError("ANTHROPIC_API_KEY not set")
    body = {
        "model": settings.ANTHROPIC_MODEL,
        "max_tokens": 8192,
        "system": SYSTEM_PROMPT,
        "tools": [OFFER_TOOL],
        "tool_choice": {"type": "tool", "name": "record_offers"},
        "messages": [{
            "role": "user",
            "content": f"WhatsApp group: {group_name or 'unknown'}\n\nMessage:\n<<<\n"
                       f"{text[:settings.MAX_MESSAGE_CHARS]}\n>>>",
        }],
    }
    headers = {
        "x-api-key": settings.ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    own = client is None
    client = client or httpx.AsyncClient(timeout=180)
    try:
        for attempt in range(5):
            try:
                r = await client.post(API_URL, json=body, headers=headers)
            except httpx.TransportError as e:
                log.warning("LLM transport error: %s", e)
                await asyncio.sleep(3 * 2 ** attempt)
                continue
            if r.status_code in (429, 500, 502, 503, 504, 529):
                wait = float(r.headers.get("retry-after") or 3 * 2 ** attempt)
                log.warning("LLM %s, retrying in %.0fs", r.status_code, wait)
                await asyncio.sleep(min(wait, 60))
                continue
            if r.status_code >= 400:
                raise ParseError(f"LLM HTTP {r.status_code}: {r.text[:300]}")
            data = r.json()
            if data.get("stop_reason") == "max_tokens":
                raise ParseError("message too long for one extraction")
            for block in data.get("content", []):
                if block.get("type") == "tool_use":
                    offers = block.get("input", {}).get("offers", [])
                    return offers if isinstance(offers, list) else []
            return []
        raise ParseError("LLM unavailable after retries")
    finally:
        if own:
            await client.aclose()
