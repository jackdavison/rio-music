"""Venue calendars: fetch each venue's agenda and extract events with a layered strategy.

Per venue (config `collectors.venues.venues: [...]`), in order:

1. Structured data
   * ``auto`` (default): schema.org Event / MusicEvent JSON-LD on the page; on WordPress sites
     also The Events Calendar REST API (``/wp-json/tribe/events/v1/events``).
   * explicit structured modes for sites whose agenda is a JSON API:
     ``wp_rest`` (any WP custom post type, field paths in config), ``fullcalendar``
     (FullCalendar-style JSON feed), ``gviz`` (public Google Sheet), ``circovoador``.
2. LLM extraction: HTML stripped to compact text (links/img srcs kept), chunked, sent to
   ``llm.json_call`` with ``EVENT_LIST_SCHEMA``. Cached by a hash of the stripped text.
   Skipped (one warning per venue) when no API key is configured.

Every venue is isolated: one failing venue is logged and the others continue.

Venue config keys::

    name: Circo Voador            # required; becomes Event.venue
    url: https://...              # agenda page (source_url; LLM/JSON-LD input)
    neighborhood: Lapa
    extra_urls: [https://...]     # more agenda pages (auto/jsonld/llm)
    mode: auto                    # auto | jsonld | llm | tribe | wp_rest | fullcalendar | gviz | circovoador
    api: https://...              # for wp_rest / fullcalendar / tribe
    music_only: false             # keyword-filter non-music items (structured modes)
    music_keywords: [soprano]     # extra keywords that count as music for music_only
    default_time: "20:00"         # used when a source gives a date but no time
    enabled: true
"""
from __future__ import annotations

import hashlib
import html as htmlmod
import json
import logging
import re
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, NavigableString

from .. import http, llm
from ..config import CityConfig
from ..models import TZ, Event, norm
from .base import HORIZON_DAYS, collector
from .util import clean_text, looks_like_music, parse_iso, parse_price, prices_in

log = logging.getLogger(__name__)

SOURCE = "venues"
DEFAULT_TIME = "20:00"
CHUNK_CHARS = 30000
MAX_CHUNKS = 4
LLM_CACHE_VERSION = "v1"


# --------------------------------------------------------------------------------------------
# small parsing helpers
# --------------------------------------------------------------------------------------------

def _today() -> date:
    return datetime.now(TZ).date()


def parse_time(s: Any) -> time | None:
    """'21:30', '21h30', '21h', '9:30 PM' -> time; anything else -> None."""
    if s is None:
        return None
    if isinstance(s, time):
        return s
    t = str(s).strip().lower()
    m = re.search(r"\b(\d{1,2})\s*(?::|h|hs|horas?)\s*(\d{2})?\b", t)
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2) or 0)
    if "pm" in t and hh < 12:
        hh += 12
    if hh == 24:
        hh = 0
    if hh > 23 or mm > 59:
        return None
    return time(hh, mm)


def infer_date(s: Any, today: date | None = None) -> date | None:
    """Parse a date; when the year is missing pick the next occurrence (not >60 days past).

    Accepts 'YYYY-MM-DD', 'YYYYMMDD', 'DD/MM/YYYY', 'DD/MM/YY', 'DD/MM', 'MM-DD', 'DD.MM'.
    """
    if s is None:
        return None
    if isinstance(s, datetime):
        return s.date()
    if isinstance(s, date):
        return s
    today = today or _today()
    t = str(s).strip()
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        return _mkdate(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.match(r"^(\d{4})(\d{2})(\d{2})$", t)
    if m:
        return _mkdate(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.match(r"^(\d{1,2})[/.](\d{1,2})[/.](\d{2,4})$", t)
    if m:
        y = int(m.group(3))
        y = y + 2000 if y < 100 else y
        return _mkdate(y, int(m.group(2)), int(m.group(1)))
    m = re.match(r"^(\d{1,2})[/.](\d{1,2})$", t)          # DD/MM (pt-BR)
    if m:
        return _next_occurrence(int(m.group(2)), int(m.group(1)), today)
    m = re.match(r"^(?:--)?(\d{1,2})-(\d{1,2})$", t)      # MM-DD (ISO without year)
    if m:
        return _next_occurrence(int(m.group(1)), int(m.group(2)), today)
    return None


def _mkdate(y: int, mo: int, d: int) -> date | None:
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def _next_occurrence(month: int, day: int, today: date) -> date | None:
    d = _mkdate(today.year, month, day)
    if d is None:
        return None
    if d < today - timedelta(days=60):          # "15/01" seen in October means next January
        d = _mkdate(today.year + 1, month, day)
    return d


def _combine(d: date, t: time | None, default_time: str) -> datetime:
    t = t or parse_time(default_time) or time(20, 0)
    return datetime.combine(d, t, tzinfo=TZ)


def _dig(obj: Any, path: str | None) -> Any:
    """Dotted path lookup: 'acf.sets.0.link_para_compra'."""
    if not path:
        return None
    cur = obj
    for part in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _text(v: Any) -> str:
    if v is None or v is False:
        return ""
    if isinstance(v, dict):
        v = v.get("rendered") or v.get("name") or ""
    return clean_text(htmlmod.unescape(str(v)), limit=2000)


def _music_ok(vcfg: dict, *texts: str) -> bool:
    """`music_only` venues keep items that look like music (plus venue `music_keywords`)."""
    if not vcfg.get("music_only") or looks_like_music(*texts):
        return True
    blob = norm(" ".join(t for t in texts if t))
    return any(norm(k) in blob for k in vcfg.get("music_keywords") or [])


def _price_range(*texts: str) -> tuple[float | None, float | None]:
    ps = [p for t in texts for p in prices_in(t or "")]
    if not ps:
        return None, None
    return min(ps), max(ps)


def _event(city: CityConfig, vcfg: dict, **kw) -> Event:
    kw.setdefault("source_url", vcfg.get("url", ""))
    return Event(city=city.slug, source=SOURCE, venue=vcfg["name"],
                 neighborhood=vcfg.get("neighborhood", ""), **kw)


def _fetch_html(url: str) -> str:
    r = http.get(url)
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding or "utf-8"
    return r.text


# --------------------------------------------------------------------------------------------
# 1a. JSON-LD
# --------------------------------------------------------------------------------------------

def _is_event_type(t: Any) -> bool:
    types = t if isinstance(t, list) else [t]
    return any(isinstance(x, str) and x.split("/")[-1].endswith("Event") for x in types)


def extract_jsonld(html: str) -> list[dict]:
    """All schema.org *Event nodes found in the page's JSON-LD blocks."""
    out: list[dict] = []
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all("script", type=re.compile(r"application/ld\+json", re.I)):
        raw = tag.string or tag.get_text() or ""
        try:
            data = json.loads(raw.strip())
        except json.JSONDecodeError:
            try:  # some sites leave trailing commas / control chars
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", raw.strip()), strict=False)
            except json.JSONDecodeError:
                continue

        def walk(x):
            if isinstance(x, list):
                for v in x:
                    walk(v)
            elif isinstance(x, dict):
                if _is_event_type(x.get("@type")):
                    out.append(x)
                    for sub in x.get("subEvent") or []:
                        walk(sub)
                    return
                for k in ("@graph", "itemListElement", "item", "mainEntity", "event", "events"):
                    if k in x:
                        walk(x[k])
        walk(data)
    return out


def _ld_name(v: Any) -> str:
    if isinstance(v, list):
        return ", ".join(filter(None, (_ld_name(x) for x in v)))
    if isinstance(v, dict):
        return str(v.get("name") or "")
    return str(v or "")


def jsonld_to_event(node: dict, city: CityConfig, vcfg: dict, page_url: str) -> Event | None:
    start = parse_iso(node.get("startDate"))
    if start is None:
        d = infer_date(node.get("startDate"))
        if d is None:
            return None
        start = _combine(d, None, vcfg.get("default_time", DEFAULT_TIME))
    title = htmlmod.unescape(_ld_name(node.get("name"))).strip()
    if not title:
        return None
    performers = node.get("performer") or []
    performers = performers if isinstance(performers, list) else [performers]
    artists = [htmlmod.unescape(_ld_name(p)) for p in performers if _ld_name(p)]
    offers = node.get("offers") or []
    offers = offers if isinstance(offers, list) else [offers]
    prices = []
    ticket_url, sold_out = "", False
    for o in offers:
        if not isinstance(o, dict):
            continue
        for k in ("price", "lowPrice", "highPrice"):
            p = parse_price(o.get(k))
            if p is not None:
                prices.append(p)
        ticket_url = ticket_url or str(o.get("url") or "")
        if "SoldOut" in str(o.get("availability") or ""):
            sold_out = True
    image = node.get("image")
    if isinstance(image, list):
        image = image[0] if image else ""
    if isinstance(image, dict):
        image = image.get("url") or ""
    status = str(node.get("eventStatus") or "")
    if "Cancelled" in status:
        return None
    desc = _text(node.get("description"))
    if not _music_ok(vcfg, title, desc):
        return None
    return _event(
        city, vcfg, title=title, artists=artists, start_datetime=start,
        end_datetime=parse_iso(node.get("endDate")),
        price_min=min(prices) if prices else None, price_max=max(prices) if prices else None,
        ticket_url=ticket_url or str(node.get("url") or ""),
        source_url=str(node.get("url") or page_url), image_url=str(image or ""),
        sold_out=sold_out, description=desc,
    )


# --------------------------------------------------------------------------------------------
# 1b. JSON APIs
# --------------------------------------------------------------------------------------------

def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def looks_like_wordpress(html: str) -> bool:
    return "wp-content/" in html or "wp-json" in html or 'content="WordPress' in html


def fetch_tribe(city: CityConfig, vcfg: dict, base_url: str | None = None) -> list[Event]:
    """The Events Calendar (tribe) REST API."""
    api = vcfg.get("api") if vcfg.get("mode") == "tribe" else None
    url = api or _origin(base_url or vcfg["url"]) + "/wp-json/tribe/events/v1/events"
    params = {"per_page": 50, "start_date": _today().isoformat(),
              "end_date": (_today() + timedelta(days=HORIZON_DAYS)).isoformat()}
    out: list[Event] = []
    for _ in range(10):
        data = http.get_json(url, params=params)
        for it in data.get("events") or []:
            start = parse_iso((it.get("start_date") or "").replace(" ", "T"))
            if start is None:
                continue
            title = _text(it.get("title"))
            desc = _text(it.get("description"))
            if not _music_ok(vcfg, title, desc):
                continue
            pmin, pmax = _price_range(str(it.get("cost") or ""))
            img = it.get("image") or {}
            out.append(_event(
                city, vcfg, title=title, start_datetime=start,
                end_datetime=parse_iso((it.get("end_date") or "").replace(" ", "T")),
                price_min=pmin, price_max=pmax,
                is_free=str(it.get("cost") or "").strip().lower() in ("free", "gratuito", "grátis"),
                ticket_url=it.get("website") or it.get("url") or "", source_url=it.get("url") or "",
                image_url=img.get("url", "") if isinstance(img, dict) else "", description=desc))
        nxt = data.get("next_rest_url")
        if not nxt:
            break
        url, params = nxt, None
    return out


WP_DEFAULT_FIELDS = {
    "title": "title.rendered",
    "link": "link",
    "description": "content.rendered",
    "image": "_embedded.wp:featuredmedia.0.source_url",
}


def fetch_wp_rest(city: CityConfig, vcfg: dict) -> list[Event]:
    """Generic WordPress custom-post-type endpoint. Config::

        api: https://bluenotesp.com/wp-json/wp/v2/shows
        fields: {start: acf.data_e_hora_de_inicio, ticket_url: acf.sets.0.link_para_compra,
                 sold_out: acf.ingresso_esgotado, subtitle: acf.subtitulo}
        sets: {path: acf.sets, time: horario_do_set, ticket_url: link_para_compra}  # optional
    """
    f = {**WP_DEFAULT_FIELDS, **(vcfg.get("fields") or {})}
    if not f.get("start"):
        raise ValueError("wp_rest venue needs fields.start")
    default_time = vcfg.get("default_time", DEFAULT_TIME)
    sets_cfg = vcfg.get("sets") or {}
    out: list[Event] = []
    page, pages = 1, 1
    while page <= pages and page <= 10:
        r = http.get(vcfg["api"], params={"per_page": 100, "page": page, "_embed": "wp:featuredmedia"})
        pages = int(r.headers.get("X-WP-TotalPages") or 1)
        for it in r.json():
            raw_start = _dig(it, f["start"])
            start = parse_iso(str(raw_start).replace(" ", "T")) if raw_start else None
            if start is None and raw_start:
                d = infer_date(str(raw_start)[:10])
                start = _combine(d, None, default_time) if d else None
            if start is None:
                continue
            title = _text(_dig(it, f["title"]))
            sub = _text(_dig(it, f.get("subtitle")))
            desc = _text(_dig(it, f.get("description")))
            if sub and sub.lower() not in title.lower():
                full_title = f"{title} — {sub}"
            else:
                full_title = title
            if not _music_ok(vcfg, full_title, desc):
                continue
            pmin, pmax = _price_range(desc)
            base = dict(
                title=full_title, artists=[title] if title else [],
                price_min=pmin, price_max=pmax,
                sold_out=bool(_dig(it, f.get("sold_out"))),
                source_url=str(_dig(it, f["link"]) or vcfg.get("url", "")),
                image_url=str(_dig(it, f.get("image")) or ""), description=desc,
            )
            sets = _dig(it, sets_cfg.get("path")) if sets_cfg else None
            if isinstance(sets, list) and len(sets) > 1:
                for s in sets:
                    t = parse_time(_dig(s, sets_cfg.get("time"))) or start.timetz()
                    out.append(_event(city, vcfg, start_datetime=datetime.combine(start.date(), t.replace(tzinfo=None), tzinfo=TZ),
                                      ticket_url=str(_dig(s, sets_cfg.get("ticket_url")) or base["source_url"]),
                                      **base))
            else:
                out.append(_event(city, vcfg, start_datetime=start,
                                  ticket_url=str(_dig(it, f.get("ticket_url")) or base["source_url"]),
                                  **base))
        page += 1
    return out


def fetch_fullcalendar(city: CityConfig, vcfg: dict) -> list[Event]:
    """FullCalendar JSON feed (list of {title, start, end, url, image}). Optional `terms` filter
    matched against extendedProps.terms (multi-venue feeds)."""
    today = _today()
    data = http.get_json(vcfg["api"], params={"start": today.isoformat(),
                                              "end": (today + timedelta(days=HORIZON_DAYS)).isoformat()})
    terms = [t.lower() for t in vcfg.get("terms") or []]
    out = []
    for it in data if isinstance(data, list) else []:
        ext = it.get("extendedProps") or {}
        if terms and not any(t.lower() in terms for t in ext.get("terms") or []):
            continue
        if ext.get("encerrado") or it.get("encerrado"):
            continue
        start = parse_iso(it.get("start"))
        title = _text(it.get("title"))
        if start is None or not title or not _music_ok(vcfg, title):
            continue
        out.append(_event(city, vcfg, title=title, start_datetime=start,
                          end_datetime=parse_iso(it.get("end")), ticket_url=it.get("url") or "",
                          source_url=it.get("url") or vcfg.get("url", ""),
                          image_url=it.get("image") or "",
                          description=_text(ext.get("sala_location"))))
    return out


GVIZ_DEFAULT_COLUMNS = {"title": "Titulo", "subtitle": "Subtitulo", "artists": "Instrutor",
                        "date": "Data", "time": "Hora de inicio", "end": "Termino", "link": "URL"}


def _gviz_value(cell: dict | None):
    if not cell:
        return None
    v = cell.get("v")
    if isinstance(v, str):
        m = re.match(r"Date\((\d+),(\d+),(\d+)(?:,(\d+),(\d+)(?:,(\d+))?)?\)", v)
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)) + 1, int(m.group(3))  # JS months are 0-based
            if m.group(4) is not None:
                if y < 1900:                       # time-only cell (Date(1899,11,30,h,m,s))
                    return time(int(m.group(4)), int(m.group(5)))
                return datetime(y, mo, d, int(m.group(4)), int(m.group(5)), tzinfo=TZ)
            return date(y, mo, d)
    return v


def parse_gviz(text: str) -> list[dict]:
    """google.visualization response -> list of {column label: python value}."""
    payload = json.loads(text[text.index("{"): text.rindex("}") + 1])
    cols = [c.get("label") or c.get("id") for c in payload["table"]["cols"]]
    rows = []
    for r in payload["table"].get("rows") or []:
        cells = r.get("c") or []
        rows.append({cols[i]: _gviz_value(c) for i, c in enumerate(cells) if i < len(cols)})
    return rows


def fetch_gviz(city: CityConfig, vcfg: dict) -> list[Event]:
    url = (f"https://docs.google.com/spreadsheets/d/{vcfg['sheet']}/gviz/tq"
           f"?tqx=out:json&gid={vcfg.get('gid', '0')}")
    cols = {**GVIZ_DEFAULT_COLUMNS, **(vcfg.get("columns") or {})}
    default_time = vcfg.get("default_time", DEFAULT_TIME)
    out = []
    for row in parse_gviz(http.get(url).text):
        d = row.get(cols["date"])
        d = d.date() if isinstance(d, datetime) else d
        if not isinstance(d, date):
            d = infer_date(d)
        title = str(row.get(cols["title"]) or "").strip()
        if not d or not title:
            continue
        sub = str(row.get(cols["subtitle"]) or "").strip()
        t = row.get(cols["time"])
        t = t if isinstance(t, time) else parse_time(t)
        end_t = row.get(cols["end"])
        end_t = end_t if isinstance(end_t, time) else parse_time(end_t)
        if not _music_ok(vcfg, title, sub):
            continue
        start = _combine(d, t, default_time)
        end = datetime.combine(d, end_t, tzinfo=TZ) if end_t else None
        if end and end <= start:
            end = end + timedelta(days=1)
        pmin, pmax = _price_range(sub)
        artists = [a.strip() for a in re.split(r"[,;]| e ", str(row.get(cols["artists"]) or "")) if a.strip()]
        link = str(row.get(cols["link"]) or "")
        out.append(_event(city, vcfg, title=f"{title} — {sub}" if sub else title,
                          artists=artists, start_datetime=start, end_datetime=end,
                          price_min=pmin, price_max=pmax,
                          is_free="gratuit" in sub.lower(),
                          ticket_url=link, description=sub))
    return out


def fetch_circovoador(city: CityConfig, vcfg: dict) -> list[Event]:
    """Circo Voador's own WP JSON API (the Angular site reads it)."""
    api = vcfg.get("api") or "https://www.circovoador.com.br/circo2017/api/wp-json/api/v2/eventos"
    default_time = vcfg.get("default_time", DEFAULT_TIME)
    out = []
    for it in http.get_json(api):
        d = infer_date(it.get("data_raw"))
        if d is None:
            continue
        extra = _text(it.get("texto_adicional_capa"))
        t = parse_time(extra) or parse_time(_text(it.get("chamada")))
        artists = [a for raw in it.get("artistas") or [] if raw
                   for a in (_text(x) for x in re.split(r"<br\s*/?>", str(raw))) if a]
        title = " + ".join(artists) or htmlmod.unescape(str(it.get("meta_titulo") or "")).split(" :: ")[0]
        lotes = it.get("lotes") or []
        lote_txt = " ".join(f"{lo.get('preco-inteira', '')} {lo.get('preco-meia', '')}" for lo in lotes)
        pmin, pmax = _price_range(lote_txt)
        status = str(it.get("status") or "").lower()
        sold_out = status == "esgotado" or (bool(lotes) and all(lo.get("esgotado") for lo in lotes))
        img = it.get("imagem_capa") or {}
        out.append(_event(city, vcfg, title=title, artists=artists,
                          start_datetime=_combine(d, t, default_time),
                          price_min=pmin, price_max=pmax, sold_out=sold_out,
                          ticket_url=it.get("url_botao") or "",
                          image_url=img.get("url", "") if isinstance(img, dict) else "",
                          description=_text(it.get("meta_descricao") or it.get("chamada"))))
    return out


API_MODES = {
    "tribe": fetch_tribe,
    "wp_rest": fetch_wp_rest,
    "fullcalendar": fetch_fullcalendar,
    "gviz": fetch_gviz,
    "circovoador": fetch_circovoador,
}


# --------------------------------------------------------------------------------------------
# 2. HTML -> compact text -> LLM
# --------------------------------------------------------------------------------------------

_DROP_TAGS = ["script", "style", "noscript", "svg", "template", "iframe", "head", "link", "meta",
              "select", "option", "form", "nav", "canvas", "video", "audio", "source"]
_BLOCK_TAGS = {"p", "div", "li", "ul", "ol", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6",
               "section", "article", "br", "hr", "table", "dt", "dd", "header", "footer", "main",
               "figure", "figcaption", "aside", "blockquote", "time"}


def _img_src(img) -> str:
    for attr in ("data-src", "data-lazy-src", "data-original", "src"):
        v = img.get(attr)
        if v and not v.startswith("data:"):
            return v
    srcset = img.get("data-srcset") or img.get("srcset") or ""
    return srcset.split(",")[0].split(" ")[0] if srcset else ""


def strip_html(html: str, base_url: str = "") -> str:
    """Visible text with link hrefs and image srcs inline, one block per line.

    Scripts, styles, forms and <nav> are dropped; <header>/<footer> too unless that would leave
    almost nothing (some page builders wrap the whole page in a <header>).
    """
    soup = BeautifulSoup(html, "lxml")
    for t in soup.find_all(_DROP_TAGS):
        t.decompose()
    body = soup.body or soup
    chrome = body.find_all(["header", "footer"])
    if chrome:
        total = len(body.get_text(" ", strip=True))
        chrome_len = sum(len(c.get_text(" ", strip=True)) for c in chrome)
        if total - chrome_len >= 400:
            for c in chrome:
                c.decompose()

    for img in body.find_all("img"):
        src = _img_src(img)
        if not src or src.lower().split("?")[0].endswith((".svg", ".ico")):
            img.decompose()
            continue
        alt = " ".join((img.get("alt") or "").split())
        img.replace_with(NavigableString(f" [img{': ' + alt if alt else ''} {urljoin(base_url, src)}] "))

    last_href = None
    for a in body.find_all("a"):
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        href = urljoin(base_url, href)
        if href == last_href:          # cards often link image, title and button to one URL
            continue
        last_href = href
        a.append(NavigableString(f" <{href}>"))

    for t in body.find_all(_BLOCK_TAGS):
        t.insert_before(NavigableString("\n"))
        t.insert_after(NavigableString("\n"))

    lines, prev = [], None
    for line in body.get_text("").splitlines():
        line = " ".join(line.split())
        if not line or line == prev:
            continue
        lines.append(line)
        prev = line
    return "\n".join(lines)


def chunk_text(text: str, size: int = CHUNK_CHARS, max_chunks: int = MAX_CHUNKS) -> list[str]:
    chunks, cur, n = [], [], 0
    for line in text.splitlines():
        if n + len(line) + 1 > size and cur:
            chunks.append("\n".join(cur))
            cur, n = [], 0
        cur.append(line[:size])
        n += len(line) + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks[:max_chunks]


def _week_start(today: date) -> date:
    return today - timedelta(days=today.weekday())


def llm_system_prompt(city: CityConfig, vcfg: dict, today: date) -> str:
    # Reference date is the start of the week so the prompt (and hence the cache key) only
    # changes weekly; that is precise enough for year inference.
    ref = _week_start(today)
    return (
        f"You extract live-music events from the agenda/programação page of a music venue in "
        f"{city.name}, Brazil: \"{vcfg['name']}\""
        f"{' (' + vcfg['neighborhood'] + ')' if vcfg.get('neighborhood') else ''}. "
        "The page text is Portuguese; links appear as <url> and images as [img: alt url].\n"
        f"Reference date (start of the current week): {ref.isoformat()} ({ref.strftime('%A')}). "
        "Many listings give dates without a year (e.g. 'sex 16/10', '17 out', '15.01'): use the "
        "next occurrence on or after the reference date — a January date listed in October is "
        "next year. Dates are day/month (Brazilian order).\n"
        "Rules:\n"
        "- Return only concerts / live music performances (shows, rodas de samba/choro, jazz, "
        "recitals, DJ nights with live bands, bailes with live music). Skip theatre, comedy, "
        "talks, courses, classes, workshops, exhibitions, kids' activities and past events.\n"
        "- One entry per performance date/time; a residency on several dates is several entries.\n"
        "- date: YYYY-MM-DD. time: HH:MM 24h start of the show (not doors) or '' if absent.\n"
        "- title: the show name as listed; artists: performer names only.\n"
        "- price_min/price_max in BRL (numbers) or null; is_free true only if explicitly free "
        "(gratuito/grátis/entrada franca); sold_out true if esgotado.\n"
        "- link: the event's ticket or detail URL exactly as it appears in the text, else ''. "
        "image_url: the flyer image URL if clearly attached to the event, else ''.\n"
        "- venue: the room/stage if given, else the venue name. description: one short "
        "sentence (genre, guests) or ''.\n"
        "- If the page lists no events, return {\"events\": []}. Never invent events."
    )


def llm_item_to_event(item: dict, city: CityConfig, vcfg: dict, page_url: str,
                      today: date | None = None) -> Event | None:
    title = " ".join(str(item.get("title") or "").split())
    d = infer_date(item.get("date"), today)
    if not title or d is None:
        return None
    start = _combine(d, parse_time(item.get("time")), vcfg.get("default_time", DEFAULT_TIME))
    link = str(item.get("link") or "").strip()
    link = urljoin(page_url, link) if link else ""
    img = str(item.get("image_url") or "").strip()
    pmin, pmax = item.get("price_min"), item.get("price_max")
    pmin = parse_price(pmin) if pmin is not None else None
    pmax = parse_price(pmax) if pmax is not None else None
    return _event(city, vcfg, title=title, artists=list(item.get("artists") or []),
                  start_datetime=start, price_min=pmin, price_max=pmax,
                  is_free=bool(item.get("is_free")), sold_out=bool(item.get("sold_out")),
                  ticket_url=link, source_url=page_url,
                  image_url=urljoin(page_url, img) if img else "",
                  description=str(item.get("description") or ""))


def llm_extract(text: str, city: CityConfig, vcfg: dict, page_url: str,
                today: date | None = None, cfg: dict | None = None,
                model: str | None = None) -> list[Event]:
    cfg = cfg or {}
    today = today or _today()
    system = llm_system_prompt(city, vcfg, today)
    out: list[Event] = []
    chunks = chunk_text(text, int(cfg.get("chunk_chars", CHUNK_CHARS)),
                        int(cfg.get("max_chunks", MAX_CHUNKS)))
    for i, chunk in enumerate(chunks):
        digest = hashlib.sha256(chunk.encode()).hexdigest()[:20]
        prompt = (f"Venue: {vcfg['name']}\nPage: {page_url}"
                  f"{f' (part {i + 1}/{len(chunks)})' if len(chunks) > 1 else ''}\n\n"
                  f"<page>\n{chunk}\n</page>")
        res = llm.json_call(system=system, prompt=prompt, schema=llm.EVENT_LIST_SCHEMA,
                            cache_tag=f"venues:{LLM_CACHE_VERSION}:{digest}", task="extract",
                            model=model)
        if not res:
            continue
        items = res.get("events", []) if isinstance(res, dict) else res
        for item in items or []:
            try:
                ev = llm_item_to_event(item, city, vcfg, page_url, today)
            except Exception as exc:  # one malformed item must not drop the rest
                log.debug("venues: bad LLM item %r: %s", item, exc)
                continue
            if ev:
                out.append(ev)
    return out


# --------------------------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------------------------

def _pages(vcfg: dict) -> list[str]:
    urls = [vcfg.get("url")] + list(vcfg.get("extra_urls") or vcfg.get("urls") or [])
    seen, out = set(), []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def collect_venue(city: CityConfig, vcfg: dict, cfg: dict | None = None,
                  today: date | None = None) -> tuple[list[Event], str]:
    """Events for one venue plus a label of the strategy that produced them."""
    cfg = cfg or {}
    mode = (vcfg.get("mode") or "auto").lower()
    use_llm = cfg.get("llm", True) and vcfg.get("llm", True)

    if mode in API_MODES:
        try:
            return API_MODES[mode](city, vcfg), mode
        except Exception as exc:
            log.warning("[venues] %s: %s API failed (%s); falling back to page", vcfg["name"],
                        mode, exc)
            mode = "auto"

    events: list[Event] = []
    strategies: list[str] = []
    llm_warned = False
    tribe_tried = False
    for url in _pages(vcfg):
        try:
            html = _fetch_html(url)
        except Exception as exc:
            log.warning("[venues] %s: fetch %s failed: %s", vcfg["name"], url, exc)
            continue
        if mode in ("auto", "jsonld"):
            found = [e for e in (jsonld_to_event(n, city, vcfg, url) for n in extract_jsonld(html)) if e]
            if found:
                events += found
                strategies.append("jsonld")
                continue
        if mode == "auto" and not tribe_tried and looks_like_wordpress(html):
            tribe_tried = True
            try:
                found = fetch_tribe(city, vcfg, url)
            except Exception:
                found = []          # 404 = plugin not installed; the usual case
            if found:
                events += found
                strategies.append("tribe")
                break               # the API covers the whole site; no need for other pages
        if mode in ("auto", "llm"):
            if not use_llm:
                continue
            if not llm.available():
                if not llm_warned:
                    log.warning("[venues] %s: no structured data and no ANTHROPIC_API_KEY; "
                                "skipping LLM extraction", vcfg["name"])
                    llm_warned = True
                continue
            text = strip_html(html, url)
            events += llm_extract(text, city, vcfg, url, today, cfg)
            strategies.append("llm")
    uniq = {e.id: e for e in events}
    return list(uniq.values()), "+".join(dict.fromkeys(strategies)) or ("llm-skipped" if llm_warned else "none")


@collector("venues")
def collect(city: CityConfig, cfg: dict) -> Iterable[Event]:
    out: list[Event] = []
    for vcfg in cfg.get("venues") or []:
        if not isinstance(vcfg, dict) or not vcfg.get("name"):
            log.warning("[venues] skipping malformed venue entry %r", vcfg)
            continue
        if vcfg.get("enabled", True) is False:
            continue
        try:
            events, strategy = collect_venue(city, vcfg, cfg)
        except Exception as exc:  # isolate each venue
            log.error("[venues] %s failed: %s", vcfg["name"], exc, exc_info=True)
            continue
        log.info("[venues] %s: %d events via %s", vcfg["name"], len(events), strategy)
        out.extend(events)
    return out
