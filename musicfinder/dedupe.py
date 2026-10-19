"""Dedupe events across sources: city + venue + date + fuzzy artist/title, LLM tiebreak."""
from __future__ import annotations

import logging
from collections import defaultdict

from rapidfuzz import fuzz

from . import llm
from .models import Event, norm, venue_key

log = logging.getLogger(__name__)

# Lower = better source for the ticket link (official/primary ticketing first)
SOURCE_RANK = {
    "eventim": 0, "sympla": 1, "bilheteria_digital": 1, "queremos": 2, "shotgun": 2, "fever": 3,
    "sesc_sp": 0, "sesc_rj": 0, "venues": 4, "instagram": 6, "recurring": 7, "gigs": 8,
}

SAME = 88       # >= : same event
AMBIGUOUS = 62  # between AMBIGUOUS and SAME: ask Claude (or fall back to "different")


def _who(ev: Event) -> str:
    return norm(" ".join(ev.artists) or ev.title)


def similarity(a: Event, b: Event) -> int:
    s1 = fuzz.token_set_ratio(_who(a), _who(b))
    s2 = fuzz.token_set_ratio(norm(a.title), norm(b.title))
    return int(max(s1, s2))


def venues_match(a: Event, b: Event) -> bool:
    va, vb = venue_key(a.venue), venue_key(b.venue)
    if not va or not vb:
        return True  # unknown venue: let the artist/date match decide
    return va == vb or fuzz.token_set_ratio(va, vb) >= 85


def _time_close(a: Event, b: Event) -> bool:
    if getattr(a, "time_unknown", False) or getattr(b, "time_unknown", False):
        return True  # a tip with no time matches any session that day
    return abs((a.start_datetime - b.start_datetime).total_seconds()) <= 3 * 3600


TIEBREAK_SCHEMA = {
    "type": "object",
    "properties": {"answers": {"type": "array", "items": {
        "type": "object",
        "properties": {"pair": {"type": "integer"}, "same": {"type": "boolean"}},
        "required": ["pair", "same"], "additionalProperties": False}}},
    "required": ["answers"], "additionalProperties": False,
}


def _llm_tiebreak(pairs: list[tuple[Event, Event]]) -> list[bool]:
    if not pairs or not llm.available():
        return [False] * len(pairs)

    def desc(e: Event):
        return f"{e.title} | artists: {', '.join(e.artists)} | {e.venue} | {e.start_datetime:%Y-%m-%d %H:%M} | {e.source}"

    lines = [f"{i}. A: {desc(a)}\n   B: {desc(b)}" for i, (a, b) in enumerate(pairs)]
    out = llm.json_call(
        system="You decide whether two event listings from different ticketing sites describe the "
               "same live music performance (same act, same venue, same night). Different sessions "
               "of the same show at different times are different events.",
        prompt="\n".join(lines), schema=TIEBREAK_SCHEMA, max_tokens=2000, cache_tag="dedupe", task="dedupe")
    res = [False] * len(pairs)
    for ans in (out or {}).get("answers", []):
        if 0 <= ans["pair"] < len(pairs):
            res[ans["pair"]] = ans["same"]
    return res


def merge(group: list[Event]) -> Event:
    group = sorted(group, key=lambda e: (SOURCE_RANK.get(e.source, 5), -len(e.description)))
    best = group[0]
    # every id this event has been known by; the pipeline keeps whichever is already stored so
    # ICS UIDs / calendar ids stay stable when a better source shows up later
    best.member_ids = sorted({i for e in group for i in getattr(e, "member_ids", [e.id])})
    for other in group[1:]:
        for f in ("venue", "neighborhood", "image_url", "description", "source_url"):
            if not getattr(best, f) and getattr(other, f):
                setattr(best, f, getattr(other, f))
        if not best.ticket_url and other.ticket_url:
            best.ticket_url = other.ticket_url
        if not best.artists and other.artists:
            best.artists = other.artists
        if best.price_min is None and other.price_min is not None:
            best.price_min, best.price_max = other.price_min, other.price_max
        if best.end_datetime is None and other.end_datetime is not None:
            best.end_datetime = other.end_datetime
        best.is_free = best.is_free or (other.is_free and best.price_min in (None, 0))
        best.sold_out = best.sold_out or other.sold_out
        for s in other.sources:
            if s not in best.sources:
                best.sources.append(s)
        for g in other.genres:
            if g not in best.genres:
                best.genres.append(g)
    return best


def dedupe(events: list[Event]) -> list[Event]:
    # exact-id collapse first
    by_id: dict[str, list[Event]] = defaultdict(list)
    for e in events:
        by_id[e.id].append(e)
    events = [merge(g) for g in by_id.values()]

    buckets: dict[tuple, list[Event]] = defaultdict(list)
    for e in events:
        buckets[(e.city, e.date)].append(e)

    parent = {id(e): e for e in events}

    def find(e):
        while parent[id(e)] is not e:
            e = parent[id(e)]
        return e

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra is not rb:
            parent[id(rb)] = ra

    ambiguous: list[tuple[Event, Event]] = []
    for bucket in buckets.values():
        for i, a in enumerate(bucket):
            for b in bucket[i + 1:]:
                if a.source == b.source and a.source not in ("venues", "instagram"):
                    continue  # a ticketing site never lists the same show twice on purpose;
                    # venue pages and Instagram (several posts per show) do
                if not venues_match(a, b):
                    continue
                s = similarity(a, b)
                if s >= SAME and _time_close(a, b):
                    union(a, b)
                elif s >= AMBIGUOUS or (s >= SAME and not _time_close(a, b)):
                    ambiguous.append((a, b))

    for (a, b), same in zip(ambiguous, _llm_tiebreak(ambiguous[:60])):
        if same:
            union(a, b)

    groups: dict[int, list[Event]] = defaultdict(list)
    for e in events:
        groups[id(find(e))].append(e)
    merged = [merge(g) for g in groups.values()]
    log.info("dedupe: %d -> %d events (%d ambiguous pairs)", len(events), len(merged), len(ambiguous))
    return merged
