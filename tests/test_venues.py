"""Tests for the venues collector: JSON-LD, structured APIs, HTML stripping, LLM mapping."""
from __future__ import annotations

import json
from datetime import date, datetime, time
from pathlib import Path

import pytest

from musicfinder import llm
from musicfinder.collectors import venues as V
from musicfinder.config import CityConfig
from musicfinder.models import TZ

FIX = Path(__file__).parent / "fixtures"
RIO = CityConfig(slug="rio", name="Rio de Janeiro")
SP = CityConfig(slug="sp", name="São Paulo")
TODAY = date(2026, 10, 16)


class FakeResp:
    def __init__(self, text="", headers=None, data=None):
        self.text = text
        self.headers = headers or {}
        self._data = data
        self.encoding = "utf-8"
        self.apparent_encoding = "utf-8"

    def json(self):
        return self._data if self._data is not None else json.loads(self.text)


# ------------------------------------------------------------------------------ date/time helpers

@pytest.mark.parametrize("raw,expected", [
    ("2026-11-13", date(2026, 11, 13)),
    ("20261016", date(2026, 10, 16)),
    ("16/10/2026", date(2026, 10, 16)),
    ("16/10/26", date(2026, 10, 16)),
    ("13/11", date(2026, 11, 13)),          # DD/MM, later this year
    ("15/01", date(2027, 1, 15)),           # DD/MM in January -> next year
    ("01-15", date(2027, 1, 15)),           # MM-DD (no year) -> next year
    ("10-20", date(2026, 10, 20)),
    ("09-01", date(2026, 9, 1)),            # recently past: same year (< 60 days ago)
    ("nonsense", None),
    ("31/02", None),
])
def test_infer_date(raw, expected):
    assert V.infer_date(raw, TODAY) == expected


@pytest.mark.parametrize("raw,expected", [
    ("21:30", time(21, 30)), ("21h30", time(21, 30)), ("21h", time(21, 0)),
    ("Abertura dos Portões: 20:00", time(20, 0)), ("9:30 PM", time(21, 30)), ("", None),
    ("sem horário", None),
])
def test_parse_time(raw, expected):
    assert V.parse_time(raw) == expected


# ------------------------------------------------------------------------------ JSON-LD

def test_jsonld_from_real_blue_note_sp_page():
    html = (FIX / "venues_bluenotesp_show.html").read_text()
    nodes = V.extract_jsonld(html)
    assert len(nodes) == 1 and nodes[0]["@type"] == "Event"
    vcfg = {"name": "Blue Note São Paulo", "neighborhood": "Bela Vista", "url": "https://bluenotesp.com/"}
    ev = V.jsonld_to_event(nodes[0], SP, vcfg, "https://bluenotesp.com/")
    assert ev.title == "BIAFRA"
    assert ev.artists == ["BIAFRA"]
    assert ev.venue == "Blue Note São Paulo" and ev.neighborhood == "Bela Vista"
    assert ev.source == "venues" and ev.city == "sp"
    assert ev.start_datetime == datetime(2026, 10, 16, 20, 0, tzinfo=TZ)
    assert ev.end_datetime == datetime(2026, 10, 16, 21, 30, tzinfo=TZ)
    assert ev.ticket_url.startswith("https://www.eventim.com.br/artist/blue-note-sp/biafra")
    assert ev.source_url == "https://bluenotesp.com/shows/biafra-no-tempo-do-vinil/"
    assert ev.image_url.endswith("Biafra.webp")
    assert not ev.sold_out


def test_jsonld_variants_list_types_soldout_cancelled():
    html = """<script type="application/ld+json">[
      {"@type": ["MusicEvent"], "name": "Roda de Choro", "startDate": "2026-11-01T19:00",
       "performer": [{"name": "Regional X"}, {"name": "Fulana"}],
       "offers": [{"price": "40,00", "availability": "https://schema.org/SoldOut", "url": "https://t/1"},
                  {"price": 20}]},
      {"@type": "Event", "name": "Cancelado", "startDate": "2026-11-02T19:00",
       "eventStatus": "https://schema.org/EventCancelled"},
      {"@type": "Organization", "name": "not an event"}
    ]</script>"""
    nodes = V.extract_jsonld(html)
    assert [n["name"] for n in nodes] == ["Roda de Choro", "Cancelado"]
    vcfg = {"name": "Casa", "url": "https://casa/"}
    evs = [V.jsonld_to_event(n, RIO, vcfg, "https://casa/") for n in nodes]
    assert evs[1] is None
    e = evs[0]
    assert e.artists == ["Regional X", "Fulana"]
    assert (e.price_min, e.price_max) == (20.0, 40.0)
    assert e.sold_out and e.ticket_url == "https://t/1"


# ------------------------------------------------------------------------------ structured APIs

def test_circovoador_mapping(monkeypatch):
    data = json.loads((FIX / "venues_circovoador_eventos.json").read_text())
    monkeypatch.setattr(V.http, "get_json", lambda url, **kw: data)
    evs = V.fetch_circovoador(RIO, {"name": "Circo Voador", "url": "https://www.circovoador.com.br/",
                                    "neighborhood": "Lapa"})
    assert len(evs) == 2
    ira, mestre = evs
    assert ira.title == "Ira! Acústico 20 anos"
    assert ira.start_datetime == datetime(2026, 10, 16, 20, 0, tzinfo=TZ)   # "Abertura ... 20:00"
    assert ira.ticket_url.startswith("https://www.eventim.com.br/")
    assert ira.price_min == 80.0
    assert mestre.artists == ["Mestre Ambrósio", "Eddie"]                  # <br /> split
    assert all(e.venue == "Circo Voador" and e.neighborhood == "Lapa" for e in evs)


def test_gviz_sheet_mapping(monkeypatch):
    text = (FIX / "venues_casadopandeiro_gviz.txt").read_text()
    monkeypatch.setattr(V.http, "get", lambda url, **kw: FakeResp(text))
    vcfg = {"name": "Casa do Pandeiro", "neighborhood": "Centro", "mode": "gviz",
            "sheet": "x", "music_only": True, "url": "https://casadopandeiro.com/"}
    evs = V.fetch_gviz(RIO, vcfg)
    titles = [e.title for e in evs]
    assert not any("Oficina" in t for t in titles)                         # workshop filtered
    dani = next(e for e in evs if e.title.startswith("Daniela Spielmann"))
    assert dani.start_datetime == datetime(2025, 11, 1, 16, 0, tzinfo=TZ)   # JS month 0-based
    assert dani.price_min == 15.0
    assert dani.ticket_url.startswith("https://www.instagram.com/")


def test_wp_rest_mapping_with_sets(monkeypatch):
    items = [{
        "title": {"rendered": "WAGNER TISO &#038; TONINHO HORTA"}, "link": "https://bn/shows/wt/",
        "content": {"rendered": "<p>Encontro de mestres. R$ 150</p>"},
        "acf": {"data_e_hora_de_inicio": "2026-10-17 20:00:00", "subtitulo": "",
                "ingresso_esgotado": True,
                "sets": [{"horario_do_set": "20:00:00", "link_para_compra": "https://t/a"},
                         {"horario_do_set": "22:30:00", "link_para_compra": "https://t/b"}]},
    }]
    monkeypatch.setattr(V.http, "get", lambda url, **kw: FakeResp(headers={"X-WP-TotalPages": "1"},
                                                                   data=items))
    vcfg = {"name": "Blue Note São Paulo", "mode": "wp_rest", "api": "https://bn/wp-json/wp/v2/shows",
            "fields": {"start": "acf.data_e_hora_de_inicio", "subtitle": "acf.subtitulo",
                       "ticket_url": "acf.sets.0.link_para_compra", "sold_out": "acf.ingresso_esgotado"},
            "sets": {"path": "acf.sets", "time": "horario_do_set", "ticket_url": "link_para_compra"}}
    evs = V.fetch_wp_rest(SP, vcfg)
    assert [(e.start_datetime.hour, e.start_datetime.minute, e.ticket_url) for e in evs] == [
        (20, 0, "https://t/a"), (22, 30, "https://t/b")]
    assert evs[0].title == "WAGNER TISO & TONINHO HORTA" and evs[0].sold_out
    assert evs[0].price_min == 150.0


def test_fullcalendar_terms_and_music_filter(monkeypatch):
    feed = [
        {"title": "Recital de piano solo", "start": "2026-10-26T20:00:00", "url": "https://a/1",
         "extendedProps": {"terms": ["Teatro Sérgio Cardoso"]}},
        {"title": "A Menina que Inundou o Mundo", "start": "2026-10-17T15:00:00", "url": "https://a/2",
         "extendedProps": {"terms": ["Teatro Sérgio Cardoso"]}},
        {"title": "Show de outra sala", "start": "2026-10-17T20:00:00", "url": "https://a/3",
         "extendedProps": {"terms": ["Teatro Estadual Araras"]}},
        {"title": "Cavalleria Rusticana com a mezzo soprano", "start": "2026-11-08T11:00:00",
         "url": "https://a/4", "extendedProps": {"terms": ["Teatro Sérgio Cardoso"]}},
    ]
    monkeypatch.setattr(V.http, "get_json", lambda url, **kw: feed)
    vcfg = {"name": "Teatro Sérgio Cardoso", "api": "https://a/feed", "terms": ["Teatro Sérgio Cardoso"],
            "music_only": True, "music_keywords": ["soprano"]}
    assert [e.ticket_url for e in V.fetch_fullcalendar(SP, vcfg)] == ["https://a/1", "https://a/4"]


# ------------------------------------------------------------------------------ HTML stripping

PAGE = """<html><head><title>x</title><style>.a{}</style></head><body>
<header><nav><a href="/menu">Menu</a></nav></header>
<script>var x = "SECRET_JS";</script>
<main>
  <div class="card">
    <a href="/eventos/roda"><img data-src="/img/roda.jpg" src="data:image/gif;base64,AAAA" alt="Roda de Samba"></a>
    <h3><a href="/eventos/roda">Roda de Samba do Zé</a></h3>
    <p>Sex 16/10 &nbsp; 21h00 — R$ 30</p>
    <a href="https://www.sympla.com.br/evento/roda/1">Ingressos</a>
    <img src="/icons/arrow.svg">
  </div>
  <div class="card"><h3>Choro na Praça</h3><p>Sáb 17/10 18h — gratuito</p></div>
  <p>Repetido</p><p>Repetido</p>
  <form><select><option>Janeiro</option></select></form>
  <p>Mais texto para passar do limite de conteúdo principal. """ + "lorem ipsum " * 40 + """</p>
</main>
<footer>Rua X, 10 · Todos os direitos reservados</footer>
</body></html>"""


def test_strip_html_keeps_links_images_and_drops_noise():
    t = V.strip_html(PAGE, "https://casa.com.br/agenda")
    assert "SECRET_JS" not in t and ".a{}" not in t
    assert "Menu" not in t                       # nav dropped
    assert "Todos os direitos" not in t          # footer dropped (main content is substantial)
    assert "Janeiro" not in t                    # form/select dropped
    assert "[img: Roda de Samba https://casa.com.br/img/roda.jpg]" in t   # lazy src, absolute
    assert "arrow.svg" not in t and "data:image" not in t
    assert t.count("<https://casa.com.br/eventos/roda>") == 1             # consecutive dup href
    assert "<https://www.sympla.com.br/evento/roda/1>" in t
    assert "Roda de Samba do Zé" in t and "Sex 16/10 21h00 — R$ 30" in t
    assert t.count("Repetido") == 1
    assert "Choro na Praça\nSáb 17/10 18h — gratuito" in t                # one block per line


def test_strip_html_keeps_header_when_it_holds_the_page():
    html = "<body><header><h2>AGENDA</h2><p>Sex 16/10 Samba do Trabalhador</p></header></body>"
    assert "Samba do Trabalhador" in V.strip_html(html, "https://x/")


def test_strip_real_page_and_chunking():
    html = (FIX / "venues_bluenotesp_show.html").read_text()
    t = V.strip_html(html, "https://bluenotesp.com/shows/biafra-no-tempo-do-vinil/")
    assert "BIAFRA" in t and "<script" not in t and "function(" not in t
    assert len(t) < len(html) / 5
    chunks = V.chunk_text("\n".join(f"linha {i} " + "x" * 50 for i in range(1000)), size=5000,
                          max_chunks=3)
    assert len(chunks) == 3 and all(len(c) <= 5000 for c in chunks)


# ------------------------------------------------------------------------------ LLM path

CANNED = {"events": [
    {"title": "Roda de Samba do Zé", "artists": ["Zé", "Regional"], "date": "2026-10-16",
     "time": "21:00", "venue": "Salão", "price_min": 30, "price_max": 60, "is_free": False,
     "sold_out": False, "link": "/eventos/roda", "image_url": "/img/roda.jpg",
     "description": "Samba de raiz"},
    {"title": "Choro de Janeiro", "artists": [], "date": "01-15", "time": "", "venue": "",
     "price_min": None, "price_max": None, "is_free": True, "sold_out": True, "link": "",
     "image_url": "", "description": ""},
    {"title": "Show 21h", "artists": [], "date": "13/11", "time": "21h30", "venue": "",
     "price_min": None, "price_max": None, "is_free": False, "sold_out": False,
     "link": "https://www.sympla.com.br/evento/x/1", "image_url": "", "description": ""},
    {"title": "", "artists": [], "date": "2026-10-20", "time": "", "venue": "", "price_min": None,
     "price_max": None, "is_free": False, "sold_out": False, "link": "", "image_url": "",
     "description": ""},                                         # no title -> dropped
    {"title": "Sem data", "artists": [], "date": "em breve", "time": "", "venue": "",
     "price_min": None, "price_max": None, "is_free": False, "sold_out": False, "link": "",
     "image_url": "", "description": ""},                        # bad date -> dropped
]}


def _patch_llm(monkeypatch, calls):
    monkeypatch.setattr(llm, "available", lambda: True)

    def fake(**kw):
        calls.append(kw)
        return CANNED
    monkeypatch.setattr(llm, "json_call", fake)


def test_llm_path_maps_events(monkeypatch):
    calls = []
    _patch_llm(monkeypatch, calls)
    monkeypatch.setattr(V.http, "get", lambda url, **kw: FakeResp(PAGE))
    vcfg = {"name": "Casa do Samba", "url": "https://casa.com.br/agenda", "neighborhood": "Lapa"}
    evs, strategy = V.collect_venue(RIO, vcfg, {}, today=TODAY)
    assert strategy == "llm"
    assert len(calls) == 1
    call = calls[0]
    assert call["schema"] is llm.EVENT_LIST_SCHEMA
    assert "Roda de Samba do Zé" in call["prompt"] and "SECRET_JS" not in call["prompt"]
    assert "2026-10-12" in call["system"]                         # week-start reference date
    assert call["cache_tag"].startswith("venues:v1:")

    by_title = {e.title: e for e in evs}
    assert set(by_title) == {"Roda de Samba do Zé", "Choro de Janeiro", "Show 21h"}
    roda = by_title["Roda de Samba do Zé"]
    assert roda.start_datetime == datetime(2026, 10, 16, 21, 0, tzinfo=TZ)
    assert roda.venue == "Casa do Samba" and roda.neighborhood == "Lapa"   # cfg venue wins
    assert roda.ticket_url == "https://casa.com.br/eventos/roda"           # relative -> absolute
    assert roda.image_url == "https://casa.com.br/img/roda.jpg"
    assert roda.source_url == "https://casa.com.br/agenda" and roda.source == "venues"
    assert (roda.price_min, roda.price_max) == (30.0, 60.0)
    assert roda.artists == ["Zé", "Regional"] and roda.description == "Samba de raiz"

    jan = by_title["Choro de Janeiro"]
    assert jan.start_datetime == datetime(2027, 1, 15, 20, 0, tzinfo=TZ)   # year inferred, default time
    assert jan.is_free and jan.sold_out

    show = by_title["Show 21h"]
    assert show.start_datetime == datetime(2026, 11, 13, 21, 30, tzinfo=TZ)


def test_llm_cache_tag_depends_on_page_text(monkeypatch):
    calls = []
    _patch_llm(monkeypatch, calls)
    vcfg = {"name": "Casa", "url": "https://casa/"}
    V.llm_extract("Sex 16/10 Show A", RIO, vcfg, "https://casa/", TODAY)
    V.llm_extract("Sex 16/10 Show A", RIO, vcfg, "https://casa/", TODAY)
    V.llm_extract("Sex 16/10 Show B", RIO, vcfg, "https://casa/", TODAY)
    tags = [c["cache_tag"] for c in calls]
    assert tags[0] == tags[1] != tags[2]


def test_llm_skipped_without_key_warns_once(monkeypatch, caplog):
    monkeypatch.setattr(llm, "available", lambda: False)
    monkeypatch.setattr(llm, "json_call", lambda **kw: pytest.fail("must not call LLM"))
    monkeypatch.setattr(V.http, "get", lambda url, **kw: FakeResp(PAGE))
    vcfg = {"name": "Casa", "url": "https://casa/a", "extra_urls": ["https://casa/b"]}
    with caplog.at_level("WARNING"):
        evs, strategy = V.collect_venue(RIO, vcfg, {})
    assert evs == [] and strategy == "llm-skipped"
    assert sum("ANTHROPIC_API_KEY" in r.message for r in caplog.records) == 1


def test_auto_prefers_jsonld_over_llm(monkeypatch):
    html = (FIX / "venues_bluenotesp_show.html").read_text()
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "json_call", lambda **kw: pytest.fail("JSON-LD found; no LLM"))
    monkeypatch.setattr(V.http, "get", lambda url, **kw: FakeResp(html))
    evs, strategy = V.collect_venue(SP, {"name": "Blue Note São Paulo", "url": "https://bluenotesp.com/x"}, {})
    assert strategy == "jsonld" and [e.title for e in evs] == ["BIAFRA"]


# ------------------------------------------------------------------------------ isolation

def test_one_bad_venue_does_not_stop_others(monkeypatch):
    def fake_get_json(url, **kw):
        if "broken" in url:
            raise RuntimeError("boom")
        return json.loads((FIX / "venues_circovoador_eventos.json").read_text())

    def fake_get(url, **kw):
        raise ConnectionError("down")
    monkeypatch.setattr(V.http, "get_json", fake_get_json)
    monkeypatch.setattr(V.http, "get", fake_get)
    monkeypatch.setattr(llm, "available", lambda: False)
    cfg = {"venues": [
        {"name": "Broken", "mode": "circovoador", "api": "https://broken/api", "url": "https://broken/"},
        {"name": "Down", "url": "https://down/"},
        {"name": "Off", "url": "https://off/", "enabled": False},
        "not-a-dict",
        {"name": "Circo Voador", "mode": "circovoador", "url": "https://www.circovoador.com.br/"},
    ]}
    evs = list(V.collect(RIO, cfg))
    assert len(evs) == 2 and {e.venue for e in evs} == {"Circo Voador"}
