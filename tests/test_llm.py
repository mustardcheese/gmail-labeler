"""Network-free tests for the provider layer. Run: .venv/bin/python tests/test_llm.py

Covers the wiring that switching providers depends on: the registry, backend
selection in build_client, and API-key resolution precedence. The actual model
calls (structured/validate_key/batch) need a real key and are exercised via
`run --dry-run`, not here.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gmail_labeler import config as cfg, llm  # noqa: E402


def test_registry_shape():
    assert cfg.DEFAULT_PROVIDER in llm.PROVIDERS
    for name, p in llm.PROVIDERS.items():
        assert p.backend in ("openai", "anthropic"), name
        assert p.key_env, name
    # Only Anthropic offers batch today; that's what backfill gates on.
    assert llm.PROVIDERS["anthropic"].supports_batch is True
    assert all(not p.supports_batch for n, p in llm.PROVIDERS.items() if n != "anthropic")


def test_build_client_selects_backend():
    oa = llm.build_client({"provider": "openrouter", "model": "openai/gpt-oss-120b",
                           "openrouter_api_key": "sk-test"})
    assert isinstance(oa, llm.OpenAICompatibleClient)
    assert oa.model == "openai/gpt-oss-120b"
    assert oa.supports_batch is False

    an = llm.build_client({"provider": "anthropic", "model": "claude-haiku-4-5",
                           "anthropic_api_key": "sk-test"})
    assert isinstance(an, llm.AnthropicClient)
    assert an.supports_batch is True


def test_build_client_defaults():
    os.environ["OPENROUTER_API_KEY"] = "sk-env"
    try:
        client = llm.build_client({})  # no provider/model -> defaults
        assert isinstance(client, llm.OpenAICompatibleClient)
        assert client.model == cfg.DEFAULT_MODEL
    finally:
        del os.environ["OPENROUTER_API_KEY"]


def test_build_client_unknown_provider_exits():
    try:
        llm.build_client({"provider": "nope"})
    except SystemExit:
        return
    raise AssertionError("expected SystemExit for unknown provider")


def test_resolve_api_key_precedence():
    # env var wins over config
    os.environ["OPENROUTER_API_KEY"] = "sk-env"
    try:
        assert cfg.resolve_api_key({"openrouter_api_key": "sk-config"},
                                   "openrouter", "OPENROUTER_API_KEY") == "sk-env"
    finally:
        del os.environ["OPENROUTER_API_KEY"]
    # config used when env absent
    assert cfg.resolve_api_key({"openrouter_api_key": "sk-config"},
                               "openrouter", "OPENROUTER_API_KEY") == "sk-config"


def test_api_key_control_characters_are_dropped():
    """A stray keypress at the hidden prompt (e.g. ESC) must not break auth."""
    assert cfg.clean_api_key("  sk-or-abc\x1b\n") == "sk-or-abc"
    # Keys already saved with one are cleaned on read, too.
    assert cfg.resolve_api_key({"openrouter_api_key": "sk-config\x1b"},
                               "openrouter", "OPENROUTER_API_KEY") == "sk-config"


def test_clear_api_keys():
    import json
    import tempfile
    orig_dir, orig_file = cfg.CONFIG_DIR, cfg.CONFIG_FILE
    with tempfile.TemporaryDirectory() as d:
        cfg.CONFIG_DIR = Path(d)
        cfg.CONFIG_FILE = Path(d) / "config.json"
        try:
            cfg.CONFIG_FILE.write_text(json.dumps({
                "provider": "openrouter", "model": "openai/gpt-oss-120b",
                "openrouter_api_key": "sk-a", "anthropic_api_key": "sk-b",
                "query": "in:inbox",
            }))
            removed = cfg.clear_api_keys()
            assert sorted(removed) == ["anthropic_api_key", "openrouter_api_key"]
            left = json.loads(cfg.CONFIG_FILE.read_text())
            assert left == {"provider": "openrouter", "model": "openai/gpt-oss-120b",
                            "query": "in:inbox"}
            # Idempotent: clearing again removes nothing.
            assert cfg.clear_api_keys() == []
        finally:
            cfg.CONFIG_DIR, cfg.CONFIG_FILE = orig_dir, orig_file


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
