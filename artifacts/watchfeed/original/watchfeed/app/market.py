"""Market maths shared by the feed (deal badges), reference pages (price history) and the digest."""
from datetime import datetime, timedelta
from typing import Optional

from .advisor import COND_FACTOR, SET_FACTOR, _q

MIN_COMPS = 4          # need at least this many other asks to call something a deal
SUSPECT_PCT = -0.40    # more than 40% below market is probably a misread price, not a deal


def normalize(price_base: Optional[float], condition: Optional[str], set_type: Optional[str]) -> Optional[float]:
    """Price expressed as a 'new, full set' equivalent so different conditions can be compared."""
    if not price_base:
        return None
    return price_base / (COND_FACTOR.get(condition, 0.95) * SET_FACTOR.get(set_type, 0.95))


def market_pct(price_norm: Optional[float], comp_norms: list) -> Optional[float]:
    """Fractional difference vs median of comparable (normalized) asks. None if not enough data."""
    comps = [c for c in comp_norms if c]
    if not price_norm or len(comps) < MIN_COMPS:
        return None
    med = _q(comps, 0.5)
    return round((price_norm - med) / med, 4) if med else None


def deal_label(pct: Optional[float], threshold_pct: float) -> Optional[str]:
    if pct is None:
        return None
    if pct <= SUSPECT_PCT:
        return "CHECK"
    if pct <= -threshold_pct / 100:
        return "DEAL"
    return None


def weekly_history(points: list, now: datetime, weeks: int = 26) -> list:
    """points: [(datetime, normalized_price)] -> [{week_start, median, p25, p75, count}] oldest first.
    Weeks with no data are included with median None so the chart shows gaps honestly."""
    start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    start -= timedelta(weeks=weeks - 1)
    buckets = [[] for _ in range(weeks)]
    for ts, p in points:
        if not p or ts < start:
            continue
        i = int((ts - start).days // 7)
        if 0 <= i < weeks:
            buckets[i].append(p)
    out = []
    for i, b in enumerate(buckets):
        out.append({
            "week_start": (start + timedelta(weeks=i)).date().isoformat(),
            "median": round(_q(b, .5)) if b else None,
            "p25": round(_q(b, .25)) if len(b) >= 3 else None,
            "p75": round(_q(b, .75)) if len(b) >= 3 else None,
            "count": len(b),
        })
    return out


def movers(rows: list, now: datetime, min_each: int = 4, top: int = 8) -> list:
    """rows: [(reference_norm, reference, brand, family, datetime, normalized_price)].
    Compares last 7 days vs the 7-28 days before. Returns biggest absolute moves."""
    cur, prev, meta = {}, {}, {}
    for ref_norm, ref, brand, family, ts, p in rows:
        if not p or not ref_norm:
            continue
        age = (now - ts).days
        meta.setdefault(ref_norm, (ref, brand, family))
        if age < 7:
            cur.setdefault(ref_norm, []).append(p)
        elif age < 28:
            prev.setdefault(ref_norm, []).append(p)
    out = []
    for r in cur:
        if len(cur[r]) >= min_each and len(prev.get(r, [])) >= min_each:
            a, b = _q(cur[r], .5), _q(prev[r], .5)
            ref, brand, family = meta[r]
            out.append({"reference": ref, "brand": brand, "family": family,
                        "change_pct": round((a - b) / b * 100, 1), "now": round(a), "before": round(b),
                        "count": len(cur[r])})
    out.sort(key=lambda x: abs(x["change_pct"]), reverse=True)
    return out[:top]
