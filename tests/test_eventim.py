import json
from datetime import datetime
from pathlib import Path

from musicfinder.collectors import eventim
from musicfinder.config import CityConfig
from musicfinder.models import TZ

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "eventim_sample.json").read_text())


def _run(monkeypatch, city=CityConfig("sp", "São Paulo", {}), cfg=None):
    calls = []

    def fake_get_json(url, **kw):
        calls.append((url, kw))
        return FIXTURE

    monkeypatch.setattr(eventim.http, "get_json", fake_get_json)
    return list(eventim.collect(city, cfg or {})), calls


def test_parses_fixture(monkeypatch):
    events, _ = _run(monkeypatch)
    titles = [e.title for e in events]
    # add-ons (transport, hospitality), cancelled dates and comedy are dropped
    assert not any("TRANSPORTE" in t or "HOSPITALIDADE" in t or "PACOTE VIP" in t for t in titles)
    assert not any("MIRANDA KASSIN" in t or "CLÔ" in t for t in titles)
    assert titles.count("WAGNER TISO & TONINHO HORTA") == 2   # two sessions -> two events

    ev = next(e for e in events if e.title == "JORGE BEN JOR")
    assert ev.source == "eventim"
    assert ev.start_datetime == datetime(2026, 10, 17, 19, 0, tzinfo=TZ)
    assert ev.start_datetime.utcoffset().total_seconds() == -3 * 3600
    assert ev.venue == "Nubank Parque"
    assert ev.artists == ["JORGE BEN JOR"]
    assert ev.ticket_url.startswith("https://www.eventim.com.br/event/")
    assert ev.source_url.startswith("https://www.eventim.com.br/artist/")
    assert ev.image_url.startswith("https://")
    assert "Samba & Pagode" in ev.description
    assert ev.price_min is None and not ev.is_free

    # mainAttraction == venue ("BLUE NOTE SÃO PAULO") is not used as artist
    wt = next(e for e in events if e.title.startswith("WAGNER TISO"))
    assert wt.venue == "Blue Note São Paulo" and wt.artists == []


def test_request_params(monkeypatch):
    _, calls = _run(monkeypatch, CityConfig("rio", "Rio de Janeiro", {}))
    url, kw = calls[0]
    assert url == eventim.API
    p = kw["params"]
    assert p["city_ids"] == "1672" and p["webId"] == "web__eventim-com-br"
    assert "Shows & Música" in p["categories"] and p["sort"] == "DateAsc"
    assert "sec-ch-ua" in kw["headers"] and kw["headers"]["Sec-Fetch-Mode"] == "cors"
    assert len(calls) == 1   # totalPages == 1


def test_city_mismatch_dropped(monkeypatch):
    # with the Rio config the São Paulo products of the fixture are all rejected by city name
    events, _ = _run(monkeypatch, CityConfig("rio", "Rio de Janeiro", {}))
    assert events == []
