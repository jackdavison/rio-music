"""Write site/events.json for the static web calendar."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from ..models import Event, TZ
from ..watchlist import matches

NEW_DAYS = 3


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
                "is_new": first >= new_cutoff, "watch": bool(matches(e)),
            }
            if row["source_url"] == row["ticket_url"]:
                row["source_url"] = ""
            # drop empty/false fields: keeps events.json small on phones
            out["events"].append({k: v for k, v in row.items() if not (v is None or v is False or v == "" or v == [])})
    out["events"].sort(key=lambda x: x["start"])
    return out


def write(site_dir: Path, by_city: dict[str, list[Event]], names: dict[str, str]) -> Path:
    site_dir.mkdir(parents=True, exist_ok=True)
    p = site_dir / "events.json"
    p.write_text(json.dumps(payload(by_city, names), ensure_ascii=False, separators=(",", ":")))
    return p
