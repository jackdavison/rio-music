import json
from datetime import datetime
from pathlib import Path

from musicfinder.collectors import bilheteria_digital as bd
from musicfinder.config import CityConfig
from musicfinder.models import TZ

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "bilheteria_digital_sample.json").read_text())


class _Resp:
    def __init__(self, text="", data=None):
        self.text, self._data = text, data

    def json(self):
        return self._data

    def raise_for_status(self):
        pass


class _Session:
    def __init__(self, calls):
        self.calls = calls

    def post(self, url, data=None, **kw):
        self.calls.append(("POST", url, data))
        return _Resp(data=FIXTURE["listing"])


def _run(monkeypatch, city=CityConfig("rio", "Rio de Janeiro", {}), cfg=None):
    calls = []

    def fake_get(url, **kw):
        calls.append(("GET", url, None))
        if url == bd.BASE + "/RJ":
            return _Resp(FIXTURE["state_html"])
        path = url[len(bd.BASE):]
        if path not in FIXTURE["pages"]:
            raise AssertionError(f"unexpected detail fetch {path}")
        return _Resp(FIXTURE["pages"][path])

    monkeypatch.setattr(bd.http, "session", lambda: _Session(calls))
    monkeypatch.setattr(bd.http, "get", fake_get)
    return list(bd.collect(city, cfg or {})), calls


def test_parses_fixture(monkeypatch):
    events, calls = _run(monkeypatch)
    titles = [e.title for e in events]
    assert "Dom Quixote – Uma Aventura Sem Palavras" not in titles      # theatre (style 8)
    assert not any("Canyon" in t for t in titles)                        # Campos dos Goytacazes
    assert calls[0] == ("POST", bd.LIST_API, {"paginaAtual": 1, "estado": "RJ"})

    ana = next(e for e in events if e.title == "ANA FRANGO ELÉTRICO DE BOLSO")
    assert ana.source == "bilheteria_digital"
    assert ana.start_datetime == datetime(2026, 10, 30, 19, 0, tzinfo=TZ)   # 22:00Z -> 19:00 BRT
    assert ana.venue == "CAIXA CULTURAL RIO DE JANEIRO"
    assert (ana.price_min, ana.price_max) == (15.0, 40.0) and not ana.is_free
    assert ana.ticket_url == bd.BASE + "/ana-frango-eletrico-de-bolso-30-de-outubro"
    assert ana.image_url.startswith("https://s3.amazonaws.com/")
    assert "Ana Frango Elétrico" in ana.description

    caio = next(e for e in events if e.title.startswith("CAIO LUCCAS"))
    assert (caio.venue, caio.neighborhood) == ("Soho Lounge", "Padre Miguel")
    assert caio.price_min is None and not caio.is_free and not caio.sold_out

    # JSON link 404s for this one; the real one comes from the state page (joined on image id).
    # It is a réveillon party (style 15) without music cues, so it is fetched but not kept.
    fetched = [c[1] for c in calls if c[0] == "GET"]
    assert bd.BASE + "/reveillon-tonamata-2027-4a-edicao-all-inclusive-31-de-dezembro" in fetched
    assert not any("TONAMATA" in t for t in titles)
    detail = bd.parse_event_page(
        FIXTURE["pages"]["/reveillon-tonamata-2027-4a-edicao-all-inclusive-31-de-dezembro"])
    assert detail["style"] == 15
    assert detail["start"].astimezone(TZ) == datetime(2026, 12, 31, 21, 0, tzinfo=TZ)


def test_listing_only_fallback(monkeypatch):
    events, calls = _run(monkeypatch, cfg={"fetch_details": False})
    assert not [c for c in calls if c[0] == "GET" and c[1] != bd.BASE + "/RJ"]
    samba = next(e for e in events if e.title == "SAMBA E PAGODE")
    assert samba.start_datetime.hour == 15 and samba.start_datetime.day == 19
    assert samba.start_datetime.month == 10


def test_venue_split():
    assert bd._venue("Rua Francisco Real, nº580 | Padre Miguel", "") == (
        "Rua Francisco Real, nº580", "Padre Miguel")
    assert bd._venue("Teatro X", "Local: Y") == ("Teatro X", "")
