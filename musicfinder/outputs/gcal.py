"""Optional direct Google Calendar sync (ENABLE_GCAL=1).

Upserts events above the city's ICS threshold into that city's calendar using a service account,
keyed by a stable event id, and deletes events we created that no longer appear.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta

from ..config import CityConfig
from ..models import Event, TZ
from .ics import _fmt_price

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar"]
TAG = "musicfinder"


def _service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def gcal_id(e: Event) -> str:
    # Google event ids: base32hex chars (a-v, 0-9), 5-1024 long. Our ids are hex -> valid.
    return f"mf{e.id}"


def body(e: Event) -> dict:
    url = e.ticket_url or e.source_url
    desc = [f"Score {e.score}/100 — {e.score_reason}"]
    if e.artists:
        desc.append("Artists: " + ", ".join(e.artists))
    if _fmt_price(e):
        desc.append("Price: " + _fmt_price(e))
    if url:
        desc.append(url)
    end = e.end_datetime or e.start_datetime + timedelta(hours=2, minutes=30)
    b = {
        "id": gcal_id(e),
        "summary": e.title + (" [ESGOTADO]" if e.sold_out else "") + f" ({e.score})",
        "location": ", ".join(x for x in [e.venue, e.neighborhood] if x),
        "description": "\n".join(desc),
        "start": {"dateTime": e.start_datetime.isoformat(), "timeZone": "America/Sao_Paulo"},
        "end": {"dateTime": end.isoformat(), "timeZone": "America/Sao_Paulo"},
        "extendedProperties": {"private": {TAG: "1"}},
        "transparency": "transparent",
    }
    if url:
        b["source"] = {"title": "Ingressos", "url": url}
    return b


def sync_city(city: CityConfig, events: list[Event], dry_run: bool = False) -> dict:
    cal_id = os.environ.get(city.gcal_calendar_env)
    if not cal_id:
        log.warning("gcal: %s not set, skipping %s", city.gcal_calendar_env, city.slug)
        return {}
    wanted = {gcal_id(e): e for e in events if (e.score or 0) >= city.ics_threshold
              and e.start_datetime >= datetime.now(TZ) - timedelta(hours=6)}
    if dry_run:
        log.info("gcal dry-run: would sync %d events to %s", len(wanted), city.slug)
        return {"would_sync": len(wanted)}
    svc = _service()
    existing: dict[str, dict] = {}
    page = None
    while True:
        res = svc.events().list(calendarId=cal_id, privateExtendedProperty=f"{TAG}=1",
                                timeMin=(datetime.now(TZ) - timedelta(days=1)).isoformat(),
                                showDeleted=True, maxResults=2500, pageToken=page).execute()
        for it in res.get("items", []):
            existing[it["id"]] = it
        page = res.get("nextPageToken")
        if not page:
            break
    stats = {"inserted": 0, "updated": 0, "deleted": 0}
    for gid, e in wanted.items():
        b = body(e)
        cur = existing.get(gid)
        if cur is None:
            try:
                svc.events().insert(calendarId=cal_id, body=b).execute()
                stats["inserted"] += 1
            except Exception as exc:
                if "409" in str(exc):  # id exists but outside our listing window: update instead
                    svc.events().update(calendarId=cal_id, eventId=gid, body=b).execute()
                    stats["updated"] += 1
                else:
                    raise
        else:
            same = (cur.get("status") != "cancelled" and cur.get("summary") == b["summary"]
                    and cur.get("location", "") == b["location"]
                    and cur.get("description", "") == b["description"]
                    and cur.get("start", {}).get("dateTime", "")[:16] == b["start"]["dateTime"][:16])
            if not same:
                b["status"] = "confirmed"
                svc.events().update(calendarId=cal_id, eventId=gid, body=b).execute()
                stats["updated"] += 1
    now_iso = datetime.now(TZ).isoformat()
    for gid, cur in existing.items():
        start = cur.get("start", {}).get("dateTime", "")
        past = bool(start) and datetime.fromisoformat(start) < datetime.fromisoformat(now_iso)
        if gid not in wanted and cur.get("status") != "cancelled" and not past:
            svc.events().delete(calendarId=cal_id, eventId=gid).execute()
            stats["deleted"] += 1
    log.info("gcal %s: %s", city.slug, stats)
    return stats
