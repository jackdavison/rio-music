"""Queremos! (queremos.com.br) — crowdfunded/promoted concerts in Rio and SP.

The site is a Wix page whose "agenda" is a CMS repeater rendered server-side: each card has a
title (artists), a pt-BR day+month ("05 de Novembro", no year, no time), a city and a ticket
link (Sympla Bileto, Ticketmaster, uhuu...). We read the cards from the homepage and the
fuller agenda page, keep the ones for this city, and enrich them from the ticket provider:

* bileto.sympla.com.br links -> the Bileto sales BFF JSON
  (`bff-sales-api-cdn.bileto.sympla.com.br/api/v1/events/<id>`, public web-app key sent as
  `x-api-key`), which gives exact local start time, venue + address, price range (cents),
  availability and description.
* other providers are SPA/WAF-protected, so those events keep the card date with a default
  start time (`default_time`, 21:00) flagged in the description, and a venue guessed from the
  ticket URL slug ("...-no-circo-voador-rj" -> "Circo Voador").

Queremos only promotes concerts, so no extra music filter is applied.
"""
from __future__ import annotations

import logging
import re
from collections import OrderedDict
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup

from .. import http
from ..config import CityConfig
from ..models import Event, TZ, norm
from .base import collector
from .util import MONTHS_PT, clean_text, parse_iso

log = logging.getLogger(__name__)

PAGES = ["https://www.queremos.com.br/", "https://www.queremos.com.br/copy-of-agenda"]
BILETO_API = "https://bff-sales-api-cdn.bileto.sympla.com.br/api/v1/events/{id}"
BILETO_KEY = "cQkazy2Wc"  # public key shipped in bileto's web bundle (config.bff.api_key)

DEFAULTS = {
    "rio": {"city_names": ["rio de janeiro", "rio"], "utm_city": "rj"},
    "sp": {"city_names": ["sao paulo", "sp"], "utm_city": "sp"},
}

_DATE_RE = re.compile(r"(\d{1,2})\s*(?:de\s+)?([a-zç]{3,9})(?:\s+(?:de\s+)?(\d{4}))?", re.I)
_TIME_RE = re.compile(r"(\d{1,2})[:h](\d{2})?")


def _clean(t: str) -> str:
    return re.sub(r"[\s​\xa0]+", " ", t or "").strip()


def parse_cards(html: str) -> list[dict]:
    """Group Wix repeater elements by item id -> {id,title,date_text,city,ticket_url,image}."""
    soup = BeautifulSoup(html, "lxml")
    items: "OrderedDict[str, list]" = OrderedDict()
    for el in soup.find_all(id=re.compile(r"__item-")):
        items.setdefault(el["id"].split("__item-", 1)[1], []).append(el)
    out = []
    for item_id, els in items.items():
        texts = []
        for el in els:
            if "wixui-rich-text" in " ".join(el.get("class", [])):
                t = _clean(el.get_text(" ", strip=True))
                if t and t not in texts:
                    texts.append(t)
        links = []
        for el in els:
            for a in el.find_all("a", href=True):
                h = a["href"]
                if "queremos.com.br" not in h and not h.startswith("#") and h not in links:
                    links.append(h)
        img = next((i.get("src") for el in els for i in el.find_all("img") if i.get("src")), "")
        date_text = next((t for t in texts if _DATE_RE.fullmatch(t.lower().strip())
                          or (_DATE_RE.search(t.lower()) and _month(t))), "")
        city = next((t for t in texts if norm(t) in ("rio de janeiro", "sao paulo", "rio", "sp",
                                                     "belo horizonte", "salvador", "curitiba",
                                                     "porto alegre", "brasilia", "recife")), "")
        rest = [t for t in texts if t not in (date_text, city)]
        if not rest or not date_text or not links:
            continue
        out.append({"id": item_id, "title": rest[0], "extra": rest[1:], "date_text": date_text,
                    "city": city, "ticket_url": links[0], "image": img})
    return out


def _month(text: str) -> int | None:
    m = _DATE_RE.search(norm(text))
    if not m:
        return None
    return MONTHS_PT.get(m.group(2)[:3]) if m.group(2)[:3] in MONTHS_PT else MONTHS_PT.get(m.group(2))


def card_date(text: str, today: datetime | None = None) -> datetime | None:
    """'05 de Novembro' -> next such date (this year, or next year if already >2 days past)."""
    m = _DATE_RE.search(norm(text))
    if not m:
        return None
    mon = MONTHS_PT.get(m.group(2)) or MONTHS_PT.get(m.group(2)[:3])
    if not mon:
        return None
    today = today or datetime.now(TZ)
    year = int(m.group(3)) if m.group(3) else today.year
    try:
        d = datetime(year, mon, int(m.group(1)), tzinfo=TZ)
    except ValueError:
        return None
    if not m.group(3) and d < today - timedelta(days=2):
        d = d.replace(year=year + 1)
    return d


def _venue_from_slug(url: str) -> str:
    """'.../the-cat-empire-no-circo-voador-rj' -> 'Circo Voador'"""
    path = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
    m = re.search(r"-(?:no|na|em)-([a-z0-9-]+?)(?:-(?:rj|sp))?$", path)
    if not m:
        return ""
    return " ".join(w.capitalize() for w in m.group(1).split("-"))


def _card_city(card: dict) -> str:
    """Normalised city of a card, from its city text or the utm_campaign (`..._rj_...`)."""
    if card.get("city"):
        return norm(card["city"])
    camp = parse_qs(urlsplit(card["ticket_url"]).query).get("utm_campaign", [""])[0]
    m = re.search(r"_(rj|sp)_", camp)
    return {"rj": "rio de janeiro", "sp": "sao paulo"}.get(m.group(1), "") if m else ""


def _cents(p) -> float | None:
    try:
        return int(p["value"]) / 100.0
    except (TypeError, KeyError, ValueError):
        return None


def bileto_event_id(url: str) -> str | None:
    m = re.search(r"bileto\.sympla\.com\.br/event/(\d+)", url)
    return m.group(1) if m else None


def fetch_bileto(event_id: str) -> dict:
    return http.get_json(BILETO_API.format(id=event_id), headers={"x-api-key": BILETO_KEY})["data"]


def events_from_bileto(card: dict, data: dict, ticket_url: str) -> list[Event]:
    venue = data.get("venue") or {}
    loc = venue.get("locale") or {}
    desc = clean_text((data.get("description") or {}).get("raw"), 600)
    img = next((m.get("url") for m in data.get("medias") or [] if m.get("url")), "") \
        or data.get("notification_image") or card.get("image", "")
    tags = list((data.get("tags") or {}).values())
    want = card_date(card["date_text"])
    pres = (data.get("presentations") or {}).get("items") or []
    if want:
        match = [p for p in pres if (parse_iso(p.get("presentation_local_date_time")) or want).date()
                 == want.date()]
        pres = match or pres
    out = []
    for p in pres:
        start = parse_iso(p.get("presentation_local_date_time"))
        if not start or p.get("enabled") is False:
            continue
        avail = p.get("total_available")
        sold = (avail is not None and avail <= 0) or not any(
            s.get("has_seats_available", True) for s in p.get("sectors") or [{}])
        out.append(Event(
            city="", title=card["title"], start_datetime=start, source="queremos",
            venue=venue.get("name", ""), neighborhood="",
            artists=_artists(card["title"]),
            price_min=_cents(p.get("lowest_price")), price_max=_cents(p.get("highest_price")),
            ticket_url=ticket_url, source_url=PAGES[0], image_url=img, sold_out=sold,
            description=_desc(card, (f"{loc.get('address', '')}. " if loc.get("address") else "")
                              + (("Tags: " + ", ".join(tags) + ". ") if tags else "") + desc),
        ))
    return out


def _artists(title: str) -> list[str]:
    t = re.sub(r"^queremos!?\s*", "", title, flags=re.I)
    return [a.strip() for a in re.split(r",|\s+\+\s+", t) if a.strip()]


def _desc(card: dict, more: str = "") -> str:
    head = "Queremos! show"
    if card.get("extra"):
        head += " — " + " ".join(card["extra"])
    return (head + ". " + more).strip()[:800]


def event_from_card(card: dict, default_time: str = "21:00") -> Event | None:
    d = card_date(card["date_text"])
    if not d:
        return None
    hh, mm = (int(x) for x in default_time.split(":"))
    start = d.replace(hour=hh, minute=mm)
    return Event(
        city="", title=card["title"], start_datetime=start, source="queremos",
        venue=_venue_from_slug(card["ticket_url"]), artists=_artists(card["title"]),
        ticket_url=card["ticket_url"], source_url=PAGES[0], image_url=card.get("image", ""),
        description=_desc(card, f"Start time not published on queremos.com.br "
                                f"({default_time} is a placeholder); see ticket link."),
    )


def _strip_utm(url: str) -> str:
    return re.sub(r"[?&]utm_[^#]*$", "", url) if "utm_" in url else url


@collector("queremos")
def collect(city: CityConfig, cfg: dict):
    d = {**DEFAULTS.get(city.slug, {}), **(cfg or {})}
    names = {norm(n) for n in d.get("city_names", [city.name])}
    pages = d.get("pages", PAGES)
    cards: "OrderedDict[str, dict]" = OrderedDict()
    for url in pages:
        try:
            for c in parse_cards(http.get(url).text):
                key = (norm(c["title"]), c["date_text"].lower(), _card_city(c))
                cards.setdefault(str(key), c)
        except Exception as exc:
            log.warning("queremos: %s failed: %s", url, exc)
    mine = [c for c in cards.values() if _card_city(c) in names]
    log.info("queremos[%s]: %d cards total, %d for this city", city.slug, len(cards), len(mine))
    events = []
    for c in mine:
        ticket = _strip_utm(c["ticket_url"]) if d.get("strip_utm", True) else c["ticket_url"]
        eid = bileto_event_id(c["ticket_url"])
        if eid:
            try:
                evs = events_from_bileto(c, fetch_bileto(eid), ticket)
                if evs:
                    events.extend(evs)
                    continue
            except Exception as exc:
                log.warning("queremos: bileto %s failed: %s", eid, exc)
        ev = event_from_card({**c, "ticket_url": ticket}, d.get("default_time", "21:00"))
        if ev:
            events.append(ev)
    return events
