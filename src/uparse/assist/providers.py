"""The four ways UniParse can reach a model.

Free tiers and local models come first; paid APIs are supported and never required. Most of
the ecosystem speaks OpenAI-compatible chat completions, so one driver covers Gemini's free
tier, Ollama, LM Studio, OpenRouter and Groq alike. `command` covers everything else that
can be run in a terminal — opencode, `llm`, `ollama run` — without UniParse knowing anything
about it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

import httpx

from uparse.assist.protocol import AssistError, Provider, Tier
from uparse.config.schema import AssistConfig
from uparse.logging import get_logger

log = get_logger("assist")

DEFAULT_TIMEOUT_S = 60.0
MAX_OUTPUT_TOKENS = 2000


class NullProvider:
    """No model. Generation still runs and says what it could not do."""

    name = "null"
    tier = Tier.TEXT

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> str:
        del system, user, schema
        raise AssistError("no assist provider configured (set assist.provider)")


class OpenAICompatibleProvider:
    """Anything speaking POST {base_url}/chat/completions.

    Verified against Gemini's compatibility layer at
    https://generativelanguage.googleapis.com/v1beta/openai/ — which does honour
    json_schema — and against Ollama at http://localhost:11434/v1, which does not.
    """

    def __init__(self, config: AssistConfig) -> None:
        if not config.base_url:
            raise AssistError("assist.base_url is required for the openai_compatible provider")
        self.name = f"openai_compatible:{config.model}"
        self.tier = Tier(config.tier)
        self.model = config.model
        self.base_url = config.base_url.rstrip("/")
        self.timeout_s = config.timeout_s
        self.api_key = _key_from_env(config.api_key_env)

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if self.tier is Tier.SCHEMA:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "uparse_proposal", "schema": schema, "strict": True},
            }
        elif self.tier is Tier.JSON:
            payload["response_format"] = {"type": "json_object"}

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            response = httpx.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=headers,
                timeout=self.timeout_s,
            )
        except httpx.HTTPError as exc:
            raise AssistError(f"{self.name}: {exc}") from exc

        if response.status_code == 429:
            raise AssistError(f"{self.name}: rate limited by the provider (HTTP 429)")
        if response.status_code >= 400:
            raise AssistError(f"{self.name}: HTTP {response.status_code} {response.text[:200]}")
        return _first_choice(response.json(), self.name)


class CommandProvider:
    """Shell out to a CLI that already has its own credentials.

    `opencode run "<prompt>" --model provider/model --format json` is the motivating case,
    but anything that reads a prompt and writes an answer works. The prompt goes on stdin
    unless the argv contains the {prompt} placeholder.
    """

    PLACEHOLDER = "{prompt}"

    def __init__(self, config: AssistConfig) -> None:
        if not config.command:
            raise AssistError("assist.command is required for the command provider")
        binary = config.command[0]
        if shutil.which(binary) is None:
            raise AssistError(f"{binary!r} is not on PATH")
        self.name = f"command:{binary}"
        self.tier = Tier(config.tier)
        self.argv = list(config.command)
        self.timeout_s = config.timeout_s

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> str:
        del schema  # a CLI takes prose, not a response_format
        prompt = f"{system}\n\n{user}"
        argv = [a.replace(self.PLACEHOLDER, prompt) for a in self.argv]
        stdin = None if any(self.PLACEHOLDER in a for a in self.argv) else prompt
        try:
            done = subprocess.run(
                argv,
                input=stdin,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AssistError(f"{self.name}: {exc}") from exc
        if done.returncode != 0:
            detail = (done.stderr or done.stdout or "").strip()[:200]
            raise AssistError(f"{self.name}: exit {done.returncode} {detail}")
        return done.stdout


class AnthropicProvider:
    """The paid path. Optional dependency; absent unless `uniparse[llm]` is installed."""

    def __init__(self, config: AssistConfig) -> None:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - depends on the optional extra
            raise AssistError("the anthropic provider needs `pip install 'uniparse[llm]'`") from exc
        self.name = f"anthropic:{config.model}"
        self.tier = Tier.SCHEMA
        self.model = config.model
        self.timeout_s = config.timeout_s
        key = _key_from_env(config.api_key_env or "ANTHROPIC_API_KEY")
        self._client = anthropic.Anthropic(**({"api_key": key} if key else {}))

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> str:
        try:
            message = self._client.messages.create(
                model=self.model,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": schema},
                },
                timeout=self.timeout_s,
            )
        except Exception as exc:  # the SDK raises its own hierarchy
            raise AssistError(f"{self.name}: {exc}") from exc
        return "".join(b.text for b in message.content if getattr(b, "type", "") == "text")


def build(config: AssistConfig) -> Provider:
    match config.provider:
        case "openai_compatible":
            return OpenAICompatibleProvider(config)
        case "command":
            return CommandProvider(config)
        case "anthropic":
            return AnthropicProvider(config)
        case _:
            return NullProvider()


def _key_from_env(name: str | None) -> str:
    """Keys live in the environment. A config file never holds one."""
    if not name:
        return ""
    value = os.environ.get(name, "")
    if not value:
        log.debug("environment variable %s is unset", name)
    return value


def _first_choice(payload: dict[str, Any], name: str) -> str:
    choices = payload.get("choices") or []
    if not choices:
        raise AssistError(f"{name}: no choices in the response")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):  # some servers return content parts
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if not isinstance(content, str) or not content.strip():
        raise AssistError(f"{name}: empty completion — {json.dumps(payload)[:200]}")
    return content
