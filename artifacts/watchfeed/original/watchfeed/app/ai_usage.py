"""Persist provider-reported usage; prices are estimates, not an invoice."""
import logging
from decimal import Decimal

from .db import AIUsage, AIUsageGroup, SessionLocal

log = logging.getLogger("ai_usage")
MILLION = Decimal("1000000")
# USD per million tokens: uncached input, output, 5m cache write, cache read.
HAIKU_45_PRICES = (Decimal("1"), Decimal("5"), Decimal("1.25"), Decimal("0.10"))


class UsageMeterError(RuntimeError):
    """Stop paid parsing if a provider response cannot be recorded."""


def _tokens(value):
    try:
        number = int(value)
        return number if number >= 0 else None
    except (TypeError, ValueError):
        return None


def estimate_usd(model: str, usage: dict):
    if not model.startswith("claude-haiku-4-5"):
        return None  # Unknown price must not silently be reported as zero.
    counts = [_tokens(usage.get(key)) for key in (
        "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"
    )]
    if counts[0] is None or counts[1] is None:
        return None
    counts = [count or 0 for count in counts]
    return (sum(Decimal(count) * rate for count, rate in zip(counts, HAIKU_45_PRICES))
            / MILLION).quantize(Decimal("0.00000001"))


def record_usage(model: str, usage: dict | None, group_weights: dict[str, int],
                 message_count: int = 1) -> None:
    """Log every HTTP 200, including max_tokens and responses rejected later.

    Per-group share is proportional to the supplied message-text lengths. A
    duplicated text shared by groups can split its weight between them.
    """
    usage = usage if isinstance(usage, dict) else {}
    cost = estimate_usd(model, usage)
    try:
        with SessionLocal() as s:
            row = AIUsage(
                model=model, input_tokens=_tokens(usage.get("input_tokens")),
                output_tokens=_tokens(usage.get("output_tokens")),
                cache_creation_input_tokens=_tokens(usage.get("cache_creation_input_tokens")) or 0,
                cache_read_input_tokens=_tokens(usage.get("cache_read_input_tokens")) or 0,
                estimated_usd=cost, message_count=message_count,
            )
            s.add(row)
            s.flush()
            weights = {key: max(0, weight) for key, weight in group_weights.items() if key}
            total = sum(weights.values())
            if total:
                allocated = Decimal("0")
                for index, (group, weight) in enumerate(weights.items()):
                    if cost is None:
                        share = None
                    elif index == len(weights) - 1:
                        share = cost - allocated
                    else:
                        share = (cost * Decimal(weight) / Decimal(total)).quantize(Decimal("0.00000001"))
                        allocated += share
                    s.add(AIUsageGroup(usage_id=row.id, group_id=group, estimated_usd=share))
            s.commit()
    except Exception as exc:
        raise UsageMeterError("AI response could not be metered; paid worker stopped") from exc
    if cost is None:
        log.warning("AI response usage could not be priced for model %s", model)