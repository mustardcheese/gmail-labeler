"""Network-free tests for the model-answer cache. Run: .venv/bin/python tests/test_cache.py

Covers the behaviour a dry run depends on: an answer stored once is replayed
without a second model call, and the fingerprint invalidates it when the
prompt, schema, or provider/model changes.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gmail_labeler import state  # noqa: E402

SCHEMA = {"type": "object", "properties": {"category": {"type": "string"}}}
ANSWER = {"category": "jobs", "subcategory": "confirmations", "confidence": "high"}


def _conn(tmp: str):
    """A state DB in a temp dir, so tests never touch ~/.config."""
    state.STATE_DB = Path(tmp) / "state.db"
    return state.connect()


def test_roundtrip_and_miss():
    orig = state.STATE_DB
    with tempfile.TemporaryDirectory() as d:
        try:
            conn = _conn(d)
            fp = state.fingerprint("prompt", SCHEMA, "openrouter", "gpt-oss-120b")
            assert state.cache_get(conn, "msg1", state.KIND_CLASSIFY, fp) is None
            state.cache_put(conn, "msg1", state.KIND_CLASSIFY, fp, ANSWER)
            assert state.cache_get(conn, "msg1", state.KIND_CLASSIFY, fp) == ANSWER
            # Different message, and different kind for the same message, miss.
            assert state.cache_get(conn, "msg2", state.KIND_CLASSIFY, fp) is None
            assert state.cache_get(conn, "msg1", state.KIND_EXTRACT, fp) is None
        finally:
            state.STATE_DB = orig


def test_fingerprint_invalidates():
    """Editing the prompt or switching model must not replay a stale answer."""
    base = state.fingerprint("prompt", SCHEMA, "openrouter", "gpt-oss-120b")
    assert state.fingerprint("prompt", SCHEMA, "openrouter", "gpt-oss-120b") == base
    assert state.fingerprint("PROMPT!", SCHEMA, "openrouter", "gpt-oss-120b") != base
    assert state.fingerprint("prompt", {"type": "string"}, "openrouter", "gpt-oss-120b") != base
    assert state.fingerprint("prompt", SCHEMA, "anthropic", "gpt-oss-120b") != base
    assert state.fingerprint("prompt", SCHEMA, "openrouter", "claude-haiku-4-5") != base
    # Delimited, so concatenation across fields can't collide.
    assert state.fingerprint("ab", "c") != state.fingerprint("a", "bc")
    # Schema hashing is key-order independent, so dict ordering isn't a miss.
    assert (state.fingerprint({"a": 1, "b": 2}) == state.fingerprint({"b": 2, "a": 1}))


def test_stale_fingerprint_is_a_miss_then_overwrites():
    orig = state.STATE_DB
    with tempfile.TemporaryDirectory() as d:
        try:
            conn = _conn(d)
            old = state.fingerprint("v1", SCHEMA, "openrouter", "m")
            new = state.fingerprint("v2", SCHEMA, "openrouter", "m")
            state.cache_put(conn, "msg1", state.KIND_CLASSIFY, old, ANSWER)
            assert state.cache_get(conn, "msg1", state.KIND_CLASSIFY, new) is None
            # Re-caching under the new fingerprint replaces the row rather than
            # accumulating one per prompt revision.
            state.cache_put(conn, "msg1", state.KIND_CLASSIFY, new, {"category": "other"})
            rows = conn.execute(
                "SELECT COUNT(*) FROM llm_cache WHERE message_id = 'msg1'"
            ).fetchone()[0]
            assert rows == 1
            assert state.cache_get(conn, "msg1", state.KIND_CLASSIFY, old) is None
        finally:
            state.STATE_DB = orig


def test_get_or_compute_calls_model_once():
    """The dry-run payoff: second look-up is a cache hit, model not re-called."""
    orig = state.STATE_DB
    with tempfile.TemporaryDirectory() as d:
        try:
            conn = _conn(d)
            fp = state.fingerprint("prompt", SCHEMA, "openrouter", "m")
            calls = []

            def compute():
                calls.append(1)
                return ANSWER

            result, hit = state.get_or_compute(
                conn, "msg1", state.KIND_CLASSIFY, fp, True, compute)
            assert (result, hit, len(calls)) == (ANSWER, False, 1)

            result, hit = state.get_or_compute(
                conn, "msg1", state.KIND_CLASSIFY, fp, True, compute)
            assert (result, hit, len(calls)) == (ANSWER, True, 1)  # no second call

            # --no-cache forces a fresh call even on a warm entry.
            result, hit = state.get_or_compute(
                conn, "msg1", state.KIND_CLASSIFY, fp, False, compute)
            assert (result, hit, len(calls)) == (ANSWER, False, 2)
        finally:
            state.STATE_DB = orig


def test_get_or_compute_does_not_cache_unusable_answers():
    """A None answer must be retried next pass, not frozen into the cache."""
    orig = state.STATE_DB
    with tempfile.TemporaryDirectory() as d:
        try:
            conn = _conn(d)
            fp = state.fingerprint("prompt", SCHEMA, "openrouter", "m")
            result, hit = state.get_or_compute(
                conn, "msg1", state.KIND_CLASSIFY, fp, True, lambda: None)
            assert (result, hit) == (None, False)
            assert conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0] == 0
        finally:
            state.STATE_DB = orig


def test_get_or_compute_propagates_errors():
    """Rate-limit/network handling in the poller relies on these escaping."""
    orig = state.STATE_DB
    with tempfile.TemporaryDirectory() as d:
        try:
            conn = _conn(d)
            fp = state.fingerprint("prompt", SCHEMA, "openrouter", "m")

            def boom():
                raise RuntimeError("rate limited")

            try:
                state.get_or_compute(conn, "msg1", state.KIND_CLASSIFY, fp, True, boom)
            except RuntimeError:
                assert conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0] == 0
                return
            raise AssertionError("expected the compute error to propagate")
        finally:
            state.STATE_DB = orig


def test_cache_is_independent_of_processed():
    """A dry run caches answers without marking anything processed."""
    orig = state.STATE_DB
    with tempfile.TemporaryDirectory() as d:
        try:
            conn = _conn(d)
            fp = state.fingerprint("prompt", SCHEMA, "openrouter", "m")
            state.cache_put(conn, "msg1", state.KIND_CLASSIFY, fp, ANSWER)
            assert state.is_processed(conn, "msg1") is False
            state.mark_processed(conn, "msg1", "jobs", "confirmations", "high")
            assert state.is_processed(conn, "msg1") is True
            assert state.cache_get(conn, "msg1", state.KIND_CLASSIFY, fp) == ANSWER
        finally:
            state.STATE_DB = orig


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
