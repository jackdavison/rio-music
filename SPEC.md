# Brazil Live Music Finder — Build Spec

Build a Python project that finds live music events in **Rio de Janeiro** (primary) and **São Paulo** (secondary), ranks them by my taste, and delivers them as a daily Telegram digest, subscribable calendar feeds, and a web calendar on GitHub Pages.

Work autonomously: open each site, inspect network requests to find JSON endpoints, write collectors, run them against live data, and fix failures before finishing. Verify that every venue/account listed below actually exists and is active; drop or replace any that aren't, and note what you changed in the README.

## Cities

Every event carries a `city` field (`rio` | `sp`). Rio and São Paulo stay separated in every output: separate digest sections, separate calendar feeds, a city toggle on the web page. Collectors are configured per city via `config/rio.yaml` and `config/sp.yaml` so adding a third city later means adding one config file.

## Sources

Each source is a pluggable collector that writes to the shared schema. One failing collector must never break the run; log it and continue.

**Ticketing (both cities):** Sympla (find the public search JSON the site uses; filter by city + music), Eventim, Shotgun, Bilheteria Digital, Queremos!, Fever.

**SESC:**
- Rio: scrape programação for all SESC RJ units.
- SP: SESC SP has a much larger music program (Pompeia, Vila Mariana, 24 de Maio, Belenzinho, Consolação, etc.). Check whether sescsp.org.br exposes a JSON API before scraping HTML.

**Venue calendars** (LLM-extract events from raw HTML instead of brittle selectors):
- Rio seeds: Circo Voador, Teatro Rival, Blue Note Rio, Vivo Rio, Casa do Choro, Escola Portátil de Música, Casa do Pandeiro, Renascença Clube, Trapiche Gamboa
- SP seeds: Casa de Francisca, Blue Note SP, Jazz nos Fundos, Bourbon Street, Ó do Borogodó, Traço de União, Teatro Sérgio Cardoso, Auditório Ibirapuera

**Instagram** (optional, behind a feature flag): pull recent posts from accounts in `config/instagram_<city>.txt` via the Apify API (`APIFY_TOKEN`). Send flyer image + caption to Claude vision and return structured JSON (date, time, venue, artists, price, link). Cache by post ID so each post is only processed once.

**Recurring events:** `config/recurring_<city>.yaml`, a hand-maintained list of weekly/monthly rodas (name, venue, weekday, time, notes, active date range). Expand into dated events for the next 8 weeks. Stub with clearly marked placeholders for me to fill in.

## Schema (SQLite, committed to the repo or stored as a workflow artifact)

`id, city, title, artists[], venue, neighborhood, start_datetime (America/Sao_Paulo), end_datetime, price_min, price_max, is_free, ticket_url, source, source_url, image_url, first_seen, last_seen, score, score_reason, sold_out`

## Processing

1. **Dedupe** on city + venue + date + fuzzy artist match; LLM tiebreak for ambiguous pairs. Merge sources, keeping the best ticket URL.
2. **Rank** with Claude (`ANTHROPIC_API_KEY`) against `taste.md`, producing a 0–100 score and a one-line reason. Batch calls and cache scores so unchanged events aren't re-scored.
3. **Track changes:** flag events that are new since the last run, newly sold out, or have changed date/venue.

## taste.md (seed this)

Love: samba raiz, rodas de samba, choro (especially syncopated, samba-leaning choro), Brazilian instrumental, gafieira, jazz, Afro-Brazilian and diasporic traditions, flute and sax-forward groups.
Interested: MPB, forró pé-de-serra, frevo/maracatu, contemporary/minimalist classical.
Deprioritize: pagode romântico, sertanejo, funk carioca, EDM, cover/tribute bands.

## Outputs

### 1. Telegram (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`)
- **8am digest:** Rio section (today, tomorrow, next 60 days' notable shows), then a separate São Paulo section (next 14 days' top picks plus big shows further out). Each line: score, time, title, venue/neighborhood, price, link.
- **Instant alerts:** an event matching `config/watchlist.txt` (artists or venues, either city) appears for the first time, or a watched event is close to selling out.

### 2. Calendar feeds (ICS, served from GitHub Pages)
- `rio.ics`, `sp.ics`: events scoring above a configurable threshold
- `watchlist.ics`: watchlist matches only
- Each VEVENT includes the venue as LOCATION, ticket link as URL, score + reason in DESCRIPTION, and stable UIDs so updates replace rather than duplicate.
- The README explains subscribing in Google Calendar via "From URL", and notes that Google refreshes subscribed feeds slowly (up to ~24h).

### 3. Optional direct Google Calendar sync
Behind a flag: push events into two Google Calendars ("Música — Rio", "Música — SP") via the Calendar API with a service account (`GOOGLE_SERVICE_ACCOUNT_JSON`, calendar IDs as secrets). Upsert by stable event ID; delete events that disappear from sources. This gives near-instant updates instead of ICS polling lag. The README documents the one-time setup step by step.

### 4. Web calendar (GitHub Pages)
A static site regenerated each run from an `events.json`:
- City toggle (Rio / SP), month/week/list views (FullCalendar from a CDN is fine)
- **Mobile-first** layout with a list view as the default on phones; I'll mostly use this on my phone
- Filters: minimum score, free only, genre tags, neighborhood, date range
- Event detail: flyer image, score reason, ticket link, source, "new" badge
- "Tonight" quick view at the top
- Lightweight with no build step: plain HTML/JS/CSS

## Deploy

GitHub Actions:
- Collection every 3h
- Digest at 8am America/Sao_Paulo
- Instagram collection at most twice daily
- Pages deploy after each successful run

All secrets live in GitHub Actions secrets. Add a manual `workflow_dispatch` trigger and a `--dry-run` CLI flag that prints the digest instead of sending it.

## Deliverables

- Working repo with all of the above
- README: setup, secrets, how to add a city, how to subscribe to feeds, approximate monthly API cost
- Stubbed config files with clear placeholders: `recurring_rio.yaml`, `recurring_sp.yaml`, `watchlist.txt`, `instagram_rio.txt`, `instagram_sp.txt`
- A short "known gaps" section in the README (e.g. Instagram Stories aren't covered)
