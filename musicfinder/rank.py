"""Score events 0-100 against taste.md with Claude. Batched; cached by event content + taste hash."""
from __future__ import annotations

import hashlib
import logging

from . import db, llm
from .config import load_taste
from .genres import heuristic_score
from .models import Event

log = logging.getLogger(__name__)

BATCH = 40

SCHEMA = {
    "type": "object",
    "properties": {"scores": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "key": {"type": "string"},
            "score": {"type": "integer", "description": "0-100"},  # API rejects min/max here
            "reason": {"type": "string", "description": "one line, <= 120 chars, in English"},
        },
        "required": ["key", "score", "reason"], "additionalProperties": False}}},
    "required": ["scores"], "additionalProperties": False,
}

SYSTEM = """You rank live music events in Brazil for one listener. Their taste profile:

{taste}

Score each event 0-100 for how much this listener would want to go:
90-100 a must-see squarely in the "love" list (e.g. a serious roda de samba, a great choro group,
a strong Brazilian instrumental/jazz act); 70-89 strong match; 50-69 plausible / "interested" list;
30-49 weak; 0-29 deprioritized genres or clearly not for them. Use what you know about the artists
and venues (e.g. Casa do Choro, Trapiche Gamboa, Sesc programming) when the listing is terse.
Unknown acts with little information land around 40-55. Give a concrete one-line reason
(the genre/artist signal you used). Return one entry per input key."""


def _taste_hash(taste: str) -> str:
    return hashlib.sha1(taste.encode()).hexdigest()[:10]


def _line(key: str, e: Event) -> str:
    bits = [f"[{key}] {e.title}"]
    if e.artists:
        bits.append("artists: " + ", ".join(e.artists[:6]))
    bits.append(f"venue: {e.venue}" + (f" ({e.neighborhood})" if e.neighborhood else ""))
    bits.append(e.start_datetime.strftime("%a %d/%m %H:%M"))
    if e.genres:
        bits.append("tags: " + ",".join(e.genres))
    if e.description:
        bits.append("desc: " + e.description[:280].replace("\n", " "))
    return " | ".join(bits)


def rank(events: list[Event]) -> None:
    taste = load_taste()
    th = _taste_hash(taste)
    todo: list[tuple[str, Event]] = []
    for e in events:
        key = f"{th}:{e.content_hash()}"
        hit = db.cache_get("score", key)
        if hit:
            e.score, e.score_reason = hit["score"], hit["reason"]
        else:
            todo.append((key, e))

    if todo and not llm.available():
        log.warning("ANTHROPIC_API_KEY not set: using keyword scores for %d events", len(todo))
    if todo:
        log.info("ranking %d events (%d cached)", len(todo), len(events) - len(todo))

    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        out = None
        if llm.available():
            short = {f"e{j}": (k, e) for j, (k, e) in enumerate(chunk)}
            out = llm.json_call(system=SYSTEM.format(taste=taste),
                                prompt="\n".join(_line(sk, e) for sk, (_, e) in short.items()),
                                schema=SCHEMA, max_tokens=8000, cache=False, task="rank")
            got = {}
            for s in (out or {}).get("scores", []):
                if s["key"] in short:
                    got[s["key"]] = s
            for sk, (key, e) in short.items():
                s = got.get(sk)
                if s:
                    e.score, e.score_reason = max(0, min(100, int(s["score"]))), s["reason"].strip()
                    db.cache_put("score", key, {"score": e.score, "reason": e.score_reason})
        for key, e in chunk:
            if e.score is None:  # no LLM, or it skipped this one: keyword fallback, not cached
                e.score, e.score_reason = heuristic_score(e)
