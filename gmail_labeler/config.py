"""Configuration, credential storage, and category definitions.

All secrets live in ~/.config/gmail-labeler/ (mode 0600), never in the repo.
The provider's API key is resolved in this order:
  1. the provider's key environment variable (e.g. OPENROUTER_API_KEY)
  2. the saved config file (key "<provider>_api_key")
  3. an interactive prompt (with an offer to save it for next time)

Which provider/model to use lives in the config as "provider" and "model";
the provider registry (env var names, base URLs) is in gmail_labeler/llm.py.
"""

import getpass
import json
import os
import stat
import sys
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "gmail-labeler"
CONFIG_FILE = CONFIG_DIR / "config.json"
CLIENT_SECRET_FILE = CONFIG_DIR / "client_secret.json"
TOKEN_FILE = CONFIG_DIR / "token.json"
STATE_DB = CONFIG_DIR / "state.db"

DEFAULT_PROVIDER = "openrouter"
DEFAULT_MODEL = "openai/gpt-oss-120b"
DEFAULT_QUERY = "in:inbox newer_than:3d"

# Categories and their prompts live in prompts/categories.md (parsed by
# gmail_labeler/prompts.py), not here.


def _write_private(path: Path, text: str) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600


def save_config(config: dict) -> None:
    _write_private(CONFIG_FILE, json.dumps(config, indent=2))


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        sys.exit(
            "No configuration found. Run the setup wizard first:\n"
            "  python -m gmail_labeler setup"
        )
    return json.loads(CONFIG_FILE.read_text())


def clear_api_keys() -> list[str]:
    """Remove every stored '<provider>_api_key' entry from the config file.

    Leaves all other settings (provider, model, query, ...) untouched. Returns
    the config keys that were removed. Does not touch Google OAuth or env vars.
    """
    if not CONFIG_FILE.exists():
        return []
    config = json.loads(CONFIG_FILE.read_text())
    removed = [k for k in list(config) if k.endswith("_api_key")]
    if removed:
        for k in removed:
            config.pop(k)
        save_config(config)
    return removed


def resolve_api_key(config: dict, provider_name: str, key_env: str) -> str:
    """Resolve the provider's key: env var -> config file -> interactive prompt."""
    config_key = f"{provider_name}_api_key"
    key = os.environ.get(key_env) or config.get(config_key)
    if key:
        return key
    print(f"No API key found for provider {provider_name!r} (env {key_env} or config).")
    key = getpass.getpass(f"Enter your {provider_name} API key (input hidden): ").strip()
    if not key:
        sys.exit(f"An API key is required for provider {provider_name!r}.")
    if input("Save it to the config file for next time? [Y/n] ").strip().lower() != "n":
        config[config_key] = key
        save_config(config)
    return key
