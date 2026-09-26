"""Buy Advisor: what should we pay a private seller for this watch, and will it resell quickly?

Method (deliberately simple and explainable to staff):
1. Comparables = WTS dealer asks for the same reference in the last 30 days (widened to 90 if thin).
2. Each comparable is converted to GBP and adjusted to the customer's condition + set
   (e.g. a full-set ask is scaled down for a watch-only customer piece).
3. Outliers (typos, misread currencies) are trimmed.
4. Quick-resale price = lower quartile of adjusted dealer asks (you must undercut to sell fast).
   Target offer   = quick-resale price minus target margin.
   Walk-away max  = quick-resale price minus half the target margin.
5. Resale verdict ("good seller?") from demand (WTB requests), supply (dealers asking),
   price trend and how tight prices are.

Dealer asks in WhatsApp are asking prices, not sold prices - staff should treat the result as guidance.
"""
from datetime import datetime
from typing import Optional

# Value relative to a new full set. Tune to your own experience.
COND_FACTOR = {"NEW": 1.00, "LIKE_NEW": 0.96, "USED": 0.92, "VINTAGE": 0.92, None: 0.95}
SET_FACTOR = {"FULL_SET": 1.00, "PAPERS_ONLY": 0.95, "BOX_ONLY": 0.90, "WATCH_ONLY": 0.85, None: 0.95}


def _q(vals: list, q: float) -> float:
    s = sorted(vals)
    if len(s) == 1:
        return s[0]
    pos = (len(s) - 1) * q
    lo, hi = int(pos), min(int(pos) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def _round(v: Optional[float]) -> Optional[float]:
    if v is None:
        return None
    step = 100 if v >= 5000 else 50
    return round(v / step) * step


def _median(vals: list) -> Optional[float]:
    return _q(vals, 0.5) if vals else None


def analyse(target: dict, comps: list, now: datetime, margin_pct: float = 10.0,
            import_uplift_pct: float = 0.0) -> dict:
    """target: condition, set_type, year, asking_price (GBP)
    comps: offer dicts with direction, price_base, condition, set_type, year, country,
           last_seen_at (datetime), dealer (str), plus display fields."""
    t_factor = COND_FACTOR.get(target.get("condition"), 0.95) * SET_FACTOR.get(target.get("set_type"), 0.95)
    margin = max(0.0, min(margin_pct, 60.0)) / 100
    reasons = []

    wts_all = [c for c in comps if c["direction"] == "WTS"]
    wtb_all = [c for c in comps if c["direction"] == "WTB"]
    priced = []
    for c in wts_all:
        if not c.get("price_base"):
            continue
        p = c["price_base"]
        if c.get("country") and c["country"] != "GB" and import_uplift_pct:
            p *= 1 + import_uplift_pct / 100
        c_factor = COND_FACTOR.get(c.get("condition"), 0.95) * SET_FACTOR.get(c.get("set_type"), 0.95)
        priced.append({**c, "adjusted": p / c_factor * t_factor,
                       "age_days": (now - c["last_seen_at"]).total_seconds() / 86400})

    # prefer last 30 days
    window = 30
    pool = [c for c in priced if c["age_days"] <= 30]
    if len(pool) < 4:
        pool, window = [c for c in priced if c["age_days"] <= 90], 90

    # same era (±2 years) when the customer's year is known and there's enough data
    ty = target.get("year")
    era_note = None
    if ty:
        era = [c for c in pool if c.get("year") and abs(c["year"] - ty) <= 2]
        if len(era) >= 3:
            pool, era_note = era, f"compared with {ty - 2}–{ty + 2} pieces only"

    # trim outliers
    if len(pool) >= 3:
        med = _median([c["adjusted"] for c in pool])
        kept = [c for c in pool if 0.6 * med <= c["adjusted"] <= 1.6 * med]
        if len(kept) >= 3:
            pool = kept

    result = {
        "window_days": window, "comparables": len(pool), "margin_pct": round(margin * 100, 1),
        "wts_count_30d": len({c.get("dealer") for c in wts_all if (now - c["last_seen_at"]).days <= 30}),
        "wtb_count_30d": len({c.get("dealer") for c in wtb_all if (now - c["last_seen_at"]).days <= 30}),
        "era_note": era_note,
    }

    if len(pool) < 3:
        result.update(verdict="NO_DATA", verdict_label="Not enough market data",
                      offer=None, max_price=None, quick_resale=None, market_median=None,
                      reasons=[f"Only {len(pool)} priced dealer offer(s) found for this reference in 90 days. "
                               "Check Chrono24 / sold prices manually before offering."],
                      asking=None, samples=sorted(pool, key=lambda c: c["adjusted"])[:12])
        if result["wtb_count_30d"]:
            result["reasons"].append(f"{result['wtb_count_30d']} dealer(s) asked to BUY this in the last 30 days.")
        return result

    adj = [c["adjusted"] for c in pool]
    low, p25, med, p75, high = min(adj), _q(adj, .25), _q(adj, .5), _q(adj, .75), max(adj)
    quick = p25
    offer, max_price = quick * (1 - margin), quick * (1 - margin / 2)
    result.update(market_low=_round(low), market_p25=_round(p25), market_median=_round(med),
                  market_p75=_round(p75), market_high=_round(high), quick_resale=_round(quick),
                  offer=_round(offer), max_price=_round(max_price),
                  expected_profit=_round(quick - offer))

    uk = [c["adjusted"] for c in pool if c.get("country") == "GB"]
    intl = [c["adjusted"] for c in pool if c.get("country") and c["country"] != "GB"]
    result["uk_median"] = _round(_median(uk)) if len(uk) >= 2 else None
    result["intl_median"] = _round(_median(intl)) if len(intl) >= 2 else None

    # ---- resale verdict
    score = 0
    supply, demand = result["wts_count_30d"], result["wtb_count_30d"]
    ratio = demand / supply if supply else (1.0 if demand else 0)
    if ratio >= 0.3:
        score += 2
        reasons.append(f"Strong demand: {demand} dealers want to buy vs {supply} selling (30 days).")
    elif ratio >= 0.1:
        score += 1
        reasons.append(f"Some demand: {demand} buy requests vs {supply} dealers selling (30 days).")
    else:
        reasons.append(f"Few buyers: {demand} buy requests vs {supply} dealers selling (30 days).")

    recent = [c["adjusted"] for c in priced if c["age_days"] <= 14]
    older = [c["adjusted"] for c in priced if 14 < c["age_days"] <= 90]
    trend = None
    if len(recent) >= 2 and len(older) >= 2:
        trend = (_median(recent) - _median(older)) / _median(older) * 100
        if trend >= 3:
            score += 1
            reasons.append(f"Prices rising: +{trend:.1f}% over the last 2 weeks.")
        elif trend <= -5:
            score -= 2
            reasons.append(f"Prices falling: {trend:.1f}% over the last 2 weeks — price in more room.")
        elif trend <= -2:
            score -= 1
            reasons.append(f"Prices softening: {trend:.1f}% over the last 2 weeks.")
        else:
            reasons.append(f"Prices stable ({trend:+.1f}% over 2 weeks).")
    result["trend_pct"] = round(trend, 1) if trend is not None else None

    if supply >= 8:
        score += 1
        reasons.append("Liquid market: many dealers trade this reference.")
    elif supply <= 2:
        score -= 1
        reasons.append("Thin market: very few dealers trading this reference.")

    spread = (p75 - p25) / med if med else 1
    if spread < 0.10:
        score += 1
        reasons.append(f"Tight pricing (±{spread * 50:.0f}%): easy to price for resale.")
    elif spread > 0.25:
        score -= 1
        reasons.append(f"Wide price spread ({spread * 100:.0f}%): market unsure, check condition carefully.")

    if score >= 4:
        v, label = "STRONG", "Strong seller — buy with confidence"
    elif score >= 2:
        v, label = "GOOD", "Good seller"
    elif score >= 0:
        v, label = "AVERAGE", "Average — may take time to sell"
    else:
        v, label = "SLOW", "Slow seller — only buy with extra margin"
    result.update(verdict=v, verdict_label=label, score=score, reasons=reasons)

    # ---- customer's asking price
    ask = target.get("asking_price")
    if ask:
        if ask <= offer:
            a = ("BUY", f"Asking price is at or below our target offer — good buy (≈{(quick - ask) / ask * 100:.0f}% margin).")
        elif ask <= max_price:
            a = ("NEGOTIATE", f"Within range but above target — try for £{_round(offer):,.0f}; don't exceed £{_round(max_price):,.0f}.")
        elif ask <= quick:
            a = ("TOO_HIGH", f"Too high — leaves under {margin * 50:.0f}% margin. Counter at £{_round(offer):,.0f}.")
        else:
            a = ("WALK_AWAY", f"Above what dealers are asking (quick-sale £{_round(quick):,.0f}) — walk away or counter at £{_round(offer):,.0f}.")
        result["asking"] = {"code": a[0], "message": a[1], "price": ask}
    else:
        result["asking"] = None

    result["samples"] = sorted(pool, key=lambda c: c["adjusted"])[:12]
    return result
