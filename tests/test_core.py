from datetime import date, datetime, timedelta

import pytest
from icalendar import Calendar

from musicfinder import changes, db, llm, rank, watchlist
from musicfinder.collectors import recurring
from musicfinder.config import CityConfig
from musicfinder.dedupe import dedupe
from musicfinder.genres import heuristic_score, tag
from musicfinder.models import Event, TZ
from musicfinder.outputs import ics, telegram, web


def ev(title, day=1, hour=21, venue="Teatro Rival", source="sympla", city="rio", **kw):
    base = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=day)
    return Event(city=city, title=title, start_datetime=base.replace(hour=hour), venue=venue,
                 source=source, **kw)


# ---------- models ----------

def test_id_is_stable_and_accent_insensitive():
    a = ev("Roda de Samba do Cacique", venue="Teatro Rival Refit")
    b = ev("RODA DE SAMBA DO CACIQUE", venue="teatro rival refit", source="eventim")
    assert a.id == b.id
    assert ev("Outro show").id != a.id


def test_free_inferred_from_zero_price():
    assert ev("x", price_min=0).is_free


# ---------- recurring ----------

def test_recurring_weekly_and_monthly():
    today = date(2026, 10, 16)  # a Friday
    weekly = {"name": "Roda", "weekday": "segunda", "time": "19:00", "every": "weekly"}
    days = recurring.expand(weekly, today)
    assert len(days) == 8 and all(d.weekday() == 0 for d in days)
    monthly = {"name": "Choro", "weekday": "saturday", "every": "monthly", "week_of_month": [1, -1]}
    days = recurring.expand(monthly, today)
    assert days and all(d.weekday() == 5 for d in days)
    assert all(d.day <= 7 or (d + timedelta(days=7)).month != d.month for d in days)


def test_recurring_skips_placeholders_and_respects_range():
    today = date(2026, 10, 16)
    entries = [
        {"name": "PLACEHOLDER", "placeholder": True, "weekday": "monday"},
        {"name": "Roda", "weekday": ["tue", "thu"], "time": "20:30", "end_time": "01:00",
         "active_until": "2026-10-31", "free": True, "venue": "Bar"},
    ]
    out = recurring.events_for("rio", entries, today)
    assert out and all(e.title == "Roda" for e in out)
    assert max(e.start_datetime.date() for e in out) <= date(2026, 10, 31)
    assert all(e.end_datetime > e.start_datetime and e.is_free for e in out)
    assert out[0].start_datetime.strftime("%H:%M") == "20:30"


# ---------- genres / heuristic ----------

def test_genre_tags_and_heuristic_order():
    choro = ev("Roda de Choro com regional", description="cavaquinho, bandolim e flauta")
    sert = ev("Show sertanejo", description="modão")
    assert {"choro", "sopros"} <= set(tag(choro))
    choro.genres, sert.genres = tag(choro), tag(sert)
    assert heuristic_score(choro)[0] > heuristic_score(sert)[0]


# ---------- dedupe ----------

def test_dedupe_merges_across_sources_keeps_best_ticket():
    a = ev("Hamilton de Holanda Quinteto", source="venues", venue="Blue Note Rio",
           ticket_url="", image_url="http://img")
    b = ev("Hamilton de Holanda - Quinteto | Blue Note", source="eventim", venue="Blue Note Rio",
           ticket_url="https://eventim/x", price_min=120)
    c = ev("Hamilton de Holanda Quinteto", source="sympla", venue="Circo Voador")  # other venue
    out = dedupe([a, b, c])
    assert len(out) == 2
    m = next(e for e in out if "Blue Note" in e.venue)
    assert m.ticket_url == "https://eventim/x" and m.image_url == "http://img"
    assert set(m.sources) == {"venues", "eventim"} and m.price_min == 120
    assert len(m.member_ids) == 2


def test_dedupe_keeps_separate_sessions_and_days():
    a = ev("Show X", hour=18, source="sympla")
    b = ev("Show X", hour=22, source="eventim")      # 4h apart: ambiguous -> no LLM -> separate
    c = ev("Show X", day=2, source="eventim")
    assert len(dedupe([a, b, c])) == 3


def test_dedupe_llm_tiebreak(monkeypatch):
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "json_call", lambda **kw: {"answers": [{"pair": 0, "same": True}]})
    a = ev("Moacyr Luz e Samba do Trabalhador", source="sympla", venue="Renascença Clube")
    b = ev("Samba do Trabalhador", source="venues", venue="Renascença")
    assert len(dedupe([a, b])) == 1


# ---------- ranking ----------

def test_rank_uses_llm_and_caches(monkeypatch):
    calls = []

    def fake(**kw):
        calls.append(kw)
        keys = [ln.split("]")[0][1:] for ln in kw["prompt"].splitlines()]
        return {"scores": [{"key": k, "score": 91, "reason": "great choro"} for k in keys]}

    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "json_call", fake)
    evs = [ev(f"Choro {i}") for i in range(3)]
    rank.rank(evs)
    assert [e.score for e in evs] == [91, 91, 91] and len(calls) == 1
    again = [ev(f"Choro {i}") for i in range(3)]
    rank.rank(again)
    assert len(calls) == 1 and again[0].score_reason == "great choro"


def test_rank_falls_back_without_key():
    e = ev("Roda de samba")
    e.genres = tag(e)
    rank.rank([e])
    assert e.score is not None and e.score_reason.startswith("[keyword]")


# ---------- changes / watchlist ----------

def test_changes_detection():
    old = ev("A")
    new = ev("A", sold_out=True)
    assert changes.diff({}, new) == [("new", "")]
    assert ("sold_out", "") in changes.diff({old.id: old}, new)
    assert changes.nearly_sold_out(ev("A", description="Últimos ingressos!"))


def test_watchlist_rules():
    rules = watchlist.parse(["artist: Hamilton de Holanda", "venue: Casa do Choro", "Moacyr Luz",
                             "artist: PLACEHOLDER Artist"])
    assert len(rules) == 3
    assert watchlist.matches(ev("Hamilton de Holanda Trio"), rules)
    assert watchlist.matches(ev("Qualquer", venue="Casa do Choro"), rules)
    assert watchlist.matches(ev("Show", artists=["Moacyr Luz"]), rules)
    assert not watchlist.matches(ev("Outro"), rules)


# ---------- pipeline (stable ids, change log) ----------

def test_pipeline_stable_ids_and_date_change(monkeypatch):
    from musicfinder import pipeline
    from musicfinder.collectors import base

    runs = [
        [ev("Roda Z", source="sympla", venue="Trapiche Gamboa")],
        # a better source now also lists it -> id must not change
        [ev("Roda Z", source="sympla", venue="Trapiche Gamboa"),
         ev("Roda Z", source="eventim", venue="Trapiche Gamboa")],
        # the show moves two days later -> same id, date_changed logged
        [ev("Roda Z", day=3, source="sympla", venue="Trapiche Gamboa")],
    ]
    city = CityConfig("rio", "Rio", {"collectors": {"fake": {}}})

    ids = []
    for evs in runs:
        monkeypatch.setattr(base, "run_city", lambda c, only=None, skip=None, evs=evs: [
            base.CollectorResult(n, "rio", [e for e in evs if e.source == n], True)
            for n in ("sympla", "eventim")])
        monkeypatch.setattr(pipeline, "run_city", base.run_city)
        out, _ = pipeline.process_city(city)
        assert len(out) == 1
        ids.append(out[0].id)
    assert len(set(ids)) == 1
    kinds = [r["kind"] for r in db.conn().execute("SELECT kind FROM changes").fetchall()]
    assert kinds == ["new", "date_changed"]
    assert len(db.load_events("rio")) == 1


def test_failed_collector_keeps_events(monkeypatch):
    from musicfinder import pipeline
    from musicfinder.collectors import base
    city = CityConfig("rio", "Rio", {})
    e = ev("Show Q", source="sympla")
    monkeypatch.setattr(pipeline, "run_city", lambda c, only=None, skip=None:
                        [base.CollectorResult("sympla", "rio", [e], True)])
    pipeline.process_city(city)
    monkeypatch.setattr(pipeline, "run_city", lambda c, only=None, skip=None:
                        [base.CollectorResult("sympla", "rio", [], False, error="boom")])
    out, _ = pipeline.process_city(city)
    assert [x.title for x in out] == ["Show Q"] and len(db.load_events("rio")) == 1
    # source ran fine but no longer lists it -> kept during the grace period, then deactivated
    monkeypatch.setattr(pipeline, "run_city", lambda c, only=None, skip=None:
                        [base.CollectorResult("sympla", "rio", [], True)])
    pipeline.process_city(city)
    assert len(db.load_events("rio")) == 1
    monkeypatch.setattr(db, "GONE_GRACE_HOURS", 0)
    pipeline.process_city(city)
    assert db.load_events("rio") == []


def test_run_collector_isolates_exceptions():
    from musicfinder.collectors import base

    @base.collector("_boom")
    def boom(city, cfg):
        raise ValueError("site changed")

    r = base.run_collector("_boom", CityConfig("rio", "Rio", {}), {})
    assert not r.ok and "site changed" in r.error


# ---------- outputs ----------

def _scored(*evs):
    for i, e in enumerate(evs):
        e.score, e.score_reason = 80 - i * 30, "reason"
        e.last_seen = e.first_seen = db.now_iso()
    return list(evs)


def test_ics_feeds(tmp_path, monkeypatch):
    monkeypatch.setattr(watchlist, "_rules", [("artist", "hamilton de holanda")])
    rio = _scored(ev("Hamilton de Holanda", ticket_url="https://t/1", neighborhood="Centro"), ev("Low"))
    sp = _scored(ev("SP Show", city="sp"))
    files = ics.write_feeds(tmp_path, {"rio": rio, "sp": sp}, {"rio": 60, "sp": 60}, {"rio": "Rio", "sp": "SP"})
    assert sorted(f.name for f in files) == ["rio.ics", "sp.ics", "watchlist.ics"]
    cal = Calendar.from_ical((tmp_path / "rio.ics").read_bytes())
    vs = list(cal.walk("VEVENT"))
    assert len(vs) == 1
    v = vs[0]
    assert str(v["uid"]) == f"{rio[0].id}@musicfinder"
    assert str(v["location"]) == "Teatro Rival, Centro" and str(v["url"]) == "https://t/1"
    assert "Score 80/100" in str(v["description"])
    w = Calendar.from_ical((tmp_path / "watchlist.ics").read_bytes())
    assert len(list(w.walk("VEVENT"))) == 1


def test_digest_layout():
    rio_city = CityConfig("rio", "Rio de Janeiro", {"digest": {"style": "full", "notable_min_score": 70}})
    sp_city = CityConfig("sp", "São Paulo", {"digest": {"style": "top", "top_min_score": 40}})
    now = datetime.now(TZ).replace(hour=8, minute=0)
    today = ev("Tonight show", day=0)
    today.score = 60
    later = ev("Big later", day=10)
    later.score = 90
    spe = ev("SP pick", day=3, city="sp")
    spe.score = 75
    text = telegram.build_digest([rio_city, sp_city], {"rio": [today, later], "sp": [spe]}, now=now)
    assert text.index("Rio de Janeiro") < text.index("São Paulo")
    assert "Tonight show" in text and "Big later" in text and "SP pick" in text
    assert text.index("Tonight show") < text.index("Big later") < text.index("SP pick")


def test_web_payload_marks_new():
    e = _scored(ev("Novo"))[0]
    p = web.payload({"rio": [e]}, {"rio": "Rio"})
    assert p["events"][0]["is_new"] is True and p["cities"][0]["slug"] == "rio"


# ---------- hand-entered gigs ----------

def test_gigs_full_and_tip(monkeypatch):
    from musicfinder.collectors import gigs
    today = datetime.now(TZ).date()
    d = (today + timedelta(days=5)).isoformat()
    out = gigs.events_for("rio", [
        {"title": "Igor Souza", "date": d, "time": "21:00", "venue": "Bar do Zé", "genre": "choro"},
        {"artists": ["Carlos Malta"], "date": d},
        {"title": "old", "date": "2020-01-01"},
        {"title": "no date"},
    ], today)
    assert [e.title for e in out] == ["Igor Souza", "Carlos Malta"]
    full, tip = out
    assert not full.time_unknown and full.start_datetime.strftime("%H:%M") == "21:00"
    assert tip.time_unknown and "horário e local a confirmar" in tip.description


def test_gig_tip_merges_into_real_listing():
    from musicfinder.collectors import gigs
    tip = gigs.events_for("rio", [{"artists": ["Carlos Malta"],
                                   "date": (datetime.now(TZ).date() + timedelta(days=1)).isoformat()}])[0]
    real = ev("Carlos Malta & Pife Muderno", hour=17, venue="Sesc Copacabana", source="sesc_rj",
              ticket_url="https://sesc/x")
    out = dedupe([tip, real])
    assert len(out) == 1
    m = out[0]
    assert m.venue == "Sesc Copacabana" and m.start_datetime.hour == 17 and set(m.sources) == {"gigs", "sesc_rj"}
    assert tip.id in m.member_ids


def test_llm_model_per_task(monkeypatch):
    monkeypatch.setenv("MUSICFINDER_MODEL_RANK", "claude-haiku-5-5")
    assert llm.model_for("rank") == "claude-haiku-5-5"
    assert llm.model_for("extract") == llm.MODEL
    assert llm.cost("claude-haiku-5-5", 1_000_000, 1_000_000) == 0.6
