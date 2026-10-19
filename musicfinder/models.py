"""Shared event schema. Every collector yields `Event` objects."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field, asdict
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Sao_Paulo")


def norm(text: str | None) -> str:
    """Lowercase, strip accents and punctuation, collapse whitespace."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9 ]+", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


_VENUE_NOISE = re.compile(r"\b(sesc|teatro|casa|clube|club|espaco|bar|rio|rj|sp|de|do|da|dos|das|e|the)\b")


def venue_key(venue: str | None) -> str:
    """Aggressive normalisation used only for matching venues across sources."""
    v = _VENUE_NOISE.sub(" ", norm(venue))
    return re.sub(r"\s+", " ", v).strip() or norm(venue)


def to_local(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


@dataclass
class Event:
    city: str
    title: str
    start_datetime: datetime
    source: str
    venue: str = ""
    artists: list[str] = field(default_factory=list)
    neighborhood: str = ""
    end_datetime: datetime | None = None
    price_min: float | None = None
    price_max: float | None = None
    is_free: bool = False
    ticket_url: str = ""
    source_url: str = ""
    image_url: str = ""
    sold_out: bool = False
    description: str = ""          # not persisted as a column of its own; feeds ranking/genres
    genres: list[str] = field(default_factory=list)
    # set by the pipeline
    id: str = ""
    first_seen: str = ""
    last_seen: str = ""
    score: int | None = None
    score_reason: str = ""
    sources: list[str] = field(default_factory=list)

    def __post_init__(self):
        self.start_datetime = to_local(self.start_datetime)
        self.end_datetime = to_local(self.end_datetime)
        self.title = re.sub(r"\s+", " ", (self.title or "")).strip()
        self.venue = re.sub(r"\s+", " ", (self.venue or "")).strip()
        self.artists = [a.strip() for a in self.artists if a and a.strip()]
        if self.price_max is None and self.price_min is not None:
            self.price_max = self.price_min
        if self.price_min == 0 and (self.price_max or 0) == 0:
            self.is_free = True
        if not self.id:
            self.id = self.make_id()
        if not self.sources:
            self.sources = [self.source]

    def make_id(self) -> str:
        """Stable across runs: city + venue + local date + time + normalised title."""
        key = "|".join([
            self.city,
            venue_key(self.venue),
            self.start_datetime.strftime("%Y-%m-%dT%H:%M"),
            norm(self.title)[:80],
        ])
        return hashlib.sha1(key.encode()).hexdigest()[:16]

    @property
    def date(self) -> str:
        return self.start_datetime.date().isoformat()

    def content_hash(self) -> str:
        """Hash of the fields that affect ranking; used as the score-cache key."""
        key = "|".join([norm(self.title), norm(" ".join(self.artists)), venue_key(self.venue),
                        norm(self.description)[:400]])
        return hashlib.sha1(key.encode()).hexdigest()

    def to_dict(self) -> dict:
        d = asdict(self)
        d["start_datetime"] = self.start_datetime.isoformat()
        d["end_datetime"] = self.end_datetime.isoformat() if self.end_datetime else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        d = dict(d)
        d["start_datetime"] = datetime.fromisoformat(d["start_datetime"])
        if d.get("end_datetime"):
            d["end_datetime"] = datetime.fromisoformat(d["end_datetime"])
        known = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in d.items() if k in known})
