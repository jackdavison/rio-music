"""Sympla (www.sympla.com.br).

The site's search pages are backed by a public JSON proxy:

    POST https://www.sympla.com.br/api/v1/search
    {"service": "/v4/search/query", "params": {...}}

Useful params (all strings): `city` (exact city name, e.g. "Rio de Janeiro"), `themes` (99 = Música,
104 = Arte, Cinema e Cultura), `type` ("normal" excludes online events), `start_date`/`end_date`
(YYYY-MM-DD window), `sort` ("start_date" = ascending), `page`, `limit` (server caps it at 24).
The response is {"data": [...], "total": N, "page": p, "limit": 24}. Dates are real UTC instants.
The search API carries no ticket prices (only an `is_free` flag) and no sold-out flag.

Sympla files many real concerts under theme 104 (Teatro / Standup subthemes), so a secondary query
over that theme is run with a stricter music filter.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Iterable

from .. import http
from ..config import CityConfig
from ..models import Event, TZ, norm
from .base import HORIZON_DAYS, collector
from .util import clean_text, looks_like_music, parse_iso

log = logging.getLogger(__name__)

NAME = "sympla"
API = "https://www.sympla.com.br/api/v1/search"
SERVICE = "/v4/search/query"
PAGE_SIZE = 24  # server-side maximum

CITY_DEFAULTS = {
    "rio": {"city_name": "Rio de Janeiro", "state": "RJ"},
    "sp": {"city_name": "São Paulo", "state": "SP"},
}

THEME_MUSIC = "99"
THEME_ARTS = "104"

# format_sk values that are never a performance (courses, talks, networking, tours, races...)
NON_PERFORMANCE_FORMATS = {81, 83, 84, 85, 86, 88, 90, 91, 158}
# formats worth scanning in the arts theme: Festa, Outro, Show, Festival, Apresentações e espetáculos
ARTS_FORMATS = "80,82,87,89,157"

# Theme 99 ("Música") also holds bazaars, surf camps, film screenings, food nights...
_JUNK_TITLE = re.compile(
    r"\b(visitacao|excursao|transfer|onibus|cancelado|adiado|bazar|brecho|surf|surfcamp|"
    r"hostel|camp|filme|cinema|open air brasil|tinder|rodizio|espetinhos?|concurso|"
    r"(?<!segunda )(?<!terca )(?<!quarta )(?<!quinta )(?<!sexta )feira|"
    r"mentoria|curso|palestra|workshop|oficina|corrida|day use|degustacao)\b")
_NOT_CONCERT = re.compile(
    r"\b(comedia|stand ?up|humor|humorista|risada|o musical|musical infantil|infantil|palestra|"
    r"tour|passeio|circuito|novo show|oficina|workshop|curso|desfile)\b")
_STRONG_MUSIC = re.compile(
    r"\b(choro|samba|jazz|mpb|concerto|orquestra|quarteto|quinteto|sexteto|trio|duo|banda|recital|"
    r"piano|pianista|violao|cavaquinho|bandolim|sax|saxofone|flauta|guitarra|percussao|tributo|"
    r"tribute|cantor|cantora|canta|cantam|interpreta|interpretam|cancoes|album|disco|sinfonic\w*|"
    r"filarmonic\w*|coral|blues|rock|bossa|gafieira|forro|frevo|maracatu|ao vivo|acustic\w*|"
    r"opera|dj|big band|seresta|sarau|show)\b")


def _params(cfg: dict, city: CityConfig) -> dict:
    d = dict(CITY_DEFAULTS.get(city.slug, {}))
    d.update({k: v for k, v in (cfg or {}).items() if v is not None})
    if not d.get("city_name"):
        d["city_name"] = city.name
    return d


def _search(params: dict, max_pages: int) -> Iterable[dict]:
    total = None
    for page in range(1, max_pages + 1):
        body = {"service": SERVICE,
                "params": dict(params, page=str(page), limit=str(PAGE_SIZE))}
        data = http.post_json(API, body, headers={"Referer": "https://www.sympla.com.br/eventos"})
        rows = data.get("data") or []
        total = data.get("total", total)
        yield from rows
        if not rows or (total is not None and page * PAGE_SIZE >= int(total)):
            return
    log.warning("[sympla] stopped at max_pages=%d (total=%s)", max_pages, total)


def _is_online(row: dict) -> bool:
    loc = row.get("location") or {}
    if "ONLINE" in (str(row.get("event_type", "")).upper(), str(row.get("type", "")).upper()):
        return True
    return not loc.get("city") and not loc.get("lat")


def _arts_is_music(row: dict) -> bool:
    """Stricter filter for theme 104 (theatre/stand-up/culture) rows."""
    name = norm(row.get("name"))
    if _NOT_CONCERT.search(name) or norm(row.get("subtheme_name")) == "standup" and not re.search(
            r"\b(choro|samba|jazz|violao|quarteto|trio|banda|album|tributo|orquestra|mpb)\b", name):
        return False
    return bool(_STRONG_MUSIC.search(name)) and looks_like_music(row.get("name"), "show")


def parse_row(row: dict, city_slug: str, state: str | None = None) -> Event | None:
    if _is_online(row):
        return None
    loc = row.get("location") or {}
    if state and loc.get("state") and loc["state"].upper() != state.upper():
        return None
    title = clean_text(row.get("name") or row.get("title"), 300)
    start = parse_iso(row.get("start_date_utc") or row.get("start_date"))
    if not title or not start:
        return None
    if row.get("format_sk") in NON_PERFORMANCE_FORMATS or _JUNK_TITLE.search(norm(title)):
        return None

    end = parse_iso(row.get("end_date_utc") or row.get("end_date"))
    if end and not (start < end <= start + timedelta(hours=24)):
        end = None  # multi-day runs / "hidden" durations: keep only the start

    images = row.get("images") or {}
    url = row.get("url") or ""
    venue = clean_text(loc.get("name"), 200)
    address = clean_text(loc.get("address"), 200)
    organizer = (row.get("organizer") or {}).get("name") or ""
    desc_bits = [row.get("theme_name"), row.get("subtheme_name"), row.get("format_name"),
                 (row.get("category_prim") or {}).get("name"),
                 f"Organizador: {organizer}" if organizer else "",
                 f"Local: {venue}{' - ' + address if address and address != venue else ''}"]
    description = " · ".join(b.strip() for b in desc_bits if b and str(b).strip())

    is_free = bool(row.get("is_free"))
    return Event(
        city=city_slug,
        title=title,
        start_datetime=start,
        end_datetime=end,
        source=NAME,
        venue=venue or address,
        neighborhood=clean_text(loc.get("neighborhood"), 100),
        price_min=0.0 if is_free else None,
        is_free=is_free,
        ticket_url=url,
        source_url=url,
        image_url=images.get("original") or images.get("lg") or images.get("xs") or "",
        description=description,
    )


@collector(NAME)
def collect(city: CityConfig, cfg: dict) -> Iterable[Event]:
    p = _params(cfg, city)
    days = int(p.get("days", HORIZON_DAYS))
    max_pages = int(p.get("max_pages", 60))
    today = datetime.now(TZ).date()
    base = {
        "city": p["city_name"],
        "type": "normal",            # excludes online events server-side
        "sort": "start_date",
        "start_date": today.isoformat(),
        "end_date": (today + timedelta(days=days)).isoformat(),
    }
    queries = [(dict(base, themes=str(p.get("themes", THEME_MUSIC))), None)]
    if p.get("include_arts", True):
        queries.append((dict(base, themes=str(p.get("arts_themes", THEME_ARTS)),
                             formats=ARTS_FORMATS), _arts_is_music))

    seen: set = set()
    n = 0
    for params, extra_filter in queries:
        for row in _search(params, max_pages):
            rid = row.get("id")
            if rid in seen:
                continue
            seen.add(rid)
            if extra_filter and not extra_filter(row):
                continue
            ev = parse_row(row, city.slug, p.get("state"))
            if ev:
                n += 1
                yield ev
    log.info("[sympla/%s] %d rows scanned, %d events kept", city.slug, len(seen), n)
