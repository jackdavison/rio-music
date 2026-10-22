"""SQLite storage: events (the schema from SPEC.md), caches, run log, change log."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from .config import DATA_DIR
from .models import Event, TZ

DB_PATH = Path(os.environ.get("MUSICFINDER_DB", DATA_DIR / "events.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    city TEXT NOT NULL,
    title TEXT NOT NULL,
    artists TEXT NOT NULL DEFAULT '[]',      -- JSON array
    venue TEXT,
    neighborhood TEXT,
    start_datetime TEXT NOT NULL,            -- ISO 8601, America/Sao_Paulo
    end_datetime TEXT,
    price_min REAL,
    price_max REAL,
    is_free INTEGER NOT NULL DEFAULT 0,
    ticket_url TEXT,
    source TEXT,                             -- primary source
    source_url TEXT,
    image_url TEXT,
    first_seen TEXT,
    last_seen TEXT,
    score INTEGER,
    score_reason TEXT,
    sold_out INTEGER NOT NULL DEFAULT 0,
    -- extras (not in the spec's column list but useful downstream)
    sources TEXT NOT NULL DEFAULT '[]',
    genres TEXT NOT NULL DEFAULT '[]',
    description TEXT,
    content_hash TEXT,
    active INTEGER NOT NULL DEFAULT 1,       -- 0 once it disappears from all sources
    kind TEXT                                -- roda | show
);
CREATE INDEX IF NOT EXISTS events_city_start ON events(city, start_datetime);

CREATE TABLE IF NOT EXISTS cache (
    ns TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, created TEXT NOT NULL,
    PRIMARY KEY (ns, key)
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, started TEXT, city TEXT, collector TEXT,
    ok INTEGER, n_events INTEGER, error TEXT, seconds REAL
);

CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_at TEXT, event_id TEXT, kind TEXT, detail TEXT,
    alerted INTEGER NOT NULL DEFAULT 0
);
"""

_conn: sqlite3.Connection | None = None


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        cols = {r[1] for r in _conn.execute("PRAGMA table_info(events)")}
        if "kind" not in cols:  # added after the first dbs were committed
            _conn.execute("ALTER TABLE events ADD COLUMN kind TEXT")
    return _conn


def reset_connection():
    global _conn
    if _conn is not None:
        _conn.close()
    _conn = None


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


# ---------- cache ----------

def cache_get(ns: str, key: str):
    row = conn().execute("SELECT value FROM cache WHERE ns=? AND key=?", (ns, key)).fetchone()
    return json.loads(row["value"]) if row else None


def cache_put(ns: str, key: str, value):
    conn().execute("INSERT OR REPLACE INTO cache(ns,key,value,created) VALUES(?,?,?,?)",
                   (ns, key, json.dumps(value, ensure_ascii=False), now_iso()))
    conn().commit()


# ---------- events ----------

def _row_to_event(r: sqlite3.Row) -> Event:
    return Event(
        id=r["id"], city=r["city"], title=r["title"], artists=json.loads(r["artists"]),
        venue=r["venue"] or "", neighborhood=r["neighborhood"] or "",
        start_datetime=datetime.fromisoformat(r["start_datetime"]),
        end_datetime=datetime.fromisoformat(r["end_datetime"]) if r["end_datetime"] else None,
        price_min=r["price_min"], price_max=r["price_max"], is_free=bool(r["is_free"]),
        ticket_url=r["ticket_url"] or "", source=r["source"] or "", source_url=r["source_url"] or "",
        image_url=r["image_url"] or "", first_seen=r["first_seen"] or "", last_seen=r["last_seen"] or "",
        score=r["score"], score_reason=r["score_reason"] or "", sold_out=bool(r["sold_out"]),
        sources=json.loads(r["sources"]), genres=json.loads(r["genres"]),
        description=r["description"] or "", kind=r["kind"] or "",
    )


def load_events(city: str | None = None, active_only: bool = True,
                since: datetime | None = None) -> list[Event]:
    q = "SELECT * FROM events WHERE 1=1"
    args: list = []
    if city:
        q += " AND city=?"
        args.append(city)
    if active_only:
        q += " AND active=1"
    rows = conn().execute(q + " ORDER BY start_datetime", args).fetchall()
    evs = [_row_to_event(r) for r in rows]
    if since:
        evs = [e for e in evs if e.start_datetime >= since]
    return evs


def load_events_by_id(city: str) -> dict[str, Event]:
    return {e.id: e for e in load_events(city, active_only=False)}


def upsert(ev: Event):
    conn().execute("""
        INSERT INTO events (id, city, title, artists, venue, neighborhood, start_datetime,
            end_datetime, price_min, price_max, is_free, ticket_url, source, source_url, image_url,
            first_seen, last_seen, score, score_reason, sold_out, sources, genres, description,
            content_hash, active, kind)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)
        ON CONFLICT(id) DO UPDATE SET
            title=excluded.title, artists=excluded.artists, venue=excluded.venue,
            neighborhood=excluded.neighborhood, start_datetime=excluded.start_datetime,
            end_datetime=excluded.end_datetime, price_min=excluded.price_min,
            price_max=excluded.price_max, is_free=excluded.is_free, ticket_url=excluded.ticket_url,
            source=excluded.source, source_url=excluded.source_url, image_url=excluded.image_url,
            last_seen=excluded.last_seen, score=excluded.score, score_reason=excluded.score_reason,
            sold_out=excluded.sold_out, sources=excluded.sources, genres=excluded.genres,
            description=excluded.description, content_hash=excluded.content_hash, active=1,
            kind=excluded.kind
    """, (
        ev.id, ev.city, ev.title, json.dumps(ev.artists, ensure_ascii=False), ev.venue,
        ev.neighborhood, ev.start_datetime.isoformat(),
        ev.end_datetime.isoformat() if ev.end_datetime else None, ev.price_min, ev.price_max,
        int(ev.is_free), ev.ticket_url, ev.source, ev.source_url, ev.image_url, ev.first_seen,
        ev.last_seen, ev.score, ev.score_reason, int(ev.sold_out),
        json.dumps(ev.sources), json.dumps(ev.genres), ev.description[:2000], ev.content_hash(),
        ev.kind or None,
    ))


GONE_GRACE_HOURS = 7  # ~2 missed 3-hourly runs: paginated sources (Sympla) drop a few rows per pass


def deactivate_missing(city: str, seen_ids: set[str], ok_sources: set[str],
                       grace_hours: float | None = None) -> list[str]:
    """Mark future events inactive once every source that produced them ran OK without them
    for longer than `grace_hours`.

    Events whose sources failed this run are kept as-is, so a flaky collector never wipes data, and
    the grace period absorbs sources that randomly miss a few rows on a single pass.
    """
    now = datetime.now(TZ)
    cutoff = now - timedelta(hours=GONE_GRACE_HOURS if grace_hours is None else grace_hours)
    gone = []
    for r in conn().execute("SELECT id, sources, last_seen FROM events WHERE city=? AND active=1 AND start_datetime>=?",
                            (city, now.isoformat())).fetchall():
        if r["id"] in seen_ids or not set(json.loads(r["sources"])) <= ok_sources:
            continue
        if r["last_seen"] and datetime.fromisoformat(r["last_seen"]) > cutoff:
            continue
        gone.append(r["id"])
    conn().executemany("UPDATE events SET active=0 WHERE id=?", [(i,) for i in gone])
    return gone


def log_run(city: str, collector: str, ok: bool, n: int, error: str, seconds: float):
    conn().execute("INSERT INTO runs(started,city,collector,ok,n_events,error,seconds) VALUES(?,?,?,?,?,?,?)",
                   (now_iso(), city, collector, int(ok), n, error, seconds))


def log_change(event_id: str, kind: str, detail: str = ""):
    conn().execute("INSERT INTO changes(run_at,event_id,kind,detail) VALUES(?,?,?,?)",
                   (now_iso(), event_id, kind, detail))


def unalerted_changes() -> list[sqlite3.Row]:
    return conn().execute("SELECT * FROM changes WHERE alerted=0 ORDER BY id").fetchall()


def mark_alerted(ids: list[int]):
    conn().executemany("UPDATE changes SET alerted=1 WHERE id=?", [(i,) for i in ids])
    conn().commit()


def commit():
    conn().commit()
