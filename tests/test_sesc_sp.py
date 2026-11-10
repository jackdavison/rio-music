import json
from datetime import datetime
from pathlib import Path

from musicfinder.collectors import sesc_sp
from musicfinder.config import CityConfig
from musicfinder.models import TZ

FIX = json.loads((Path(__file__).parent / "fixtures" / "sesc_sp_sample.json").read_text())


class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code, self._payload, self.text = status, payload, text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class _Session:
    def get(self, url, params=None, **kw):
        slug = (params or {}).get("slug")
        if slug in FIX["details"]:
            return _Resp(200, FIX["details"][slug])
        return _Resp(404, {"code": "sessions_not_found"})


def _run(monkeypatch, cfg=None):
    def fake_get_json(url, params=None, **kw):
        assert url.endswith("/atividades/filter")
        assert params["linguagem"] == "musica"
        return FIX["list"]

    def fake_get(url, **kw):
        return _Resp(200, text=FIX["pages"].get(url, "<html></html>"))

    monkeypatch.setattr(sesc_sp.http, "get_json", fake_get_json)
    monkeypatch.setattr(sesc_sp.http, "get", fake_get)
    monkeypatch.setattr(sesc_sp.http, "session", lambda: _Session())
    return list(sesc_sp.collect(CityConfig("sp", "São Paulo", {}), cfg or {}))


def test_units_filtered_and_sessions_expanded(monkeypatch):
    events = _run(monkeypatch)
    venues = {e.venue for e in events}
    # interior unit (Ribeirão Preto) and the unit-less activity are dropped
    assert venues == {"Sesc 24 de Maio", "Sesc Pompeia", "Sesc Casa Verde"}
    m80 = sorted((e for e in events if e.title == "A Música Dos 80 Anos"), key=lambda e: e.start_datetime)
    assert [e.start_datetime for e in m80] == [
        datetime(2026, 10, 16, 20, 0, tzinfo=TZ),
        datetime(2026, 10, 17, 20, 0, tzinfo=TZ),
        datetime(2026, 10, 18, 18, 0, tzinfo=TZ),
    ]
    assert len({e.id for e in m80}) == 3


def test_paid_tiers_and_sold_out(monkeypatch):
    events = _run(monkeypatch)
    m80 = next(e for e in events if e.title == "A Música Dos 80 Anos")
    assert (m80.price_min, m80.price_max, m80.is_free) == (15.0, 50.0, False)  # credencial plena / inteira
    assert m80.neighborhood == "República"
    assert m80.ticket_url.startswith("https://centralrelacionamento.sescsp.org.br/")
    assert "&amp;" not in m80.ticket_url
    assert "Ayrton Montarroyos" in m80.artists and "Leila Pinheiro" in m80.artists
    assert m80.source == "sesc_sp" and m80.source_url.startswith("https://www.sescsp.org.br/programacao/")
    tom = next(e for e in events if e.title == "Tom Zé")
    assert tom.sold_out and tom.price_min == 21.0 and tom.price_max == 70.0
    assert tom.artists == ["Tom Zé"]
    assert m80.description.startswith("Com Ayrton Montarroyos")


def test_free_activity_uses_page_sessions(monkeypatch):
    events = _run(monkeypatch)
    q = [e for e in events if e.venue == "Sesc Casa Verde"]
    assert len(q) == 1
    ev = q[0]
    assert ev.title == "Quintetinho Caracaxá"
    assert ev.is_free and ev.price_min == 0
    assert ev.start_datetime == datetime(2026, 10, 16, 17, 0, tzinfo=TZ)
    assert ev.end_datetime == datetime(2026, 10, 16, 18, 30, tzinfo=TZ)
    assert "choro" in ev.description.lower()
    assert "Cafeteria" in ev.description


def test_unit_config(monkeypatch):
    events = _run(monkeypatch, {"units": ["pompeia"], "extra_units": ["ribeirao-preto"]})
    assert {e.venue for e in events} == {"Sesc Pompeia", "Sesc Ribeirão Preto"}


# ---- fallback when /wp-json/ answers 403 (since 2026-10-31): RSS feed + activity pages

def _page(title, unit, unit_name, sessions="", com="", free=True, cancelado=""):
    return f"""<html><head><meta property="og:title" content="{title} - Sesc São Paulo" />
<meta property="og:image" content="https://www.sescsp.org.br/img/{unit}.jpg" />
<meta property="og:description" content="Show de choro." /></head><body>
<div class="breadcrumbs--container"> <!-- Home --> <a href="https://www.sescsp.org.br">Home</a>&nbsp;|
<!-- Unidade/Institucional --> <a href="/unidades/{unit}">{unit_name}</a> | Música</div>
<h2 class="principal--post--row--programacao--info--titulo">{title}</h2>
<h3 class="principal--post--row--programacao--info--com">{com}</h3>
<div data-esgotado="" data-cancelado="{cancelado}"></div>
{'<div class="evento--sessao--entrada--gratis"></div>' if free else ''}{sessions}
<p class="evento--sessao--entrada--local"> Local: <span>&nbsp;Comedoria</span> </p></body></html>"""


def _feed(urls):
    items = "".join(f"<item><title>x</title><link>{u}</link><pubDate>{d}</pubDate></item>" for u, d in urls)
    return f"<rss><channel>{items}</channel></rss>"


def test_feed_fallback_when_api_blocked(monkeypatch):
    import requests
    from datetime import timedelta
    from email.utils import format_datetime
    from musicfinder import db
    from musicfinder.models import Event

    now = datetime.now(TZ).replace(second=0, microsecond=0)
    day = (now + timedelta(days=5)).strftime("%Y-%m-%dT19:00")
    pub = format_datetime(now - timedelta(days=3))
    B = sesc_sp.BASE + "/programacao/"
    pages = {
        B + "choro/": _page("Choro na Comedoria", "pompeia", "Pompeia", com="com Trio Tal",
                            sessions=f'<i data-inicial="{day}" data-final="" ></i>'),
        B + "ingressos/": _page("Show Pago", "pinheiros", "Pinheiros", free=False),
        B + "longe/": _page("Interior", "marilia", "Marília",
                            sessions=f'<i data-inicial="{day}" data-final="" ></i>'),
        B + "cancelado/": _page("Cancelado", "pompeia", "Pompeia", cancelado="1",
                                sessions=f'<i data-inicial="{day}" data-final="" ></i>'),
    }
    # a ticketed show an earlier (API) run stored: kept, since its page carries no dates
    old = Event(city="sp", title="Show Pago", start_datetime=now + timedelta(days=9), source="sesc_sp",
                venue="Sesc Pinheiros", price_min=21.0, price_max=70.0, source_url=B + "ingressos/")
    old.sources = ["sesc_sp"]
    db.upsert(old)
    fetched = []

    def fake_get(url, params=None, **kw):
        fetched.append(url)
        if "/wp-json/" in url:
            r = requests.Response()
            r.status_code = 403
            raise requests.HTTPError(response=r)
        if url.endswith("/feed/"):
            if params["paged"] > 1:
                raise requests.HTTPError(response=requests.Response())
            return _Resp(200, text=_feed([(u, pub) for u in pages]))
        return _Resp(200, text=pages[url])

    monkeypatch.setattr(sesc_sp.http, "get", fake_get)
    monkeypatch.setattr(sesc_sp.http, "get_json", lambda url, **kw: fake_get(url, **kw).json())
    events = list(sesc_sp.collect(CityConfig("sp", "São Paulo", {}), {}))
    assert {(e.title, e.venue) for e in events} == {("Choro na Comedoria", "Sesc Pompeia"),
                                                    ("Show Pago", "Sesc Pinheiros")}
    choro = next(e for e in events if e.title == "Choro na Comedoria")
    assert choro.is_free and choro.start_datetime.hour == 19 and choro.artists == ["Trio Tal"]
    assert choro.neighborhood == "Pompeia" and "Comedoria" in choro.description
    assert next(e for e in events if e.title == "Show Pago").price_max == 70.0

    # second run: pages are cached, only the feed's first page is read
    fetched.clear()
    assert len(list(sesc_sp.collect(CityConfig("sp", "São Paulo", {}), {}))) == 2
    assert not [u for u in fetched if "/programacao/" in u]
