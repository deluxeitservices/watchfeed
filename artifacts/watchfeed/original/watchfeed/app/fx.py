"""Currency conversion. Rates held as 'units per 1 GBP' and refreshed from ECB (frankfurter)."""
import logging
from datetime import datetime
from typing import Optional

import httpx

from .config import settings

log = logging.getLogger("fx")

# Approximate fallbacks (units per 1 GBP) used until the first live refresh succeeds.
FALLBACK_PER_GBP = {
    "GBP": 1.0, "USD": 1.33, "EUR": 1.16, "HKD": 10.4, "CHF": 1.07, "SGD": 1.72,
    "JPY": 198.0, "CNY": 9.5, "AUD": 2.03, "CAD": 1.84, "THB": 43.0, "MYR": 5.6,
    "KRW": 1850.0, "INR": 117.0, "TRY": 55.0, "TWD": 40.0, "NZD": 2.25,
}
# USD-pegged currencies the ECB does not publish (units per 1 USD)
USD_PEGS = {"AED": 3.6725, "SAR": 3.75, "QAR": 3.64, "BHD": 0.376, "OMR": 0.3845, "USDT": 1.0}

ALIASES = {
    "$": "USD", "US$": "USD", "USDT": "USDT", "U": "USD", "DOLLAR": "USD",
    "£": "GBP", "POUND": "GBP", "GBP": "GBP",
    "€": "EUR", "EURO": "EUR", "EUR": "EUR",
    "HK$": "HKD", "HK": "HKD", "HKD": "HKD",
    "DHS": "AED", "DH": "AED", "DIRHAM": "AED", "AED": "AED",
    "SFR": "CHF", "CHF": "CHF", "S$": "SGD", "SGD": "SGD",
    "RMB": "CNY", "CNY": "CNY", "¥": "JPY", "YEN": "JPY", "JPY": "JPY",
}

_rates: dict = dict(FALLBACK_PER_GBP)
for _c, _peg in USD_PEGS.items():
    _rates[_c] = _peg * FALLBACK_PER_GBP["USD"]
updated_at: Optional[datetime] = None


def normalize_currency(cur: Optional[str]) -> Optional[str]:
    if not cur:
        return None
    c = cur.strip().upper()
    c = ALIASES.get(c, c)
    return c if len(c) in (3, 4) and c.isalpha() else None


async def refresh() -> None:
    global updated_at
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(settings.FX_URL)
            r.raise_for_status()
            data = r.json()
        rates = {k.upper(): float(v) for k, v in data.get("rates", {}).items()}
        rates["GBP"] = 1.0
        usd = rates.get("USD")
        if usd:
            for c, peg in USD_PEGS.items():
                rates[c] = peg * usd
        _rates.update(rates)
        updated_at = datetime.utcnow()
        log.info("FX rates refreshed (%d currencies)", len(rates))
    except Exception as e:  # keep fallbacks
        log.warning("FX refresh failed: %s", e)


def to_base(price: Optional[float], currency: Optional[str]) -> Optional[float]:
    if price is None or not currency:
        return None
    per_gbp = _rates.get(currency)
    base_per_gbp = _rates.get(settings.BASE_CURRENCY)
    if not per_gbp or not base_per_gbp:
        return None
    return round(price / per_gbp * base_per_gbp, 2)
