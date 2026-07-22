"""Provider-agnostic LLM layer.

Every model call in the labeler goes through here, so the rest of the code
never imports a vendor SDK. Two backends cover everything:

  * OpenAICompatibleClient - any OpenAI-shaped endpoint (OpenRouter, OpenAI,
    Google, Mistral, a local OSS server). Switching between them is just a
    base_url + key + model string.
  * AnthropicClient - Anthropic's native API, the only backend with a Batch
    API (used by `backfill`).

Callers catch the normalized LLM* exceptions below rather than SDK-specific
ones. Structured calls always request a strict JSON schema; an unparseable
response comes back as None and is treated as "leave the email alone".
"""

import json
import logging
import sys
import time
from dataclasses import dataclass
from typing import NamedTuple

from . import config as cfg

log = logging.getLogger(__name__)

MAX_TOKENS = 512
BATCH_POLL_SECONDS = 30


# --- Normalized errors -----------------------------------------------------
# Backends translate their SDK's exceptions into these so poller/backfill
# don't depend on which provider is configured.

class LLMError(Exception):
    """Any error talking to the model provider."""


class LLMConnectionError(LLMError):
    """Network-level failure; the caller should retry later."""


class LLMRateLimitError(LLMError):
    """Provider rate limit hit; the caller should back off."""


class BatchResult(NamedTuple):
    custom_id: str
    ok: bool          # False == the provider failed this request (retry it)
    data: dict | None  # parsed JSON, or None if the response was unusable


# --- Provider registry -----------------------------------------------------

@dataclass(frozen=True)
class Provider:
    backend: str            # "openai" | "anthropic"
    key_env: str            # environment variable holding the API key
    supports_batch: bool
    base_url: str | None = None


PROVIDERS: dict[str, Provider] = {
    "openrouter": Provider("openai", "OPENROUTER_API_KEY", False,
                           "https://openrouter.ai/api/v1"),
    "openai": Provider("openai", "OPENAI_API_KEY", False),
    "google": Provider("openai", "GEMINI_API_KEY", False,
                       "https://generativelanguage.googleapis.com/v1beta/openai/"),
    "mistral": Provider("openai", "MISTRAL_API_KEY", False,
                        "https://api.mistral.ai/v1"),
    "anthropic": Provider("anthropic", "ANTHROPIC_API_KEY", True),
}


def _json_schema_format(schema: dict) -> dict:
    return {"type": "json_schema",
            "json_schema": {"name": "result", "strict": True, "schema": schema}}


# --- OpenAI-compatible backend ---------------------------------------------

class OpenAICompatibleClient:
    supports_batch = False

    def __init__(self, base_url: str | None, api_key: str, model: str):
        import openai
        self._openai = openai
        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = openai.OpenAI(**kwargs)
        self.model = model

    def _create(self, system_prompt: str, schema: dict, content: str):
        try:
            return self._client.chat.completions.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": content},
                ],
                response_format=_json_schema_format(schema),
            )
        except self._openai.APIConnectionError as e:
            raise LLMConnectionError(str(e)) from e
        except self._openai.RateLimitError as e:
            raise LLMRateLimitError(str(e)) from e
        except self._openai.APIError as e:
            raise LLMError(str(e)) from e

    def structured(self, system_prompt: str, schema: dict, content: str) -> dict | None:
        response = self._create(system_prompt, schema, content)
        choice = response.choices[0]
        if choice.finish_reason not in (None, "stop"):
            log.warning("Unexpected finish_reason %r", choice.finish_reason)
            return None
        text = choice.message.content
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            log.warning("Model returned non-JSON structured output: %.120r", text)
            return None

    def validate_key(self) -> None:
        """Cheap end-to-end auth check for the setup wizard (no tokens spent)."""
        try:
            self._client.models.list()
        except self._openai.AuthenticationError as e:
            raise LLMError("that API key was rejected") from e
        except self._openai.APIError as e:
            # Some OpenAI-compatible endpoints don't expose /models; don't block
            # setup on a non-auth failure.
            log.warning("Could not fully validate the key (%s); continuing", e)


# --- Anthropic backend -----------------------------------------------------

class AnthropicClient:
    supports_batch = True

    def __init__(self, api_key: str, model: str):
        import anthropic
        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    @staticmethod
    def _parse(message) -> dict | None:
        if message.stop_reason != "end_turn":
            log.warning("Unexpected stop_reason %r", message.stop_reason)
            return None
        text = next((b.text for b in message.content if b.type == "text"), None)
        return json.loads(text) if text else None

    def structured(self, system_prompt: str, schema: dict, content: str) -> dict | None:
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=system_prompt,
                messages=[{"role": "user", "content": content}],
                output_config={"format": {"type": "json_schema", "schema": schema}},
            )
        except self._anthropic.APIConnectionError as e:
            raise LLMConnectionError(str(e)) from e
        except self._anthropic.RateLimitError as e:
            raise LLMRateLimitError(str(e)) from e
        except self._anthropic.APIError as e:
            raise LLMError(str(e)) from e
        return self._parse(response)

    def batch_structured(self, items: list[tuple], label: str) -> list[BatchResult]:
        """Submit one Batch API job and block until it ends.

        items: (custom_id, system_prompt, schema, content) tuples.
        Note: once submitted, tokens are billed even if interrupted.
        """
        from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
        from anthropic.types.messages.batch_create_params import Request

        requests = [
            Request(
                custom_id=custom_id,
                params=MessageCreateParamsNonStreaming(
                    model=self.model,
                    max_tokens=MAX_TOKENS,
                    system=system_prompt,
                    messages=[{"role": "user", "content": content}],
                    output_config={"format": {"type": "json_schema", "schema": schema}},
                ),
            )
            for custom_id, system_prompt, schema, content in items
        ]
        batch = self._client.messages.batches.create(requests=requests)
        log.info("%s batch %s submitted (%d requests, billed at 50%% of standard price)",
                 label, batch.id, len(requests))
        while True:
            b = self._client.messages.batches.retrieve(batch.id)
            if b.processing_status == "ended":
                c = b.request_counts
                log.info("%s batch ended: %d succeeded, %d errored, %d canceled, %d expired",
                         label, c.succeeded, c.errored, c.canceled, c.expired)
                break
            log.info("%s batch %s: %d still processing...",
                     label, batch.id, b.request_counts.processing)
            time.sleep(BATCH_POLL_SECONDS)

        results = []
        for r in self._client.messages.batches.results(batch.id):
            if r.result.type != "succeeded":
                results.append(BatchResult(r.custom_id, False, None))
            else:
                results.append(BatchResult(r.custom_id, True, self._parse(r.result.message)))
        return results

    def validate_key(self) -> None:
        try:
            self._client.messages.count_tokens(
                model=self.model,
                messages=[{"role": "user", "content": "ping"}],
            )
        except self._anthropic.AuthenticationError as e:
            raise LLMError("that API key was rejected") from e


# --- Factory ---------------------------------------------------------------

def build_client(config: dict):
    """Construct the LLM client for the configured provider, resolving its key."""
    name = config.get("provider", cfg.DEFAULT_PROVIDER)
    provider = PROVIDERS.get(name)
    if provider is None:
        sys.exit(f"Unknown provider {name!r}. Choose one of: {', '.join(PROVIDERS)}")
    model = config.get("model", cfg.DEFAULT_MODEL)
    api_key = cfg.resolve_api_key(config, name, provider.key_env)
    if provider.backend == "anthropic":
        return AnthropicClient(api_key, model)
    return OpenAICompatibleClient(provider.base_url, api_key, model)
