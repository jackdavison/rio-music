"""config/watchlist.txt matching. Lines: `artist: X`, `venue: Y`, or bare text (matches either)."""
from __future__ import annotations

from rapidfuzz import fuzz

from .config import load_watchlist
from .models import Event, norm


def parse(lines: list[str]) -> list[tuple[str, str]]:
    out = []
    for line in lines:
        kind, _, val = line.partition(":")
        if val and kind.strip().lower() in ("artist", "venue"):
            out.append((kind.strip().lower(), norm(val)))
        else:
            out.append(("any", norm(line)))
    return [(k, v) for k, v in out if v and "placeholder" not in v]


_rules: list[tuple[str, str]] | None = None


def rules() -> list[tuple[str, str]]:
    global _rules
    if _rules is None:
        _rules = parse(load_watchlist())
    return _rules


def _hit(needle: str, hay: str) -> bool:
    if not hay:
        return False
    if f" {needle} " in f" {hay} ":
        return True
    return len(needle) >= 6 and fuzz.partial_ratio(needle, hay) >= 92


def matches(ev: Event, rule_list: list[tuple[str, str]] | None = None) -> list[str]:
    rule_list = rules() if rule_list is None else rule_list
    who = norm(" ".join([ev.title] + ev.artists))
    where = norm(ev.venue)
    out = []
    for kind, val in rule_list:
        if (kind in ("artist", "any") and _hit(val, who)) or (kind in ("venue", "any") and _hit(val, where)):
            out.append(val)
    return out
