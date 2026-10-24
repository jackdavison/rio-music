"""Write site/events.json for the static web calendar."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from ..models import Event, TZ, norm, venue_key
from ..watchlist import matches

NEW_DAYS = 3
DESC_CHARS = 280


def _desc(e: Event) -> str:
    d = " ".join((e.description or "").split())
    if d.startswith("Música ·"):
        return ""  # Sympla's category/organizer line: nothing the card doesn't already show
    return d if len(d) <= DESC_CHARS else d[:DESC_CHARS].rsplit(" ", 1)[0] + "…"


def payload(by_city: dict[str, list[Event]], names: dict[str, str]) -> dict:
    now = datetime.now(TZ)
    new_cutoff = now - timedelta(days=NEW_DAYS)
    out = {"generated": now.isoformat(timespec="seconds"),
           "cities": [{"slug": s, "name": names.get(s, s)} for s in by_city], "events": []}
    for city, evs in by_city.items():
        for e in evs:
            if e.start_datetime < now - timedelta(hours=6):
                continue
            first = datetime.fromisoformat(e.first_seen) if e.first_seen else now
            row = {
                "id": e.id, "city": city, "title": e.title, "artists": e.artists, "venue": e.venue,
                "neighborhood": e.neighborhood, "start": e.start_datetime.isoformat(),
                "end": e.end_datetime.isoformat() if e.end_datetime else None,
                "price_min": e.price_min, "price_max": e.price_max, "is_free": e.is_free,
                "ticket_url": e.ticket_url or e.source_url, "source_url": e.source_url,
                "sources": e.sources, "image_url": e.image_url, "score": e.score,
                "score_reason": e.score_reason, "sold_out": e.sold_out, "genres": e.genres,
                "is_new": first >= new_cutoff, "watch": bool(matches(e)), "desc": _desc(e),
                "tbc": bool(getattr(e, "time_unknown", False)), "kind": e.kind,
            }
            if row["source_url"] == row["ticket_url"]:
                row["source_url"] = ""
            # drop empty/false fields: keeps events.json small on phones
            out["events"].append({k: v for k, v in row.items() if not (v is None or v is False or v == "" or v == [])})
    out["events"].sort(key=lambda x: x["start"])
    out["events"] = group_sessions(out["events"])
    return out


def group_sessions(rows: list[dict]) -> list[dict]:
    """Fold several sessions of one show (same title, venue and day: Candlelight at 17h, 19h and
    21h; Blue Note's two sets) into the first row, listing every start time in `times`.
    Rows must be sorted by start."""
    first: dict[tuple, dict] = {}
    out = []
    for r in rows:
        k = (r["city"], r["start"][:10], norm(r["title"]), venue_key(r.get("venue", "")))
        g = first.get(k)
        if g is None:
            first[k] = r
            out.append(r)
            continue
        g.setdefault("times", [g["start"][11:16]]).append(r["start"][11:16])
        g.setdefault("session_ids", []).append(r["id"])
        g["score"] = max(g.get("score") or 0, r.get("score") or 0) or g.get("score")
        for f in ("watch", "is_new"):
            if r.get(f):
                g[f] = True
        if g.get("sold_out") and not r.get("sold_out"):
            del g["sold_out"]  # sold out only if every session is
    return out


def bust_cache(site_dir: Path) -> None:
    """Stamp index.html's app.js/style.css links with a content hash, so a phone that cached the
    old files (Pages sends max-age=600) picks up UI changes on the next load."""
    index = site_dir / "index.html"
    if not index.exists():
        return
    html = old = index.read_text()
    for name in ("app.js", "style.css"):
        f = site_dir / name
        if f.exists():
            h = hashlib.sha1(f.read_bytes()).hexdigest()[:8]
            html = re.sub(rf'{re.escape(name)}(\?v=\w+)?"', f'{name}?v={h}"', html)
    if html != old:
        index.write_text(html)


def write(site_dir: Path, by_city: dict[str, list[Event]], names: dict[str, str]) -> Path:
    site_dir.mkdir(parents=True, exist_ok=True)
    bust_cache(site_dir)
    p = site_dir / "events.json"
    p.write_text(json.dumps(payload(by_city, names), ensure_ascii=False, separators=(",", ":")))
    return p
