# gmail-labeler
Script to classify gmail with an llm of choice since Notion is sunsetting Notion Mail. Applies gmail labels and archives out of main inbox into separate boxes. Pluggable with multiple APIs (Openrouter, OpenAI, Google, Anthropic, etc).

No credentials are stored in this repo. Everything secret lives in
`~/.config/gmail-labeler/` and is collected interactively by the setup wizard.

## Quick start

`./run.sh`:

```sh
./run.sh run --all       # first time: setup, then sort the whole inbox once
./run.sh                 # later: sort everything since the last run
./run.sh watch           # poll continuously
./run.sh run --dry-run   # classify + log, without touching the inbox
./run.sh clear           # wipe stored API keys, then re-enter with setup
```

## Manual install

```sh
cd ~/Projects/gmail_labeling
python -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Setup (one time)

```sh
.venv/bin/python -m gmail_labeler setup
```

## Usage

Every run picks up where the last complete one left off, so it doesn't matter
whether it runs every few minutes or every few weeks — nothing between runs is
missed. Four ways to run it:

| Mode | Command | What it checks |
|---|---|---|
| Startup (once) | `run --all` | The whole inbox. Interrupt and re-run freely; it resumes. |
| Constant | `watch` (or the systemd timer below) | New mail, every interval. |
| Infrequent | `run` | Everything since the last complete run, then exits. |
| Specific | `run --since/--until/--limit/--only` | Just what you ask for (combine freely). |

```sh
run --since 2026-09-01 --until 2026-09-30   # one month (both dates inclusive)
run --limit 50                              # the newest 50 unsorted messages
run --only jobs,security                    # label only these categories
```

`--only` still classifies everything in range, but only acts on the listed
categories; the rest stay unsorted and their answers are cached, so the next
full run labels them without asking the model again. A specific run never
moves the last-checked point, since it didn't cover everything. Neither does a
run that stopped early (rate limit, network or model error) — the next run
retries from the same point.

```sh
# See what it would do without touching your inbox:
.venv/bin/python -m gmail_labeler run --dry-run

# Happy with the preview? This applies it — no further model calls (see below):
.venv/bin/python -m gmail_labeler run

# Poll every 3 minutes in the foreground:
.venv/bin/python -m gmail_labeler watch --interval 180
```

### Dry runs are a paid preview, not wasted tokens

A dry run still calls the model, so it still costs money — but the answers are
cached in the state DB. The real run that follows replays them and makes **no
model calls at all**, so previewing before applying is free of charge:

```
$ ... run --dry-run
[dry-run] 'Your application to Acme' -> jobs/confirmations -- would archive
[dry-run] 2 message(s): 3 model call(s), 0 reused from cache. Re-run without
          --dry-run to apply these decisions with no further model calls.

$ ... run
'Your application to Acme' -> jobs/confirmations -- labeled + archived
2 message(s): 0 model call(s), 3 reused from cache
```

The cache is keyed by message id and fingerprinted against the prompt, the
schema, and the provider/model, so editing `prompts/categories.md` or switching
model invalidates the affected entries automatically — you never replay a stale
answer. Classification and job-detail extraction are fingerprinted separately,
so changing the classification prompt doesn't discard cached extractions.
Pass `--no-cache` to force a fresh classification anyway.

## Backfill old mail

`backfill` classifies a large date range through a provider's **Batch API**.
Only batch-capable providers support it (**Anthropic**) to query at 50% of
standard price. 

```sh
# Preview:
.venv/bin/python -m gmail_labeler backfill --query "in:inbox newer_than:6m" --dry-run

# Pass:
.venv/bin/python -m gmail_labeler backfill --query "in:inbox newer_than:6m"
```

`--limit n` to limit how many emails included.

The preview caches its classifications too, so the real pass reuses them
instead of paying for a second classification batch — the one place this is
worth real money. Extraction is the exception: a dry run skips it entirely
rather than caching it, since previewing it would make the dry run cost *more*
than it does today.

## Run automatically (systemd user timer)

```sh
mkdir -p ~/.config/systemd/user
cp systemd/gmail-labeler.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now gmail-labeler.timer

journalctl --user -u gmail-labeler.service -f   # watch the logs
```

## Tuning

### Categories & prompts — `prompts/categories.md`

The taxonomy and the classifier prompt are the same file: `## Name` is a
category (its heading becomes the Gmail label), `### Name` is a subcategory
(label `Category/Name`), `archive: yes|no` under a category heading controls
whether matching mail is moved out of the inbox, and the text under each
heading is sent to the model verbatim as that category's definition. Reword,
add, or remove freely — labels for new categories are created on the next run.

The extraction prompt for job-application confirmations lives in
`prompts/extraction.md`.

### Runtime settings — `~/.config/gmail-labeler/config.json`

- **`query`** — which mail is considered (default `in:inbox`). Any Gmail
  search syntax works, e.g. `in:inbox category:primary`. Leave dates out: each
  run adds its own window starting at the last complete run, and a date term
  here (like `newer_than:3d`) would hide mail that arrived between runs.
- **`skip_low_confidence`** — when `true` (default), emails whose *category*
  is uncertain stay in the inbox unlabeled. (Job emails with an unclear
  subcategory go to `Jobs/Unsure` instead — that's handled by the prompt.)
- **`job_tracker_csv`** — where extracted application details are appended
  (default `~/.config/gmail-labeler/job_applications.csv`). Columns: date,
  company, role_title, req_number, role_type, season, email_subject,
  message_id — import or paste into your tracking sheet.
- **`provider`** — which backend to use (default `openrouter`). One of
  `openrouter`, `openai`, `google`, `mistral`, `anthropic`. The key comes from
  that provider's env var (e.g. `OPENROUTER_API_KEY`) or `<provider>_api_key`
  in the config.
- **`model`** — defaults to `openai/gpt-oss-120b` on OpenRouter (paid, no free-
  tier rate caps, pennies at personal-email volume). Any model slug the chosen
  provider offers works; within one provider, switching model is just this
  string. Swap in a bigger model if classification quality disappoints.
- **`prompts_dir`** — optional override for where the prompt files live.

State lives in `~/.config/gmail-labeler/state.db`, in three tables:

- **`processed`** — which messages are done. Only written on a real run, so
  `--dry-run` stays repeatable. Delete the DB to reprocess everything the
  query matches.
- **`llm_cache`** — what the model said about each message. Written on every
  run, dry or not, so a preview's tokens carry over to the run that applies
  it. Stale entries are ignored via the fingerprint rather than deleted, so
  clearing it is never necessary; `--no-cache` bypasses it for one run.
- **`meta`** — holds `last_checked`, the start time of the last complete run;
  each run searches from a day before it. Installs from before this existed
  start from their first processed message.
