"""AI extraction of WTS / WTB watch offers from raw WhatsApp dealer messages."""
import asyncio
import hashlib
import logging
import re
from typing import Optional

import httpx

from .ai_usage import record_usage
from .config import settings

log = logging.getLogger("parser")

# ---------------------------------------------------------------- prefilter
_WATCH_WORDS = re.compile(
    r"\b(rolex|patek|pp|audemars|ap|richard ?mille|rm|vacheron|vc|cartier|omega|tudor|"
    r"iwc|breitling|hublot|panerai|jlc|jaeger|lange|fp ?journe|mb&f|tag|zenith|"
    r"daytona|submariner|sub|gmt|datejust|dj|day-?date|dd|yacht|explorer|nautilus|aquanaut|"
    r"royal ?oak|offshore|santos|panthere|speedmaster|batman|pepsi|sprite|hulk|"
    r"speedy|seamaster)\b",
    re.I,
)
_REF = re.compile(r"\b(?:\d{5,6}[a-z]{0,6}|[a-z]{1,5}\d{4,}[a-z0-9]*|"
                  r"\d{4,5}/[a-z0-9][a-z0-9/\-]*|\d{4,5}[a-z]{2}\.[a-z0-9.]+)\b", re.I)
_SHORT_REF = re.compile(r"\b(?!19\d\d\b|20\d\d\b)\d{4}\b", re.I)
_PRICE = re.compile(r"[$£€¥]|\d+(?:[.,]\d+)?\s?[km]\b|\b\d{1,3}(?:[,.]\d{3})+\b", re.I)
_CURRENCY = re.compile(r"\b(?:hkd|usd|usdt|aed|eur|gbp|chf|sgd|dhs)\b", re.I)
_TRADE = re.compile(r"\b(?:fs|lf|ltb|sell|selling|buy|buying|looking|need|wanted|"
                    r"avail|available|stock)\b", re.I)
_WATCH_CONTEXT = re.compile(
    r"\b(?:watch(?:es)?|timepiece|wts|wtb|full ?set|dial|bezel|bracelet|unworn|lnib|bnib)\b",
    re.I,
)
_NON_WATCH = re.compile(r"\b(?:room|rent|rental|flat|apartment|postcode|licen[cs]e|"
                        r"driving|job|loan|delivery account|letting)\b", re.I)


def looks_like_offer(text: str) -> bool:
    t = (text or "").strip()
    if len(t) < 5:
        return False
    if _NON_WATCH.search(t):
        return False
    return bool(_WATCH_WORDS.search(t) or _REF.search(t)
                or (_SHORT_REF.search(t) and (_CURRENCY.search(t) or _WATCH_CONTEXT.search(t)))
                or (_WATCH_CONTEXT.search(t) and (_TRADE.search(t) or _PRICE.search(t))))


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

COMPACT_OFFERS = {"type": "array", "items": {"type": "object", "properties": {
    "l": {"type": "array", "items": {"type": "integer"}},
    "dir": {"type": "string", "enum": ["WTS", "WTB"]},
    "brand": {"type": "string"}, "fam": {"type": "string"}, "model": {"type": "string"},
    "ref": {"type": "string"}, "dial": {"type": "string"}, "case": {"type": "string"},
    "brace": {"type": "string"}, "dia": {"type": "boolean"},
    "cond": {"type": "string", "enum": ["N", "L", "U", "V"]},
    "set": {"type": "string", "enum": ["F", "W", "B", "P"]},
    "yr": {"type": "integer"}, "mo": {"type": "integer"}, "price": {"type": "number"},
    "cur": {"type": "string"}, "disc": {"type": "number"},
    "cc": {"type": "string"}, "city": {"type": "string"}, "note": {"type": "string"},
}, "required": ["l", "dir"]}}

BATCH_TOOL = {
    "name": "record_batch",
    "description": "Return one item for each message id, with every watch offer in that message.",
    "input_schema": {
        "type": "object",
        "properties": {
            "messages": {"type": "array", "items": {"type": "object", "properties": {
                "id": {"type": "string"},
                "offers": COMPACT_OFFERS,
            }, "required": ["id", "offers"]}},
        },
        "required": ["messages"],
    },
}
BATCH_PROMPT = """Extract every individual WTS/WTB watch offer from these numbered WhatsApp
dealer messages. Call record_batch exactly once, returning one entry for EACH id,
including empty offers lists. Never mix offers between ids. One watch line = one
offer. Use the exact numbered source line(s) in l, including all lines describing
that offer; do not attach an offer to a header alone. Non-watch chat = no offers.
Use short fields: dir=WTS/WTB, brand=canonical brand, fam=collection, model=nickname,
ref=manufacturer reference as written (never invent), dial=color, case=material,
brace=bracelet, dia=true only when stated, cond=N(new)/L(like new)/U(used)/V(vintage),
set=F(full set)/W(watch only)/B(box only)/P(papers only), yr=4-digit card year,
mo=month, price=full units (23.5k=23500), cur=ISO currency, disc=percent
vs retail if price absent, cc=ISO-2 watch location, city, note=short details.
Omit unknown values rather than returning null. Header/footer context such as
brand, location, currency, year or full set applies to the watch lines beneath
it when explicitly shared; never copy a condition from one watch line to another.
Keep distinct repeated watch lines as distinct offers. Output only via the tool."""

_COND = {"N": "NEW", "L": "LIKE_NEW", "U": "USED", "V": "VINTAGE"}
_SET = {"F": "FULL_SET", "W": "WATCH_ONLY", "B": "BOX_ONLY", "P": "PAPERS_ONLY"}


def _expand_compact(offer: dict, lines: list[str]) -> dict:
    nums = offer.get("l")
    if not isinstance(nums, list) or not nums or any(
        type(n) is not int or n < 1 or n > len(lines) for n in nums
    ):
        raise InvalidBatch("Offer contains missing or invalid source line numbers")
    if offer.get("dir") not in ("WTS", "WTB"):
        raise InvalidBatch("Offer has invalid direction")
    return {
        "direction": offer["dir"], "brand": offer.get("brand"), "family": offer.get("fam"),
        "model": offer.get("model"), "reference": offer.get("ref"),
        "dial_color": offer.get("dial"), "case_material": offer.get("case"),
        "bracelet": offer.get("brace"), "diamond_indices": bool(offer.get("dia")),
        "condition": _COND.get(offer.get("cond")), "set_type": _SET.get(offer.get("set")),
        "year": offer.get("yr"), "month": offer.get("mo"), "price": offer.get("price"),
        "currency": offer.get("cur"), "discount_pct": offer.get("disc"),
        "country": offer.get("cc"), "city": offer.get("city"), "notes": offer.get("note"),
        "source_text": "\n".join(lines[n - 1].strip() for n in nums), "_lines": nums,
    }

API_URL = "https://api.anthropic.com/v1/messages"


class ParseError(Exception):
    pass


class OversizedBatch(ParseError):
    pass


class InvalidBatch(ParseError):
    pass


async def _request(body: dict, client: Optional[httpx.AsyncClient],
                   group_weights: dict[str, int], message_count: int) -> dict:
    if not settings.ANTHROPIC_API_KEY:
        raise ParseError("ANTHROPIC_API_KEY not set")
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
            try:
                data = r.json()
            except ValueError as exc:
                record_usage(settings.ANTHROPIC_MODEL, None, group_weights, message_count)
                raise ParseError("LLM returned invalid JSON") from exc
            if not isinstance(data, dict):
                record_usage(settings.ANTHROPIC_MODEL, None, group_weights, message_count)
                raise ParseError("LLM returned a non-object response")
            # Record before inspecting content/stop_reason: truncated or malformed
            # successful responses have already consumed tokens.
            model = data.get("model")
            record_usage(model if isinstance(model, str) else settings.ANTHROPIC_MODEL,
                         data.get("usage"),
                         group_weights, message_count)
            return data
        raise ParseError("LLM unavailable after retries")
    finally:
        if own:
            await client.aclose()


def _offers_from_response(data: dict) -> list:
    if data.get("stop_reason") == "max_tokens":
        raise OversizedBatch("message too long for one extraction")
    for block in data.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == "record_offers":
            offers = block.get("input", {}).get("offers")
            if isinstance(offers, list):
                return offers
    raise ParseError("LLM did not return a valid offer list")


async def extract_offers(text: str, group_name: Optional[str] = None,
                         client: Optional[httpx.AsyncClient] = None) -> list:
    body = {
        "model": settings.ANTHROPIC_MODEL, "max_tokens": 8192,
        "system": SYSTEM_PROMPT, "tools": [OFFER_TOOL],
        "tool_choice": {"type": "tool", "name": "record_offers"},
        "messages": [{"role": "user", "content": f"WhatsApp group: {group_name or 'unknown'}\n\n"
                      f"Message:\n<<<\n{text[:settings.MAX_MESSAGE_CHARS]}\n>>>"}],
    }
    data = await _request(body, client, {}, 1)
    return _offers_from_response(data)


async def extract_offers_batch(items: list[dict], client: Optional[httpx.AsyncClient] = None,
                               group_weights: Optional[dict[str, int]] = None) -> dict[str, list]:
    """Items have stable string id, group and text fields. Reject ambiguous replies."""
    if not items or len(items) > 8:
        raise ValueError("Batch size must be between 1 and 8")
    body = {
        "model": settings.ANTHROPIC_MODEL, "max_tokens": 8192,
        "system": BATCH_PROMPT, "tools": [BATCH_TOOL],
        "tool_choice": {"type": "tool", "name": "record_batch"},
        "messages": [{"role": "user", "content": "\n\n".join(
            f"Message id: {item['id']}\nWhatsApp group: {item['group']}\n"
            f"Text:\n<<<\n" + "\n".join(
                f"L{n}: {line}" for n, line in enumerate(
                    item["text"][:settings.MAX_MESSAGE_CHARS].splitlines(), 1)
            ) + "\n>>>"
            for item in items)}],
    }
    data = await _request(body, client, group_weights or {}, len(items))
    if data.get("stop_reason") == "max_tokens":
        raise OversizedBatch("batch response reached the output token limit")
    expected = {str(item["id"]) for item in items}
    for block in data.get("content", []):
        if block.get("type") != "tool_use" or block.get("name") != "record_batch":
            continue
        entries = block.get("input", {}).get("messages")
        if not isinstance(entries, list):
            break
        result = {}
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("offers"), list):
                break
            key = entry.get("id")
            if key not in expected or key in result:
                break
            original = next(item for item in items if str(item["id"]) == key)
            lines = original["text"][:settings.MAX_MESSAGE_CHARS].splitlines()
            try:
                result[key] = [
                    _expand_compact(offer, lines) if isinstance(offer, dict) and "l" in offer
                    else offer for offer in entry["offers"]
                ]
            except (TypeError, ValueError, KeyError) as exc:
                raise InvalidBatch("Invalid compact offer") from exc
        else:
            if set(result) == expected:
                return result
        break
    raise InvalidBatch("LLM returned missing, duplicate or unknown message ids")
