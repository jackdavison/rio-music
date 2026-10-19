"""Offline tests for the Fever collector (trimmed serverapp-state from live feverup.com pages)."""
from datetime import datetime, timedelta
from pathlib import Path

from musicfinder.collectors import fever
from musicfinder.config import CityConfig
from musicfinder.models import TZ

FIX = Path(__file__).parent / "fixtures"
FILTER = (FIX / "fever_filter_sample.html").read_text()
PLAN = (FIX / "fever_plan_sample.html").read_text()
PLAN_ID = 431209


class _Resp:
    def __init__(self, text, status=200):
        self.text, self.status_code = text, status

    def raise_for_status(self):
        pass


class _Sess:
    def get(self, url, **kw):
        return _Resp(FILTER) if url.endswith("eventos-musica") else _Resp("", 404)


def test_plans_from_filter_ignores_carousels():
    plans = fever.plans_from_filter(fever.page_state(FILTER))
    assert len(plans) == 5
    assert plans[0]["name"].startswith("Candlelight")


def test_plan_detail_and_sessions():
    detail, sessions = fever.plan_detail(fever.page_state(PLAN), PLAN_ID)
    assert detail["cityCode"] == "GIG"
    assert sessions and all("starts_at_iso" in s for s in sessions)
    plan = [p for p in fever.plans_from_filter(fever.page_state(FILTER)) if p["id"] == PLAN_ID][0]
    assert fever.is_music(plan, detail)
    evs = fever.build_events(plan, detail, sessions)
    assert len(evs) == 4  # one per distinct start time
    first = evs[0]
    assert first.start_datetime == datetime(2026, 10, 17, 17, 0, tzinfo=TZ)
    assert first.sold_out  # no ticket left in any zone
    later = [e for e in evs if not e.sold_out][0]
    assert later.price_min == 59.0 and later.price_max == 200.0
    assert later.venue == "Solar de Botafogo" and later.neighborhood == "Botafogo"
    assert later.description.startswith("Candlelight concert")
    assert later.ticket_url == f"https://feverup.com/m/{PLAN_ID}"


def test_gift_cards_are_not_music():
    plans = fever.plans_from_filter(fever.page_state(FILTER))
    gift = [p for p in plans if "presente" in p["name"].lower()][0]
    assert not fever.is_music(gift, {"category": "concert"})


def test_collect_offline(monkeypatch):
    monkeypatch.setattr(fever.http, "session", lambda: _Sess())
    monkeypatch.setattr(fever.http, "get", lambda url, **kw: _Resp(PLAN if url.endswith(str(PLAN_ID)) else "<html></html>"))
    lo = datetime(2026, 10, 1, tzinfo=TZ)
    monkeypatch.setattr(fever, "horizon", lambda: (lo, lo + timedelta(days=90)))
    evs = list(fever.collect(CityConfig("rio", "Rio", {}), {}))
    candle = [e for e in evs if e.title.startswith("Candlelight: Coldplay")]
    assert len(candle) == 4
    assert not any("presente" in e.title.lower() for e in evs)
    # SP filter must drop the Rio plan by cityCode
    sp = list(fever.collect(CityConfig("sp", "SP", {}), {}))
    assert not any(e.title.startswith("Candlelight: Coldplay") for e in sp)
