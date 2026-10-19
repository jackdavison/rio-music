"""Collector plumbing: registry + failure isolation."""
from __future__ import annotations

import logging
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Iterable

from ..config import CityConfig
from ..models import Event, TZ

log = logging.getLogger(__name__)

# name -> function(city_cfg, collector_cfg) -> Iterable[Event]
REGISTRY: dict[str, Callable[[CityConfig, dict], Iterable[Event]]] = {}

HORIZON_DAYS = 90


def collector(name: str):
    def deco(fn):
        REGISTRY[name] = fn
        return fn
    return deco


def horizon() -> tuple[datetime, datetime]:
    now = datetime.now(TZ)
    return now - timedelta(hours=6), now + timedelta(days=HORIZON_DAYS)


def in_horizon(ev: Event) -> bool:
    lo, hi = horizon()
    return lo <= ev.start_datetime <= hi


@dataclass
class CollectorResult:
    name: str
    city: str
    events: list[Event]
    ok: bool
    error: str = ""
    seconds: float = 0.0


def run_collector(name: str, city: CityConfig, cfg: dict) -> CollectorResult:
    fn = REGISTRY[name]
    t0 = time.time()
    try:
        events = [e for e in fn(city, cfg) if in_horizon(e)]
        for e in events:
            e.city = city.slug
        dt = time.time() - t0
        log.info("[%s/%s] %d events in %.1fs", city.slug, name, len(events), dt)
        return CollectorResult(name, city.slug, events, True, seconds=dt)
    except Exception as exc:  # one failing collector must never break the run
        dt = time.time() - t0
        log.error("[%s/%s] FAILED after %.1fs: %s\n%s", city.slug, name, dt, exc,
                  traceback.format_exc(limit=3))
        return CollectorResult(name, city.slug, [], False, error=f"{type(exc).__name__}: {exc}",
                               seconds=dt)


def run_city(city: CityConfig, only: list[str] | None = None,
             skip: list[str] | None = None) -> list[CollectorResult]:
    from . import load_all  # noqa: F401  (populate registry)
    load_all()
    results = []
    for name in city.collectors:
        cfg = city.collector(name)
        if cfg is None or (only and name not in only) or (skip and name in skip):
            continue
        if name not in REGISTRY:
            log.warning("[%s] unknown collector %r in config", city.slug, name)
            continue
        results.append(run_collector(name, city, cfg))
    return results
