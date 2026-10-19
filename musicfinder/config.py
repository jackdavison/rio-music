"""Config loading. One YAML per city in config/<city>.yaml; adding a city = adding a file."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
SITE_DIR = ROOT / "site"


def flag(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class CityConfig:
    slug: str
    name: str
    raw: dict = field(default_factory=dict)

    def collector(self, name: str) -> dict | None:
        """Return the collector's config block, or None if it is disabled/absent."""
        c = (self.raw.get("collectors") or {}).get(name)
        if c is None or c is False:
            return None
        if c is True:
            c = {}
        if c.get("enabled", True) is False:
            return None
        return c

    @property
    def collectors(self) -> dict:
        return self.raw.get("collectors") or {}

    @property
    def digest(self) -> dict:
        return self.raw.get("digest") or {}

    @property
    def ics_threshold(self) -> int:
        return int(self.raw.get("ics_min_score", 60))

    @property
    def gcal_calendar_env(self) -> str:
        return self.raw.get("gcal_calendar_id_env", f"GCAL_ID_{self.slug.upper()}")


def city_slugs() -> list[str]:
    return sorted(p.stem for p in CONFIG_DIR.glob("*.yaml")
                  if not p.stem.startswith(("recurring_", "gigs_", "_")))


def load_city(slug: str) -> CityConfig:
    raw = yaml.safe_load((CONFIG_DIR / f"{slug}.yaml").read_text()) or {}
    return CityConfig(slug=slug, name=raw.get("name", slug), raw=raw)


def load_cities(only: list[str] | None = None) -> list[CityConfig]:
    return [load_city(s) for s in city_slugs() if not only or s in only]


def _lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def load_watchlist() -> list[str]:
    return _lines(CONFIG_DIR / "watchlist.txt")


def load_instagram_accounts(slug: str) -> list[str]:
    return [a.lstrip("@") for a in _lines(CONFIG_DIR / f"instagram_{slug}.txt")]


def load_recurring(slug: str) -> list[dict]:
    p = CONFIG_DIR / f"recurring_{slug}.yaml"
    if not p.exists():
        return []
    data = yaml.safe_load(p.read_text()) or {}
    return data.get("events") or []


def load_gigs(slug: str) -> list[dict]:
    p = CONFIG_DIR / f"gigs_{slug}.yaml"
    if not p.exists():
        return []
    data = yaml.safe_load(p.read_text()) or {}
    return data.get("gigs") or []


def load_taste() -> str:
    p = ROOT / "taste.md"
    return p.read_text() if p.exists() else ""
