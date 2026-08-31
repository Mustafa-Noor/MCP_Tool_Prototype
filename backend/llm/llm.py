"""Standalone, swappable LLM client for the ANA log-tools MCP server.

This module intentionally contains no pipeline/business logic. It only exposes a
minimal `LLMClient` protocol, a Google Gemini implementation, and a
`get_default_llm()` factory. Pipeline tools accept an `LLMClient` as a parameter
so the model provider can be swapped without touching pipeline code.

Stdlib-only (urllib) so the module stays dependency-free.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_TIMEOUT_SEC = 30.0

_ENV_CANDIDATE_PATHS = [
    Path(__file__).resolve().with_name(".env"),
    Path(__file__).resolve().parent.parent / ".env",
    # Repo-root .env, one level above MCP_Tool_Prototype.
    Path(__file__).resolve().parents[3] / ".env",
]


def _strip_wrapping_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'", "`"}:
        return value[1:-1]
    return value


def _read_env_value(name: str) -> str | None:
    pattern = re.compile(rf"^\s*{re.escape(name)}\s*=\s*(.*)\s*$")

    for env_path in _ENV_CANDIDATE_PATHS:
        if not env_path.exists():
            continue
        try:
            text = env_path.read_text(encoding="utf-8")
        except OSError:
            continue

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            match = pattern.match(raw_line)
            if not match:
                continue

            value = match.group(1).strip()
            return _strip_wrapping_quotes(value) or None

    return None


def get_api_key() -> str | None:
    """Return the Google AI Studio API key used by the Gemini client."""
    for env_name in ("GOOGLE_API_KEY", "GEMINI_API_KEY"):
        value = os.getenv(env_name) or _read_env_value(env_name)
        if value:
            return value
    return None


def get_request_timeout(fallback: float = DEFAULT_TIMEOUT_SEC) -> float:
    """Return the configured Gemini request timeout in seconds."""
    raw_value = os.getenv("GEMINI_TIMEOUT_SEC") or _read_env_value("GEMINI_TIMEOUT_SEC")
    try:
        return max(1.0, float(raw_value)) if raw_value else fallback
    except (TypeError, ValueError):
        return fallback


def has_llm_configuration() -> bool:
    """Return whether a Gemini API key is configured."""
    return bool(get_api_key())


def _tls_context() -> ssl.SSLContext | None:
    """Use normal certificate verification unless explicitly disabled."""
    raw_value = os.getenv("GEMINI_TLS_VERIFY") or _read_env_value("GEMINI_TLS_VERIFY")
    if raw_value is not None and raw_value.strip().lower() in {"0", "false", "no", "off"}:
        return ssl._create_unverified_context()
    return None


def extract_json_object(raw: str) -> dict[str, Any]:
    """Extract the first JSON object from a raw model response."""
    match = re.search(r"\{.*\}", raw or "", flags=re.DOTALL)
    if not match:
        raise ValueError("LLM did not return JSON")
    parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("LLM response is not a JSON object")
    return parsed


@runtime_checkable
class LLMClient(Protocol):
    """Minimal interface any LLM provider must implement to be swappable."""

    def complete(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.0,
        timeout: float = 30.0,
        model: str | None = None,
    ) -> str:
        """Return the assistant text for a single system+user exchange."""
        ...


class GeminiLLMClient:
    """Google Gemini client over the Generative Language REST API."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = GEMINI_BASE_URL,
        model: str | None = None,
    ) -> None:
        self.model = model or os.getenv("GEMINI_MODEL") or _read_env_value("GEMINI_MODEL") or DEFAULT_MODEL
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key or get_api_key()
        if not self._api_key:
            raise RuntimeError("Missing GOOGLE_API_KEY (or GEMINI_API_KEY) for the Gemini client.")

    @staticmethod
    def _thinking_budget() -> int | None:
        """Return an explicit thinking budget, or None to use the model default."""
        raw = os.getenv("GEMINI_THINKING_BUDGET") or _read_env_value("GEMINI_THINKING_BUDGET")
        try:
            return int(raw) if raw is not None and raw.strip() else None
        except ValueError:
            return None

    def complete(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.0,
        timeout: float = 30.0,
        model: str | None = None,
    ) -> str:
        generation_config: dict[str, Any] = {"temperature": temperature}
        budget = self._thinking_budget()
        if budget is not None:
            generation_config["thinkingConfig"] = {"thinkingBudget": budget}

        payload: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": generation_config,
        }

        request = Request(
            f"{self._base_url}/models/{model or self.model}:generateContent",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "x-goog-api-key": self._api_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout, context=_tls_context()) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            # Gemini returns a JSON body explaining *why* (bad key, quota, bad model
            # name); surfacing it beats a bare "HTTP Error 400: Bad Request".
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace").strip()
            except Exception:  # noqa: BLE001 - best-effort detail only
                pass
            raise RuntimeError(f"Gemini request failed: {exc}{f' — {detail}' if detail else ''}") from exc
        except (URLError, TimeoutError) as exc:
            raise RuntimeError(f"Gemini request failed: {exc}") from exc

        try:
            candidate = result["candidates"][0]
        except (KeyError, IndexError, TypeError) as exc:
            # A prompt blocked by safety filters comes back with no candidates at all.
            blocked = (result.get("promptFeedback") or {}).get("blockReason") if isinstance(result, dict) else None
            reason = f" (prompt blocked: {blocked})" if blocked else ""
            raise RuntimeError(f"Gemini response contained no candidates{reason}.") from exc

        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(part.get("text", "") for part in parts if isinstance(part, dict)).strip()
        if not text:
            # An empty answer is nearly always a truncated response — most often the
            # 2.5 thinking budget eating the whole output allowance.
            raise RuntimeError(
                f"Gemini returned no text (finishReason={candidate.get('finishReason')!r}). "
                "If this is a thinking model, try setting GEMINI_THINKING_BUDGET=0."
            )
        return text


def get_default_llm(api_key: str | None = None) -> LLMClient | None:
    """Return the default Gemini client, or None if no API key is configured."""
    key = api_key or get_api_key()
    if not key:
        return None
    return GeminiLLMClient(api_key=key)


def run_interactive_chat() -> None:
    client = get_default_llm()
    if client is None:
        print("No LLM configured — set GOOGLE_API_KEY (or GEMINI_API_KEY) in .env.")
        return
    model = client.model
    conversation: list[str] = []

    print(f"Connected to Gemini. Model: {model}")

    while True:
        user_input = input("You: ").strip()

        if not user_input:
            continue

        if user_input.lower() in {"/exit", "exit", "quit"}:
            print("Session ended.")
            break

        if user_input.lower().startswith("/model "):
            new_model = user_input[7:].strip()
            if not new_model:
                print("Usage: /model <name>")
                continue
            model = new_model
            print(f"Model switched to: {model}")
            continue

        if user_input.lower() == "/clear":
            conversation = []
            print("Conversation history cleared.")
            continue

        conversation.append(f"User: {user_input}")

        try:
            start = time.perf_counter()
            assistant_text = client.complete(
                system="You are a helpful assistant. Keep responses clear and concise.",
                user="\n".join(conversation),
                model=model,
                temperature=0.2,
            )
            elapsed = time.perf_counter() - start
            print(f"Assistant: {assistant_text}")
            print(f"Response time: {elapsed:.2f}s")
            conversation.append(f"Assistant: {assistant_text}")
        except Exception as exc:
            print(f"Request failed: {exc}")


if __name__ == "__main__":
    print("This module is intended to be imported, not run directly. Use `run_interactive_chat()` for a simple test.")
    run_interactive_chat()
