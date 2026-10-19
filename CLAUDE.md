# Music finder — notes for Claude

Aggregates live-music listings for Rio (primary) and São Paulo. README.md has the architecture;
SETUP.md tracks the owner's remaining setup steps (tick boxes there as they get done).

## Gig inbox

The owner often opens a session (frequently Claude Code remote, from a phone) just to tell you
about a gig they heard about. Their messages are terse, in English or Portuguese, e.g.
"igor souza 25/10 21h bar do zé, choro", "carlos malta sábado?", or a photo of a poster.

1. Decide the city (Rio unless the venue or message says SP) and the kind:
   - **One-off** (full or partial details) → append to `config/gigs_<city>.yaml`.
     Only a title/artists and a date are required; leave out time/venue you don't know rather than
     guessing. Relative dates ("sábado", "next friday") resolve to the next occurrence from today.
   - **Recurring** ("toda terça", "1º sábado do mês") → `config/recurring_<city>.yaml`.
   - **"Tell me when X plays"** → `config/watchlist.txt` (`artist: X` or `venue: Y`).
   - **"Track @handle"** → a venue or city-bound collective goes in `config/instagram_<city>.txt`;
     a musician goes in `config/instagram_artists.txt` as `handle homecity` (rio/sp). Add a short
     `# comment` naming them. You can't verify handles from here (Instagram blocks scraping);
     say so. Only scraped when `ENABLE_INSTAGRAM=1` and `APIFY_TOKEN` are set.
2. For a tip with missing details, also add the artist to `config/watchlist.txt` (if not already
   there) so the owner is alerted when a real listing appears.
3. Check it parses: `.venv/bin/python -m musicfinder collect --only gigs,recurring --no-publish`
   (no `.venv` in a fresh remote checkout: `pip install -r requirements.txt`, use `python`).
4. Commit and push straight to main (`git pull --rebase` first: the collect bot commits
   `data/events.db` every 3h). The owner has authorized this for inbox edits; the next collect
   run (or a manual workflow_dispatch) publishes it.
5. Reply with one line saying what you added and anything you assumed (date, city).

If something's ambiguous (which Saturday, which city), make the likely assumption and say so
rather than asking, unless the date itself is unknown.

## Questions about what's on

"What's on this weekend?", "anything choro in SP next week?": run a collect first if the local
db is stale (or in a fresh remote checkout, where `data/events.db` comes from the bot's commits),
then query `data/events.db` (`events` table, `active=1`) and answer briefly with date, time,
venue, price and ticket link.

## Models

LLM steps and their model variables: venue pages `MUSICFINDER_MODEL_EXTRACT`, Instagram flyers
`_VISION`, ranking `_RANK`, dedupe tiebreak `_DEDUPE`; all default to `MUSICFINDER_MODEL`.
`python -m musicfinder compare-models` runs the same inputs through several models and writes
`data/model-comparison.md`.
