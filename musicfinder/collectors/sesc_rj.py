"""SESC Rio de Janeiro — culture programme at cultura.sescrio.org.br.

www.sescrio.org.br blocks its WP REST API (403) and only offers an admin-ajax HTML card list with
no prices or units. The culture portal ``cultura.sescrio.org.br`` (Next.js app on top of a headless
WordPress at apicultura.sescrio.org.br, whose REST API is also closed) server-renders all data into
the React Server Components payload (``self.__next_f.push([1, "..."])`` script chunks):

* ``GET /programacao?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD&page=N`` -> ``{"eventos": [...],
  "currentPage", "totalPages"}``; 12 cards per page with slug, title, date, image, location and tags
  (the "MÚSICA" tag marks music, taxonomy ``tipo_evento``).
* ``GET /programacao/<slug>`` -> ``{"evento": {...}}`` with ``categoria`` (Show, Concerto, Roda de
  Samba ...), ``sinopseMarkdown``, and ``schedule``: one block per unit (``location``) with
  ``acao.url`` (ticket link, usually ingresso.com) and ``sessions`` (``dateIso`` "start/end",
  ``timeIso`` "HH:MM:SS/HH:MM:SS", ``ingressos`` [{tipo: Inteira|Meia|Credencial Sesc Plena|...,
  preco}], ``isFree``, ``action``).

Config (config/rio.yaml -> collectors.sesc_rj), all optional::

    sesc_rj:
      units: [Sesc Copacabana, Sesc Tijuca, ...]  # unit names (prefix match), default: Rio city
      extra_units: [Sesc Niterói]                # added to `units`
      include_metro: false                       # also Niterói, São Gonçalo, Baixada units
      days: 90                                   # listing window
      music_tags: [MÚSICA]
      exclude_categories: [Roda de Conversa, Palestra, Oficina, ...]
      workers: 6
"""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

from .. import http
from ..config import CityConfig
from ..models import Event, TZ, norm
from .base import collector
from .util import clean_text, parse_price

log = logging.getLogger(__name__)

NAME = "sesc_rj"
BASE = "https://cultura.sescrio.org.br"

# unit name -> neighbourhood. Units inside the city of Rio de Janeiro.
CITY_UNITS: dict[str, str] = {
    "Sesc Copacabana": "Copacabana",
    "Sesc Tijuca": "Tijuca",
    "Sesc Madureira": "Madureira",
    "Sesc Engenho de Dentro": "Engenho de Dentro",
    "Sesc Ramos": "Ramos",
    "Sesc Ginástico": "Centro",
    "Sesc Flamengo": "Flamengo",
    "Arte Sesc": "Flamengo",
    "Sesc Botafogo": "Botafogo",
    "Sesc Cocotá": "Ilha do Governador",
    "Sesc Barra da Tijuca": "Barra da Tijuca",
    "Sesc Campo Grande": "Campo Grande",
}
# Rio metro outside the city — opt in via include_metro or extra_units.
METRO_UNITS: dict[str, str] = {
    "Sesc Niterói": "Niterói",
    "Sesc São Gonçalo": "São Gonçalo",
    "Sesc Duque de Caxias": "Duque de Caxias",
    "Sesc Nova Iguaçu": "Nova Iguaçu",
    "Sesc São João de Meriti": "São João de Meriti",
}

DEFAULT_EXCLUDE_CATEGORIES = (
    "Roda de Conversa", "Palestra", "Oficina", "Encontro", "Atividade Formativa", "Aula",
    "Mediação", "Mediação de Leitura", "Curta-Metragem", "Longa-Metragem", "Exposição", "Imersão",
    "Exibição", "Podcast", "Leitura", "Literatura", "Campanha", "Feira", "Contação de História",
    "Graffiti", "Vivência",
)

_PUSH = re.compile(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>', re.S)


# --------------------------------------------------------------------------- RSC parsing

def rsc_payload(page_html: str) -> str:
    """Concatenate the Next.js flight-data chunks embedded in a server-rendered page."""
    out = []
    for chunk in _PUSH.findall(page_html):
        try:
            out.append(json.loads('"' + chunk + '"'))
        except json.JSONDecodeError:
            continue
    return "".join(out)


def _extract_object(payload: str, key: str) -> dict | None:
    i = payload.find('{"%s":' % key)
    if i < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(payload, i)
    except json.JSONDecodeError:
        return None
    return obj


def parse_listing(page_html: str) -> tuple[list[dict], int]:
    obj = _extract_object(rsc_payload(page_html), "eventos") or {}
    return obj.get("eventos") or [], int(obj.get("totalPages") or 1)


def parse_detail(page_html: str) -> dict | None:
    obj = _extract_object(rsc_payload(page_html), "evento")
    return (obj or {}).get("evento")


def _undef(v):
    return None if v in ("$undefined", None, "") else v


# --------------------------------------------------------------------------- event building

def _match_unit(location: str, units: dict[str, str]) -> tuple[str, str] | None:
    loc = norm(location)
    for name, hood in units.items():
        if loc == norm(name) or loc.startswith(norm(name) + " "):
            return name, hood
    return None


def _artists(title: str) -> list[str]:
    t = re.sub(r"(?i)^sesc [^:]+:\s*", "", title).strip()
    m = re.match(r"(?i)^(.+?)\s+convida\s+(.+)$", t)
    if m:
        return [m.group(1).strip(), *[p.strip() for p in re.split(r",\s*|\s+e\s+", m.group(2))]]
    # "Title – Artist" when the part after the dash looks like a name (shorter), else lead part
    parts = re.split(r"\s*[–—]\s*|\s+-\s+|(?<=\w)-\s+", t)
    lead = re.split(r"\s+(?:apresenta|canta|toca)\s+", parts[0], flags=re.I)[0].strip()
    if len(parts) > 1 and len(parts[-1]) < len(parts[0]) and ":" not in parts[0]:
        return [parts[-1].strip()]
    return [lead] if lead else []


def _num(v) -> float | None:
    """'7.50' / '7,50' / 'R$ 15' -> float (the site uses a dot as decimal separator)."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    t = str(v).strip()
    try:
        return float(t)
    except ValueError:
        return parse_price(t)


def _session_prices(s: dict) -> tuple[float | None, float | None, bool]:
    if s.get("isFree") or norm(s.get("price") or "") in ("gratis", "gratuito", "entrada franca"):
        return 0.0, 0.0, True
    tiers = {}
    for ing in s.get("ingressos") or []:
        p = _num(ing.get("preco"))
        if p is not None:
            tiers[norm(ing.get("tipo") or "")] = p
    if not tiers:
        vals = [_num(x) for x in str(s.get("price") or "").split("|")]
        vals = [v for v in vals if v is not None]
        if not vals:
            return None, None, False
        return min(vals), max(vals), max(vals) == 0
    positive = [v for v in tiers.values() if v > 0]
    if not positive:
        return 0.0, 0.0, True
    inteira = tiers.get("inteira")
    return min(positive), (inteira if inteira else max(positive)), False


def _session_starts(s: dict) -> list[tuple[datetime, datetime | None]]:
    d_iso = (s.get("dateIso") or "").split("/")
    t_iso = (s.get("timeIso") or s.get("time") or "").split("/")
    try:
        d0 = date.fromisoformat(d_iso[0])
        d1 = date.fromisoformat(d_iso[1]) if len(d_iso) > 1 and d_iso[1] else d0
    except ValueError:
        return []
    def hm(x):
        m = re.match(r"(\d{1,2}):(\d{2})", x or "")
        return (int(m.group(1)), int(m.group(2))) if m else None
    start_t = hm(t_iso[0]) or (0, 0)
    end_t = hm(t_iso[1]) if len(t_iso) > 1 else None
    out = []
    days = min((d1 - d0).days, 62) if d1 >= d0 else 0
    for i in range(days + 1):
        d = d0 + timedelta(days=i)
        st = datetime(d.year, d.month, d.day, *start_t, tzinfo=TZ)
        en = datetime(d.year, d.month, d.day, *end_t, tzinfo=TZ) if end_t else None
        out.append((st, en if en and en > st else None))
    return out


def build_events(ev: dict, units: dict[str, str], card: dict | None = None,
                 city_slug: str = "rio") -> list[Event]:
    card = card or {}
    title = clean_text(ev.get("title") or card.get("title") or "", 200)
    if not title:
        return []
    slug = ev.get("slug") or card.get("slug") or ""
    source_url = f"{BASE}/programacao/{slug}" if slug else ""
    category = _undef(ev.get("categoria")) or ""
    synopsis = clean_text(_undef(ev.get("sinopseMarkdown")) or "", 1000)
    description = " — ".join(p for p in (category, synopsis) if p)
    image = _undef(ev.get("image")) or card.get("image") or ""
    generic_ticket = _undef(ev.get("urlVendaIngressos")) or ""
    artists = [e.get("nome") for e in (ev.get("elenco") or []) if isinstance(e, dict) and e.get("nome")]
    artists = artists or _artists(title)

    out, seen = [], set()
    for block in ev.get("schedule") or []:
        loc = block.get("location") or ""
        hit = _match_unit(loc, units)
        if not hit:
            continue
        venue = loc.strip()
        hood = hit[1]
        acao = _undef(block.get("acao"))
        ticket = (acao or {}).get("url") if isinstance(acao, dict) else None
        for s in block.get("sessions") or []:
            pmin, pmax, free = _session_prices(s)
            sold = "esgotad" in norm(s.get("action") or "")
            for st, en in _session_starts(s):
                key = (venue, st)
                if key in seen:
                    continue
                seen.add(key)
                out.append(Event(
                    city=city_slug, title=title, start_datetime=st, end_datetime=en,
                    source=NAME, venue=venue, neighborhood=hood, artists=list(artists),
                    price_min=pmin, price_max=pmax, is_free=free, sold_out=sold,
                    ticket_url=ticket or generic_ticket or source_url, source_url=source_url,
                    image_url=image, description=description,
                ))
    return out


# --------------------------------------------------------------------------- fetching

def _wanted_units(cfg: dict) -> dict[str, str]:
    known = {**CITY_UNITS, **METRO_UNITS}
    if cfg.get("units"):
        units = {u: known.get(u, "") for u in cfg["units"]}
    else:
        units = dict(CITY_UNITS)
    if cfg.get("include_metro"):
        units.update(METRO_UNITS)
    for u in cfg.get("extra_units") or []:
        units[u] = known.get(u, "")
    return units


def fetch_listing(days: int) -> list[dict]:
    today = datetime.now(TZ).date()
    params = {"date_from": today.isoformat(),
              "date_to": (today + timedelta(days=days)).isoformat()}
    cards, page, total = [], 1, 1
    while page <= total and page <= 60:
        r = http.get(f"{BASE}/programacao", params={**params, "page": page})
        batch, total = parse_listing(r.text)
        if not batch:
            break
        cards.extend(batch)
        page += 1
    return cards


def fetch_detail(slug: str) -> dict | None:
    try:
        return parse_detail(http.get(f"{BASE}/programacao/{slug}").text)
    except Exception as exc:
        log.debug("sesc_rj detail %s failed: %s", slug, exc)
        return None


@collector(NAME)
def collect(city: CityConfig, cfg: dict):
    cfg = cfg or {}
    units = _wanted_units(cfg)
    music_tags = {norm(t) for t in (cfg.get("music_tags") or ["MÚSICA"])}
    excluded = {norm(c) for c in (cfg.get("exclude_categories") or DEFAULT_EXCLUDE_CATEGORIES)}

    cards = fetch_listing(int(cfg.get("days", 90)))
    music: dict[str, dict] = {}
    for c in cards:
        if any(norm(t.get("label") or "") in music_tags for t in c.get("tags") or []):
            music.setdefault(c.get("slug"), c)
    log.info("sesc_rj: %d listing cards, %d music", len(cards), len(music))

    def enrich(card):
        ev = fetch_detail(card["slug"])
        if not ev:
            return []
        if norm(_undef(ev.get("categoria")) or "") in excluded:
            return []
        return build_events(ev, units, card, city.slug)

    with ThreadPoolExecutor(max_workers=int(cfg.get("workers", 6))) as pool:
        for evs in pool.map(enrich, [c for c in music.values() if c.get("slug")]):
            yield from evs
