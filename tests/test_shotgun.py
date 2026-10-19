"""Offline tests for the Shotgun collector (fixtures trimmed from live shotgun.live pages)."""
from datetime import datetime, timedelta
from pathlib import Path

from musicfinder.collectors import shotgun
from musicfinder.config import CityConfig
from musicfinder.models import TZ

FIX = Path(__file__).parent / "fixtures"
LISTING = (FIX / "shotgun_sample.html").read_text()
DETAIL = (FIX / "shotgun_event_sample.html").read_text()


def test_parse_listing_cards():
    cards = {c["slug"]: c for c in shotgun.parse_listing(LISTING)}
    assert len(cards) == 4
    b = cards["beefy1610"]
    assert b["title"] == "Beefy"
    assert b["venue"] == "RETRÔ LAPA PUB DANCING"
    assert b["start"] == "2026-10-17T02:00:00.000Z"
    assert b["label"] == "R$30.00"
    assert "House" in b["tags"]
    assert b["image"].startswith("https://")
    free = [c for c in cards.values() if c["label"] == "Free"]
    assert free


def test_parse_detail_and_build_event():
    detail = shotgun.parse_detail(DETAIL)
    assert detail["@type"] == "MusicEvent"
    card = {c["slug"]: c for c in shotgun.parse_listing(LISTING)}["beefy1610"]
    ev = shotgun.build_event(card, detail)
    # 02:00Z -> 23:00 local the previous day
    assert ev.start_datetime == datetime(2026, 10, 16, 23, 0, tzinfo=TZ)
    assert ev.end_datetime.hour == 4
    assert ev.venue == "RETRÔ LAPA PUB DANCING"
    assert ev.neighborhood == "Lapa"
    assert ev.artists == ["DJ Edu_", "J U N O", "Valdo S"]
    # sold-out promo lots ignored; cheapest in-stock lot is R$30, dearest R$40
    assert (ev.price_min, ev.price_max) == (30.0, 40.0)
    assert not ev.sold_out and not ev.is_free
    assert ev.ticket_url == "https://shotgun.live/en/events/beefy1610"
    assert ev.description.startswith("Genres:")
    assert ev.source == "shotgun"


def test_card_only_free_event():
    card = [c for c in shotgun.parse_listing(LISTING) if c["label"] == "Free"][0]
    ev = shotgun.build_event(card, None)
    assert ev.is_free and ev.price_min == 0


def test_price_en_and_neighborhood():
    assert shotgun._price_en("R$1,234.50") == 1234.5
    assert shotgun._price_en("Free") is None
    assert shotgun._neighborhood({"addressLocality": "Copacabana"}) == "Copacabana"
    assert shotgun._neighborhood({"addressLocality": "Rio De Janeiro",
                                  "streetAddress": "Rua X 1 - Lapa - Centro, Rio"}) == "Lapa"


def test_collect_offline(monkeypatch):
    def fake_fetch(url, transport="auto"):
        return DETAIL if "/events/beefy1610" in url else ("<html></html>" if "/events/" in url
                                                           else LISTING)
    monkeypatch.setattr(shotgun, "_fetch", fake_fetch)
    lo = datetime(2026, 10, 1, tzinfo=TZ)
    monkeypatch.setattr(shotgun, "horizon", lambda: (lo, lo + timedelta(days=90)))
    evs = list(shotgun.collect(CityConfig("rio", "Rio", {}), {}))
    titles = {e.title for e in evs}
    assert "Beefy" in titles and len(evs) == 4
    assert all(e.source == "shotgun" for e in evs)


def test_challenge_detection():
    assert shotgun._is_challenge(429, "")
    assert shotgun._is_challenge(200, "<title>Vercel Security Checkpoint</title>")
    assert not shotgun._is_challenge(200, "<html>ok</html>")
