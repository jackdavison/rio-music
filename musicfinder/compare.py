"""compare-models: run the LLM steps with several models on the same live inputs and report where
they disagree, so each task can use the cheapest model that does the job.

Covers venue-page extraction (live pages), Instagram flyers (posts cached by earlier Instagram
runs) and taste ranking (a sample of stored events). The
first model is the reference; the report lists every disagreement so a human can judge which
side is right (the reference is not assumed to be correct).
"""
from __future__ import annotations

import json
import logging
import random
from datetime import datetime
from pathlib import Path

from rapidfuzz import fuzz

from . import db, llm, rank
from .collectors import venues as V
from .config import CityConfig, load_taste
from .models import Event, TZ, norm

log = logging.getLogger(__name__)


def _short(model: str) -> str:
    return model.replace("claude-", "")


def _match(a: Event, pool: list[Event]) -> Event | None:
    best, best_s = None, 0
    for b in pool:
        if b.start_datetime.date() != a.start_datetime.date():
            continue
        s = fuzz.token_set_ratio(norm(a.title), norm(b.title))
        if s > best_s:
            best, best_s = b, s
    return best if best_s >= 80 else None


def _desc(e: Event) -> str:
    price = "free" if e.is_free else (f"R${e.price_min:g}" if e.price_min is not None else "?")
    return f"{e.start_datetime:%a %d/%m %H:%M} · {e.title} · {price}"


def _extract(cities: list[CityConfig], models: list[str]) -> list[str]:
    out = ["## Venue page extraction", ""]
    totals = {m: [0, 0, 0] for m in models[1:]}  # matched, missed, extra
    for city in cities:
        cfg = city.collector("venues") or {}
        for vcfg in cfg.get("venues") or []:
            if (vcfg.get("mode") or "auto").lower() in V.API_MODES or not vcfg.get("llm", True):
                continue
            for url in V._pages(vcfg):
                try:
                    html = V._fetch_html(url)
                except Exception as exc:
                    out.append(f"### {vcfg['name']}: fetch failed ({exc})\n")
                    continue
                if V.extract_jsonld(html):
                    continue  # structured data, no LLM involved
                text = V.strip_html(html, url)
                found = {m: V.llm_extract(text, city, vcfg, url, cfg=cfg, model=m) for m in models}
                ref = found[models[0]]
                out.append(f"### {vcfg['name']} ({city.slug}) — {url}")
                out.append(", ".join(f"{_short(m)}: {len(found[m])} events" for m in models))
                for m in models[1:]:
                    other = found[m]
                    missed = [e for e in ref if not _match(e, other)]
                    extra = [e for e in other if not _match(e, ref)]
                    diffs = []
                    for e in ref:
                        o = _match(e, other)
                        if not o:
                            continue
                        bits = []
                        if o.start_datetime.time() != e.start_datetime.time():
                            bits.append(f"time {e.start_datetime:%H:%M} vs {o.start_datetime:%H:%M}")
                        if o.price_min != e.price_min or o.is_free != e.is_free:
                            bits.append(f"price {e.price_min} vs {o.price_min}")
                        if bits:
                            diffs.append(f"{e.title}: " + "; ".join(bits))
                    t = totals[m]
                    t[0] += len(ref) - len(missed)
                    t[1] += len(missed)
                    t[2] += len(extra)
                    if not (missed or extra or diffs) and len(other) == len(ref):
                        out.append(f"- **{_short(m)}**: identical to {_short(models[0])}")
                        continue
                    out.append(f"- **{_short(m)}**: {len(missed)} missed, {len(extra)} extra, "
                               f"{len(diffs)} field differences"
                               + (f", {len(other) - len(ref)} duplicates" if not (missed or extra)
                                  and len(other) > len(ref) else ""))
                    out += [f"  - missed: {_desc(e)}" for e in missed[:10]]
                    out += [f"  - extra: {_desc(e)}" for e in extra[:10]]
                    out += [f"  - differs: {d}" for d in diffs[:10]]
                out.append("")
    out.append("**Totals vs " + _short(models[0]) + ":** " + "; ".join(
        f"{_short(m)} matched {t[0]}, missed {t[1]}, extra {t[2]}" for m, t in totals.items()))
    out.append("")
    return out


def _spearman(a: list[float], b: list[float]) -> float:
    def ranks(xs):
        order = sorted(range(len(xs)), key=lambda i: xs[i])
        r = [0.0] * len(xs)
        for pos, i in enumerate(order):
            r[i] = pos
        return r
    ra, rb = ranks(a), ranks(b)
    n = len(a)
    if n < 2:
        return float("nan")
    ma, mb = sum(ra) / n, sum(rb) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va = sum((x - ma) ** 2 for x in ra) ** .5
    vb = sum((y - mb) ** 2 for y in rb) ** .5
    return cov / (va * vb) if va and vb else float("nan")


def _rank(cities: list[CityConfig], models: list[str], sample: int) -> list[str]:
    out = ["## Taste ranking", ""]
    now = datetime.now(TZ)
    evs = [e for c in cities for e in db.load_events(c.slug, since=now)]
    if not evs:
        return out + ["No stored events: run `collect` first.", ""]
    random.Random(0).shuffle(evs)
    evs = evs[:sample]
    system = rank.SYSTEM.format(taste=load_taste())
    scores: dict[str, dict[int, tuple[int, str]]] = {m: {} for m in models}
    for m in models:
        for i in range(0, len(evs), rank.BATCH):
            chunk = list(enumerate(evs))[i:i + rank.BATCH]
            res = llm.json_call(system=system, prompt="\n".join(rank._line(f"e{j}", e) for j, e in chunk),
                                schema=rank.SCHEMA, max_tokens=8000, cache=False, task="rank", model=m)
            for s in (res or {}).get("scores", []):
                if s["key"].startswith("e") and s["key"][1:].isdigit():
                    scores[m][int(s["key"][1:])] = (int(s["score"]), s["reason"])
    ref = scores[models[0]]
    out.append(f"{len(evs)} events scored by each model.")
    top = lambda sc: {i for i, _ in sorted(sc.items(), key=lambda kv: -kv[1][0])[:15]}
    for m in models[1:]:
        common = sorted(set(ref) & set(scores[m]))
        a = [ref[i][0] for i in common]
        b = [scores[m][i][0] for i in common]
        mad = sum(abs(x - y) for x, y in zip(a, b)) / len(common) if common else float("nan")
        out.append(f"- **{_short(m)}**: rank correlation {_spearman(a, b):.2f}, mean score gap "
                   f"{mad:.1f}, top-15 overlap {len(top(ref) & top(scores[m]))}/15")
    out += ["", "Biggest disagreements:", ""]
    spread = sorted(ref, key=lambda i: -(max(scores[m].get(i, ref[i])[0] for m in models)
                                         - min(scores[m].get(i, ref[i])[0] for m in models)))
    for i in spread[:12]:
        out.append(f"- {_desc(evs[i])} @ {evs[i].venue}")
        for m in models:
            if i in scores[m]:
                out.append(f"  - {_short(m)} {scores[m][i][0]}: {scores[m][i][1]}")
    out.append("")
    return out


def _vision(models: list[str], sample: int = 30) -> list[str]:
    from .collectors import instagram as ig
    out = ["## Instagram flyers", ""]
    rows = db.conn().execute("SELECT value FROM cache WHERE ns='instagram_post' ORDER BY created DESC "
                             "LIMIT ?", (sample,)).fetchall()
    posts = [json.loads(r["value"]) for r in rows]
    if not posts:
        return out + ["No cached posts: set APIFY_TOKEN and ENABLE_INSTAGRAM=1 and run "
                      "`collect --only instagram` first.", ""]
    agree = {m: 0 for m in models[1:]}
    for p in posts:
        today = datetime.now(TZ).date().isoformat()
        # past dates (recap posts) are dropped by the collector anyway, so don't count them
        res = {m: [i for i in ig.extract(p, model=m, use_cache=False) or [] if (i.get("date") or "") >= today]
               for m in models}
        key = lambda it: (it.get("date"), it.get("time"), norm(it.get("city") or ""))
        ref = sorted(key(i) for i in res[models[0]])
        diffs = [m for m in models[1:] if sorted(key(i) for i in res[m]) != ref]
        for m in models[1:]:
            agree[m] += m not in diffs
        if diffs:
            out.append(f"### @{p.get('ownerUsername')} {p.get('url', '')}")
            out.append("> " + (p.get("caption") or "")[:200].replace("\n", " "))
            for m in models:
                evs = res[m]
                out.append(f"- **{_short(m)}**: " + ("; ".join(
                    f"{i.get('date')} {i.get('time') or '??:??'} {i.get('title')} @ {i.get('venue')}"
                    f" ({i.get('city') or '?'})" for i in evs) or "no event"))
            out.append("")
    out.append(f"{len(posts)} posts. Same dates/times/cities as {_short(models[0])}: " + ", ".join(
        f"{_short(m)} {n}/{len(posts)}" for m, n in agree.items()))
    out.append("")
    return out


def run(cities: list[CityConfig], models: list[str], rank_sample: int = 80,
        out: str = "data/model-comparison.md"):
    if not llm.available():
        raise SystemExit("ANTHROPIC_API_KEY is not set")
    llm.USAGE.clear()
    lines = [f"# Model comparison {datetime.now(TZ):%Y-%m-%d %H:%M}", "",
             f"Reference: {models[0]}. Others: {', '.join(models[1:])}.", ""]
    lines += _extract(cities, models)
    lines += _vision(models)
    lines += _rank(cities, models, rank_sample)
    lines += ["## Cost of this comparison run", "", "```", llm.usage_summary(), "```", ""]
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines))
    print("\n".join(lines))
    print(f"\nwritten to {path}")
