"""CLI entry point: python -m gmail_labeler {setup,run,watch}"""

import argparse
import logging
import time


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="gmail-labeler",
        description="Classify incoming Gmail with an LLM and apply labels.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("setup", help="Interactive first-run setup (API key, Google OAuth, labels)")

    sub.add_parser("clear", help="Remove stored API keys from the config (re-enter with setup)")

    p_run = sub.add_parser("run", help="Process new mail once and exit")
    p_run.add_argument("--dry-run", action="store_true",
                       help="Classify and log, but don't apply labels or record state")

    p_watch = sub.add_parser("watch", help="Poll continuously")
    p_watch.add_argument("--interval", type=int, default=180,
                         help="Seconds between passes (default: 180)")
    p_watch.add_argument("--dry-run", action="store_true",
                         help="Classify and log, but don't apply labels or record state")

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
                                 "or record anything (classification tokens are still billed)")

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
        run_backfill(args.query, dry_run=args.dry_run, limit=args.limit)
        return

    from .poller import run_once
    if args.command == "run":
        run_once(dry_run=args.dry_run)
        return

    log = logging.getLogger(__name__)
    while True:
        try:
            run_once(dry_run=args.dry_run)
        except KeyboardInterrupt:
            raise
        except Exception:
            log.exception("Pass failed; retrying after the interval")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
