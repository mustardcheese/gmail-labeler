"""Backfill: classify a large date range via a provider's Batch API.

Flow: page through every message matching the query, skip already-processed
ones, submit one classification batch, wait until it ends, apply labels and
archive per result, then submit a second (small) batch extracting job details
from any application confirmations and append them to the tracker CSV.

Batching is only offered by some providers (currently Anthropic, at 50% of the
standard price). On a provider without a Batch API this command exits with a
hint; use `run`/`watch` instead. Batches usually finish within an hour; the
process stays in the foreground while polling. If it's interrupted after
submission, the tokens are still billed, so let it run to completion.
"""

import logging
import sys
from collections import Counter

from . import classifier, config as cfg, gmail_client, llm, prompts, state, tracker
from .poller import CONFIRMATIONS_SUBCATEGORY, JOBS_CATEGORY, apply_decision, tracker_row

log = logging.getLogger(__name__)


def run_backfill(query: str, dry_run: bool = False, limit: int | None = None) -> None:
    config = cfg.load_config()
    client = llm.build_client(config)
    if not client.supports_batch:
        provider = config.get("provider", cfg.DEFAULT_PROVIDER)
        sys.exit(
            f"Backfill uses the provider's Batch API, which {provider!r} doesn't "
            "support. Switch to a batch-capable provider (e.g. 'anthropic') in "
            "your config, or process the range with `run`/`watch` instead."
        )
    service = gmail_client.get_service()
    conn = state.connect()

    pdir = prompts.prompts_dir(config)
    categories = prompts.load_categories(pdir / "categories.md")
    extraction_prompt = prompts.load_extraction_prompt(pdir / "extraction.md")
    skip_low_confidence = config.get("skip_low_confidence", True)
    tracker_csv = config.get("job_tracker_csv", str(cfg.CONFIG_DIR / "job_applications.csv"))

    system_prompt = classifier.build_system_prompt(categories)
    schema = classifier.build_output_schema(categories)
    label_ids = gmail_client.ensure_labels(service, prompts.all_label_names(categories))

    log.info("Listing messages for query: %s", query)
    message_ids = gmail_client.list_message_ids(service, query, max_messages=limit)
    new_ids = [m for m in message_ids if not state.is_processed(conn, m)]
    log.info("Query matched %d message(s); %d not yet processed", len(message_ids), len(new_ids))
    if not new_ids:
        log.info("Nothing to backfill.")
        return

    emails: dict[str, dict] = {}
    for i, mid in enumerate(new_ids, 1):
        emails[mid] = gmail_client.get_message(service, mid)
        if i % 100 == 0 or i == len(new_ids):
            log.info("Fetched %d/%d messages from Gmail", i, len(new_ids))

    classify_items = [
        (mid, system_prompt, schema, classifier.email_content(emails[mid]))
        for mid in new_ids
    ]
    results = client.batch_structured(classify_items, "classification")

    confirmations: list[str] = []
    stats: Counter = Counter()
    for r in results:
        email = emails.get(r.custom_id)
        if email is None:
            continue
        if not r.ok:
            # Provider failed this request -> not marked processed, so it's
            # picked up again on the next backfill run.
            log.warning("Message %s (%r): batch request failed; will retry on next backfill",
                        r.custom_id, email["subject"][:60])
            continue
        if r.data is None:
            if not dry_run:
                state.mark_processed(conn, r.custom_id, None, None, None)
            continue
        category, subcategory, skipped = apply_decision(
            service, conn, categories, label_ids, skip_low_confidence,
            email, r.data, dry_run,
        )
        key = f"{category}/{subcategory}" if subcategory else category
        stats[key + (" (left in inbox)" if skipped else "")] += 1
        if not skipped and category == JOBS_CATEGORY and subcategory == CONFIRMATIONS_SUBCATEGORY:
            confirmations.append(r.custom_id)

    log.info("Backfill summary: %s", dict(sorted(stats.items())))

    if not confirmations:
        return
    if dry_run:
        log.info("[dry-run] %d confirmation(s) would be extracted to the tracker",
                 len(confirmations))
        return

    extraction_items = [
        (mid, extraction_prompt, classifier.EXTRACTION_SCHEMA,
         classifier.email_content(emails[mid]))
        for mid in confirmations
    ]
    rows = 0
    for r in client.batch_structured(extraction_items, "extraction"):
        email = emails.get(r.custom_id)
        if email is None or not r.ok or r.data is None:
            log.warning("Extraction failed for %s; add it to the tracker manually",
                        r.custom_id)
            continue
        tracker.append_row(tracker_csv, tracker_row(r.data, email))
        rows += 1
    log.info("Tracker: %d application row(s) recorded in %s", rows, tracker_csv)
