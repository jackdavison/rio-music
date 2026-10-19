"""Parsing helpers shared by collectors (pt-BR dates, BRL prices, music filtering)."""
from __future__ import annotations

import re
from datetime import datetime

from ..models import TZ, norm

MONTHS_PT = {
    "jan": 1, "janeiro": 1, "fev": 2, "fevereiro": 2, "mar": 3, "marco": 3, "abr": 4, "abril": 4,
    "mai": 5, "maio": 5, "jun": 6, "junho": 6, "jul": 7, "julho": 7, "ago": 8, "agosto": 8,
    "set": 9, "setembro": 9, "out": 10, "outubro": 10, "nov": 11, "novembro": 11,
    "dez": 12, "dezembro": 12,
}


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=TZ)


def parse_epoch(v) -> datetime | None:
    if v in (None, ""):
        return None
    v = float(v)
    if v > 1e11:  # milliseconds
        v /= 1000
    return datetime.fromtimestamp(v, TZ)


def parse_price(text) -> float | None:
    """'R$ 1.234,50' -> 1234.5; numbers pass through."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    m = re.search(r"(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?)", str(text))
    if not m:
        return None
    num = m.group(1)
    if "," in num:
        num = num.replace(".", "").replace(",", ".")
    try:
        return float(num)
    except ValueError:
        return None


def prices_in(text: str) -> list[float]:
    out = []
    for m in re.finditer(r"R\$\s*(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?|\d+)", text or ""):
        p = parse_price(m.group(1))
        if p is not None:
            out.append(p)
    return out


MUSIC_WORDS = (
    "show", "musica", "musical", "samba", "choro", "roda", "jazz", "mpb", "forro", "concerto",
    "orquestra", "banda", "cantor", "cantora", "instrumental", "gafieira", "baile", "rock",
    "funk", "pagode", "sertanejo", "dj", "festival", "recital", "piano", "violao", "sax", "flauta",
    "frevo", "maracatu", "blues", "soul", "rap", "hip hop", "reggae", "bossa", "sinfonica",
    "quarteto", "trio", "quinteto", "big band", "opera", "coral", "turne", "tour", "ao vivo",
    "acustico", "lancamento do disco", "album", "eletronica", "techno", "house", "tributo",
)

NOT_MUSIC_WORDS = (
    "stand up", "standup", "comedia", "palestra", "curso", "workshop", "oficina", "peca",
    "infantil", "teatro infantil", "danca contemporanea", "exposicao", "visita", "yoga",
    "corrida", "congresso", "seminario", "mentoria", "imersao", "treinamento", "networking",
)


def looks_like_music(*texts: str) -> bool:
    t = " " + norm(" ".join(x for x in texts if x)) + " "
    if any(f" {w} " in t for w in NOT_MUSIC_WORDS) and not any(
            f" {w} " in t for w in ("show", "samba", "choro", "jazz", "concerto", "musica")):
        return False
    return any(f" {w} " in t for w in MUSIC_WORDS)


def clean_text(html_or_text: str | None, limit: int = 1500) -> str:
    if not html_or_text:
        return ""
    t = re.sub(r"<[^>]+>", " ", html_or_text)
    t = re.sub(r"&nbsp;", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:limit]
