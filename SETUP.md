# Remaining setup

Tick these off as you go (details for each step are in README.md).

## Next up: API key and model test (local)
- [ ] Console: link the Console organization to your Max plan so the $100/month credit applies
- [ ] Create an API key in that organization; set a monthly spend limit (e.g. $60) on its workspace
- [x] Put it in `.env` at the repo root (done 2026-10-17)
- [x] Model comparison (2026-10-17, report in `data/model-comparison.md`): keep Opus for
      everything, no repo variables needed. Sonnet put Casa de Francisca shows on the wrong
      dates; Haiku dropped about a third of venue events and invented artist facts when ranking
- [ ] Re-run `compare-models` once Instagram posts are cached, to check the flyer model

## Instagram (the big one)
- [x] Apify account; token in `.env` (2026-10-18)
- [x] Following list exported (2026-10-19): 44 accounts picked (11 Rio venues, 33 musicians)
- [ ] ~~Request your following list from Instagram~~ (done; steps kept for later re-exports) (it's emailed, from minutes to a day later):
      1. Instagram app → your profile → ☰ menu → **Accounts Center**
         (or on the web: accountscenter.instagram.com)
      2. **Your information and permissions** → **Export your information** → **Create export**
      3. Pick your Instagram profile → **Export to device**
      4. **Customize information**: untick everything except **Followers and following**
         (under Connections). Date range: **All time**. Format: **JSON**. Media quality doesn't matter
      5. **Start export**. When it's ready, download the zip from **Available downloads** in that
         same screen (it's kept for 4 days) and put it in the repo folder, or give Claude the path
- [x] Claude picks out the Rio/SP musicians and venues → `config/instagram_artists.txt` and
      `config/instagram_<city>.txt`; you skim the list
      (Impatient? Just tell Claude 10–20 handles to start; the export can be added later.)
- [x] First live Instagram run (2026-10-18): works; 4 events from the seed venues at ~$0.02/post
      on Opus. Seed handles trapichegamboa, escolaportatildemusica and the 3 SP ones returned
      nothing (probably wrong); replace them when the export arrives
- [x] Flyer comparison on 100 posts (2026-10-19): Sonnet matched Opus on 98, Haiku on 97; Opus was
      the one that skipped non-music posts. Kept Opus (~$0.02/post, ~$9/month)

## Get it running
- [x] Public repo pushed: https://github.com/jackdavison/rio-music (2026-10-19)
- [x] Settings → Pages → Source: **GitHub Actions** (switched by Claude 2026-10-19)
- [x] Secrets `ANTHROPIC_API_KEY` and `APIFY_TOKEN` (the values in `.env`). Do this soon: until
      the key is there, scheduled runs can't read the venue sites and their shows drop off after ~7h
- [x] Variable `PAGES_URL` = `https://jackdavison.github.io/rio-music/`
- [x] First collect + instagram runs on GitHub (2026-10-19): 0 failures, site live at
      https://jackdavison.github.io/rio-music/ (Shotgun skipped in CI: blocks GitHub IPs)
- [ ] ~2026-10-26: check API spend in the Anthropic console (expect ~$45/month) and Apify usage
      (free plan ~$5/month)

## Fill in your own data
- [ ] `config/watchlist.txt`: artists and venues to be alerted about (replace the placeholders)
- [x] `config/recurring_rio.yaml`: 7 rodas added 2026-10-19 (tell Claude to add more)
- [ ] `config/recurring_sp.yaml`
- [ ] Skim `taste.md`; edit it if anything's off (everything gets re-scored once)
- [ ] Check the neighborhoods in the `venues:` blocks of `config/rio.yaml` / `config/sp.yaml`

## Gig inbox
- [ ] Open a Claude Code remote session on the repo and tell it a gig ("carlos malta sábado?");
      it adds it to `config/gigs_<city>.yaml` and pushes (rules in CLAUDE.md)

## Subscribe
- [ ] Google Calendar → Other calendars → From URL → `https://jackdavison.github.io/rio-music/rio.ics` (and `sp.ics`, `watchlist.ics`)
- [ ] Add the web calendar to your phone's home screen

## Optional
- [ ] Telegram (skipped for now: web calendar + Claude Code instead). Adds the 8am digest and
      instant watchlist/sold-out alerts. Bot via @BotFather → `TELEGRAM_BOT_TOKEN`; your chat id
      from `getUpdates` → `TELEGRAM_CHAT_ID`
- [ ] Google Calendar direct sync: service account + two calendars (README steps 1–8),
      secrets `GOOGLE_SERVICE_ACCOUNT_JSON`, `GCAL_ID_RIO`, `GCAL_ID_SP`, variable `ENABLE_GCAL=1`

## API billing (decided: Max 5x)
The $100/month API credit covers the ~$40/month estimate. Key steps are under "Next up".
