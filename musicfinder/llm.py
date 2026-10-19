"""Thin Claude wrapper: JSON-schema structured output, optional images, result cache.

Every call is optional: if ANTHROPIC_API_KEY is missing, `available()` is False and callers
fall back to non-LLM behaviour.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from collections import defaultdict

log = logging.getLogger(__name__)

MODEL = os.environ.get("MUSICFINDER_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("MUSICFINDER_EFFORT", "low")
# Per-task overrides: MUSICFINDER_MODEL_EXTRACT (venue pages), _VISION (Instagram flyers),
# _RANK (taste scores), _DEDUPE (same-show tiebreak). Unset -> MUSICFINDER_MODEL.
TASKS = ("extract", "vision", "rank", "dedupe")
# USD per million tokens (input, output), platform.claude.com pricing as of 2026-10
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0),
          "claude-haiku-5-5": (0.10, 0.50)}
# (task, model) -> {"calls", "input", "output"}; read by the CLI to log spend per run
USAGE: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: {"calls": 0, "input": 0, "output": 0})
# Models that accept the server-side refusal fallback (`fallbacks: "default"`)
FALLBACK_MODELS = ("claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5")

_client = None


def available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def model_for(task: str) -> str:
    return os.environ.get(f"MUSICFINDER_MODEL_{task.upper()}") or MODEL


def cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    if model not in PRICES:
        return None
    pin, pout = PRICES[model]
    return (input_tokens * pin + output_tokens * pout) / 1e6


def usage_summary() -> str:
    lines = []
    for (task, model), u in sorted(USAGE.items()):
        c = cost(model, u["input"], u["output"])
        lines.append(f"{task:8} {model:20} {u['calls']:4} calls {u['input']:>9} in {u['output']:>8} out"
                     + (f"  ${c:.3f}" if c is not None else ""))
    return "\n".join(lines)


def client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic()
    return _client


def _cache_get(key: str):
    from . import db
    return db.cache_get("llm", key)


def _cache_put(key: str, value):
    from . import db
    db.cache_put("llm", key, value)


def json_call(*, system: str, prompt: str, schema: dict, images: list[tuple[bytes, str]] | None = None,
              max_tokens: int = 16000, cache: bool = True, cache_tag: str = "",
              task: str = "extract", model: str | None = None) -> dict | list | None:
    """Ask Claude for JSON matching `schema`. Returns parsed JSON, or None on failure/refusal."""
    if not available():
        return None
    model = model or model_for(task)
    img_digest = "".join(hashlib.sha1(b).hexdigest() for b, _ in (images or []))
    key = hashlib.sha256(json.dumps([model, cache_tag, system, prompt, schema, img_digest],
                                    sort_keys=True).encode()).hexdigest()
    if cache:
        hit = _cache_get(key)
        if hit is not None:
            return hit

    content: list[dict] = []
    for data, media_type in images or []:
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": media_type, "data": base64.b64encode(data).decode()}})
    content.append({"type": "text", "text": prompt})

    import anthropic
    try:
        fallback = {}
        if model in FALLBACK_MODELS:
            fallback = {"betas": ["server-side-fallback-2026-07-01"], "extra_body": {"fallbacks": "default"}}
        resp = client().beta.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": EFFORT,
                           "format": {"type": "json_schema", "schema": schema}},
            **fallback,
        )
    except anthropic.BadRequestError as exc:
        log.error("Claude rejected request: %s", exc)
        return None
    except anthropic.RateLimitError as exc:
        log.warning("Claude rate-limited: %s", exc)
        return None
    except anthropic.APIStatusError as exc:
        log.error("Claude API error %s: %s", exc.status_code, exc)
        return None
    except anthropic.APIConnectionError as exc:
        log.error("Claude connection error: %s", exc)
        return None

    u = USAGE[(task, model)]
    u["calls"] += 1
    usage = getattr(resp, "usage", None)
    u["input"] += getattr(usage, "input_tokens", 0) or 0
    u["output"] += getattr(usage, "output_tokens", 0) or 0
    if resp.stop_reason == "refusal":
        log.warning("Claude refused (%s)", getattr(resp, "stop_details", None))
        return None
    if resp.stop_reason == "max_tokens":
        log.warning("Claude output truncated at max_tokens")
        return None
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        out = json.loads(text)
    except json.JSONDecodeError:
        log.error("Claude returned non-JSON: %.200s", text)
        return None
    if cache:
        _cache_put(key, out)
    return out


EVENT_LIST_SCHEMA = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "artists": {"type": "array", "items": {"type": "string"}},
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                    "time": {"type": "string", "description": "HH:MM 24h, empty if unknown"},
                    "venue": {"type": "string"},
                    "price_min": {"type": ["number", "null"]},
                    "price_max": {"type": ["number", "null"]},
                    "is_free": {"type": "boolean"},
                    "sold_out": {"type": "boolean"},
                    "link": {"type": "string"},
                    "image_url": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["title", "artists", "date", "time", "venue", "price_min", "price_max",
                             "is_free", "sold_out", "link", "image_url", "description"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["events"],
    "additionalProperties": False,
}
