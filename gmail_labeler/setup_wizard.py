"""Interactive first-run setup: model provider + key, Google OAuth, Gmail labels."""

import getpass
import json
import os
import shutil
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

from . import config as cfg, gmail_client, llm, prompts

GOOGLE_INSTRUCTIONS = """
--- Google Cloud one-time setup -------------------------------------------
The labeler needs an OAuth client to access your own Gmail:

  1. Go to https://console.cloud.google.com/ and create a project
     (any name, e.g. "gmail-labeler").
  2. APIs & Services > Library > enable the "Gmail API".
  3. APIs & Services > OAuth consent screen > External > fill in the
     app name and your email, then click "Publish app" so it is
     "In production". (If you leave it in "Testing", Google expires the
     login after 7 days and you have to re-authorize every week.)
  4. APIs & Services > Credentials > Create Credentials >
     OAuth client ID > Application type: "Desktop app".
  5. Download the JSON file for that client.

At sign-in you'll see an "unverified app" warning (expected for a personal
app) -- click Advanced > "Go to <app> (unsafe)" to continue.
----------------------------------------------------------------------------
"""


def run_clear() -> None:
    """Wipe stored API keys so you can re-enter them, then explain how to restart."""
    removed = cfg.clear_api_keys()
    if removed:
        print("Cleared stored API key(s): " + ", ".join(sorted(removed)))
    else:
        print("No stored API keys were found in the config.")

    # An exported key env var overrides the config, so clearing the file alone
    # won't prompt for a new key while it's still set.
    active = sorted({p.key_env for p in llm.PROVIDERS.values() if os.environ.get(p.key_env)})
    if active:
        print("\nHeads up: these key environment variables are still set and take")
        print("precedence over the config, so unset any you want to replace:")
        for env in active:
            print(f"  unset {env}")

    print("\nTo restart, re-enter your key with:\n  ./run.sh setup")


def _setup_provider(config: dict) -> None:
    providers = ", ".join(llm.PROVIDERS)
    print(f"\nModel provider. Available: {providers}.")
    print("Default routes everything through OpenRouter (one key, any model).")
    provider = input(f"Provider [{cfg.DEFAULT_PROVIDER}]: ").strip() or cfg.DEFAULT_PROVIDER
    if provider not in llm.PROVIDERS:
        sys.exit(f"Unknown provider {provider!r}. Choose one of: {providers}")
    config["provider"] = provider

    default_model = config.get("model", cfg.DEFAULT_MODEL)
    model = input(f"Model [{default_model}]: ").strip() or default_model
    config["model"] = model

    key_env = llm.PROVIDERS[provider].key_env
    if os.environ.get(key_env):
        print(f"Found {key_env} in the environment; it will be used at runtime.")
        if input("Also save a key to the config file? [y/N] ").strip().lower() != "y":
            return
    key = getpass.getpass(f"{provider} API key (input hidden): ").strip()
    if not key:
        sys.exit(f"An API key is required for provider {provider!r}.")
    print("Validating key...", end=" ", flush=True)
    try:
        llm.build_client({"provider": provider, "model": model,
                          f"{provider}_api_key": key}).validate_key()
    except llm.LLMError as e:
        sys.exit(f"failed: {e}. Check it and rerun setup.")
    print("ok.")
    config[f"{provider}_api_key"] = key


def _setup_google_oauth() -> None:
    if not cfg.CLIENT_SECRET_FILE.exists():
        print(GOOGLE_INSTRUCTIONS)
        path = Path(input("Path to the downloaded OAuth client JSON: ").strip()).expanduser()
        if not path.exists():
            sys.exit(f"File not found: {path}")
        cfg.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy(path, cfg.CLIENT_SECRET_FILE)
        cfg.CLIENT_SECRET_FILE.chmod(0o600)
    else:
        print(f"Using existing OAuth client at {cfg.CLIENT_SECRET_FILE}")

    if cfg.TOKEN_FILE.exists():
        if input("A Gmail token already exists. Re-authorize? [y/N] ").strip().lower() != "y":
            return
    print("Opening a browser window for Google sign-in...")
    flow = InstalledAppFlow.from_client_secrets_file(
        str(cfg.CLIENT_SECRET_FILE), gmail_client.SCOPES
    )
    creds = flow.run_local_server(port=0)
    cfg.TOKEN_FILE.write_text(creds.to_json())
    cfg.TOKEN_FILE.chmod(0o600)
    print(f"Gmail authorized; token saved to {cfg.TOKEN_FILE}")


def run_setup() -> None:
    print("gmail-labeler setup\n===================")
    config = json.loads(cfg.CONFIG_FILE.read_text()) if cfg.CONFIG_FILE.exists() else {}
    config.setdefault("provider", cfg.DEFAULT_PROVIDER)
    config.setdefault("model", cfg.DEFAULT_MODEL)
    config.setdefault("query", cfg.DEFAULT_QUERY)
    config.setdefault("skip_low_confidence", True)
    config.setdefault("job_tracker_csv", str(cfg.CONFIG_DIR / "job_applications.csv"))
    config.pop("categories", None)  # legacy: categories now live in prompts/categories.md

    _setup_provider(config)
    _setup_google_oauth()

    print("Creating Gmail labels...")
    service = gmail_client.get_service()
    categories = prompts.load_categories(prompts.prompts_dir(config) / "categories.md")
    gmail_client.ensure_labels(service, prompts.all_label_names(categories))

    cfg.save_config(config)
    print(f"\nDone. Config saved to {cfg.CONFIG_FILE}")
    print("Categories and their prompts are editable in prompts/categories.md.")
    print("\nTry a dry run next:\n  python -m gmail_labeler run --dry-run")
