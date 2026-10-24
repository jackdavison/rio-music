"""collect -> dedupe -> stable ids -> tag -> rank -> track changes -> store."""
from __future__ import annotations

import logging
from datetime import datetime

from rapidfuzz import fuzz

from . import changes, db
from .collectors.base import CollectorResult, run_city
from .config import CityConfig
from .dedupe import SAME, _time_close, dedupe, similarity, venues_match
from .genres import kind_of, tag
from .models import Event, TZ, norm, venue_key
from .rank import rank

log = logging.getLogger(__name__)


def _canonical_ids(city: str, events: list[Event], prev: dict[str, Event]) -> None:
    """Give each merged event a stable id across runs.

    Order: a known alias of any member id -> a member id already stored -> a vanished stored event
    at the same venue with the same title (= the show moved date/time) -> the event's own id.
    """
    seen_now = set()
    for e in events:
        members = getattr(e, "member_ids", [e.id])
        canon = None
        for m in members:
            a = db.cache_get("alias", m)
            if a and a not in seen_now:
                canon = a
                break
        if canon is None:
            canon = next((m for m in members if m in prev and m not in seen_now), None)
        if canon is None:
            canon = _moved_from(e, prev, seen_now, {i for x in events for i in getattr(x, "member_ids", [x.id])})
        canon = canon or e.id
        seen_now.add(canon)
        for m in members:
            if m != canon:
                db.cache_put("alias", m, canon)
        e.id = canon


def retire_superseded(city: str, events: list[Event]) -> None:
    """Deactivate stored rows that this run's events stand in for, which deactivate_missing would
    keep showing: rows merged into another event (an Instagram-sourced row is never retired as
    missing) and rows a paginated source skipped this pass that duplicate a current event (they'd
    otherwise sit beside it for the grace period)."""
    current = {e.id for e in events}
    absorbed = {m for e in events for m in getattr(e, "member_ids", []) if m not in current}
    now = datetime.now(TZ)
    by_day: dict = {}
    for e in events:
        by_day.setdefault(e.date, []).append(e)
    for old in db.load_events(city):
        if old.id in current or old.start_datetime < now:
            continue
        if old.id in absorbed or any(
                venues_match(old, e) and similarity(old, e) >= SAME and _time_close(old, e)
                for e in by_day.get(old.date, [])):
            db.deactivate(old.id)


def _moved_from(e: Event, prev: dict[str, Event], taken: set[str], present: set[str]) -> str | None:
    now = datetime.now(TZ)
    vk, tn = venue_key(e.venue), norm(e.title)
    for pid, p in prev.items():
        if pid in taken or pid in present or p.start_datetime < now:
            continue
        if venue_key(p.venue) == vk and fuzz.ratio(norm(p.title), tn) >= 92 \
                and abs((p.start_datetime - e.start_datetime).days) <= 60:
            return pid
    return None


def process_city(city: CityConfig, only: list[str] | None = None,
                 skip: list[str] | None = None) -> tuple[list[Event], list[CollectorResult]]:
    results = run_city(city, only=only, skip=skip)
    for r in results:
        db.log_run(city.slug, r.name, r.ok, len(r.events), r.error, r.seconds)
    raw = [e for r in results for e in r.events]
    for e in raw:
        e.genres = tag(e)
        e.kind = e.kind or kind_of(e)

    prev = db.load_events_by_id(city.slug)
    # Carry forward stored events whose sources didn't run OK this time (skipped, e.g. Instagram on
    # its own schedule, or failed) so they still take part in dedupe and the outputs.
    # Instagram only sees recent posts, so absence from a run never means an event is gone.
    ok_sources = {r.name for r in results if r.ok} - {"instagram"}
    now_dt = datetime.now(TZ)
    # An event any of whose sources didn't run is carried too (e.g. Instagram + a hand-entered gig):
    # otherwise the source that did run yields it under a new id and the stored row, which
    # deactivate_missing never retires while Instagram is among its sources, lingers as a duplicate.
    carried = [e for e in db.load_events(city.slug) if e.start_datetime >= now_dt
               and set(e.sources) - ok_sources]
    for e in carried:
        e.carried = True
        e.kind = e.kind or kind_of(e)  # stored before kind existed
    events = dedupe(raw + carried)
    _canonical_ids(city.slug, events, prev)

    rank(events)

    now = db.now_iso()
    for e in events:
        old = prev.get(e.id)
        e.first_seen = old.first_seen if old and old.first_seen else now
        e.last_seen = now
        # a re-appearing event that was inactive is not "new" again
        for kind, detail in changes.diff(prev, e):
            db.log_change(e.id, kind, detail)
        if changes.nearly_sold_out(e) and not (old and changes.nearly_sold_out(old)):
            db.log_change(e.id, "nearly_sold_out", "")
        db.upsert(e)

    retire_superseded(city.slug, events)
    gone = db.deactivate_missing(city.slug, {e.id for e in events}, ok_sources)
    if gone:
        log.info("[%s] %d events disappeared from sources", city.slug, len(gone))
    db.commit()
    return events, results
