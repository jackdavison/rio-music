"""Shotgun (shotgun.live) city pages.

The city page is a server-rendered Next.js page; `?page=N` is cumulative ("load more"), so one
request with a large page number returns the whole upcoming list as event cards (title, venue,
UTC start time, cheapest price, genre tags, image). Each event page carries a schema.org
`MusicEvent` JSON-LD block with performers, full address, end time, all offers (with
availability) and the description; we fetch those for the first `max_details` events.

shotgun.live sits behind a Vercel bot checkpoint that answers plain `requests` with HTTP 429.
We try the shared http session first and, if challenged, switch to `curl_cffi` with a browser
TLS fingerprint (pip install curl_cffi) for the rest of the run.
"""
from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor

from bs4 import BeautifulSoup

from .. import http
from ..config import CityConfig
from ..models import Event
from .base import collector, horizon
from .util import clean_text, looks_like_music, parse_iso

log = logging.getLogger(__name__)

BASE = "https://shotgun.live"
DEFAULTS = {
    "rio": {"city_slug": "rio-de-janeiro", "pages": 60},
    "sp": {"city_slug": "sao-paulo", "pages": 60},
}

NON_MUSIC_TYPES = ("ComedyEvent", "EducationEvent", "TheaterEvent", "SportsEvent",
                   "ExhibitionEvent", "BusinessEvent", "FoodEvent", "ScreeningEvent")

_use_impersonation = False


class Blocked(Exception):
    pass


def _is_challenge(status: int, text: str) -> bool:
    return status == 429 or "Vercel Security Checkpoint" in text[:3000]


def _fetch(url: str, transport: str = "auto") -> str:
    """GET text through the shared session; fall back to curl_cffi when Vercel challenges us."""
    global _use_impersonation
    if transport != "impersonate" and not _use_impersonation:
        try:
            r = http.session().get(url)
            if not _is_challenge(r.status_code, r.text):
                r.raise_for_status()
                return r.text
        except Exception as exc:  # urllib3 raises RetryError after repeated 429s
            if "429" not in str(exc) and "too many" not in str(exc).lower():
                raise
        if transport == "requests":
            raise Blocked(f"Vercel checkpoint on {url}")
        log.info("shotgun: Vercel checkpoint hit, switching to curl_cffi impersonation")
        _use_impersonation = True
    try:
        from curl_cffi import requests as cffi
    except ImportError as exc:  # pragma: no cover
        raise Blocked("shotgun.live blocks plain requests; pip install curl_cffi") from exc
    r = cffi.get(url, impersonate="chrome", timeout=40,
                 headers={"Accept-Language": "en-US,en;q=0.9,pt-BR;q=0.8"})
    if _is_challenge(r.status_code, r.text):
        raise Blocked(f"Vercel checkpoint even with impersonation: {url}")
    r.raise_for_status()
    return r.text


def _price_en(text: str) -> float | None:
    """'R$1,234.50' (en locale) -> 1234.5"""
    m = re.search(r"R\$\s*([\d,]+(?:\.\d+)?)", text or "")
    return float(m.group(1).replace(",", "")) if m else None


def parse_listing(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []
    seen: dict[str, dict] = {}
    for a in soup.select('a[href*="/events/"]'):
        href = a.get("href", "").split("?")[0]
        slug = href.rstrip("/").rsplit("/events/", 1)[-1]
        t = a.select_one("time[datetime]")
        if not slug or not t:
            continue
        title_el = a.find("p")
        title = title_el.get_text(" ", strip=True) if title_el else ""
        venue_el = a.select_one("div.text-muted-foreground.shrink")
        venue = venue_el.get_text(" ", strip=True) if venue_el else ""
        img = a.find("img")
        image = ""
        if img:
            # Cloudinary URLs contain commas, so split srcset on the " 1x"/" 2x" descriptors
            urls = re.findall(r"(https://\S+)\s+\d+(?:\.\d+)?[xw]",
                              img.get("srcset") or img.get("srcSet") or "")
            image = urls[-1] if urls else (img.get("src") or "")
            if image.startswith("data:"):
                image = ""
        # price label sits in the div after the date/time group
        row = t.find_parent("div").find_parent("div")
        label = ""
        if row:
            kids = row.find_all("div", recursive=False)
            if len(kids) > 1:
                label = kids[-1].get_text(" ", strip=True)
        if not re.search(r"R\$|free|gr[aá]tis|sold out|esgotad|wait", label, re.I):
            label = ""  # the "Featured" hero card has no price slot
        tags = [d.get_text(" ", strip=True) for d in a.select("div.rounded-full")]
        tags = [x for x in tags if x and not x.startswith("+")]
        card = {"slug": slug, "title": title, "venue": venue, "start": t["datetime"],
                "image": image, "label": label, "tags": tags}
        # the featured event also appears in the regular grid; keep the richer card
        prev = seen.get(slug)
        if prev is None:
            seen[slug] = card
            out.append(card)
        elif len([v for v in card.values() if v]) > len([v for v in prev.values() if v]):
            prev.update(card)
    return out


def parse_detail(html: str) -> dict | None:
    for m in re.finditer(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S):
        try:
            j = json.loads(m.group(1))
        except ValueError:
            continue
        for item in (j if isinstance(j, list) else [j]):
            t = item.get("@type") if isinstance(item, dict) else None
            if isinstance(t, str) and (t.endswith("Event") or t == "Festival"):
                return item
    return None


_CITY_NAMES = {"rio de janeiro", "sao paulo", "são paulo", "brasil", "brazil", "rj", "sp"}


def _neighborhood(addr) -> str:
    """Prefer addressLocality when it is a bairro; else parse
    'Rua do Resende 82 A - Lapa - Centro, Rio de Janeiro - RJ, ...' -> 'Lapa'."""
    if isinstance(addr, dict):
        loc = (addr.get("addressLocality") or "").strip()
        if loc and loc.lower() not in _CITY_NAMES:
            return loc
        addr = addr.get("streetAddress", "")
    first = (addr or "").split(",")[0]
    parts = [p.strip() for p in first.split(" - ")[1:] if p.strip()]
    parts = [p for p in parts if not re.search(r"\d{5}|^(rj|sp|brasil|brazil)$", p, re.I)]
    return parts[0] if parts else ""


def build_event(card: dict, detail: dict | None) -> Event | None:
    start = parse_iso(card["start"])
    if start is None:
        return None
    url = f"{BASE}/en/events/{card['slug']}"
    label = card.get("label", "")
    is_free = label.strip().lower() in ("free", "grátis", "gratuito")
    price_min = 0.0 if is_free else _price_en(label)
    price_max = None
    sold_out = bool(re.search(r"sold out|esgotad|wait", label, re.I))
    artists: list[str] = []
    desc = ""
    neighborhood = ""
    end = None
    etype = ""
    venue = card.get("venue", "")
    image = card.get("image", "")
    if detail:
        etype = detail.get("@type", "")
        end = parse_iso(detail.get("endDate"))
        start = parse_iso(detail.get("startDate")) or start
        loc = detail.get("location") or {}
        if isinstance(loc, list):
            loc = loc[0] if loc else {}
        venue = venue or loc.get("name", "")
        addr = (loc.get("address") or {})
        neighborhood = _neighborhood(addr if isinstance(addr, dict) else str(addr))
        artists = [p.get("name", "") for p in (detail.get("performer") or []) if isinstance(p, dict)]
        offers = detail.get("offers") or []
        if isinstance(offers, dict):
            offers = [offers]
        prices = [float(o["price"]) for o in offers if isinstance(o.get("price"), (int, float))]
        avail = [o for o in offers if "InStock" in str(o.get("availability", ""))
                 or "LimitedAvailability" in str(o.get("availability", ""))]
        avail_prices = [float(o["price"]) for o in avail if isinstance(o.get("price"), (int, float))]
        if avail_prices:
            price_min, price_max = min(avail_prices), max(avail_prices)
        elif prices:
            price_min, price_max = min(prices), max(prices)
        if offers and not avail:
            sold_out = True
        if "EventCancelled" in str(detail.get("eventStatus", "")):
            return None
        desc = clean_text(detail.get("description"), 600)
        image = detail.get("image") or image
        if isinstance(image, list):
            image = image[0] if image else ""
    tags = card.get("tags", [])
    parts = []
    if tags:
        parts.append("Genres: " + ", ".join(tags) + ".")
    if desc:
        parts.append(desc)
    description = " ".join(parts)[:800]
    # The city page is Shotgun's music listing, so untagged cards are kept; only events whose
    # JSON-LD declares a non-music type must also pass the keyword check.
    if etype in NON_MUSIC_TYPES and not tags and not looks_like_music(card["title"], description):
        return None
    return Event(
        city="", title=card["title"], start_datetime=start, end_datetime=end, source="shotgun",
        venue=venue, neighborhood=neighborhood, artists=artists, price_min=price_min,
        price_max=price_max, is_free=is_free or price_min == 0, ticket_url=url, source_url=url,
        image_url=image, sold_out=sold_out, description=description,
        genres=[t.lower() for t in tags],
    )


@collector("shotgun")
def collect(city: CityConfig, cfg: dict):
    d = {**DEFAULTS.get(city.slug, {}), **(cfg or {})}
    slug = d.get("city_slug")
    if not slug:
        log.warning("shotgun: no city_slug for %s", city.slug)
        return []
    transport = d.get("transport", "auto")  # auto | requests | impersonate
    # always the en locale: the price parser expects "R$1,234.00"
    html = _fetch(f"{BASE}/en/cities/{slug}?page={int(d.get('pages', 60))}", transport)
    cards = parse_listing(html)
    log.info("shotgun[%s]: %d cards on city page", city.slug, len(cards))
    lo, hi = horizon()
    cards = [c for c in cards if (s := parse_iso(c["start"])) and lo <= s <= hi]
    details: dict[str, dict | None] = {}
    if d.get("fetch_details", True):
        todo = cards[: int(d.get("max_details", 250))]

        def one(c):
            for attempt in range(2):
                try:
                    return c["slug"], parse_detail(_fetch(f"{BASE}/en/events/{c['slug']}", transport))
                except Exception as exc:
                    if attempt:
                        log.warning("shotgun detail %s failed: %r", c["slug"], exc)
                    else:
                        time.sleep(2)
            return c["slug"], None
        with ThreadPoolExecutor(int(d.get("workers", 6))) as ex:
            details = dict(ex.map(one, todo))
    events = []
    for c in cards:
        ev = build_event(c, details.get(c["slug"]))
        if ev:
            events.append(ev)
    return events
