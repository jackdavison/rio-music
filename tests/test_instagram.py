from datetime import datetime, timedelta

from musicfinder import db, llm
from musicfinder.collectors import instagram as ig
from musicfinder.models import TZ


def _post(owner="carlosmalta", pid="1", days_ago=0, **kw):
    ts = (datetime.now(TZ) - timedelta(days=days_ago)).astimezone().isoformat()
    return {"id": pid, "ownerUsername": owner, "timestamp": ts, "url": f"https://instagram.com/p/{pid}",
            "displayUrl": "https://img/1.jpg", "caption": "show!", **kw}


def _item(date, city="", time="21:00", title="Carlos Malta"):
    return {"title": title, "artists": [title], "date": date, "time": time, "venue": "Blue Note",
            "price_min": None, "price_max": None, "is_free": False, "sold_out": False, "link": "",
            "image_url": "", "description": "", "city": city}


def test_carousel_images_deduped_and_capped():
    p = _post(childPosts=[{"displayUrl": "https://img/1.jpg"}, {"displayUrl": "https://img/2.jpg"}],
              images=["https://img/3.jpg", "https://img/4.jpg", "https://img/5.jpg"])
    assert ig.image_urls(p) == ["https://img/1.jpg", "https://img/2.jpg", "https://img/3.jpg",
                                "https://img/4.jpg"]


def test_city_routing():
    d = (datetime.now(TZ) + timedelta(days=5)).date().isoformat()
    items = [_item(d, "São Paulo"), _item(d, "Rio de Janeiro", title="Rio date"), _item(d, "", title="no city")]
    rio = ig.to_events("rio", _post(), items, "Rio de Janeiro")
    sp = ig.to_events("sp", _post(), items, "São Paulo", default_city=False)
    assert [e.title for e in rio] == ["Rio date", "no city"]
    assert [e.title for e in sp] == ["Carlos Malta"]


def test_incremental_fetch_and_shared_accounts(monkeypatch):
    calls = []

    class R:
        def __init__(self, data): self.data = data
        def raise_for_status(self): pass
        def json(self): return self.data

    class S:
        def post(self, url, params, json, timeout):
            calls.append(json)
            return R([_post(owner=u, pid=u) for u in json["username"]])

    monkeypatch.setenv("APIFY_TOKEN", "t")
    monkeypatch.setattr(ig.http, "session", lambda: S())
    monkeypatch.setattr(ig, "_fetched", {})
    ig.posts_for(["a", "b"], 6, 10)
    ig.posts_for(["b", "c"], 6, 10)          # b already fetched this process
    assert [c["username"] for c in calls] == [["a", "b"], ["c"]]
    assert calls[0]["onlyPostsNewerThan"] == (datetime.now(TZ) - timedelta(days=10)).date().isoformat()
    monkeypatch.setattr(ig, "_fetched", {})   # next run: window starts at the last fetch
    ig.posts_for(["a"], 6, 10)
    assert calls[-1]["onlyPostsNewerThan"] >= (datetime.now(TZ) - timedelta(days=1)).date().isoformat()
    assert db.cache_get("instagram_post", "a")["ownerUsername"] == "a"


def test_extract_caches_and_retries_on_failure(monkeypatch):
    monkeypatch.setattr(ig, "_image", lambda u: (b"x", "image/jpeg"))
    monkeypatch.setattr(llm, "available", lambda: True)
    res = [None, {"events": []}]
    seen = []
    monkeypatch.setattr(llm, "json_call", lambda **kw: seen.append(kw) or res.pop(0))
    p = _post()
    assert ig.extract(p) is None                  # failure: not cached
    assert ig.extract(p) == [] and ig.extract(p) == []
    assert len(seen) == 2 and seen[0]["task"] == "vision" and len(seen[0]["images"]) == 1


def test_collab_post_credited_to_requested_account(monkeypatch):
    class R:
        def raise_for_status(self): pass
        def json(self): return [_post(owner="teatrorivalpetrobras", pid="c1",
                                      inputUrl="https://www.instagram.com/guinga_oficial")]

    class S:
        def post(self, *a, **k): return R()

    monkeypatch.setenv("APIFY_TOKEN", "t")
    monkeypatch.setattr(ig.http, "session", lambda: S())
    monkeypatch.setattr(ig, "_fetched", {})
    assert [p["id"] for p in ig.posts_for(["guinga_oficial"], 6, 10)] == ["c1"]
