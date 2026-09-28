"""Re-use unchanged offer lines while retaining header/footer context."""
import hashlib
import re
from dataclasses import dataclass

from .parser import looks_like_offer

MAX_CONTEXT_LINES = 8
CONTEXT_CHARS = 120


def _norm(line: str) -> str:
    return re.sub(r"\s+", " ", line.strip().lower())


@dataclass
class Plan:
    lines: list[str]
    cand: list[int]
    ctx: list[int]
    keys: dict[int, str]


def plan(text: str) -> Plan:
    lines = text.splitlines()
    cand, ctx = [], []
    for i, line in enumerate(lines):
        if line.strip():
            # Brand-only lines are list headers, not individual watch offers.
            header = (len(line.split()) <= 2 and not re.search(r"\d|[$£€¥]", line)
                      and not re.search(r"\b(?:wts|wtb|lf|buy|sell)\b", line, re.I))
            (cand if looks_like_offer(line) and not header else ctx).append(i)
    # All context contributes to the cache key, including a late currency footer.
    sig = hashlib.sha256("\n".join(_norm(lines[i])[:CONTEXT_CHARS] for i in ctx).encode()).hexdigest()[:20]
    if len(ctx) > MAX_CONTEXT_LINES:
        ctx = ctx[:MAX_CONTEXT_LINES // 2] + ctx[-MAX_CONTEXT_LINES // 2:]
    keys = {i: hashlib.sha256(f"{sig}|{_norm(lines[i])}".encode()).hexdigest() for i in cand}
    return Plan(lines, cand, ctx, keys)


@dataclass
class Item:
    text: str
    orig: list[int | None]  # reduced line number (1-based) -> original index
    n_cand: int


def make_items(p: Plan, todo: list[int], max_lines: int) -> list[Item]:
    items = []
    for start in range(0, len(todo), max_lines):
        chunk = set(todo[start:start + max_lines])
        keep = sorted(chunk | set(p.ctx))
        items.append(Item(
            text="\n".join(p.lines[i] if i in chunk else p.lines[i][:CONTEXT_CHARS] for i in keep),
            orig=[i if i in chunk else None for i in keep], n_cand=len(chunk),
        ))
    return items


def batches(items: list[tuple], max_lines: int, max_items: int) -> list[list[tuple]]:
    out, current, count = [], [], 0
    for key, item in items:
        if current and (len(current) >= max_items or count + item.n_cand > max_lines
                        or sum(len(it.text) for _, it in current) + len(item.text) > 16000):
            out.append(current)
            current, count = [], 0
        current.append((key, item))
        count += item.n_cand
    if current:
        out.append(current)
    return out


def assign(item: Item, offers: list[dict]) -> tuple[dict[int, list], list, bool]:
    per_line = {i: [] for i in item.orig if i is not None}
    loose, safe = [], True
    for offer in offers:
        target = None
        for line_number in offer.get("_lines") or []:
            if isinstance(line_number, int) and 1 <= line_number <= len(item.orig):
                target = item.orig[line_number - 1]
                if target is not None:
                    break
        if target is None:
            source = offer.get("source_text", "").strip()
            matches = [i for i in per_line if source and source == item.text.splitlines()[item.orig.index(i)].strip()]
            if len(matches) == 1:
                target = matches[0]
        clean = {key: value for key, value in offer.items() if key != "_lines"}
        if target is None:
            safe = False
            loose.append(clean)
        else:
            per_line[target].append(clean)
    return per_line, loose, safe