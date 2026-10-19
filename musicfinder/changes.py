"""Compare this run's events with what's stored: new, newly sold out, date/venue changed."""
from __future__ import annotations

from .models import Event, venue_key


def diff(prev: dict[str, Event], ev: Event) -> list[tuple[str, str]]:
    old = prev.get(ev.id)
    if old is None:
        return [("new", "")]
    out = []
    if ev.sold_out and not old.sold_out:
        out.append(("sold_out", ""))
    if old.start_datetime != ev.start_datetime:
        out.append(("date_changed", f"{old.start_datetime:%d/%m %H:%M} → {ev.start_datetime:%d/%m %H:%M}"))
    if old.venue and ev.venue and venue_key(old.venue) != venue_key(ev.venue):
        out.append(("venue_changed", f"{old.venue} → {ev.venue}"))
    return out


# Fever / Eventim / Sympla phrasing for "few tickets left"
LOW_STOCK = ("ultimos ingressos", "últimos ingressos", "lote final", "quase esgotado",
             "few tickets left", "selling fast", "last tickets", "ultimas unidades", "últimas unidades")


def nearly_sold_out(ev: Event) -> bool:
    t = (ev.title + " " + ev.description).lower()
    return not ev.sold_out and any(w in t for w in LOW_STOCK)
