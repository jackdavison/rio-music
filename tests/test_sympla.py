import json
from datetime import datetime
from pathlib import Path

from musicfinder.collectors import sympla
from musicfinder.config import CityConfig
from musicfinder.models import TZ

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "sympla_sample.json").read_text())


def _run(monkeypatch, city=CityConfig("rio", "Rio", {}), cfg=None):
    calls = []

    def fake_post_json(url, payload=None, **kw):
        calls.append(payload)
        # first query (theme 99) gets the fixture, the arts query gets nothing
        return FIXTURE if len(calls) == 1 else {"data": [], "total": 0}

    monkeypatch.setattr(sympla.http, "post_json", fake_post_json)
    return list(sympla.collect(city, cfg or {})), calls


def test_parses_fixture(monkeypatch):
    events, calls = _run(monkeypatch)
    titles = [e.title for e in events]
    # junk (surf camp, daily visit) and online rows are dropped
    assert not any("surf camp" in t or "Visitação" in t or "Live solidária" in t for t in titles)
    assert len(events) == 3

    ev = next(e for e in events if "Orquestra Furiosa" in e.title)
    assert ev.source == "sympla"
    assert ev.start_datetime == datetime(2026, 10, 28, 19, 0, tzinfo=TZ)  # 22:00 UTC -> 19:00 BRT
    assert ev.start_datetime.utcoffset().total_seconds() == -3 * 3600
    assert ev.venue == "Rua da Carioca, 38"
    assert ev.neighborhood == "Centro"
    assert ev.ticket_url.startswith("https://www.sympla.com.br/evento/")
    assert ev.image_url.startswith("https://")
    assert "Casa do Choro" in ev.description
    assert ev.is_free is False and ev.price_min is None

    free = next(e for e in events if "Mulheres Negras" in e.title)
    assert free.is_free and free.price_min == 0.0


def test_request_params(monkeypatch):
    _, calls = _run(monkeypatch, CityConfig("sp", "São Paulo", {}))
    p = calls[0]
    assert p["service"] == "/v4/search/query"
    assert p["params"]["city"] == "São Paulo"
    assert p["params"]["themes"] == "99"
    assert p["params"]["type"] == "normal"
    assert p["params"]["page"] == "1"
    assert calls[1]["params"]["themes"] == "104"  # secondary arts query
