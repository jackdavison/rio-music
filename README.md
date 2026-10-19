# Brazil Live Music Finder

Finds live music in **Rio de Janeiro** (primary) and **São Paulo** (secondary), ranks it against
[`taste.md`](taste.md) with Claude, and delivers it as:

- a **Telegram digest** every morning at 08:00 (Rio section, then São Paulo) plus **instant alerts**
  for watchlist artists/venues,
- **ICS calendar feeds** (`rio.ics`, `sp.ics`, `watchlist.ics`) served from GitHub Pages,
- optional **direct Google Calendar sync** (near-instant updates),
- a **mobile-first web calendar** on GitHub Pages (city toggle, list/week/month, filters, "tonight").

Everything runs on GitHub Actions; state lives in `data/events.db` (SQLite, committed by the bot).

```
collectors (per city) ──► dedupe ──► stable ids ──► rank (Claude, cached) ──► change log ──► SQLite
                                                                                    │
                    site/events.json + index.html ◄── publish ──► rio.ics / sp.ics / watchlist.ics
                                                        │
                                       Telegram digest / alerts, Google Calendar (optional)
```

## Quick start (local)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export ANTHROPIC_API_KEY=...          # optional locally; without it ranking uses keyword scores
.venv/bin/python -m musicfinder collect --dry-run     # collect everything, print alerts
.venv/bin/python -m musicfinder digest --dry-run      # print the digest instead of sending
.venv/bin/python -m musicfinder status                # last run of every collector
cd site && python3 -m http.server                     # open http://localhost:8000
.venv/bin/python -m pytest -q                         # tests (offline, use saved fixtures)
```

Useful flags: `--city rio`, `--only sympla,sesc_sp`, `--skip venues`, `--no-publish`, `-v`.

## Sources

Each source is a collector module in `musicfinder/collectors/` registered with `@collector("name")`
and enabled per city in `config/<city>.yaml`. A collector that throws is logged (see
`python -m musicfinder status`) and the run continues; events from a failed source are kept from the
previous run rather than deleted.

| Collector | Rio | SP | How it works | Notes |
|---|---|---|---|---|
| `sympla` | ~990 | ~1,080 | Public search JSON the site uses (`POST /api/v1/search`, theme 99 Música + concerts filed under 104 Arte/Cultura), filtered by city, online events excluded | No prices or artists in the API; many party/DJ nights (ranking pushes them down) |
| `eventim` | ~36 | ~150 | `public-api.eventim.com` exploration API (the one embedded in the city pages), categories Shows & Música / Festivais | Needs Chrome-like headers (Akamai); no prices in the API |
| `bilheteria_digital` | ~25 | ~2 | `POST /eventos/paginacao` per state + event pages for times, style and prices | Style ids → music mapping inferred; most SP-state listings are outside the capital |
| `shotgun` | ~185 | ~310 | City page + each event's `MusicEvent` JSON-LD (performers, lots, sold out) | Behind a Vercel bot checkpoint; falls back to `curl_cffi` browser impersonation |
| `queremos` | ~2 | ~1 | Wix homepage/agenda cards + Sympla Bileto API for times/prices | Thin, but active. Ticketmaster-ticketed shows get a 21:00 placeholder time |
| `fever` | ~70 | ~150 | Server-rendered app state on category pages (music, concertos, Candlelight, jazz, opera, festivals) + plan pages for sessions | Filtered to metro city codes (drops Campinas, Santos, Petrópolis…) |
| `sesc_rj` | ~15 | — | cultura.sescrio.org.br listing + detail pages (JSON embedded in HTML), all Rio-city units | Main sescrio.org.br API returns 403; Sesc RJ publishes fewer shows and a shorter horizon |
| `sesc_sp` | — | ~95 | **sescsp.org.br has a JSON API** (`/wp-json/wp/v1/atividades/filter?linguagem=musica` + `/detail`), ~20 São Paulo city units, one event per session, credencial/meia/inteira prices | Publishes ~6 weeks ahead |
| `venues` | ~25+ | ~95+ | see below | 8 venues need `ANTHROPIC_API_KEY` |
| `recurring` | — | — | `config/recurring_<city>.yaml` | placeholders until you fill it in |
| `instagram` | — | — | Apify + Claude vision | optional, own schedule |

Counts are from a live run on 2026-10-16 (90-day window, before dedupe). Several of the APIs above
are undocumented internals of the sites (as the spec asked); if a site changes, that collector fails
loudly in the logs while everything else keeps working.

### Venue calendars

`venues` reads each venue's agenda page. It uses structured data when the page has it (JSON-LD
`Event`, WordPress event APIs) and otherwise strips the HTML to text and asks Claude to extract the
events (cached on a hash of the page text, so an unchanged page costs nothing).

Verified 2026-10-16:

| City | Venue | Status | Source used |
|---|---|---|---|
| Rio | Circo Voador | active | its own JSON API (the page is an empty JS app) |
| Rio | Teatro Rival → **Teatro Rival Petrobras** | active (renamed) | page → Claude |
| Rio | Blue Note Rio | site up, no shows listed right now (tickets via Eventim) | page → Claude |
| Rio | Vivo Rio | active | page → Claude |
| Rio | Casa do Pandeiro | active | public Google Sheet behind its page |
| Rio | Casa do Choro | site lists courses only | **disabled**; its shows are on Sympla (collected) + Instagram |
| Rio | Escola Portátil de Música | no calendar (it's a school; domain belongs to someone else) | **dropped** → Instagram |
| Rio | Renascença Clube | active, agenda only as flyer images | **disabled** → Instagram |
| Rio | Trapiche Gamboa | **website taken over by a casino spam site** | **dropped** → Instagram |
| Rio | *Sala Cecília Meireles* | active | **added**; page → Claude |
| SP | Casa de Francisca | active | page → Claude |
| SP | Blue Note SP | active | its WordPress REST API (two sets → two events) |
| SP | Jazz nos Fundos → **JazzB** | active (same team; old domain redirects) | page → Claude |
| SP | Bourbon Street | active | page → Claude (its iCal export is stale) |
| SP | Teatro Sérgio Cardoso | active | Amigos da Arte calendar feed, music only |
| SP | Ó do Borogodó | **no website** | **dropped** → Instagram |
| SP | Traço de União | **domain now a Lisbon care home; venue reportedly moved** | **dropped** → Instagram |
| SP | Auditório Ibirapuera | **domain lost (spam site)**; tickets via Eventim/Sympla (collected) | **dropped** → Instagram |
| SP | *Casa Natura Musical* | active | **added** (replaces Auditório Ibirapuera); page → Claude |

Each venue entry in `config/<city>.yaml` takes `name`, `url`, `neighborhood`, optional
`extra_urls`, `mode` (`auto` | `wp_rest` | `fullcalendar` | `gviz` | `circovoador`) and
`enabled: false`. If a structured mode breaks, the venue falls back to Claude extraction.

### Instagram (optional)

Set `ENABLE_INSTAGRAM=1` + `APIFY_TOKEN`. The `instagram` workflow runs twice a day, pulls recent
posts from `config/instagram_<city>.txt` with Apify's Instagram Post Scraper, and sends each flyer
image + caption to Claude vision, which returns structured events. Every post id is cached, so each
post is processed once (including ones that turn out not to be events).

### Recurring rodas

`config/recurring_<city>.yaml` is hand-maintained; each entry is expanded into dated events for the
next 8 weeks (weekly, or monthly on given weeks such as "1st and last Saturday"). The shipped
entries are placeholders (`placeholder: true` — they are skipped until you fill them in).

## Processing

1. **Dedupe** — events in the same city on the same day at matching venues are compared with fuzzy
   artist/title matching (rapidfuzz). Clear matches merge; ambiguous pairs go to Claude in one batch.
   Merging keeps the best ticket link (official ticketing > aggregators > venue page > Instagram).
2. **Stable ids** — an event keeps its id across runs even if a better source appears or the show
   moves to another date, so ICS UIDs and Google Calendar ids never duplicate.
3. **Rank** — events go to Claude in batches of 40 with `taste.md`; each gets a 0–100 score and a
   one-line reason. Scores are cached by event content + `taste.md` hash, so only new or changed
   events cost tokens. Without an API key a keyword heuristic is used (reasons start with `[keyword]`).
4. **Changes** — each run logs `new`, `sold_out`, `nearly_sold_out`, `date_changed`, `venue_changed`.
   Alerts are sent for watchlist matches.

## Setup on GitHub

1. Push this repo to GitHub (private is fine; Pages on private repos needs a paid plan — otherwise
   make it public, the data is public listings anyway).
2. **Settings → Pages → Source: GitHub Actions.**
3. **Settings → Secrets and variables → Actions**, add secrets:

   | Secret | Needed for |
   |---|---|
   | `ANTHROPIC_API_KEY` | ranking, venue extraction, dedupe tiebreak, Instagram flyers |
   | `TELEGRAM_BOT_TOKEN` | digest + alerts (create a bot with [@BotFather](https://t.me/BotFather)) |
   | `TELEGRAM_CHAT_ID` | your chat id (message the bot, then open `https://api.telegram.org/bot<TOKEN>/getUpdates`) |
   | `APIFY_TOKEN` | Instagram (optional) |
   | `GOOGLE_SERVICE_ACCOUNT_JSON` | Google Calendar sync (optional) |
   | `GCAL_ID_RIO`, `GCAL_ID_SP` | Google Calendar sync (optional) |

   and **variables** (not secrets): optional `MUSICFINDER_MODEL` (default `claude-opus-5-5`), `PAGES_URL` (e.g. `https://<you>.github.io/<repo>/`, linked
   from the digest) and `ENABLE_GCAL` = `1` if you set up Google Calendar.
4. Run **Actions → collect → Run workflow** once. It collects, commits `data/events.db`, and
   deploys the site.

Schedules (all UTC in the workflow files; Brazil has no DST):

| Workflow | When | What |
|---|---|---|
| `collect` | every 3 h | all collectors except Instagram → publish → alerts → Pages deploy |
| `digest` | 08:00 America/Sao_Paulo | Telegram digest |
| `instagram` | 08:40 and 19:40 | Instagram collector only → publish → Pages deploy |
| `test` | on push | pytest |

All three have a manual **Run workflow** button (`workflow_dispatch`) with a dry-run option.

## Subscribing to the calendars

Feeds are at `https://<you>.github.io/<repo>/rio.ics`, `.../sp.ics`, `.../watchlist.ics`. Each
feed only includes events at or above `ics_min_score` in the city's config.

**Google Calendar:** on a computer, open Google Calendar → the **+** next to "Other calendars" →
**From URL** → paste the feed URL → **Add calendar**. It then appears on your phone too.
**Note: Google refreshes subscribed calendars slowly — typically every 8–24 hours, and you cannot
force it.** If you want updates within minutes, use the direct sync below instead.

**Apple Calendar:** File → New Calendar Subscription (or on iOS: Settings → Calendar → Accounts →
Add Subscribed Calendar) and paste the URL. Apple honours the feed's 3-hour refresh hint.

## Optional: direct Google Calendar sync

Pushes events straight into two calendars ("Música — Rio", "Música — SP") through the Calendar API,
upserting by stable event id and deleting events that disappear from the sources. Updates show up
within minutes of each 3-hourly run.

1. Go to <https://console.cloud.google.com/>, create a project (e.g. `musicfinder`).
2. **APIs & Services → Library → Google Calendar API → Enable.**
3. **IAM & Admin → Service Accounts → Create service account** (no roles needed). Open it →
   **Keys → Add key → Create new key → JSON**. A `.json` file downloads.
4. In Google Calendar (your normal account) create two calendars: **Música — Rio** and
   **Música — SP** (Settings → Add calendar → Create new calendar).
5. For each: **Settings and sharing → Share with specific people → Add** the service account's
   email (`...@...iam.gserviceaccount.com`) with **Make changes to events**.
6. Same page → **Integrate calendar → Calendar ID** (looks like `abc123@group.calendar.google.com`).
7. GitHub secrets: `GOOGLE_SERVICE_ACCOUNT_JSON` = the full contents of the JSON key file,
   `GCAL_ID_RIO` / `GCAL_ID_SP` = the two calendar IDs. Variable `ENABLE_GCAL` = `1`.
8. Run the `collect` workflow; the log shows `gcal rio: {'inserted': …}`.

Only events the bot created (tagged with a private extended property) are ever updated or deleted.

## Adding a city

1. Copy `config/sp.yaml` to `config/<slug>.yaml` (e.g. `bh.yaml`), set `name`, the digest block and
   the collectors with that city's parameters (Sympla city name, Eventim city, Shotgun/Fever city
   slugs, venue list). Collectors take their per-city settings from this file.
2. Optionally add `config/recurring_<slug>.yaml` and `config/instagram_<slug>.txt`.
3. That's it: the city gets its own digest section, `<slug>.ics`, and an entry in the web toggle.
   For Google Calendar sync add `gcal_calendar_id_env: GCAL_ID_<SLUG>` and that secret.

## Configuration files

| File | Purpose |
|---|---|
| `taste.md` | your taste, sent verbatim to Claude; editing it re-scores everything once |
| `config/rio.yaml`, `config/sp.yaml` | collectors + parameters, ICS threshold, digest knobs |
| `config/recurring_<city>.yaml` | weekly/monthly rodas (**placeholders — fill in**) |
| `config/watchlist.txt` | artists / venues that trigger instant alerts (**placeholders**) |
| `config/instagram_<city>.txt` | Instagram handles (**placeholders**) |

## Hand-entered gigs

`config/gigs_<city>.yaml` holds one-off gigs you hear about; a tip needs only an act and a date.
Missing time or venue shows as "a confirmar", and the tip merges into the real listing when one
appears. CLAUDE.md tells a Claude Code session how to file these from a terse message.

## Choosing models per step

Each LLM step has its own variable (`MUSICFINDER_MODEL_EXTRACT`, `_VISION`, `_RANK`, `_DEDUPE`,
all defaulting to `MUSICFINDER_MODEL`). `python -m musicfinder compare-models` runs live venue
pages and a sample of stored events through several models and writes
`data/model-comparison.md` listing every disagreement and what the comparison cost. Each collect
run also logs tokens and dollars per step.

## Approximate monthly cost

Model: `claude-opus-5-5` by default (override with `MUSICFINDER_MODEL`, effort with
`MUSICFINDER_EFFORT`, default `low`). Prices: $4 / $20 per million input / output tokens.

| Item | Volume (estimate) | Tokens / month | Cost / month |
|---|---|---|---|
| Ranking | first run ~3,100 events (one-off ≈ $4–6), then ~100–200 new or changed events/day | ~0.5M in, ~0.3M out | ~$8 |
| Venue pages | 8 pages × 10–15k tokens, re-read only when the page text changes (~daily) | ~3.5M in, ~0.7M out | ~$28 |
| Instagram | ~20 accounts, ~300 new posts/month (image + caption) | ~0.8M in, ~0.15M out | ~$6 |
| Dedupe tiebreaks | a few dozen ambiguous pairs/run, cached | small | <$1 |
| **Total** | | | **≈ $40/month** (≈ $35 without Instagram) |

Figures include an allowance for thinking tokens at effort `low`; check the real numbers in the
Anthropic console after the first week. To cut cost by roughly 40×, set the repo variable
`MUSICFINDER_MODEL` to `claude-haiku-5-5`, at the expense of ranking quality. Venue pages are the biggest line item; disable
venues already covered by ticketing sources to save more.

GitHub Actions: ~8 collect runs/day × ~5 min ≈ 1,200 min/month — free for public repos, and
within the 2,000 free minutes for private ones. Apify: the free plan's $5/month credit covers
roughly 2 runs/day for ~20 accounts with the post scraper; more accounts need a paid plan.
Telegram, Pages and the Calendar API are free.

## Known gaps

- **Instagram Stories aren't covered** (Apify's post scraper reads feed posts only), and
  Instagram handles added for venues without websites could not be verified (Instagram blocks
  logged-out access) — check them in `config/instagram_<city>.txt`.
- **Prices are missing for Sympla and Eventim events**: neither search API exposes them and their
  detail pages are bot-protected. They show without a price; "free only" still works (Sympla
  marks free events).
- **Sold-out detection** only works where the source says so (SESC SP, Shotgun, Circo Voador,
  Blue Note SP, Bilheteria Digital). "Close to selling out" alerts rely on phrases such as
  "últimos ingressos"/"lote final" in titles or descriptions.
- **Sympla pagination is slightly unstable**: a full pass misses ~0.5% of rows (ties on start
  time shuffle across pages). An event is therefore only marked gone after it has been missing for
  7 h (≈2 runs), which also protects Google Calendar from spurious deletes.
- **Short horizons**: SESC SP and SESC RJ publish ~4–6 weeks ahead; Casa do Pandeiro's sheet
  currently has no upcoming dates.
- **Placeholder times**: Queremos shows ticketed through Ticketmaster, and venue listings without a
  time, get 21:00 / 20:00; the description says so.
- **Venue extraction needs `ANTHROPIC_API_KEY`**; without it those 8 venues return nothing
  (other collectors are unaffected) and ranking falls back to keyword scores marked `[keyword]`.
- **Rodas that only exist by word of mouth** must go in `config/recurring_<city>.yaml` by hand.
- **Undocumented APIs**: Sympla, Eventim, Fever, Shotgun, Bileto and the SESC sites are used
  through the same endpoints their own pages call. Expect an occasional collector to break; the
  run keeps going and `python -m musicfinder status` shows which.
- **Google Calendar subscriptions refresh slowly** (up to ~24h); use the direct sync for speed.
- The database is committed to the repo by the bot, so history grows by a few MB per day of
  commits; squash or move it to an Actions artifact/release asset if that ever matters.

## Changes to the seed list

The spec asked to verify the seed venues; summary of what changed (details in the venue table):

- **Renamed:** Teatro Rival → Teatro Rival Petrobras; Jazz nos Fundos → JazzB.
- **Dropped (dead website, moved to Instagram list):** Trapiche Gamboa, Escola Portátil de Música
  (Rio); Ó do Borogodó, Traço de União, Auditório Ibirapuera (SP).
- **Disabled as venue pages (covered elsewhere):** Casa do Choro (shows on Sympla), Renascença
  Clube (flyers only → Instagram).
- **Added:** Sala Cecília Meireles (Rio), Casa Natura Musical (SP, replacing Auditório Ibirapuera).
- Blue Note Rio is kept although its site currently lists no shows; its shows come via Eventim.
