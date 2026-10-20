"""CLI entry point: python -m gmail_labeler {setup,clear,run,watch,backfill}"""

import argparse
import logging
import time
from datetime import date


def _date(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {s!r}")


def _categories(s: str) -> list[str]:
    return [c.strip() for c in s.split(",") if c.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="gmail-labeler",
        description="Classify incoming Gmail with an LLM and apply labels.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("setup", help="Interactive first-run setup (API key, Google OAuth, labels)")

    sub.add_parser("clear", help="Remove stored API keys from the config (re-enter with setup)")

    # Cached model answers make a dry run reusable: the real run that follows
    # replays the same decisions without calling the model again.
    cache_help = ("Ignore cached model answers and re-classify from scratch "
                  "(the cache already self-invalidates when prompts or the "
                  "provider/model change)")

    p_run = sub.add_parser(
        "run",
        help="Sort everything since the last complete run, then exit",
        description="With no options, sorts every unprocessed inbox message since "
                    "the last complete run, however long ago that was. --since, "
                    "--until, --limit and --only narrow the run; a narrowed run "
                    "doesn't move the last-checked point.",
    )
    start = p_run.add_mutually_exclusive_group()
    start.add_argument("--all", dest="all_mail", action="store_true",
                       help="Sort the whole inbox, not just mail since the last run "
                            "(one-time startup pass; safe to resume if interrupted)")
    start.add_argument("--since", type=_date, metavar="YYYY-MM-DD",
                       help="Only mail received on or after this date")
    p_run.add_argument("--until", type=_date, metavar="YYYY-MM-DD",
                       help="Only mail received on or before this date")
    p_run.add_argument("--limit", type=int, metavar="N",
                       help="Only the newest N unprocessed messages")
    p_run.add_argument("--only", type=_categories, metavar="CAT[,CAT...]",
                       help="Only label these categories (e.g. jobs,security); the "
                            "rest are classified and cached but left for a later run")
    p_run.add_argument("--dry-run", action="store_true",
                       help="Classify and log, but don't apply labels or record state")
    p_run.add_argument("--no-cache", action="store_true", help=cache_help)

    p_watch = sub.add_parser("watch", help="Poll continuously")
    p_watch.add_argument("--interval", type=int, default=180,
                         help="Seconds between passes (default: 180)")
    p_watch.add_argument("--dry-run", action="store_true",
                         help="Classify and log, but don't apply labels or record state")
    p_watch.add_argument("--no-cache", action="store_true", help=cache_help)

    p_backfill = sub.add_parser(
        "backfill",
        help="Classify a large date range via a batch-capable provider (e.g. anthropic)",
    )
    p_backfill.add_argument("--query", required=True,
                            help='Gmail search query, e.g. "in:inbox newer_than:6m"')
    p_backfill.add_argument("--limit", type=int, default=None,
                            help="Cap the number of messages (default: no cap)")
    p_backfill.add_argument("--dry-run", action="store_true",
                            help="Classify and log decisions, but don't label, archive, "
                                 "or record anything (classification tokens are still "
                                 "billed, but cached for the real run)")
    p_backfill.add_argument("--no-cache", action="store_true", help=cache_help)

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.command == "setup":
        from .setup_wizard import run_setup
        run_setup()
        return

    if args.command == "clear":
        from .setup_wizard import run_clear
        run_clear()
        return

    if args.command == "backfill":
        from .backfill import run_backfill
        run_backfill(args.query, dry_run=args.dry_run, limit=args.limit,
                     use_cache=not args.no_cache)
        return

    from .poller import run_once
    if args.command == "run":
        run_once(dry_run=args.dry_run, use_cache=not args.no_cache,
                 all_mail=args.all_mail, since=args.since, until=args.until,
                 limit=args.limit, only=args.only)
        return

    log = logging.getLogger(__name__)
    while True:
        try:
            run_once(dry_run=args.dry_run, use_cache=not args.no_cache)
        except KeyboardInterrupt:
            raise
        except Exception:
            log.exception("Pass failed; retrying after the interval")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
