"""Gmail API helpers: auth, fetching messages, and managing/applying labels."""

import base64
import html
import logging
import re
from datetime import datetime, timezone

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from .config import TOKEN_FILE

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
MAX_MESSAGES_PER_PASS = 200


def get_service():
    if not TOKEN_FILE.exists():
        raise SystemExit(
            "No Gmail token found. Run the setup wizard first:\n"
            "  python -m gmail_labeler setup"
        )
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as e:
            detail = e.args[0] if e.args else e
            raise SystemExit(
                f"Gmail authorization is no longer valid ({detail}).\n"
                "Re-authorize by running setup again:\n"
                "  ./run.sh setup   (answer 'y' when asked to re-authorize)\n\n"
                "If this keeps happening every ~7 days, your Google OAuth consent\n"
                "screen is in 'Testing' mode, which expires refresh tokens weekly.\n"
                "Publish it to 'In production' (APIs & Services > OAuth consent\n"
                "screen > Publish app) to get a long-lived token."
            ) from e
        TOKEN_FILE.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def list_message_ids(
    service, query: str, max_messages: int | None = MAX_MESSAGES_PER_PASS
) -> list[str]:
    """List message IDs matching the query; max_messages=None pages through all."""
    ids: list[str] = []
    page_token = None
    while max_messages is None or len(ids) < max_messages:
        resp = (
            service.users()
            .messages()
            .list(userId="me", q=query, maxResults=500, pageToken=page_token)
            .execute()
        )
        ids.extend(m["id"] for m in resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids if max_messages is None else ids[:max_messages]


def _decode_part_data(data: str) -> str:
    return base64.urlsafe_b64decode(data + "===").decode("utf-8", errors="replace")


def _extract_text(payload: dict) -> str:
    """Walk the MIME tree, preferring text/plain; fall back to stripped HTML."""
    plain, html_text = [], []

    def walk(part):
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data:
            if mime == "text/plain":
                plain.append(_decode_part_data(data))
            elif mime == "text/html":
                html_text.append(_decode_part_data(data))
        for child in part.get("parts", []):
            walk(child)

    walk(payload)
    if plain:
        return "\n".join(plain)
    if html_text:
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", "\n".join(html_text),
                      flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        return re.sub(r"\s+", " ", html.unescape(text))
    return ""


def get_message(service, message_id: str) -> dict:
    msg = (
        service.users()
        .messages()
        .get(userId="me", id=message_id, format="full")
        .execute()
    )
    headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
    body = _extract_text(msg["payload"]) or msg.get("snippet", "")
    received = datetime.fromtimestamp(
        int(msg["internalDate"]) / 1000, tz=timezone.utc
    ).date().isoformat()
    return {
        "id": message_id,
        "sender": headers.get("from", ""),
        "subject": headers.get("subject", ""),
        "date": received,
        "body": body,
    }


def ensure_labels(service, label_names: list[str]) -> dict[str, str]:
    """Create any missing labels; return a name -> label ID map."""
    existing = service.users().labels().list(userId="me").execute().get("labels", [])
    name_to_id = {lab["name"]: lab["id"] for lab in existing}
    for name in label_names:
        if name in name_to_id:
            continue
        created = (
            service.users()
            .labels()
            .create(
                userId="me",
                body={
                    "name": name,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                },
            )
            .execute()
        )
        name_to_id[name] = created["id"]
        log.info("Created Gmail label: %s", name)
    return name_to_id


def apply_labels(
    service,
    message_id: str,
    add_label_ids: list[str],
    remove_label_ids: list[str] | None = None,
) -> None:
    body: dict = {"addLabelIds": add_label_ids}
    if remove_label_ids:
        body["removeLabelIds"] = remove_label_ids  # e.g. ["INBOX"] to archive
    service.users().messages().modify(userId="me", id=message_id, body=body).execute()
