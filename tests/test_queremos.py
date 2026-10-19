"""Offline tests for the Queremos! collector (Wix agenda HTML + Sympla Bileto JSON fixtures)."""
import json
from datetime import datetime
from pathlib import Path

from musicfinder.collectors import queremos
from musicfinder.config import CityConfig
from musicfinder.models import TZ

FIX = Path(__file__).parent / "fixtures"
AGENDA = (FIX / "queremos_sample.html").read_text()
BILETO = json.loads((FIX / "queremos_bileto_sample.json").read_text())


class _Resp:
    def __init__(self, text):
        self.text = text


def test_parse_cards():
    cards = queremos.parse_cards(AGENDA)
    assert len(cards) == 8
    ocean = [c for c in cards if c["title"].startswith("Ocean Alley")][0]
    assert ocean["date_text"] == "05 de Novembro"
    assert ocean["city"] == "Rio de Janeiro"
    assert "bileto.sympla.com.br/event/123300" in ocean["ticket_url"]
    assert {queremos._card_city(c) for c in cards} == {"rio de janeiro", "sao paulo"}


def test_card_date_year_rollover():
    today = datetime(2026, 10, 16, tzinfo=TZ)
    assert queremos.card_date("05 de Novembro", today) == datetime(2026, 11, 5, tzinfo=TZ)
    assert queremos.card_date("14 de Agosto", today).year == 2027
    assert queremos.card_date("13 de Abril", today) == datetime(2027, 4, 13, tzinfo=TZ)


def test_events_from_bileto():
    card = {"title": "Ocean Alley", "date_text": "05 de Novembro", "extra": [], "image": ""}
    evs = queremos.events_from_bileto(card, BILETO["data"], "https://bileto.sympla.com.br/event/123300")
    assert len(evs) == 1
    e = evs[0]
    assert e.start_datetime == datetime(2026, 11, 5, 21, 30, tzinfo=TZ)
    assert e.venue == "Circo Voador"
    assert (e.price_min, e.price_max) == (180.0, 360.0)
    assert not e.sold_out
    assert e.artists == ["Ocean Alley"]
    assert e.source == "queremos"


def test_card_fallback_venue_and_time():
    card = {"title": "The Cat Empire", "date_text": "13 de Novembro", "extra": [], "image": "",
            "ticket_url": "https://www.ticketmaster.com.br/event/the-cat-empire-no-circo-voador-rj"}
    e = queremos.event_from_card(card)
    assert e.venue == "Circo Voador"
    assert (e.start_datetime.hour, e.start_datetime.minute) == (21, 0)
    assert "placeholder" in e.description


def test_collect_offline(monkeypatch):
    monkeypatch.setattr(queremos.http, "get", lambda url, **kw: _Resp(AGENDA))
    monkeypatch.setattr(queremos, "fetch_bileto", lambda eid: BILETO["data"])
    rio = list(queremos.collect(CityConfig("rio", "Rio", {}), {}))
    assert {e.title for e in rio} >= {"Ocean Alley", "The Cat Empire"}
    assert all("São Paulo" not in e.title for e in rio)
    sp = list(queremos.collect(CityConfig("sp", "SP", {}), {}))
    assert any(e.venue == "Audio" for e in sp)
