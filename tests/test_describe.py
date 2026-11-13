from datetime import datetime, timedelta

from musicfinder import describe, llm
from musicfinder.models import Event, TZ
from musicfinder.outputs import web


def ev(title, venue="Bip Bip", neighborhood=""):
    start = datetime.now(TZ).replace(minute=0, second=0, microsecond=0) + timedelta(days=1)
    return Event(city="rio", title=title, start_datetime=start, venue=venue, neighborhood=neighborhood,
                 source="sympla")


def test_describe_caches_and_feeds_web(monkeypatch):
    calls = []

    def fake(**kw):
        calls.append(kw)
        keys = [ln.split("]")[0][1:] for ln in kw["prompt"].splitlines()]
        return {"events": [{"key": k, "format": "roda", "listing": "music", "zone": "orla",
                            "neighborhood": "Copacabana", "blurb_en": "Samba roda",
                            "blurb_pt": "Roda de samba"} for k in keys]}

    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "json_call", fake)
    a, b = ev("Roda do Bip"), ev("Roda do Bip")   # same content (a weekly roda): one item
    describe.describe([a, b, ev("Pagode")], "Rio de Janeiro")
    assert len(calls) == 1 and len(calls[0]["prompt"].splitlines()) == 2
    assert "orla" in calls[0]["schema"]["properties"]["events"]["items"]["properties"]["zone"]["enum"]
    describe.describe([ev("Roda do Bip")])
    assert len(calls) == 1

    row = web.payload({"rio": [a]}, {"rio": "Rio"})["events"][0]
    assert (row["fmt"], row["zone"], row["neighborhood"], row["blurb_pt"]) == ("roda", "orla", "Copacabana", "Roda de samba")
    assert "listing" not in row   # "music" is the default and left out


def test_web_flags_parties_and_keeps_listed_neighborhood(monkeypatch):
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "json_call", lambda **kw: {"events": [{
        "key": "e0", "format": "show", "listing": "tourist", "zone": "centro", "neighborhood": "Lapa",
        "blurb_en": "x", "blurb_pt": "y"}]})
    e = ev("Lapa Pub Crawl", venue="Rua do Lavradio", neighborhood="Centro")
    describe.describe([e])
    row = web.payload({"rio": [e]}, {"rio": "Rio"})["events"][0]
    assert row["listing"] == "tourist" and row["neighborhood"] == "Centro"


def test_undescribed_events_still_publish():
    row = web.payload({"rio": [ev("Show")]}, {"rio": "Rio"})["events"][0]
    assert "fmt" not in row and "zone" not in row


def test_jack_page_points_up_a_level():
    html = ('<link rel="stylesheet" href="style.css?v=1"><script src="app.js?v=2" defer></script>'
            '<script src="https://cdn.x/y.js"></script><script data-goatcounter="https://a/count" '
            'src="//gc.zgo.at/count.js"></script></head><body>')
    out = web.jack_page(html)
    assert 'href="../style.css?v=1"' in out and 'src="../app.js?v=2"' in out
    assert 'src="https://cdn.x/y.js"' in out and 'src="//gc.zgo.at/count.js"' in out
    assert '<body data-mode="jack" data-root="../">' in out

