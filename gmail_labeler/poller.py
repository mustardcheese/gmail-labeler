"""One run: find unprocessed messages, classify, label/archive, track jobs.

Each run searches from the last complete run onward (state's last_checked),
so running every few minutes or every few weeks both leave nothing unsorted.

Model answers are cached in the state DB (see state.py), including on dry
runs. A `run --dry-run` therefore acts as a paid preview: the follow-up real
run replays the same decisions from cache and makes no model calls at all.
"""

import logging
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta

from . import classifier, config as cfg, gmail_client, llm, prompts, state, tracker

log = logging.getLogger(__name__)

JOBS_CATEGORY = "jobs"
CONFIRMATIONS_SUBCATEGORY = "confirmations"

# Each run re-searches this far before last_checked, in case mail is delivered
# with a timestamp slightly older than its arrival. Already-processed messages
# in the overlap are skipped via the state DB, so it costs nothing.
OVERLAP_SECONDS = 24 * 3600


def apply_decision(
    service,
    conn,
    categories: dict,
    label_ids: dict,
    skip_low_confidence: bool,
    email: dict,
    result: dict,
    dry_run: bool,
) -> tuple[str, str | None, bool]:
    """Act on one classification result: label/archive the message and record
    state. Returns (category, subcategory, skipped)."""
    result = classifier.validate_result(result, categories)
    category = result["category"]
    subcategory = result.get("subcategory")
    confidence = result.get("confidence", "high")
    cat_spec = categories.get(category)

    summary = f"{email['subject'][:60]!r} -> {category}" + (
        f"/{subcategory}" if subcategory else ""
    )
    skipped = cat_spec is None or (skip_low_confidence and confidence == "low")
    if skipped:
        log.info("%s (%s confidence) -- left in inbox", summary, confidence)
    else:
        to_add = [label_ids[prompts.category_label(cat_spec)]]
        if subcategory:
            sub_spec = cat_spec["subcategories"][subcategory]
            to_add.append(label_ids[prompts.subcategory_label(cat_spec, sub_spec)])
        to_remove = ["INBOX"] if cat_spec["archive"] else []
        if dry_run:
            log.info("[dry-run] %s (%s confidence)%s", summary, confidence,
                     " -- would archive" if to_remove else "")
        else:
            gmail_client.apply_labels(service, email["id"], to_add, to_remove)
            log.info("%s (%s confidence)%s", summary, confidence,
                     " -- labeled + archived" if to_remove else " -- labeled")

    if not dry_run:
        state.mark_processed(conn, email["id"], category, subcategory, confidence)
    return category, subcategory, skipped


def build_query(base: str, after: int | None, before: int | None) -> str:
    """Add an epoch-seconds date window (Gmail accepts after:/before: as epochs)."""
    parts = [base] if base else []
    if after is not None:
        parts.append(f"after:{after}")
    if before is not None:
        parts.append(f"before:{before}")
    return " ".join(parts)


def _local_midnight(d: date) -> int:
    return int(datetime.combine(d, datetime.min.time()).timestamp())


def _fmt(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def run_once(
    dry_run: bool = False,
    use_cache: bool = True,
    *,
    all_mail: bool = False,
    since: date | None = None,
    until: date | None = None,
    limit: int | None = None,
    only: list[str] | None = None,
) -> None:
    """Sort every unprocessed message in the search window.

    The window normally starts at state's last_checked (minus OVERLAP_SECONDS),
    so however long it's been since the last run, nothing is missed. all_mail
    drops the start for a one-time pass over the whole inbox. since/until/
    limit/only narrow the run; a narrowed run never moves last_checked, since
    it didn't cover everything.
    """
    if all_mail and since is not None:
        raise ValueError("all_mail and since are mutually exclusive")
    started = int(time.time())
    config = cfg.load_config()
    conn = state.connect()

    pdir = prompts.prompts_dir(config)
    categories = prompts.load_categories(pdir / "categories.md")
    if only is not None:
        valid = list(categories) + [classifier.NO_LABEL_CATEGORY]
        unknown = [c for c in only if c not in valid]
        if unknown:
            sys.exit(f"Unknown --only categor{'y' if len(unknown) == 1 else 'ies'} "
                     f"{', '.join(unknown)}. Choose from: {', '.join(valid)}")

    if all_mail:
        after = None
    elif since is not None:
        after = _local_midnight(since)
    else:
        last = state.get_last_checked(conn)
        if last is None:
            sys.exit(
                "No previous run recorded, so there's no point to continue from.\n"
                "Sort the whole inbox once with:   run --all\n"
                "or start from a date with:        run --since YYYY-MM-DD"
            )
        after = last - OVERLAP_SECONDS
    before = _local_midnight(until + timedelta(days=1)) if until is not None else None
    targeted = since is not None or until is not None or limit is not None or only is not None

    client = llm.build_client(config)
    service = gmail_client.get_service()
    extraction_prompt = prompts.load_extraction_prompt(pdir / "extraction.md")
    query = build_query(config.get("query", cfg.DEFAULT_QUERY), after, before)
    skip_low_confidence = config.get("skip_low_confidence", True)
    tracker_csv = config.get("job_tracker_csv", str(cfg.CONFIG_DIR / "job_applications.csv"))

    system_prompt = classifier.build_system_prompt(categories)
    schema = classifier.build_output_schema(categories)
    label_ids = gmail_client.ensure_labels(service, prompts.all_label_names(categories))

    provider = config.get("provider", cfg.DEFAULT_PROVIDER)
    model = config.get("model", cfg.DEFAULT_MODEL)
    classify_fp = state.fingerprint(system_prompt, schema, provider, model)
    extract_fp = state.fingerprint(
        extraction_prompt, classifier.EXTRACTION_SCHEMA, provider, model
    )

    log.info("Checking mail %s", f"since {_fmt(after)}" if after is not None
             else "across the whole inbox")
    message_ids = gmail_client.list_message_ids(service, query, max_messages=None)
    new_ids = [m for m in message_ids if not state.is_processed(conn, m)]
    log.info("Query matched %d message(s); %d new", len(message_ids), len(new_ids))
    if limit is not None:
        new_ids = new_ids[:limit]

    complete = True  # every new message was settled; cleared on any skip
    stats: Counter = Counter()
    for i, mid in enumerate(new_ids, 1):
        email = gmail_client.get_message(service, mid)
        try:
            result, from_cache = state.get_or_compute(
                conn, mid, state.KIND_CLASSIFY, classify_fp, use_cache,
                lambda: classifier.classify(client, system_prompt, schema, email),
            )
        except llm.LLMConnectionError:
            log.warning("Network error classifying %s; will retry next run", mid)
            complete = False
            continue  # not marked processed -> retried next run
        except llm.LLMRateLimitError:
            log.warning("Rate limited; stopping this run early")
            complete = False
            break
        except llm.LLMError as e:
            log.warning("Model error classifying %s (%s); will retry next run", mid, e)
            complete = False
            continue
        stats["cached" if from_cache else "classified"] += 1
        if i % 100 == 0:
            log.info("Progress: %d/%d", i, len(new_ids))
        if result is None:
            # Unusable response; mark processed so we don't loop on it forever.
            # A dry run touches nothing, so it re-asks on the next pass.
            if not dry_run:
                state.mark_processed(conn, mid, None, None, None)
            continue
        if only is not None and classifier.validate_result(result, categories)["category"] not in only:
            stats["not_in_only"] += 1
            continue  # left unprocessed; its cached answer is reused later

        category, subcategory, skipped = apply_decision(
            service, conn, categories, label_ids, skip_low_confidence,
            email, result, dry_run,
        )
        if not skipped and category == JOBS_CATEGORY and subcategory == CONFIRMATIONS_SUBCATEGORY:
            _track_confirmation(
                client, extraction_prompt, email, tracker_csv, dry_run,
                conn, extract_fp, use_cache, stats,
            )

    _log_summary(stats, dry_run)
    if stats["not_in_only"]:
        log.info("%d message(s) outside --only %s left for a later run",
                 stats["not_in_only"], ",".join(only))
    if dry_run or targeted:
        return
    if complete:
        state.set_last_checked(conn, started)
        log.info("All caught up; the next run checks from %s", _fmt(started))
    else:
        log.info("Some messages were skipped; the next run retries from %s",
                 _fmt(after) if after is not None else "the whole inbox")


def _log_summary(stats: Counter, dry_run: bool) -> None:
    if not stats:
        return
    calls = stats["classified"] + stats["extracted"]
    log.info(
        "%s%d message(s): %d model call(s), %d reused from cache%s",
        "[dry-run] " if dry_run else "",
        stats["classified"] + stats["cached"],
        calls,
        stats["cached"] + stats["extract_cached"],
        ". Re-run without --dry-run to apply these decisions with no further "
        "model calls." if dry_run else "",
    )


def _track_confirmation(client, extraction_prompt, email, tracker_csv, dry_run,
                        conn, extract_fp, use_cache, stats):
    try:
        details, from_cache = state.get_or_compute(
            conn, email["id"], state.KIND_EXTRACT, extract_fp, use_cache,
            lambda: classifier.extract_job_details(client, extraction_prompt, email),
        )
    except llm.LLMError:
        log.warning(
            "Extraction failed for %s (%r); add it to the tracker manually",
            email["id"], email["subject"][:60],
        )
        return
    stats["extract_cached" if from_cache else "extracted"] += 1
    if details is None:
        log.warning("Extraction returned nothing for %s", email["id"])
        return
    row = tracker_row(details, email)
    if dry_run:
        log.info("[dry-run] tracker row: %s", row)
    else:
        tracker.append_row(tracker_csv, row)
        log.info("Tracker: %s / %s recorded", details.get("company"), details.get("role_title"))


def tracker_row(details: dict, email: dict) -> dict:
    return {
        **details,
        "date": email["date"],
        "email_subject": email["subject"],
        "message_id": email["id"],
    }
