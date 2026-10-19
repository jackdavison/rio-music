"""Fever (feverup.com) — music plans in Rio / SP, including Candlelight and The Jazz Room.

feverup.com is an Angular SSR app; every page embeds its API responses in
`<script id="serverapp-state" type="application/json">`.

1. Category ("what plan filter") pages such as /pt/rio-de-janeiro/eventos-musica carry
   `WhatPlanFilterService.getWPFSkeleton.<CITY><wpf id>` -> skeleton[type=plan_grid].plans
   (id, name, venue, first/last active session, "from" price). We union the music filters.
2. Each plan page (/m/<id>) carries `PlanService.getPlanDetail.<id>` (categories, description,
   address, cover) and `LEVEL_TICKET_SELECTOR_SESSIONS...` — a date -> time -> zone -> ticket
   tree whose leaves have starts_at_iso / price / has_available_tickets. One Event is emitted per
   distinct start time (a Candlelight plan runs on many dates).
"""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor

from .. import http
from ..config import CityConfig
from ..models import Event
from .base import collector, horizon
from .util import clean_text, looks_like_music, parse_iso

log = logging.getLogger(__name__)

BASE = "https://feverup.com"
MUSIC_FILTERS = ["eventos-musica", "concertos", "candlelight", "the-jazz-room", "opera",
                 "festivais"]
# Fever's city pages also list plans in nearby cities (Campinas, Santos, Petrópolis...);
# keep only plans whose cityCode is the city itself or its immediate metro area.
DEFAULTS = {
    "rio": {"city_slug": "rio-de-janeiro", "filters": MUSIC_FILTERS,
            "city_codes": ["GIG", "QNT"]},                      # Rio, Niterói
    "sp": {"city_slug": "sao-paulo", "filters": MUSIC_FILTERS,
           "city_codes": ["GRU", "CCE", "NDF"]},                # SP, ABC, Guarulhos
}
MUSIC_CATEGORIES = ("concert", "music", "candlelight", "jazz", "opera", "gig", "festival",
                    "orchestra", "dj")
NON_MUSIC_NAMES = re.compile(r"cart[aã]o[- ]presente|gift ?card|vale[- ]presente", re.I)

_STATE_RE = re.compile(r'<script id="serverapp-state" type="application/json">(.*?)</script>', re.S)


def page_state(html: str) -> dict:
    m = _STATE_RE.search(html)
    return json.loads(m.group(1)) if m else {}


def plans_from_filter(state: dict) -> list[dict]:
    out = []
    for k, v in state.items():
        if not k.startswith("WhatPlanFilterService.getWPFSkeleton") or not isinstance(v, dict):
            continue
        for block in v.get("skeleton") or []:
            if block.get("type") == "plan_grid":  # carousels are city-wide highlights
                out.extend((block.get("content") or {}).get("plans") or [])
    return out


def _walk_sessions(level: dict | None, out: list):
    if not level:
        return
    for it in level.get("items") or []:
        if level.get("type") == "session":
            out.append(it.get("value") or {})
        _walk_sessions(it.get("level"), out)


def plan_detail(state: dict, plan_id) -> tuple[dict, list[dict]]:
    # regular plan pages use PlanService.getPlanDetail.<id>; "immersive" (branded) plan pages
    # carry the same object under immersive-plan-detail:<id>:...
    detail = state.get(f"PlanService.getPlanDetail.{plan_id}") or next(
        (v for k, v in state.items()
         if k.startswith(("PlanService.getPlanDetail", f"immersive-plan-detail:{plan_id}"))
         and isinstance(v, dict)), {}) or {}
    sessions: list[dict] = []
    for k, v in state.items():
        if k.startswith("LEVEL_TICKET_SELECTOR_SESSIONS") and isinstance(v, dict):
            _walk_sessions(v.get("level"), sessions)
    artists = []
    a = state.get(f"plan-artists:{plan_id}")
    if isinstance(a, dict):
        artists = [x.get("name", "") for x in a.get("artists") or []]
    detail = dict(detail)
    detail["_artists"] = artists
    return detail, sessions


def _neighborhood(address: str) -> str:
    """'Rua General Polidoro, 180 - Botafogo, Rio de Janeiro, 22280-003' -> 'Botafogo'"""
    m = re.search(r" - ([^,]+),", address or "")
    return m.group(1).split(" - ")[-1].strip() if m else ""


def is_music(plan: dict, detail: dict) -> bool:
    name = plan.get("name") or detail.get("name") or ""
    if NON_MUSIC_NAMES.search(name) or detail.get("isTimeless") or (plan.get("extra") or {}).get(
            "timeless"):
        return False
    cats = " ".join([detail.get("category") or ""] + list(detail.get("categories") or [])).lower()
    if cats.strip():
        return any(c in cats for c in MUSIC_CATEGORIES)
    return looks_like_music(name, clean_text(detail.get("description"), 400))


def build_events(plan: dict, detail: dict, sessions: list[dict]) -> list[Event]:
    pid = plan.get("id") or detail.get("id")
    name = (plan.get("name") or detail.get("name") or "").strip()
    url = f"{BASE}/m/{pid}"
    place = detail.get("defaultPlace") or (detail.get("places") or [{}])[0] or {}
    venue = place.get("name") or (plan.get("location") or {}).get("name", "")
    if (plan.get("location") or {}).get("is_hidden"):
        venue = venue or "Local secreto"
    addr = place.get("address", "")
    image = detail.get("coverImage") or plan.get("cover_image", "")
    cats = list(detail.get("categories") or [])
    candle = name.lower().startswith("candlelight") or any("candlelight" in c.lower() for c in cats)
    tags = []
    if candle:
        tags.append("Candlelight concert (candle-lit, usually a string quartet or piano playing "
                    "classical or tribute arrangements)")
    elif name.lower().startswith("the jazz room"):
        tags.append("The Jazz Room (Fever jazz tribute series)")
    if cats:
        tags.append("Categories: " + ", ".join(cats))
    desc = clean_text(detail.get("description"), 500)
    description = ". ".join(tags + ([addr] if addr else []) + ([desc] if desc else []))[:900]
    artists = [a for a in detail.get("_artists") or [] if a]

    by_start: dict[str, list[dict]] = {}
    for s in sessions:
        if s.get("starts_at_iso"):
            by_start.setdefault(s["starts_at_iso"], []).append(s)
    if not by_start and plan.get("first_active_session_date"):
        by_start[plan["first_active_session_date"]] = []
    from_price = (plan.get("price_info") or detail.get("priceInfo") or {}).get("amount")

    out = []
    for start_s, items in sorted(by_start.items()):
        start = parse_iso(start_s)
        if not start:
            continue
        avail = [i for i in items if i.get("has_available_tickets")]
        prices = [float(i["price"]) for i in (avail or items) if isinstance(i.get("price"), (int, float))]
        end = parse_iso(next((i.get("ends_at_iso") for i in items if i.get("ends_at_iso")), None))
        out.append(Event(
            city="", title=name, start_datetime=start, end_datetime=end, source="fever",
            venue=venue, neighborhood=_neighborhood(addr), artists=artists,
            price_min=min(prices) if prices else (float(from_price) if from_price else None),
            price_max=max(prices) if prices else None,
            ticket_url=url, source_url=url, image_url=image,
            sold_out=bool(items) and not avail, description=description,
            genres=["candlelight"] if candle else [],
        ))
    return out


@collector("fever")
def collect(city: CityConfig, cfg: dict):
    d = {**DEFAULTS.get(city.slug, {}), **(cfg or {})}
    slug = d.get("city_slug")
    if not slug:
        return []
    lang = d.get("lang", "pt")
    plans: dict = {}
    for f in d.get("filters", MUSIC_FILTERS):
        try:
            r = http.session().get(f"{BASE}/{lang}/{slug}/{f}")
            if r.status_code == 404:
                continue
            r.raise_for_status()
            found = plans_from_filter(page_state(r.text))
            log.info("fever[%s]: filter %s -> %d plans", city.slug, f, len(found))
            for p in found:
                plans.setdefault(p["id"], p)
        except Exception as exc:
            log.warning("fever[%s]: filter %s failed: %s", city.slug, f, exc)
    lo, hi = horizon()
    todo = [p for p in plans.values()
            if not NON_MUSIC_NAMES.search(p.get("name", ""))
            and (parse_iso(p.get("first_active_session_date")) or hi) <= hi
            and (parse_iso(p.get("last_active_session_date")) or lo) >= lo]
    todo = todo[: int(d.get("max_plans", 150))]
    codes = set(d.get("city_codes") or [])

    def one(p):
        try:
            detail, sessions = plan_detail(page_state(http.get(f"{BASE}/m/{p['id']}").text), p["id"])
        except Exception as exc:
            log.warning("fever: plan %s failed: %s", p["id"], exc)
            detail, sessions = {}, []
        if detail and not is_music(p, detail):
            return []
        if codes and detail.get("cityCode") and detail["cityCode"] not in codes:
            return []
        return build_events(p, detail, sessions)

    events = []
    with ThreadPoolExecutor(int(d.get("workers", 6))) as ex:
        for evs in ex.map(one, todo):
            events.extend(evs)
    return events
