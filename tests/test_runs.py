"""Network-free tests for run modes and the last-checked point.
Run: .venv/bin/python tests/test_runs.py

Drives poller.run_once end to end against fake Gmail and model clients (the
real prompts/categories.md is used), covering what "never miss mail between
infrequent runs" depends on: the search window starts at the last complete
run, and only a complete, untargeted, real run moves it forward.
"""

import contextlib
import sys
import tempfile
import time
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gmail_labeler import (  # noqa: E402
    classifier, config as cfg, gmail_client, llm, poller, state,
)

JOBS = {"category": "jobs", "subcategory": "interviews", "confidence": "high"}
SECURITY = {"category": "security", "subcategory": None, "confidence": "high"}
OTHER = {"category": "other", "subcategory": None, "confidence": "high"}


class FakeClient:
    """Answers by message id (the fake emails use the id as their subject)."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def structured(self, system_prompt, schema, content):
        if schema is classifier.EXTRACTION_SCHEMA:
            return {k: None for k in schema["required"]}
        mid = content.split("Subject: ", 1)[1].split("\n", 1)[0]
        self.calls.append(mid)
        answer = self.answers[mid]
        if isinstance(answer, Exception):
            raise answer
        return answer


class Harness:
    def __init__(self, tmp, answers, ids=None):
        self.tmp = Path(tmp)
        self.client = FakeClient(answers)
        self.ids = list(answers) if ids is None else ids  # newest first, like Gmail
        self.queries = []
        self.labeled = []

    def list_message_ids(self, service, query, max_messages=None):
        self.queries.append(query)
        return list(self.ids)

    def get_message(self, service, mid):
        return {"id": mid, "sender": "a@b.c", "subject": mid, "date": "2026-10-01",
                "body": "hi"}

    def apply_labels(self, service, mid, add, remove=None):
        self.labeled.append(mid)


@contextlib.contextmanager
def harness(answers, ids=None):
    """Patch the Gmail/LLM/config edges of run_once; state DB lives in a temp dir."""
    with tempfile.TemporaryDirectory() as d:
        h = Harness(d, answers, ids)
        config = {"provider": "openrouter", "model": "m", "query": "in:inbox",
                  "skip_low_confidence": True,
                  "job_tracker_csv": str(h.tmp / "jobs.csv")}
        patches = [
            (state, "STATE_DB", h.tmp / "state.db"),
            (cfg, "load_config", lambda: dict(config)),
            (llm, "build_client", lambda c: h.client),
            (gmail_client, "get_service", lambda: object()),
            (gmail_client, "ensure_labels", lambda s, names: {n: n for n in names}),
            (gmail_client, "list_message_ids", h.list_message_ids),
            (gmail_client, "get_message", h.get_message),
            (gmail_client, "apply_labels", h.apply_labels),
        ]
        saved = [(obj, name, getattr(obj, name)) for obj, name, _ in patches]
        for obj, name, value in patches:
            setattr(obj, name, value)
        try:
            h.conn = state.connect()
            yield h
        finally:
            for obj, name, value in saved:
                setattr(obj, name, value)


def _seed_last_checked(h, ts):
    state.set_last_checked(h.conn, ts)


def _expect_exit(fn):
    try:
        fn()
    except SystemExit:
        return
    raise AssertionError("expected SystemExit")


# --- state -----------------------------------------------------------------

def test_last_checked_roundtrip_and_fallback():
    with harness({}) as h:
        assert state.get_last_checked(h.conn) is None
        # Installs from before this existed fall back to their first run.
        h.conn.execute("INSERT INTO processed (message_id, processed_at)"
                       " VALUES ('a', '2026-07-08 19:33:06'), ('b', '2026-09-24 10:00:00')")
        h.conn.commit()
        expected = int(datetime.fromisoformat("2026-07-08 19:33:06+00:00").timestamp())
        assert state.get_last_checked(h.conn) == expected
        state.set_last_checked(h.conn, 1_800_000_000)
        assert state.get_last_checked(h.conn) == 1_800_000_000


def test_build_query():
    assert poller.build_query("in:inbox", None, None) == "in:inbox"
    assert poller.build_query("in:inbox", 100, None) == "in:inbox after:100"
    assert poller.build_query("in:inbox", 100, 200) == "in:inbox after:100 before:200"
    assert poller.build_query("", None, 200) == "before:200"


# --- infrequent: plain run -------------------------------------------------

def test_fresh_install_requires_all_or_since():
    with harness({"m1": JOBS}) as h:
        _expect_exit(poller.run_once)
        assert h.client.calls == []


def test_run_searches_from_last_checked_and_advances():
    with harness({"m1": JOBS, "m2": SECURITY, "m3": OTHER}) as h:
        _seed_last_checked(h, 1_000_000)
        started = int(time.time())
        poller.run_once()
        assert h.queries == [f"in:inbox after:{1_000_000 - poller.OVERLAP_SECONDS}"]
        assert h.labeled == ["m1", "m2"]  # "other" stays in the inbox, unlabeled
        assert all(state.is_processed(h.conn, m) for m in ("m1", "m2", "m3"))
        assert state.get_last_checked(h.conn) >= started


def test_run_processes_more_than_one_old_cap():
    """A long gap is caught up in one run, not 200 messages at a time."""
    answers = {f"m{i}": OTHER for i in range(450)}
    with harness(answers) as h:
        _seed_last_checked(h, 1_000_000)
        poller.run_once()
        assert len(h.client.calls) == 450


def test_already_processed_mail_is_skipped():
    with harness({"m1": JOBS, "m2": SECURITY}) as h:
        _seed_last_checked(h, 1_000_000)
        state.mark_processed(h.conn, "m1", "jobs", "interviews", "high")
        poller.run_once()
        assert h.client.calls == ["m2"]


def test_rate_limit_keeps_last_checked():
    answers = {"m1": JOBS, "m2": llm.LLMRateLimitError("429"), "m3": SECURITY}
    with harness(answers) as h:
        _seed_last_checked(h, 1_000_000)
        poller.run_once()
        assert h.client.calls == ["m1", "m2"]  # stops at the rate limit
        assert state.get_last_checked(h.conn) == 1_000_000


def test_model_error_skips_message_and_keeps_last_checked():
    answers = {"m1": llm.LLMError("400 bad request"), "m2": SECURITY}
    with harness(answers) as h:
        _seed_last_checked(h, 1_000_000)
        poller.run_once()  # must not crash
        assert h.labeled == ["m2"]
        assert not state.is_processed(h.conn, "m1")
        assert state.get_last_checked(h.conn) == 1_000_000


def test_network_error_keeps_last_checked():
    answers = {"m1": llm.LLMConnectionError("reset"), "m2": SECURITY}
    with harness(answers) as h:
        _seed_last_checked(h, 1_000_000)
        poller.run_once()
        assert h.labeled == ["m2"]
        assert state.get_last_checked(h.conn) == 1_000_000


def test_dry_run_keeps_last_checked():
    with harness({"m1": JOBS}) as h:
        _seed_last_checked(h, 1_000_000)
        poller.run_once(dry_run=True)
        assert h.labeled == []
        assert state.get_last_checked(h.conn) == 1_000_000


# --- startup: --all ----------------------------------------------------------

def test_all_searches_whole_inbox_and_sets_last_checked():
    with harness({"m1": JOBS, "m2": OTHER}) as h:
        started = int(time.time())
        poller.run_once(all_mail=True)  # works on a fresh install
        assert h.queries == ["in:inbox"]
        assert state.get_last_checked(h.conn) >= started


# --- specific: --since/--until/--limit/--only ---------------------------------

def test_since_until_window_and_keeps_last_checked():
    with harness({"m1": JOBS}) as h:
        _seed_last_checked(h, 1_000_000)
        poller.run_once(since=date(2026, 9, 1), until=date(2026, 9, 30))
        after = int(datetime(2026, 9, 1).timestamp())
        before = int(datetime(2026, 10, 1).timestamp())  # --until is inclusive
        assert h.queries == [f"in:inbox after:{after} before:{before}"]
        assert h.labeled == ["m1"]
        assert state.get_last_checked(h.conn) == 1_000_000


def test_limit_takes_newest_unprocessed():
    with harness({"m1": JOBS, "m2": SECURITY, "m3": OTHER}) as h:
        _seed_last_checked(h, 1_000_000)
        state.mark_processed(h.conn, "m1", "jobs", "interviews", "high")
        poller.run_once(limit=1)
        assert h.client.calls == ["m2"]
        assert state.get_last_checked(h.conn) == 1_000_000


def test_only_acts_on_listed_categories():
    with harness({"m1": JOBS, "m2": SECURITY, "m3": OTHER}) as h:
        _seed_last_checked(h, 1_000_000)
        poller.run_once(only=["jobs"])
        assert h.client.calls == ["m1", "m2", "m3"]  # everything is classified...
        assert h.labeled == ["m1"]                    # ...but only jobs is acted on
        assert state.is_processed(h.conn, "m1")
        assert not state.is_processed(h.conn, "m2")
        assert not state.is_processed(h.conn, "m3")
        assert state.get_last_checked(h.conn) == 1_000_000

        # The skipped ones were cached, so a later full run doesn't re-ask.
        h.client.calls.clear()
        poller.run_once()
        assert h.client.calls == []
        assert h.labeled == ["m1", "m2"]


def test_only_rejects_unknown_category():
    with harness({"m1": JOBS}) as h:
        _seed_last_checked(h, 1_000_000)
        _expect_exit(lambda: poller.run_once(only=["jbos"]))
        assert h.client.calls == []


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
