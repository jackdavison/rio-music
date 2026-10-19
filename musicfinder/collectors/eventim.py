"""Eventim Brasil (www.eventim.com.br).

The city/category listing pages embed a search widget whose config points at Eventim's public
exploration API (`apiPath` + `apiClientId` in the inline `widget.config`):

    GET https://public-api.eventim.com/websearch/search/api/exploration/v2/productGroups
        ?webId=web__eventim-com-br&language=pt&retail_partner=BR1
        &city_ids=1672&categories=Shows & Música&categories=Festivais & Rodeios
        &sort=DateAsc&page=1

Response: {"productGroups": [...], "page", "totalPages", "totalResults", "facets"}; 20 groups per
page. Each product group (a show/tour) carries `products` already filtered to the requested city,
one per date, with `typeAttributes.liveEntertainment.{startDate (local offset), location.name}`,
`status` ("Available", "Cancelled", ...), `link` (ticket page). `categories` is repeatable and
OR-ed; subcategories are filtered client-side ("Festivais & Rodeios" also holds rodeos/food fests).

Akamai guards both www.eventim.com.br and public-api.eventim.com: without browser-like
`sec-ch-ua` / `Sec-Fetch-*` headers the request hangs or gets 403. Event detail pages sit behind an
interactive Akamai challenge, so ticket prices are unavailable (the listing JSON-LD always reports
lowPrice 0.0, which is meaningless); price fields are left empty.
"""
from __future__ import annotations

import logging
import re
from typing import Iterable
from urllib.parse import urlencode

import requests

from .. import http
from ..config import CityConfig
from ..models import Event, norm, venue_key
from .base import collector, horizon
from .util import clean_text, looks_like_music, parse_iso

log = logging.getLogger(__name__)

NAME = "eventim"
API = "https://public-api.eventim.com/websearch/search/api/exploration/v2/productGroups"
WEB_ID = "web__eventim-com-br"

CITY_DEFAULTS = {
    "rio": {"city_id": "1672", "city_name": "Rio de Janeiro"},   # /city/rio-de-janeiro-1672/
    "sp": {"city_id": "943", "city_name": "São Paulo"},          # /city/sao-paulo-943/
}
DEFAULTS = {
    "categories": ["Shows & Música", "Festivais & Rodeios"],
    # a group is kept if any of its (sub)categories is in here...
    "music_categories": ["Shows & Música", "Festivais de Música"],
    "max_pages": 15,
}

# Eventim sells add-ons (bus transfers, parking, VIP upgrades) as their own product groups.
# Hospitality/box packages are listed as separate products of the same show; the plain ticket
# product is always present too, so drop them to avoid duplicates.
_ADDON = re.compile(r"^(transporte|estacionamento|translado|transfer|excursao|upgrade|"
                    r"vale ?presente|gift ?card)\b|\b(estacionamento|translado|camarote|"
                    r"hospitalidade|hospitality|open bar|pacote vip|vip package|"
                    r"meet (and|e) greet)\b")
_STATUS_SKIP = {"cancelled", "canceled", "postponed"}

# Browser-like headers; Akamai stalls requests that lack them.
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Encoding": "gzip, deflate",
    "Origin": "https://www.eventim.com.br",
    "Referer": "https://www.eventim.com.br/",
    "sec-ch-ua": '"Chromium";v="141", "Google Chrome";v="141", "Not?A_Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
}


def _settings(city: CityConfig, cfg: dict) -> dict:
    s = dict(DEFAULTS)
    s.update(CITY_DEFAULTS.get(city.slug, {}))
    s.update({k: v for k, v in (cfg or {}).items() if v is not None})
    if not s.get("city_name"):
        s["city_name"] = city.name
    if not s.get("city_id"):
        raise ValueError(f"eventim: no city_id configured for city {city.slug!r}")
    return s


_impersonate = False


def _get_json(params: dict) -> dict:
    """Plain session first; Akamai 403s datacenter IPs (GitHub runners) unless the TLS fingerprint
    looks like a browser, so switch to curl_cffi for the rest of the run on a 403."""
    global _impersonate
    if not _impersonate:
        try:
            return http.get_json(API, params=params, headers=HEADERS)
        except requests.HTTPError as exc:
            if exc.response is None or exc.response.status_code != 403:
                raise
            log.info("eventim: 403, switching to curl_cffi impersonation")
            _impersonate = True
    from curl_cffi import requests as cffi
    r = cffi.get(f"{API}?{urlencode(params, doseq=True)}", impersonate="chrome", timeout=40)
    r.raise_for_status()
    return r.json()


def _fetch_groups(s: dict) -> Iterable[dict]:
    _, hi = horizon()
    for page in range(1, int(s["max_pages"]) + 1):
        params = {"webId": WEB_ID, "language": "pt", "retail_partner": "BR1",
                  "city_ids": str(s["city_id"]), "categories": list(s["categories"]),
                  "sort": "DateAsc", "page": page}
        data = _get_json(params)
        groups = data.get("productGroups") or []
        yield from groups
        if page >= int(data.get("totalPages") or 0) or not groups:
            return
        # sorted by first date: once a whole page starts beyond the horizon we can stop
        firsts = [parse_iso(g.get("startDate")) for g in groups]
        if all(d and d > hi for d in firsts):
            return


def _same_place(name: str, venue: str) -> bool:
    a, v = norm(name), norm(venue)
    return bool(a) and bool(v) and (a == v or a in v or v in a or venue_key(a) == venue_key(v))


def _is_music(group: dict, music_cats: set[str]) -> bool:
    cats = {norm(c.get("name")) for c in group.get("categories") or []}
    if cats & music_cats:
        return True
    if cats:  # categorised as something else (rodeio, teatro...)
        return False
    # Uncategorised: title only -- every Eventim blurb ends with "...apresentação imperdível ao vivo"
    return looks_like_music(group.get("name", ""))


def parse_group(group: dict, s: dict, slug: str) -> list[Event]:
    title_g = clean_text(group.get("name"), 300)
    if _ADDON.search(norm(title_g)):
        return []
    music_cats = {norm(c) for c in s["music_categories"]}
    if not _is_music(group, music_cats):
        return []
    city_n = norm(s["city_name"])
    # mainAttraction is frequently the venue itself (e.g. "CIRCO VOADOR"); ignore it then
    artist = ((group.get("mainAttraction") or {}).get("name") or "").strip()
    subcats = [c["name"] for c in group.get("categories") or [] if c.get("parentCategory")]
    desc = clean_text(group.get("description"), 600)
    if subcats:
        desc = (f"[{', '.join(subcats)}] " + desc).strip()
    group_url = group.get("link") or ""
    out = []
    for p in group.get("products") or []:
        if str(p.get("status", "")).lower() in _STATUS_SKIP:
            continue
        le = (p.get("typeAttributes") or {}).get("liveEntertainment") or {}
        loc = le.get("location") or {}
        if loc.get("city") and norm(loc["city"]) != city_n:
            continue
        start = parse_iso(le.get("startDate"))
        if not start:
            continue
        title = clean_text(p.get("name") or title_g, 300)
        if _ADDON.search(norm(title)):
            continue
        venue = (loc.get("name") or "").strip()
        artists = [artist] if artist and not _same_place(artist, venue) else []
        status = str(p.get("status", "")).lower()
        out.append(Event(
            city=slug,
            title=title,
            start_datetime=start,
            source=NAME,
            venue=venue,
            artists=artists,
            ticket_url=p.get("link") or group_url,
            source_url=group_url or p.get("link") or "",
            image_url=group.get("imageUrl") or "",
            sold_out=("sold" in status or "esgot" in status),
            description=desc,
        ))
    return out


@collector(NAME)
def collect(city: CityConfig, cfg: dict) -> Iterable[Event]:
    s = _settings(city, cfg)
    seen: set[str] = set()
    for group in _fetch_groups(s):
        gid = str(group.get("productGroupId") or group.get("name"))
        if gid in seen:
            continue
        seen.add(gid)
        yield from parse_group(group, s, city.slug)
