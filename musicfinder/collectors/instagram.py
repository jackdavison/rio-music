"""Instagram flyers via Apify + Claude vision. Behind the ENABLE_INSTAGRAM flag.

Accounts:
  config/instagram_<city>.txt   venues/collectives in that city
  config/instagram_artists.txt  musicians; read for every city, each gig routed to the city
                                printed on the flyer (a Rio musician's SP date lands in SP)

Cost control:
- Each account is fetched only for posts newer than its last successful fetch (minus a small
  overlap), so a post is paid for on Apify about once. Accounts shared by several cities are
  fetched once per process.
- Each post is read by Claude once (cache keyed by post id), including posts that turned out not
  to be events. Raw posts are cached too, so `compare-models` can replay them.
"""
from __future__ import annotations

import copy
import logging
import os
from datetime import datetime, timedelta, timezone

from .. import db, http, llm
from ..config import CONFIG_DIR, _lines, flag, load_instagram_accounts
from ..models import Event, TZ, norm
from .base import collector, in_horizon

log = logging.getLogger(__name__)

APIFY_ACTOR = os.environ.get("APIFY_INSTAGRAM_ACTOR", "apify~instagram-post-scraper")
APIFY_URL = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"
MAX_IMAGES = 4          # carousel slides sent to Claude (2nd/3rd slides often hold the dates)
OVERLAP = timedelta(hours=2)   # Apify bills per post returned, so keep re-fetches small

CITY_ALIASES = {
    "rio": ["rio de janeiro", "rio", "rj", "niteroi"],
    "sp": ["sao paulo", "sp", "sampa"],
}

SYSTEM = (
    "You read Instagram posts from Brazilian music venues and musicians and extract upcoming live "
    "music performances announced in the flyer images and/or caption. Today is {today}.\n"
    "- Dates on flyers often omit the year: pick the next occurrence on or after the post date. "
    "A tour flyer listing several dates/cities is several entries.\n"
    "- Ignore posts that are not announcements of a specific dated live performance (recaps, "
    "merch, memes, album releases without a show, generic promos): return an empty list.\n"
    "- city: the city the show is in, as printed or clearly implied by the venue; '' if unknown.\n"
    "- Times are local, 24h HH:MM, the show start (not doors), '' if absent. Prices in BRL.\n"
    "- venue: as printed on the flyer; for a venue's own account with no venue printed, use the "
    "account's name. title: the show name; artists: performer names only.\n"
    "- link: a ticket URL from the caption (sympla, eventim, linktree...) or ''."
)

SCHEMA = copy.deepcopy(llm.EVENT_LIST_SCHEMA)
_item = SCHEMA["properties"]["events"]["items"]
_item["properties"]["city"] = {"type": "string"}
_item["required"].append("city")


def _state_key(account: str) -> str:
    return account.lower()


def fetch_posts(accounts: list[str], per_account: int, since_days: int) -> list[dict]:
    """Posts for `accounts`, each account only since its last fetch. One Apify run per distinct
    window (normally a single run: all accounts share the same last-run time)."""
    token = os.environ["APIFY_TOKEN"]
    now = datetime.now(TZ)
    floor = now - timedelta(days=since_days)
    windows: dict[str, list[str]] = {}
    for a in accounts:
        last = db.cache_get("instagram_fetched", _state_key(a))
        since = max(floor, datetime.fromisoformat(last) - OVERLAP) if last else floor
        # hour precision (UTC ISO, which the actor accepts) so accounts fetched together share a run
        key = since.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        windows.setdefault(key.strftime("%Y-%m-%dT%H:%M:%SZ"), []).append(a)
    posts: list[dict] = []
    for since, group in windows.items():
        payload = {"username": group, "resultsLimit": per_account,
                   "onlyPostsNewerThan": since, "skipPinnedPosts": True}
        r = http.session().post(APIFY_URL.format(actor=APIFY_ACTOR), params={"token": token},
                                json=payload, timeout=600)
        r.raise_for_status()
        got = r.json()
        posts += got
        for a in group:
            db.cache_put("instagram_fetched", _state_key(a), now.isoformat(timespec="seconds"))
        log.info("apify: %d posts from %d accounts since %s", len(got), len(group), since)
    return posts


_fetched: dict[str, list[dict]] = {}   # account -> posts, shared across cities in one process


def posts_for(accounts: list[str], per_account: int, since_days: int) -> list[dict]:
    todo = [a for a in accounts if a.lower() not in _fetched]
    if todo:
        for a in todo:
            _fetched[a.lower()] = []
        for p in fetch_posts(todo, per_account, since_days):
            # credit the account we asked for: collab posts carry the partner as ownerUsername
            _fetched.setdefault(_source_account(p), []).append(p)
            if p.get("id") or p.get("shortCode"):
                db.cache_put("instagram_post", _post_id(p), p)
    return [p for a in accounts for p in _fetched.get(a.lower(), [])]


def _source_account(p: dict) -> str:
    src = p.get("inputUrl") or p.get("ownerUsername") or ""
    return str(src).lower().rstrip("/").split("/")[-1].split("?")[0]


def _post_id(p: dict) -> str:
    return str(p.get("id") or p.get("shortCode") or p.get("url"))


def image_urls(p: dict) -> list[str]:
    urls = [p.get("displayUrl")]
    urls += [c.get("displayUrl") for c in p.get("childPosts") or [] if isinstance(c, dict)]
    urls += list(p.get("images") or [])
    seen, out = set(), []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out[:MAX_IMAGES]


def _image(url: str) -> tuple[bytes, str] | None:
    try:
        r = http.get(url, timeout=30)
    except Exception as exc:
        log.warning("instagram image fetch failed %s: %s", url, exc)
        return None
    ctype = r.headers.get("content-type", "image/jpeg").split(";")[0]
    if ctype not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
        ctype = "image/jpeg"
    return r.content, ctype


def extract(post: dict, model: str | None = None, use_cache: bool = True) -> list[dict] | None:
    """Events announced in one post (list of SCHEMA items), or None on API failure."""
    pid = _post_id(post)
    if use_cache:
        cached = db.cache_get("instagram", pid)
        if cached is not None:
            return cached
    owner = post.get("ownerFullName") or post.get("ownerUsername") or ""
    imgs = [i for i in (_image(u) for u in image_urls(post)) if i]
    prompt = (f"Account: @{post.get('ownerUsername', '')} ({owner})\n"
              f"Posted: {post.get('timestamp', '')}\nPost URL: {post.get('url', '')}\n"
              f"Location tag: {post.get('locationName') or ''}\n"
              f"{len(imgs)} image(s) attached.\n\n"
              f"Caption:\n{(post.get('caption') or '')[:3000]}")
    out = llm.json_call(
        system=SYSTEM.format(today=datetime.now(TZ).date().isoformat()),
        prompt=prompt, schema=SCHEMA, images=imgs or None,
        max_tokens=4000, cache=False, task="vision", model=model,
    )
    if out is None:  # API failure: don't cache, retry next run
        return None
    events = out.get("events", [])
    if use_cache:
        db.cache_put("instagram", pid, events)
    return events


def city_matches(printed: str, slug: str, name: str) -> bool:
    """True if the flyer's city is this city, or unknown."""
    p = norm(printed)
    if not p:
        return True
    return p in {norm(a) for a in CITY_ALIASES.get(slug, []) + [name, slug]}


def to_events(city: str, post: dict, items: list[dict], city_name: str = "",
              default_city: bool = True) -> list[Event]:
    out = []
    for it in items:
        printed = it.get("city") or ""
        if printed and not city_matches(printed, city, city_name):
            continue
        if not printed and not default_city:
            continue  # artist account, no city on the flyer: only the home city keeps it
        try:
            d = datetime.fromisoformat(it["date"]).date()
        except (KeyError, ValueError):
            continue
        if it.get("time") in ("23:59", "00:00"):
            it["time"] = ""  # what the model writes for "no time on the flyer" despite the prompt
        hh, mm = (it.get("time") or "20:00").split(":")[:2] if it.get("time") else ("20", "00")
        start = datetime(d.year, d.month, d.day, int(hh), int(mm), tzinfo=TZ)
        ev = Event(
            city=city, title=it.get("title") or ", ".join(it.get("artists") or []),
            artists=it.get("artists") or [], venue=it.get("venue") or "",
            start_datetime=start, price_min=it.get("price_min"), price_max=it.get("price_max"),
            is_free=bool(it.get("is_free")), sold_out=bool(it.get("sold_out")),
            ticket_url=it.get("link") or post.get("url", ""), source="instagram",
            source_url=post.get("url", ""), image_url=post.get("displayUrl", ""),
            description=(it.get("description") or (post.get("caption") or "")[:500]),
        )
        if not it.get("time"):
            ev.time_unknown = True
        if in_horizon(ev):
            out.append(ev)
    return out


def _artist_accounts() -> list[tuple[str, str]]:
    """(handle, home city slug) from instagram_artists.txt; lines may end with `rio`/`sp`."""
    out = []
    for line in _lines(CONFIG_DIR / "instagram_artists.txt"):
        parts = line.split()
        out.append((parts[0].lstrip("@"), parts[1].lower() if len(parts) > 1 else "rio"))
    return out


@collector("instagram")
def collect(city, cfg):
    if not flag("ENABLE_INSTAGRAM"):
        log.info("[%s/instagram] disabled (set ENABLE_INSTAGRAM=1)", city.slug)
        return []
    if not os.environ.get("APIFY_TOKEN"):
        raise RuntimeError("APIFY_TOKEN not set")
    if not llm.available():
        raise RuntimeError("ANTHROPIC_API_KEY not set (needed for flyer extraction)")
    local = load_instagram_accounts(city.slug)
    artists = _artist_accounts()
    home = {a.lower(): c for a, c in artists}
    accounts = list(dict.fromkeys(local + [a for a, _ in artists]))
    if not accounts:
        return []
    per, since = int(cfg.get("posts_per_account", 6)), int(cfg.get("since_days", 10))
    cutoff = datetime.now(TZ) - timedelta(days=since)
    out = []
    for p in posts_for(accounts, per, since):
        if p.get("error"):
            log.warning("apify: %s", p.get("error"))
            continue
        ts = p.get("timestamp")
        if ts and datetime.fromisoformat(ts.replace("Z", "+00:00")) < cutoff:
            continue
        owner = _source_account(p)
        # venue accounts listed for this city keep city-less flyers; artist accounts only in
        # their home city
        default_city = owner not in home or home[owner] == city.slug or owner in {a.lower() for a in local}
        try:
            items = extract(p)
            out.extend(to_events(city.slug, p, items or [], city.name, default_city))
        except Exception as exc:
            log.warning("instagram post %s failed: %s", _post_id(p), exc)
    return out
