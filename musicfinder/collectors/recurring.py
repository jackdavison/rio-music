"""Expand config/recurring_<city>.yaml (hand-maintained rodas) into dated events.

Entry shape:
  - name: Roda de Samba da Pedra do Sal
    venue: Pedra do Sal
    neighborhood: Saúde
    weekday: monday            # or a list; english or portuguese names; or 0=Mon..6=Sun
    time: "19:00"
    end_time: "23:00"          # optional
    every: weekly              # weekly | monthly
    week_of_month: [1, 3]      # monthly only: 1..5, or -1 for the last one
    free: true
    price: 20                  # optional
    artists: [ ... ]           # optional
    link: https://...
    notes: free text
    active_from: 2026-01-01    # optional
    active_until: 2026-12-31   # optional
    placeholder: true          # entries flagged placeholder are skipped
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from ..config import load_recurring
from ..models import Event, TZ
from .base import collector

WEEKDAYS = {
    "monday": 0, "mon": 0, "segunda": 0, "seg": 0,
    "tuesday": 1, "tue": 1, "terca": 1, "terça": 1, "ter": 1,
    "wednesday": 2, "wed": 2, "quarta": 2, "qua": 2,
    "thursday": 3, "thu": 3, "quinta": 3, "qui": 3,
    "friday": 4, "fri": 4, "sexta": 4, "sex": 4,
    "saturday": 5, "sat": 5, "sabado": 5, "sábado": 5, "sab": 5,
    "sunday": 6, "sun": 6, "domingo": 6, "dom": 6,
}

WEEKS_AHEAD = 8


def _weekdays(v) -> list[int]:
    vals = v if isinstance(v, list) else [v]
    out = []
    for x in vals:
        if isinstance(x, int):
            out.append(x)
        else:
            out.append(WEEKDAYS[str(x).strip().lower()])
    return out


def _as_date(v) -> date | None:
    if v in (None, ""):
        return None
    return v if isinstance(v, date) else date.fromisoformat(str(v))


def _as_time(v) -> time:
    if isinstance(v, int):  # YAML may parse 19:00 as sexagesimal int
        return time(v // 60, v % 60)
    h, m = str(v or "20:00").split(":")[:2]
    return time(int(h), int(m))


def _week_of_month(d: date) -> tuple[int, bool]:
    n = (d.day - 1) // 7 + 1
    is_last = (d + timedelta(days=7)).month != d.month
    return n, is_last


def expand(entry: dict, today: date, weeks: int = WEEKS_AHEAD) -> list[date]:
    days = _weekdays(entry["weekday"])
    lo = max(today, _as_date(entry.get("active_from")) or today)
    hi = today + timedelta(weeks=weeks)
    until = _as_date(entry.get("active_until"))
    if until:
        hi = min(hi, until)
    every = entry.get("every", "weekly")
    wom = entry.get("week_of_month") or []
    wom = wom if isinstance(wom, list) else [wom]
    out = []
    d = lo
    while d <= hi:
        if d.weekday() in days:
            if every == "weekly":
                out.append(d)
            elif every == "monthly":
                n, last = _week_of_month(d)
                if n in wom or (-1 in wom and last):
                    out.append(d)
        d += timedelta(days=1)
    return out


def events_for(city: str, entries: list[dict], today: date | None = None) -> list[Event]:
    today = today or datetime.now(TZ).date()
    out = []
    for e in entries:
        if e.get("placeholder") or not e.get("name") or e.get("weekday") is None:
            continue
        t = _as_time(e.get("time"))
        end_t = _as_time(e["end_time"]) if e.get("end_time") else None
        price = e.get("price")
        for d in expand(e, today):
            start = datetime.combine(d, t, TZ)
            end = None
            if end_t:
                end = datetime.combine(d, end_t, TZ)
                if end <= start:
                    end += timedelta(days=1)
            out.append(Event(
                city=city, title=e["name"], start_datetime=start, end_datetime=end,
                venue=e.get("venue", ""), neighborhood=e.get("neighborhood", ""),
                artists=list(e.get("artists") or []), source="recurring",
                is_free=bool(e.get("free")), price_min=price, price_max=price,
                ticket_url=e.get("link", ""), source_url=e.get("link", ""),
                description=" ".join(x for x in [e.get("notes", ""), e.get("genre", "")] if x),
            ))
    return out


@collector("recurring")
def collect(city, cfg):
    return events_for(city.slug, load_recurring(city.slug))
