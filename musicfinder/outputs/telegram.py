"""Telegram: 8am digest (Rio section, then São Paulo) and instant watchlist alerts."""
from __future__ import annotations

import html
import logging
import os
from datetime import datetime, timedelta

from .. import db, http
from ..config import CityConfig
from ..models import Event, TZ, norm, venue_key
from ..watchlist import matches

log = logging.getLogger(__name__)

MAX_LEN = 4000  # Telegram limit is 4096
WEEKDAY_PT = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]


def _price(e: Event) -> str:
    if e.is_free:
        return "grátis"
    if e.price_min is None:
        return ""
    return f"R${e.price_min:.0f}" + (f"–{e.price_max:.0f}" if e.price_max and e.price_max != e.price_min else "")


def line(e: Event, with_date: bool) -> str:
    when = (f"{WEEKDAY_PT[e.start_datetime.weekday()]} {e.start_datetime:%d/%m} " if with_date else "") \
        + e.start_datetime.strftime("%H:%M")
    where = e.venue + (f" / {e.neighborhood}" if e.neighborhood and e.neighborhood not in e.venue else "")
    bits = [f"<b>{e.score}</b>", when, html.escape(e.title)]
    tail = " · ".join(x for x in [html.escape(where), _price(e)] if x)
    url = e.ticket_url or e.source_url
    s = " ".join(bits) + (f" — {tail}" if tail else "")
    if e.sold_out:
        s += " <i>(esgotado)</i>"
    if url:
        s += f' <a href="{html.escape(url, quote=True)}">↗</a>'
    return s


def _section(title: str, evs: list[Event], with_date: bool, n: int) -> list[str]:
    """Top-n shows by score, listed by date. Repeat dates of one show collapse into '+N datas'."""
    if not evs:
        return []
    groups: dict[tuple, list[Event]] = {}
    for e in sorted(evs, key=lambda e: e.start_datetime):
        groups.setdefault((norm(e.title), venue_key(e.venue)), []).append(e)
    best = sorted(groups.values(), key=lambda g: (-max(x.score or 0 for x in g), g[0].start_datetime))[:n]
    lines = []
    for g in sorted(best, key=lambda g: g[0].start_datetime):
        s = line(g[0], with_date)
        if len(g) > 1:
            s += f" <i>(+{len(g) - 1} {'data' if len(g) == 2 else 'datas'})</i>"
        lines.append(s)
    return [f"\n<u>{title}</u>"] + lines


def build_digest(cities: list[CityConfig], by_city: dict[str, list[Event]], now: datetime | None = None) -> str:
    now = now or datetime.now(TZ)
    today = now.date()
    out = [f"🎶 <b>Música ao vivo</b> — {WEEKDAY_PT[today.weekday()]} {today:%d/%m}"]
    for c in cities:
        evs = [e for e in by_city.get(c.slug, []) if e.start_datetime >= now - timedelta(hours=2)]
        d = c.digest
        out.append(f"\n<b>{'📍 ' + c.name}</b>")
        if d.get("style", "full") == "full":  # today / tomorrow / notable
            min_today = int(d.get("today_min_score", 50))
            tdy = sorted([e for e in evs if e.start_datetime.date() == today and (e.score or 0) >= min_today],
                         key=lambda e: e.start_datetime)
            tmw = sorted([e for e in evs if e.start_datetime.date() == today + timedelta(days=1)
                          and (e.score or 0) >= min_today], key=lambda e: e.start_datetime)
            later = [e for e in evs if today + timedelta(days=2) <= e.start_datetime.date()
                     <= today + timedelta(days=int(d.get("notable_days", 60)))
                     and (e.score or 0) >= int(d.get("notable_min_score", 75))]
            out += _section("Hoje", tdy, False, 12) or ["\n<u>Hoje</u>\n(nada acima do corte)"]
            out += _section("Amanhã", tmw, False, 10)
            out += _section(f"Destaques (próx. {d.get('notable_days', 60)} dias)", later, True,
                            int(d.get("max_notable", 15)))
        else:
            top_days = int(d.get("top_days", 14))
            soon = [e for e in evs if e.start_datetime.date() <= today + timedelta(days=top_days)
                    and (e.score or 0) >= int(d.get("top_min_score", 70))]
            big = [e for e in evs if today + timedelta(days=top_days) < e.start_datetime.date()
                   <= today + timedelta(days=int(d.get("big_days", 90)))
                   and (e.score or 0) >= int(d.get("big_min_score", 85))]
            out += _section(f"Top (próx. {top_days} dias)", soon, True, int(d.get("max_top", 12)))
            out += _section("Grandes shows mais adiante", big, True, int(d.get("max_big", 6)))
            if not soon and not big:
                out.append("(nada acima do corte)")
    pages = os.environ.get("PAGES_URL")
    if pages:
        out.append(f'\n<a href="{pages}">Calendário completo</a>')
    return "\n".join(out)


def build_alerts(by_id: dict[str, Event]) -> tuple[list[str], list[int]]:
    """Alerts for unalerted changes on watchlist events. Returns (messages, change ids handled)."""
    msgs, handled = [], []
    for ch in db.unalerted_changes():
        handled.append(ch["id"])
        e = by_id.get(ch["event_id"])
        if e is None or e.start_datetime < datetime.now(TZ):
            continue
        hits = matches(e)
        if not hits:
            continue
        label = {"new": "🆕 Novo", "nearly_sold_out": "⏳ Quase esgotado", "sold_out": "❌ Esgotou",
                 "date_changed": "📅 Mudou a data", "venue_changed": "📍 Mudou o local"}.get(ch["kind"])
        if not label:
            continue
        city = {"rio": "Rio", "sp": "SP"}.get(e.city, e.city)
        detail = f" ({html.escape(ch['detail'])})" if ch["detail"] else ""
        msgs.append(f"{label} · {city} · watchlist: {html.escape(', '.join(hits))}{detail}\n{line(e, True)}")
    return msgs, handled


def split(text: str) -> list[str]:
    chunks, cur = [], ""
    for ln in text.split("\n"):
        if len(cur) + len(ln) + 1 > MAX_LEN:
            chunks.append(cur)
            cur = ""
        cur += ln + "\n"
    if cur.strip():
        chunks.append(cur)
    return chunks


def configured() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def send(text: str, dry_run: bool = False) -> None:
    if dry_run:
        print(text)
        print("-" * 40)
        return
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set (use --dry-run to print)")
    for chunk in split(text):
        http.post_json(f"https://api.telegram.org/bot{token}/sendMessage", {
            "chat_id": chat, "text": chunk, "parse_mode": "HTML", "disable_web_page_preview": True})
