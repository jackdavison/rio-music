# Build plan

## Layout
```
musicfinder/
  cli.py            python -m musicfinder {collect,digest,alerts,publish,run} [--city] [--dry-run]
  config.py         loads config/<city>.yaml (+ recurring, instagram, watchlist), env flags
  models.py         Event dataclass = the shared schema, stable id hashing
  db.py             SQLite (data/events.db): events, llm_cache, instagram_cache, runs, changes
  http.py           shared requests session: UA, retries, timeouts
  llm.py            Anthropic wrapper: JSON-mode helper, disk/sqlite cache, no-key fallback
  collectors/
    base.py         Collector ABC + registry; run_all() isolates each collector's failure
    sympla.py  eventim.py  shotgun.py  bilheteria_digital.py  queremos.py  fever.py
    sesc_rj.py  sesc_sp.py
    venues.py       generic "fetch HTML -> strip -> Claude extracts events" for venue seeds
    instagram.py    Apify -> Claude vision, cached per post id (flag)
    recurring.py    expand recurring_<city>.yaml into next 8 weeks
  genres.py         keyword genre tagging (for web filters + rank fallback)
  dedupe.py         city+venue+date + rapidfuzz artist/title; LLM tiebreak for ambiguous
  rank.py           batched Claude scoring vs taste.md, cached by content hash; keyword fallback
  changes.py        new / newly sold out / date-or-venue changed
  outputs/
    telegram.py     8am digest + instant watchlist alerts
    ics.py          rio.ics, sp.ics, watchlist.ics with stable UIDs
    gcal.py         optional service-account upsert/delete sync (flag)
    web.py          writes site/events.json
site/               static index.html + app.js + style.css (FullCalendar CDN, list default on mobile)
config/             rio.yaml sp.yaml recurring_*.yaml watchlist.txt instagram_*.txt
taste.md
.github/workflows/  collect.yml (3h + pages deploy), digest.yml (11:00 UTC = 08:00 BRT), instagram.yml (2x daily)
tests/              unit tests for dedupe, recurring, ics, ranking cache, parsers (fixtures from live data)
```

## Order
1. Core: models, db, http, config, collector base, CLI skeleton.
2. Probe every source live (network inspection), write collectors, run each against live data, fix.
3. Venue seeds: verify each exists/active, record URL in config; LLM extraction.
4. Processing: dedupe, rank, changes.
5. Outputs: ICS, events.json + site, Telegram, gcal.
6. Workflows, README (setup, secrets, add a city, feeds, cost, known gaps, venue changes).
7. End-to-end dry run.

## Notes
- No ANTHROPIC_API_KEY in this environment: LLM steps degrade gracefully (keyword ranking, venue
  extraction skipped with a warning) so the pipeline still runs; LLM code paths are unit-tested with a
  mocked client.
- Stable event id = sha1(city|venue_norm|date|title_norm) so ICS UIDs and gcal ids survive re-runs.
