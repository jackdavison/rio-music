"""ICS feeds: <city>.ics (score >= threshold) and watchlist.ics. Stable UIDs = event id."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from icalendar import Calendar, Event as VEvent

from ..models import Event, TZ
from ..watchlist import matches

UID_DOMAIN = "musicfinder"


def _fmt_price(e: Event) -> str:
    if e.is_free:
        return "Grátis"
    if e.price_min is None:
        return ""
    if e.price_max and e.price_max != e.price_min:
        return f"R$ {e.price_min:.0f}–{e.price_max:.0f}"
    return f"R$ {e.price_min:.0f}"


def vevent(e: Event, stamp: datetime) -> VEvent:
    v = VEvent()
    v.add("uid", f"{e.id}@{UID_DOMAIN}")
    v.add("dtstamp", stamp)
    v.add("dtstart", e.start_datetime)
    v.add("dtend", e.end_datetime or e.start_datetime + timedelta(hours=2, minutes=30))
    title = e.title + (" [ESGOTADO]" if e.sold_out else "")
    v.add("summary", f"{title} ({e.score})" if e.score is not None else title)
    loc = ", ".join(x for x in [e.venue, e.neighborhood] if x)
    if loc:
        v.add("location", loc)
    url = e.ticket_url or e.source_url
    if url:
        v.add("url", url)
    desc = [f"Score {e.score}/100 — {e.score_reason}"]
    if e.artists:
        desc.append("Artists: " + ", ".join(e.artists))
    price = _fmt_price(e)
    if price:
        desc.append("Price: " + price)
    if url:
        desc.append("Tickets: " + url)
    desc.append("Source: " + ", ".join(e.sources))
    v.add("description", "\n".join(desc))
    return v


def calendar(name: str, events: list[Event]) -> Calendar:
    cal = Calendar()
    cal.add("prodid", "-//musicfinder//Brazil Live Music Finder//PT")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("x-wr-calname", name)
    cal.add("x-wr-timezone", "America/Sao_Paulo")
    cal.add("refresh-interval;value=duration", "PT3H")
    cal.add("x-published-ttl", "PT3H")
    stamp = datetime.now(TZ)
    for e in sorted(events, key=lambda x: x.start_datetime):
        cal.add_component(vevent(e, stamp))
    return cal


def write_feeds(out_dir: Path, by_city: dict[str, list[Event]], thresholds: dict[str, int],
                names: dict[str, str]) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    watch: list[Event] = []
    for city, evs in by_city.items():
        picked = [e for e in evs if (e.score or 0) >= thresholds.get(city, 60)]
        p = out_dir / f"{city}.ics"
        p.write_bytes(calendar(f"Música — {names.get(city, city)}", picked).to_ical())
        written.append(p)
        watch += [e for e in evs if matches(e)]
    p = out_dir / "watchlist.ics"
    p.write_bytes(calendar("Música — Watchlist", watch).to_ical())
    written.append(p)
    return written
