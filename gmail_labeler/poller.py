"""One polling pass: find new messages, classify, label/archive, track jobs."""

import logging

from . import classifier, config as cfg, gmail_client, llm, prompts, state, tracker

log = logging.getLogger(__name__)

JOBS_CATEGORY = "jobs"
CONFIRMATIONS_SUBCATEGORY = "confirmations"


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


def run_once(dry_run: bool = False) -> None:
    config = cfg.load_config()
    client = llm.build_client(config)
    service = gmail_client.get_service()
    conn = state.connect()

    pdir = prompts.prompts_dir(config)
    categories = prompts.load_categories(pdir / "categories.md")
    extraction_prompt = prompts.load_extraction_prompt(pdir / "extraction.md")
    query = config.get("query", cfg.DEFAULT_QUERY)
    skip_low_confidence = config.get("skip_low_confidence", True)
    tracker_csv = config.get("job_tracker_csv", str(cfg.CONFIG_DIR / "job_applications.csv"))

    system_prompt = classifier.build_system_prompt(categories)
    schema = classifier.build_output_schema(categories)
    label_ids = gmail_client.ensure_labels(service, prompts.all_label_names(categories))

    message_ids = gmail_client.list_message_ids(service, query)
    new_ids = [m for m in message_ids if not state.is_processed(conn, m)]
    log.info("Query matched %d message(s); %d new", len(message_ids), len(new_ids))

    for mid in new_ids:
        email = gmail_client.get_message(service, mid)
        try:
            result = classifier.classify(client, system_prompt, schema, email)
        except llm.LLMConnectionError:
            log.warning("Network error classifying %s; will retry next pass", mid)
            continue  # not marked processed -> retried next pass
        except llm.LLMRateLimitError:
            log.warning("Rate limited; stopping this pass early")
            break
        if result is None:
            # Unusable response; mark processed so we don't loop on it forever.
            state.mark_processed(conn, mid, None, None, None)
            continue

        category, subcategory, skipped = apply_decision(
            service, conn, categories, label_ids, skip_low_confidence,
            email, result, dry_run,
        )
        if not skipped and category == JOBS_CATEGORY and subcategory == CONFIRMATIONS_SUBCATEGORY:
            _track_confirmation(
                client, extraction_prompt, email, tracker_csv, dry_run
            )


def _track_confirmation(client, extraction_prompt, email, tracker_csv, dry_run):
    try:
        details = classifier.extract_job_details(client, extraction_prompt, email)
    except llm.LLMError:
        log.warning(
            "Extraction failed for %s (%r); add it to the tracker manually",
            email["id"], email["subject"][:60],
        )
        return
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
