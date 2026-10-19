"""SESC São Paulo (sescsp.org.br).

The programação SPA is backed by a public (unauthenticated) WordPress REST namespace:

* ``GET /wp-json/wp/v1/atividades/filter?tipo=atividade&linguagem=musica&ppp=300&page=N``
  -> ``{"atividade": [...], "total": {"value": N}}``. Each item has titulo, complemento, link
  (``/programacao/<slug>``), imagem, unidade[{name, link:/unidades/<slug>}], datasSessoes,
  dataPrimeiraSessao, gratuito, cancelado, esgotado and ``id_java`` (only for ticketed activities).
* ``GET /wp-json/wp/v1/atividades/detail?slug=<slug>`` -> ``{"sessoes": [...]}`` for ticketed
  activities: per-session start/end (local time), valorInteira / valorMeia / valorComerciario
  (credencial plena), statusSessaoSesc (``ESGOTADO`` ...), urlCompra.
* The activity HTML page (server rendered) carries ``og:description`` and, for free activities that
  have no ticketing sessions, ``data-inicial`` / ``data-final`` attributes per session.
* ``GET /wp-json/wp/v1/unidades-atividades`` lists units (slug + "capital"/"interior"/"litoral").

Config (config/sp.yaml -> collectors.sesc_sp), all optional::

    sesc_sp:
      units: [pompeia, vila-mariana, ...]   # unit slugs to keep (default: São Paulo city units)
      extra_units: [santo-andre, guarulhos] # added to `units` (e.g. Greater SP units)
      include_metro: false                  # shortcut: also keep Greater-SP units
      linguagens: [musica]                  # tipos-linguagens slugs to query
      fetch_pages: true                     # fetch activity HTML for description/free sessions
      workers: 6
"""
from __future__ import annotations

import html as htmllib
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from .. import http
from ..config import CityConfig
from ..models import Event
from .base import collector
from .util import clean_text, parse_iso

log = logging.getLogger(__name__)

NAME = "sesc_sp"
BASE = "https://www.sescsp.org.br"
API = BASE + "/wp-json/wp/v1"

# slug -> (display name, neighbourhood). Units inside the city of São Paulo.
CITY_UNITS: dict[str, tuple[str, str]] = {
    "14-bis": ("Sesc 14 Bis", "Bela Vista"),
    "24-de-maio": ("Sesc 24 de Maio", "República"),
    "avenida-paulista": ("Sesc Avenida Paulista", "Bela Vista"),
    "belenzinho": ("Sesc Belenzinho", "Belenzinho"),
    "bom-retiro": ("Sesc Bom Retiro", "Bom Retiro"),
    "campo-limpo": ("Sesc Campo Limpo", "Campo Limpo"),
    "carmo": ("Sesc Carmo", "Sé"),
    "casa-verde": ("Sesc Casa Verde", "Casa Verde"),
    "centro-de-pesquisa-e-formacao": ("Sesc Centro de Pesquisa e Formação", "Bela Vista"),
    "cinesesc": ("CineSesc", "Cerqueira César"),
    "consolacao": ("Sesc Consolação", "Vila Buarque"),
    "interlagos": ("Sesc Interlagos", "Interlagos"),
    "ipiranga": ("Sesc Ipiranga", "Ipiranga"),
    "itaquera": ("Sesc Itaquera", "Itaquera"),
    "pinheiros": ("Sesc Pinheiros", "Pinheiros"),
    "pompeia": ("Sesc Pompeia", "Pompeia"),
    "santana": ("Sesc Santana", "Santana"),
    "santo-amaro": ("Sesc Santo Amaro", "Santo Amaro"),
    "sao-bento": ("Sesc São Bento", "Centro"),
    "vila-mariana": ("Sesc Vila Mariana", "Vila Mariana"),
}
# Greater São Paulo (outside the municipality) — opt in with include_metro / extra_units.
METRO_UNITS: dict[str, tuple[str, str]] = {
    "guarulhos": ("Sesc Guarulhos", "Guarulhos"),
    "mogi-das-cruzes": ("Sesc Mogi das Cruzes", "Mogi das Cruzes"),
    "osasco": ("Sesc Osasco", "Osasco"),
    "santo-andre": ("Sesc Santo André", "Santo André"),
    "sao-caetano": ("Sesc São Caetano", "São Caetano do Sul"),
}

PAGE_SIZE = 300


# --------------------------------------------------------------------------- helpers

def _unit_slug(item: dict) -> str:
    for u in item.get("unidade") or []:
        link = (u or {}).get("link") or ""
        m = re.search(r"/unidades/([^/?#]+)", link)
        if m:
            return m.group(1)
    return ""


def _unit_name(item: dict, slug: str) -> str:
    if slug in CITY_UNITS:
        return CITY_UNITS[slug][0]
    if slug in METRO_UNITS:
        return METRO_UNITS[slug][0]
    for u in item.get("unidade") or []:
        if (u or {}).get("name"):
            return "Sesc " + u["name"].strip()
    return "Sesc " + slug.replace("-", " ").title()


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def _artists(title: str, complemento: str) -> list[str]:
    """Best-effort performer list: 'Com A, B e C' in the complement, else the title's lead name."""
    comp = (complemento or "").strip()
    m = re.match(r"(?i)^com\s+(.+)$", comp)
    if m:
        body = re.split(r"\.\s+|;", m.group(1))[0]
        parts = re.split(r",\s*|\s+e\s+", body)
        out = [re.split(r"\s+[(–—-]\s*|\s*\(", p)[0].strip(" .") for p in parts]
        out = [p for p in out if p and len(p) < 50 and p[0].isupper()
               and not re.match(r"(?i)(show|espet[aá]culo|participa|convidad|banda de apoio)", p)]
        if out:
            return out
    lead = re.split(r"\s+(?:convida|apresenta|canta|toca|e convidad[oa]s)\s+|\s+[–—-]\s+|:\s+",
                    title, maxsplit=1, flags=re.I)[0].strip()
    return [lead] if lead else []


_OG_DESC = re.compile(r'<meta\s+property="og:description"\s+content="([^"]*)"', re.I)
_SESSION_ATTR = re.compile(r'data-inicial="([^"]+)"\s+data-final="([^"]*)"')
_LOCAL = re.compile(r'evento--sessao--entrada--local">\s*Local:\s*<span>(.*?)</span>', re.S)


def parse_activity_page(page_html: str) -> dict:
    """Pull description, room and (free-activity) session times from the activity HTML."""
    out: dict = {"description": "", "sessions": [], "room": "", "free": False}
    m = _OG_DESC.search(page_html)
    if m:
        out["description"] = clean_text(htmllib.unescape(m.group(1)), 800)
    seen = set()
    for a, b in _SESSION_ATTR.findall(page_html):
        if a in seen:
            continue
        seen.add(a)
        out["sessions"].append((a, b))
    m = _LOCAL.search(page_html)
    if m:
        out["room"] = clean_text(htmllib.unescape(m.group(1)))
    out["free"] = "evento--sessao--entrada--gratis" in page_html
    return out


def sessions_from_detail(detail: dict) -> list[dict]:
    """Normalise the /atividades/detail payload into session dicts."""
    out = []
    for s in (detail or {}).get("sessoes") or []:
        start = parse_iso(s.get("dataInicialSessaoFmt"))
        if not start:
            continue
        end = parse_iso(s.get("dataFinalSessaoFmt"))
        tiers = [_num(s.get(k)) for k in ("valorComerciario", "valorMeia", "valorInteira")]
        tiers = [t for t in tiers if t is not None]
        inteira = _num(s.get("valorInteira"))
        free = bool(s.get("gratuito")) or (tiers and max(tiers) == 0)
        positive = [t for t in tiers if t > 0]
        status = " ".join(str(s.get(k) or "") for k in ("statusSessaoSesc", "dscStatusEvento",
                                                         "statusIngresso")).upper()
        out.append({
            "start": start,
            "end": end if end and end > start else None,
            "price_min": None if free else (min(positive) if positive else None),
            "price_max": None if free else (inteira if inteira else (max(positive) if positive else None)),
            "is_free": free,
            "sold_out": "ESGOTADO" in status,
            "ticket_url": htmllib.unescape(s.get("urlCompra") or ""),
        })
    return out


def build_events(item: dict, unit_slug: str, detail: dict | None, page: dict | None,
                 city_slug: str = "sp") -> list[Event]:
    title = clean_text(htmllib.unescape(item.get("titulo") or ""), 200)
    if not title:
        return []
    complemento = clean_text(htmllib.unescape(item.get("complemento") or ""), 300)
    venue = _unit_name(item, unit_slug)
    hood = (CITY_UNITS.get(unit_slug) or METRO_UNITS.get(unit_slug) or ("", ""))[1]
    link = item.get("link") or ""
    source_url = (BASE + link) if link.startswith("/") else link
    if source_url and not source_url.endswith("/"):
        source_url += "/"
    page = page or {}
    item_free = bool(item.get("gratuito"))  # "Atividade gratuita" / "Retirada de ingresso"
    item_sold_out = bool(item.get("esgotado"))

    desc_parts = [complemento, page.get("description", "")]
    if page.get("room"):
        desc_parts.append(f"Local: {page['room']}")
    description = " — ".join(p for p in desc_parts if p)[:1200]

    sessions = sessions_from_detail(detail) if detail else []
    if not sessions and page.get("sessions"):
        for a, b in page["sessions"]:
            start, end = parse_iso(a), parse_iso(b)
            if start:
                sessions.append({"start": start, "end": end if end and end > start else None,
                                 "price_min": None, "price_max": None, "is_free": item_free,
                                 "sold_out": False, "ticket_url": ""})
    if not sessions:
        # last resort: list payload dates + the first session's time of day
        first = parse_iso(item.get("dataPrimeiraSessao") or item.get("dataProxSessao"))
        hhmm = first.strftime("%H:%M") if first else "00:00"
        for d in item.get("datasSessoes") or []:
            start = parse_iso(f"{d}T{hhmm}")
            if start:
                sessions.append({"start": start, "end": None, "price_min": None,
                                 "price_max": None, "is_free": item_free, "sold_out": False,
                                 "ticket_url": ""})

    events, seen = [], set()
    for s in sessions:
        if s["start"] in seen:
            continue
        seen.add(s["start"])
        is_free = s["is_free"] or (item_free and s["price_min"] is None)
        events.append(Event(
            city=city_slug,
            title=title,
            start_datetime=s["start"],
            end_datetime=s["end"],
            source=NAME,
            venue=venue,
            neighborhood=hood,
            artists=_artists(title, complemento),
            price_min=0.0 if is_free else s["price_min"],
            price_max=0.0 if is_free else s["price_max"],
            is_free=is_free,
            sold_out=s["sold_out"] or item_sold_out,
            ticket_url=s["ticket_url"] or source_url,
            source_url=source_url,
            image_url=item.get("imagem") or "",
            description=description,
        ))
    return events


# --------------------------------------------------------------------------- fetching

def fetch_list(linguagem: str) -> list[dict]:
    items, page = [], 1
    while True:
        data = http.get_json(API + "/atividades/filter", params={
            "tipo": "atividade", "linguagem": linguagem, "ppp": PAGE_SIZE, "page": page})
        batch = data.get("atividade") or []
        items.extend(batch)
        total = int(((data.get("total") or {}).get("value")) or 0)
        if not batch or len(items) >= total or page >= 20:
            break
        page += 1
    return items


def fetch_detail(slug: str) -> dict | None:
    try:
        r = http.session().get(API + "/atividades/detail", params={"slug": slug})
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        log.debug("sesc_sp detail %s failed: %s", slug, exc)
        return None


def fetch_page(url: str) -> dict | None:
    try:
        return parse_activity_page(http.get(url).text)
    except Exception as exc:
        log.debug("sesc_sp page %s failed: %s", url, exc)
        return None


def _wanted_units(cfg: dict) -> set[str]:
    units = set(cfg.get("units") or CITY_UNITS.keys())
    if cfg.get("include_metro"):
        units |= set(METRO_UNITS)
    units |= set(cfg.get("extra_units") or [])
    return units


@collector(NAME)
def collect(city: CityConfig, cfg: dict):
    cfg = cfg or {}
    wanted = _wanted_units(cfg)
    items: dict[str, dict] = {}
    for ling in cfg.get("linguagens") or ["musica"]:
        for it in fetch_list(ling):
            items.setdefault(str(it.get("id")), it)

    keep, skipped_unit = [], 0
    for it in items.values():
        if it.get("cancelado") or it.get("online"):
            continue
        slug = _unit_slug(it)
        if slug not in wanted:
            skipped_unit += 1
            continue
        keep.append((it, slug))
    log.info("sesc_sp: %d music activities, %d in selected units (%d other/unknown unit)",
             len(items), len(keep), skipped_unit)

    fetch_pages = cfg.get("fetch_pages", True)

    def enrich(pair):
        it, slug = pair
        link = it.get("link") or ""
        act_slug = link.rstrip("/").rsplit("/", 1)[-1]
        detail = fetch_detail(act_slug) if it.get("id_java") else None
        page = fetch_page(BASE + link) if fetch_pages and link.startswith("/") else None
        return build_events(it, slug, detail, page, city.slug)

    with ThreadPoolExecutor(max_workers=int(cfg.get("workers", 6))) as pool:
        for evs in pool.map(enrich, keep):
            yield from evs
