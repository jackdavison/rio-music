"""python -m musicfinder <command> [options]

  collect   run collectors -> dedupe -> rank -> store, then publish outputs and send alerts
  digest    send (or with --dry-run, print) the daily Telegram digest
  alerts    send pending watchlist alerts
  publish   rebuild site/events.json and ICS feeds from the database (and gcal if enabled)
  status    show the last run of each collector
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta

from . import db
from .config import SITE_DIR, flag, load_cities
from .models import TZ

log = logging.getLogger("musicfinder")


def _by_city(cities):
    since = datetime.now(TZ) - timedelta(hours=6)
    return {c.slug: db.load_events(c.slug, since=since) for c in cities}


def cmd_publish(cities, args):
    from .outputs import ics, web
    by_city = _by_city(cities)
    names = {c.slug: c.name for c in cities}
    p = web.write(SITE_DIR, by_city, names)
    feeds = ics.write_feeds(SITE_DIR, by_city, {c.slug: c.ics_threshold for c in cities}, names)
    log.info("wrote %s and %s", p.name, ", ".join(f.name for f in feeds))
    if flag("ENABLE_GCAL"):
        from .outputs import gcal
        for c in cities:
            try:
                gcal.sync_city(c, by_city[c.slug], dry_run=args.dry_run)
            except Exception as exc:
                log.error("gcal sync failed for %s: %s", c.slug, exc)


def cmd_alerts(cities, args):
    from .outputs import telegram
    by_id = {e.id: e for evs in _by_city(load_cities()).values() for e in evs}
    msgs, handled = telegram.build_alerts(by_id)
    if not args.dry_run and not telegram.configured():
        db.mark_alerted(handled)  # no bot: drop them, so enabling Telegram later doesn't flood
        log.info("Telegram not configured; %d alerts skipped", len(msgs))
        return
    for m in msgs:
        telegram.send(m, dry_run=args.dry_run)
    if not args.dry_run:
        db.mark_alerted(handled)
    log.info("%d alerts (%d changes processed)", len(msgs), len(handled))


def cmd_digest(cities, args):
    from .outputs import telegram
    text = telegram.build_digest(cities, _by_city(cities))
    if not args.dry_run and not telegram.configured():
        log.info("Telegram not configured; digest not sent (use --dry-run to print it)")
        return
    telegram.send(text, dry_run=args.dry_run)


def cmd_collect(cities, args):
    from .pipeline import process_city
    only = args.only.split(",") if args.only else None
    skip = args.skip.split(",") if args.skip else None
    failures = 0
    total = 0
    for c in cities:
        events, results = process_city(c, only=only, skip=skip)
        total += len(events)
        failures += sum(not r.ok for r in results)
        for r in results:
            log.info("  %-20s %-4s %4d  %s", f"{c.slug}/{r.name}", "ok" if r.ok else "FAIL",
                     len(r.events), r.error[:100])
    log.info("collected %d events; %d collector failures", total, failures)
    from . import llm
    if llm.USAGE:
        log.info("Claude usage this run:\n%s", llm.usage_summary())
    if not args.no_publish:
        cmd_publish(load_cities(), args)
        cmd_alerts(cities, args)
    # exit 0 even when some collectors fail: partial data is still published


def cmd_status(cities, args):
    rows = db.conn().execute("""SELECT r.* FROM runs r JOIN (SELECT city, collector, MAX(id) id FROM runs
                                GROUP BY city, collector) m ON r.id=m.id ORDER BY r.city, r.collector""").fetchall()
    for r in rows:
        print(f"{r['started']}  {r['city']:4} {r['collector']:20} {'ok ' if r['ok'] else 'FAIL'} "
              f"{r['n_events']:4}  {r['seconds']:.1f}s  {r['error'] or ''}")


def cmd_compare(cities, args):
    from .compare import run
    run(cities, models=args.models.split(","), rank_sample=args.rank_sample, out=args.out)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="musicfinder", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["collect", "digest", "alerts", "publish", "status",
                                            "compare-models"])
    ap.add_argument("--city", help="comma-separated city slugs (default: all config/*.yaml)")
    ap.add_argument("--only", help="comma-separated collectors to run")
    ap.add_argument("--skip", help="comma-separated collectors to skip")
    ap.add_argument("--dry-run", action="store_true", help="print Telegram messages instead of sending")
    ap.add_argument("--no-publish", action="store_true", help="collect only; don't write outputs/alerts")
    ap.add_argument("--models", default="claude-opus-5-5,claude-sonnet-5-5,claude-haiku-5-5",
                    help="compare-models: models to try, first one is the reference")
    ap.add_argument("--rank-sample", type=int, default=80,
                    help="compare-models: number of stored events to score with each model")
    ap.add_argument("--out", default="data/model-comparison.md", help="compare-models: report path")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cities = load_cities(args.city.split(",") if args.city else None)
    {"collect": cmd_collect, "digest": cmd_digest, "alerts": cmd_alerts,
     "publish": cmd_publish, "status": cmd_status,
     "compare-models": cmd_compare}[args.command](cities, args)


if __name__ == "__main__":
    main()
