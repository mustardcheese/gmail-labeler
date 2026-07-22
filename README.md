# gmail-labeler
Script to classify gmail with an llm of choice since Notion is sunsetting Notion Mail. Applies gmail labels and archives out of main inbox into separate boxes. Pluggable with multiple APIs (Openrouter, OpenAI, Google, Anthropic, etc).

No credentials are stored in this repo. Everything secret lives in
`~/.config/gmail-labeler/` and is collected interactively by the setup wizard.

## Quick start

`./run.sh`:

```sh
./run.sh                 # initial launch: setup env then a single inbox pass
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

```sh
# See what it would do without touching your inbox:
.venv/bin/python -m gmail_labeler run --dry-run

# One pass:
.venv/bin/python -m gmail_labeler run

# Poll every 3 minutes in the foreground:
.venv/bin/python -m gmail_labeler watch --interval 180
```

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

- **`query`** — which mail is considered (default `in:inbox newer_than:3d`).
  Any Gmail search syntax works, e.g. `in:inbox category:primary newer_than:1d`.
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

State (which messages were already processed) lives in
`~/.config/gmail-labeler/state.db`. Delete it to reprocess everything the
query matches; `--dry-run` never writes state, so dry runs are repeatable.
