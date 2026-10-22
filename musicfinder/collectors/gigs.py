"""One-off gigs added by hand to config/gigs_<city>.yaml, including vague tips.

Entry shape:
  - title: Igor Souza           # or artists: [...]; one of the two is required
    artists: [Igor Souza]
    date: 2026-10-25            # required
    time: "21:00"               # optional; missing -> tentative, matches any time that day
    venue: Bar do Zé            # optional; missing -> matches any venue that day
    neighborhood: Lapa
    price: 30                   # or free: true
    link: https://...
    genre: choro
    notes: saw it on a poster at the Lapa arches
    heard: 2026-10-17           # when you heard about it (for your own reference)

A tip without time or venue is shown as "a confirmar". When a real listing (Sympla, a venue
page...) for the same act on the same day shows up, dedupe merges the two and the real listing's
details win; the event keeps its id, so calendar entries update in place.
"""
from __future__ import annotations

from datetime import date, datetime, time

from ..config import load_gigs
from ..models import Event, TZ
from .base import collector, in_horizon
from .recurring import _as_time

TBC = "a confirmar"


def events_for(city: str, entries: list[dict], today: date | None = None) -> list[Event]:
    today = today or datetime.now(TZ).date()
    out = []
    for g in entries:
        if g.get("placeholder") or not g.get("date") or not (g.get("title") or g.get("artists")):
            continue
        d = g["date"] if isinstance(g["date"], date) else date.fromisoformat(str(g["date"]))
        if d < today:
            continue
        artists = list(g.get("artists") or [])
        unknown_time = not g.get("time")
        start = datetime.combine(d, _as_time(g["time"]) if not unknown_time else time(20, 0), TZ)
        missing = [w for w, v in (("horário", not unknown_time), ("local", g.get("venue"))) if not v]
        notes = [g.get("notes", ""), g.get("genre", "")]
        if missing:
            notes.insert(0, f"{' e '.join(missing)} {TBC}")
        price = g.get("price")
        ev = Event(
            city=city, title=g.get("title") or ", ".join(artists), artists=artists,
            start_datetime=start, venue=g.get("venue", ""), neighborhood=g.get("neighborhood", ""),
            source="gigs", is_free=bool(g.get("free")), price_min=price, price_max=price,
            ticket_url=g.get("link", ""), source_url=g.get("link", ""),
            description=" · ".join(x for x in notes if x), kind=g.get("kind", ""),
        )
        ev.time_unknown = unknown_time
        if in_horizon(ev):
            out.append(ev)
    return out


@collector("gigs")
def collect(city, cfg):
    return events_for(city.slug, load_gigs(city.slug))
