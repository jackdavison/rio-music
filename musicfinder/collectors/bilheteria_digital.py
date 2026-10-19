"""Bilheteria Digital (www.bilheteriadigital.com).

Listing: the home page's "ver mais eventos" button (assets/widgets/eventos-widget.js) posts to

    POST https://www.bilheteriadigital.com/eventos/paginacao   (form: paginaAtual=1, estado=RJ)

which returns {"eventos": [...], "qtd_eventos": "43"} -- every upcoming event of the state in one go
(paginaAtual is ignored; no CSRF token needed). Rows carry nomeevento, cidade, localevento,
diainicio/mesinicio/horainicio (no year), imagem and link. There is no category in this JSON.

Caveat: the JSON `link` is re-slugified from the event name and 404s for ~25% of events, so the
state page (GET /<UF>), whose cards carry the real links, is fetched too and joined on the numeric
event id embedded in the image file name (".../eventos/71268d.jpg" == event 71268).

Details: each event page has hidden form inputs with `dataInicio` (UTC ISO instant), `estiloId`
(the site's internal style/category id), `local`, and one `ingressos[i][valorUnit]` per ticket
type (prices), plus the description in `.informacoes-adicionais-evento`. Style ids are not
published anywhere; observed values: 1 festa/DJ, 2 baile/MC, 3 baile funk/pagode night, 4 shows,
35 boteco/forró nights, 36 big concert series -> music; 11 = sports/rides -> never music;
others (5 churrasco/festa, 6, 7 misc, 8 theatre/CAIXA Cultural, 15 réveillon) fall back to keywords.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Iterable

from bs4 import BeautifulSoup

from .. import http
from ..config import CityConfig
from ..models import Event, TZ, norm
from .base import collector
from .util import MONTHS_PT, clean_text, looks_like_music, parse_iso

log = logging.getLogger(__name__)

NAME = "bilheteria_digital"
BASE = "https://www.bilheteriadigital.com"
LIST_API = BASE + "/eventos/paginacao"

CITY_DEFAULTS = {
    "rio": {"state": "RJ", "cities": ["Rio de Janeiro", "Niterói"]},
    "sp": {"state": "SP", "cities": ["São Paulo"]},
}
DEFAULTS = {
    "music_styles": [1, 2, 3, 4, 35, 36],
    "non_music_styles": [11],
    "fetch_details": True,
    "max_details": 120,
}

_IMG_ID = re.compile(r"/eventos/(\d+)[a-z]?\.(?:jpe?g|png|gif|webp)", re.I)
_JUNK = re.compile(r"\b(maratona|corrida|passeio|exposicao|visita|monster trucks?|"
                   r"capela sistina|black mirror|giro)\b")


def _settings(city: CityConfig, cfg: dict) -> dict:
    s = dict(DEFAULTS)
    s.update(CITY_DEFAULTS.get(city.slug, {}))
    s.update({k: v for k, v in (cfg or {}).items() if v is not None})
    if not s.get("state"):
        raise ValueError(f"bilheteria_digital: no state configured for city {city.slug!r}")
    if not s.get("cities"):
        s["cities"] = [city.name]
    return s


def _img_id(url: str | None) -> str | None:
    m = _IMG_ID.search(url or "")
    return m.group(1) if m else None


def fetch_listing(state: str) -> list[dict]:
    r = http.session().post(LIST_API, data={"paginaAtual": 1, "estado": state},
                            headers={"X-Requested-With": "XMLHttpRequest",
                                     "Referer": BASE + "/"})
    r.raise_for_status()
    return (r.json() or {}).get("eventos") or []


def fetch_state_links(state: str) -> dict[str, str]:
    """event id -> real relative link, from the server-rendered state page."""
    try:
        html = http.get(f"{BASE}/{state}").text
    except Exception as exc:  # links from the JSON still work for most events
        log.warning("bilheteria_digital: state page %s failed: %s", state, exc)
        return {}
    return parse_state_links(html)


def parse_state_links(html: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "lxml")
    out = {}
    for li in soup.select("li.box-li-evento"):
        a, img = li.find("a", href=True), li.find("img")
        if not a or not img:
            continue
        eid = _img_id(img.get("data-src") or img.get("src"))
        if eid:
            out.setdefault(eid, a["href"].strip())
    return out


def _guess_start(row: dict, today: datetime) -> datetime | None:
    """Listing-only fallback: day/month/time without a year."""
    try:
        day = int(row.get("diainicio"))
        month = MONTHS_PT.get(norm(row.get("mesinicio"))[:3])
        hh, mm = (row.get("horainicio") or "20:00").split(":")[:2]
        if not month:
            return None
        dt = datetime(today.year, month, day, int(hh), int(mm), tzinfo=TZ)
    except (TypeError, ValueError):
        return None
    if dt < today - timedelta(days=60):
        dt = dt.replace(year=dt.year + 1)
    return dt


def parse_event_page(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")

    def hidden(name):
        el = soup.find("input", attrs={"name": name})
        return (el.get("value") or "").strip() if el else ""

    prices = []
    for el in soup.find_all("input", attrs={"name": re.compile(r"^ingressos\[\d+\]\[valorUnit\]$")}):
        try:
            prices.append(float(el.get("value")))
        except (TypeError, ValueError):
            pass
    desc_el = soup.select_one(".informacoes-adicionais-evento")
    desc = clean_text(desc_el.get_text(" ") if desc_el else "", 1500)
    style = hidden("estiloId")
    page_text = soup.get_text(" ")
    return {
        "start": parse_iso(hidden("dataInicio")),
        "style": int(style) if style.isdigit() else None,
        "local": hidden("local"),
        "title": hidden("nomeEvento"),
        "prices": prices,
        "description": desc,
        "sold_out": not prices and "ESGOTADO" in page_text.upper(),
        "sales_closed": "VENDAS ENCERRADAS" in page_text.upper(),
    }


_LOCAL_IN_DESC = re.compile(r"\bLocal\s*:\s*([^*\n|]{3,80}?)(?=\s*(?:\*|Endere|Cidade|$))", re.I)


def _venue(local: str, description: str) -> tuple[str, str]:
    """('Rua X, 580 | Padre Miguel', '...Local: Soho Lounge...') -> ('Soho Lounge', 'Padre Miguel')."""
    venue, hood = local, ""
    if "|" in local:
        addr, hood = [p.strip() for p in local.split("|", 1)]
        venue = addr
        m = _LOCAL_IN_DESC.search(description or "")
        if m:
            venue = m.group(1).strip(" -,.")
    return venue, hood


# For styles of unknown meaning the description must carry a specific music cue; generic words
# like "show" / "música" also show up in theatre and party blurbs.
_STRONG_MUSIC = re.compile(
    r"\b(samba|choro|jazz|mpb|forro|pagode|concerto|concert|orquestra|banda|cantora?|compositora?|"
    r"instrumentista|multi instrumentista|repertorio|album|disco|ao vivo|dj|djs|mc|baile|"
    r"atracoes|roda de samba|gafieira|bossa|rock|reggae|sertanejo|funk|trio|quarteto|quinteto|"
    r"recital|sinfonic\w*|coral|percussao|violao|piano|sax\w*|flauta)\b")
_THEATRE = re.compile(r"\b(pantomima|teatro fisico|montagem|dramaturgia|elenco|peca|stand ?up|"
                      r"comedia|humor|infantil)\b")


def _is_music(style: int | None, title: str, desc: str, s: dict) -> bool:
    t = norm(title)
    if _JUNK.search(t):
        return False
    if style is not None and style in set(s["non_music_styles"]):
        return False
    if style is not None and style in set(s["music_styles"]):
        return True
    if looks_like_music(title):
        return True
    d = norm(desc)
    return bool(_STRONG_MUSIC.search(d)) and not _THEATRE.search(d)


def build_event(row: dict, link: str, detail: dict | None, slug: str, s: dict,
                today: datetime) -> Event | None:
    detail = detail or {}
    title = clean_text(row.get("nomeevento") or detail.get("title"), 300)
    desc = detail.get("description", "")
    if not _is_music(detail.get("style"), title, desc, s):
        return None
    start = detail.get("start") or _guess_start(row, today)
    if not start:
        return None
    local = detail.get("local") or row.get("localevento") or ""
    venue, hood = _venue(clean_text(local, 200), desc)
    prices = [p for p in detail.get("prices", []) if p is not None]
    paid = [p for p in prices if p > 0]
    url = BASE + link if link.startswith("/") else link
    return Event(
        city=slug,
        title=title,
        start_datetime=start,
        source=NAME,
        venue=venue,
        neighborhood=hood,
        price_min=min(paid) if paid else (0.0 if prices else None),
        price_max=max(paid) if paid else (0.0 if prices else None),
        ticket_url=url,
        source_url=url,
        image_url=row.get("imagem") or "",
        sold_out=bool(detail.get("sold_out")),
        description=desc[:600],
    )


@collector(NAME)
def collect(city: CityConfig, cfg: dict) -> Iterable[Event]:
    s = _settings(city, cfg)
    wanted = {norm(c) for c in s["cities"]}
    rows = [r for r in fetch_listing(s["state"]) if norm(r.get("cidade")) in wanted]
    log.info("bilheteria_digital: %d listing rows for %s", len(rows), sorted(wanted))
    links = fetch_state_links(s["state"]) if rows else {}
    today = datetime.now(TZ)
    budget = int(s["max_details"]) if s["fetch_details"] else 0
    seen = set()
    for row in rows:
        eid = _img_id(row.get("imagem"))
        link = links.get(eid or "") or row.get("link") or ""
        if not link or link in seen:
            continue
        seen.add(link)
        detail = None
        if budget > 0:
            budget -= 1
            try:
                detail = parse_event_page(http.get(BASE + link if link.startswith("/") else link).text)
            except Exception as exc:
                log.warning("bilheteria_digital: detail %s failed: %s", link, exc)
        ev = build_event(row, link, detail, city.slug, s, today)
        if ev:
            yield ev
