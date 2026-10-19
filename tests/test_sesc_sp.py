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
