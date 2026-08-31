"""Standalone, swappable LLM client for the ANA log-tools MCP server.

This module intentionally contains no pipeline/business logic. It only exposes a
minimal `LLMClient` protocol, an EDAI Qwen implementation, and a `get_default_llm()`
factory. Pipeline tools accept an `LLMClient` as a parameter so the model provider
can be swapped without touching pipeline code.
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

DEFAULT_AUTH_URL = "https://orw-edai.wv.mentorg.com/auth/api/auth"
DEFAULT_BASE_URL = "https://orw-edai.wv.mentorg.com/model-manager/api"
DEFAULT_MODEL = "gpt-4.1"
DEFAULT_TIMEOUT_SEC = 30.0

_ENV_CANDIDATE_PATHS = [
    Path(__file__).resolve().with_name(".env"),
    Path(__file__).resolve().parent.parent / ".env",
]

_SHARED_TOKEN_CANDIDATE_PATHS = [
    Path(__file__).resolve().parents[3] / "vscode" / "fuse-chat-client" / "src" / "constants.ts",
    * _ENV_CANDIDATE_PATHS,
]


def _strip_wrapping_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'", "`"}:
        return value[1:-1]
    return value


def _read_literal_value(source_text: str, const_name: str) -> str | None:
    marker = f"export const {const_name} ="
    start_index = source_text.find(marker)
    if start_index == -1:
        return None

    tail = source_text[start_index + len(marker):]
    if "||" in tail:
        tail = tail.split("||", 1)[1]
    tail = tail.strip().rstrip(";")
    if not tail:
        return None

    literal = _strip_wrapping_quotes(tail)
    return literal.strip() or None


def _read_shared_token_from_file(const_name: str) -> str | None:
    for token_path in _SHARED_TOKEN_CANDIDATE_PATHS:
        if not token_path.exists():
            continue
        try:
            text = token_path.read_text(encoding="utf-8")
        except OSError:
            continue

        value = _read_literal_value(text, const_name)
        if value:
            return value

    return None


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
    """Return a bearer/API token already configured for ANA/EDAI usage.

    The runtime may already provide a valid token through the ANA environment or a
    managed client. Prefer that directly instead of re-requesting a fresh token from
    the EDAI auth service.
    """
    for env_name in (
        "EDAI_AUTH_TOKEN",
        "ANA_LLM_TOKEN",
        "ANA_AUTH_TOKEN",
        "SIEMENS_API_KEY",
        "OPENAI_API_KEY",
    ):
        value = os.getenv(env_name) or _read_env_value(env_name)
        if value:
            return value
    return None


def get_app_token() -> str | None:
    """Return the same app token used by the chat UI.

    We prefer the exact environment variable used by the UI, and if that is not
    present we read the same literal from the shared TS constants file used by the
    chat extension. This avoids using a separate token source or a regex-only
    interpretation of the constant file.
    """
    for env_name in ("FUSE_APP_TOKEN", "ANA_APP_TOKEN", "ANA_FUSE_APP_TOKEN"):
        value = os.getenv(env_name) or _read_env_value(env_name)
        if value:
            return value

    shared_token = _read_shared_token_from_file("FUSE_APP_TOKEN")
    if shared_token:
        return shared_token
    return None


def get_request_timeout(fallback: float = DEFAULT_TIMEOUT_SEC) -> float:
    """Return the configured EDAI request timeout in seconds."""
    raw_value = (
        os.getenv("ANA_LLM_TIMEOUT_SEC")
        or os.getenv("EDAI_TIMEOUT_SEC")
        or _read_env_value("ANA_LLM_TIMEOUT_SEC")
        or _read_env_value("EDAI_TIMEOUT_SEC")
    )
    try:
        return max(1.0, float(raw_value)) if raw_value else fallback
    except (TypeError, ValueError):
        return fallback


def has_llm_configuration() -> bool:
    """Return whether a bearer token or FUSE app token is configured."""
    return bool(
        get_api_key()
        or get_app_token()
    )


def _tls_context() -> ssl.SSLContext | None:
    """Use normal certificate verification unless explicitly disabled for EDAI."""
    raw_value = os.getenv("EDAI_TLS_VERIFY") or _read_env_value("EDAI_TLS_VERIFY")
    if raw_value is not None and raw_value.strip().lower() in {"0", "false", "no", "off"}:
        return ssl._create_unverified_context()
    return None


def _request_auth_token(timeout: float = 30.0) -> str:
    """Request an EDAI bearer token using the configured FUSE app token."""
    fuse_base_url = os.getenv("FUSE_BASE_URL") or _read_env_value("FUSE_BASE_URL")
    auth_url = os.getenv("EDAI_AUTH_URL") or _read_env_value("EDAI_AUTH_URL")
    if not auth_url and fuse_base_url:
        auth_url = f"{fuse_base_url.rstrip('/')}/auth/api/auth"
    auth_url = auth_url or DEFAULT_AUTH_URL
    app_token = get_app_token()
    if not app_token:
        raise RuntimeError("Missing FUSE_APP_TOKEN for EDAI authentication.")

    payload = {
        "app_key": app_token,
        "payload": {
            "email": os.getenv("EDAI_EMAIL", "john.doe@email.com"),
            "first_name": os.getenv("EDAI_FIRST_NAME", "John"),
            "groups": [],
            "last_name": os.getenv("EDAI_LAST_NAME", "Doe"),
            "unique_id": os.getenv("EDAI_UNIQUE_ID", "1234567"),
        },
    }

    request = Request(
        auth_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout, context=_tls_context()) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError(f"EDAI authentication request failed: {exc}") from exc

    token = result.get("token") if isinstance(result, dict) else None
    if not token:
        raise RuntimeError("EDAI authentication response did not contain a token.")
    return str(token)


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


class EdaiQwenLLMClient:
    """OpenAI-compatible EDAI client using the Qwen reasoning model."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        model: str | None = None,
    ) -> None:
        self.model = model or os.getenv("EDAI_LLM_MODEL", DEFAULT_MODEL)
        self._base_url = base_url.rstrip("/")
        self._token: str | None = None

    def _get_token(self, timeout: float) -> str:
        if self._token:
            return self._token

        # ORW requires a bearer issued by its own auth endpoint. The ANA app token
        # is exchanged once per client and the resulting ORW bearer is cached here.
        self._token = _request_auth_token(timeout=timeout)
        return self._token

    def complete(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.0,
        timeout: float = 30.0,
        model: str | None = None,
    ) -> str:
        request = Request(
            f"{self._base_url}/v1/chat/completions",
            data=json.dumps(
                {
                    "model": model or self.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": temperature,
                }
            ).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._get_token(timeout)}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout, context=_tls_context()) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError) as exc:
            raise RuntimeError(f"EDAI chat completion request failed: {exc}") from exc

        try:
            return str(result["choices"][0]["message"].get("content") or "").strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("EDAI chat completion response had an unexpected shape.") from exc


def get_default_llm(api_key: str | None = None) -> LLMClient | None:
    """Return the default client using a supplied bearer or local credentials."""
    if api_key:
        return EdaiQwenLLMClient(api_key=api_key)
    if not has_llm_configuration():
        return None
    return EdaiQwenLLMClient()


def run_interactive_chat() -> None:
    client = EdaiQwenLLMClient()
    model = os.getenv("EDAI_LLM_MODEL", DEFAULT_MODEL)
    conversation: list[str] = []

    print(f"Connected to Siemens LLM endpoint. Model: {model}")

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
