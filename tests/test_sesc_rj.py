from datetime import datetime
from pathlib import Path

from musicfinder.collectors import sesc_rj
from musicfinder.config import CityConfig
from musicfinder.models import TZ

FIXDIR = Path(__file__).parent / "fixtures"


class _Resp:
    def __init__(self, text):
        self.text = text


def _run(monkeypatch, cfg=None):
    calls = []

    def fake_get(url, params=None, **kw):
        calls.append((url, params))
        if url.endswith("/programacao"):
            assert params["page"] == 1 and "date_from" in params and "date_to" in params
            return _Resp((FIXDIR / "sesc_rj_sample.html").read_text())
        slug = url.rsplit("/", 1)[-1]
        p = FIXDIR / f"sesc_rj_detail_{slug}.html"
        if not p.exists():
            raise AssertionError(f"non-music card fetched: {slug}")
        return _Resp(p.read_text())

    monkeypatch.setattr(sesc_rj.http, "get", fake_get)
    return list(sesc_rj.collect(CityConfig("rio", "Rio", {}), cfg or {})), calls


def test_rsc_parsing():
    cards, total = sesc_rj.parse_listing((FIXDIR / "sesc_rj_sample.html").read_text())
    assert total == 1 and len(cards) == 10
    ev = sesc_rj.parse_detail((FIXDIR / "sesc_rj_detail_heloa-apresenta-arida.html").read_text())
    assert ev["title"] == "Héloa apresenta Árida" and len(ev["schedule"]) == 4


def test_city_units_and_prices(monkeypatch):
    events, calls = _run(monkeypatch)
    # only cards tagged MÚSICA get a detail fetch (theatre/dance/etc. are skipped)
    fetched = {u.rsplit("/", 1)[-1] for u, p in calls if p is None}
    assert "mesmo-rio" not in fetched and "heloa-apresenta-arida" in fetched

    by_venue = {(e.title, e.venue): e for e in events}
    # Héloa plays 4 units; only Copacabana + Ramos are in the city of Rio
    assert {v for t, v in by_venue if t == "Héloa apresenta Árida"} == {"Sesc Copacabana", "Sesc Ramos"}
    h = by_venue[("Héloa apresenta Árida", "Sesc Copacabana")]
    assert h.start_datetime == datetime(2026, 10, 20, 19, 0, tzinfo=TZ)
    assert h.end_datetime == datetime(2026, 10, 20, 20, 10, tzinfo=TZ)
    assert (h.price_min, h.price_max, h.is_free) == (7.5, 15.0, False)
    assert h.neighborhood == "Copacabana" and h.artists == ["Héloa"]
    assert h.source == "sesc_rj" and h.source_url.endswith("/programacao/heloa-apresenta-arida")
    ramos = by_venue[("Héloa apresenta Árida", "Sesc Ramos")]
    assert ramos.ticket_url.startswith("https://www.ingresso.com/")

    elymar = by_venue[("Elymar Santos", "Sesc Ginástico")]
    assert (elymar.price_min, elymar.price_max) == (15.0, 60.0)
    assert elymar.neighborhood == "Centro"
    assert "Show" in elymar.description

    # Vivência-category item (and São Gonçalo unit) is excluded
    assert not any("Slam" in e.title for e in events)


def test_metro_units_and_free_sessions(monkeypatch):
    events, _ = _run(monkeypatch, {"include_metro": True})
    taylor = {e.venue: e for e in events if e.title == "FORRÓ DA TAYLOR"}
    assert set(taylor) == {"Sesc Tijuca", "Sesc Nova Iguaçu", "Sesc São João de Meriti"}
    assert taylor["Sesc Nova Iguaçu"].is_free and taylor["Sesc Nova Iguaçu"].price_min == 0
    assert taylor["Sesc Tijuca"].price_min == 5.0 and taylor["Sesc Tijuca"].price_max == 10.0
    assert taylor["Sesc São João de Meriti"].end_datetime is None
    assert len({e.id for e in events}) == len(events)


def test_session_date_range_expands():
    s = {"dateIso": "2026-11-01/2026-11-03", "timeIso": "20:00:00/22:00:00"}
    starts = sesc_rj._session_starts(s)
    assert [st.day for st, _ in starts] == [1, 2, 3]
    assert all(en.hour == 22 for _, en in starts)
