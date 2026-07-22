"""Append extracted job-application details to a CSV for the tracking sheet."""

import csv
from pathlib import Path

COLUMNS = [
    "date",
    "company",
    "role_title",
    "req_number",
    "role_type",
    "season",
    "email_subject",
    "message_id",
]


def append_row(csv_path: str, row: dict) -> None:
    path = Path(csv_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        if is_new:
            writer.writeheader()
        writer.writerow({k: (row.get(k) or "") for k in COLUMNS})
