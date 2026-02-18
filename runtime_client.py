#!/usr/bin/env python3
"""Runtime adapters for direct model API calls (no external CLI required)."""

from __future__ import annotations

import importlib.util
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional


@dataclass(frozen=True)
class RuntimeConfig:
    label: str
    env_keys: List[str]
    package: str


RUNTIME_CONFIGS: Dict[str, RuntimeConfig] = {
    "anthropic": RuntimeConfig(
        label="Anthropic-compatible API",
        env_keys=["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"],
        package="anthropic",
    ),
    "openai": RuntimeConfig(
        label="OpenAI-compatible API",
        env_keys=["OPENAI_API_KEY"],
        package="openai",
    ),
}

DEFAULT_RUNTIME_BACKEND = os.environ.get("PATENT_RUNTIME_BACKEND", "anthropic")


class RuntimeClientError(RuntimeError):
    """Raised when runtime setup or generation fails."""


def _load_local_env() -> None:
    """Load env values from .claude/settings.local.json when available."""
    settings = Path(__file__).resolve().parent / ".claude" / "settings.local.json"
    if not settings.exists():
        return

    try:
        payload = json.loads(settings.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return

    env_map = payload.get("env")
    if not isinstance(env_map, dict):
        return

    for key, value in env_map.items():
        if not isinstance(key, str):
            continue
        if key in os.environ:
            continue
        if value is None:
            continue
        os.environ[key] = str(value)


_load_local_env()


def normalize_runtime_backend(runtime_backend: str) -> str:
    backend = (runtime_backend or "").strip().lower()
    if backend in RUNTIME_CONFIGS:
        return backend
    supported = ", ".join(sorted(RUNTIME_CONFIGS.keys()))
    raise RuntimeClientError(
        f"Unsupported runtime backend '{runtime_backend}'. Supported: {supported}"
    )


def get_runtime_label(runtime_backend: str) -> str:
    return RUNTIME_CONFIGS[normalize_runtime_backend(runtime_backend)].label


def _first_env(keys: List[str]) -> Optional[str]:
    for key in keys:
        value = os.environ.get(key)
        if value:
            return value
    return None


def _has_package(runtime_backend: str) -> bool:
    backend = normalize_runtime_backend(runtime_backend)
    package = RUNTIME_CONFIGS[backend].package
    return importlib.util.find_spec(package) is not None


def get_missing_env_keys(runtime_backend: str) -> List[str]:
    backend = normalize_runtime_backend(runtime_backend)
    keys = RUNTIME_CONFIGS[backend].env_keys
    if _first_env(keys):
        return []
    return keys


def is_runtime_available(runtime_backend: str) -> bool:
    return not get_missing_env_keys(runtime_backend) and _has_package(runtime_backend)


def get_available_runtime_backends() -> List[str]:
    return [backend for backend in RUNTIME_CONFIGS if is_runtime_available(backend)]


def runtime_setup_hint(runtime_backend: str) -> str:
    backend = normalize_runtime_backend(runtime_backend)
    hints: List[str] = []

    missing = get_missing_env_keys(backend)
    if missing:
        if len(missing) == 1:
            hints.append(f"Missing environment variable: {missing[0]}")
        else:
            hints.append("Missing one of environment variables: " + ", ".join(missing))

    if not _has_package(backend):
        hints.append(f"Missing Python package: {RUNTIME_CONFIGS[backend].package}")

    return "; ".join(hints)


def _generate_with_anthropic(
    prompt: str,
    system_prompt: Optional[str],
    max_tokens: int,
    temperature: float,
    timeout_seconds: int,
) -> str:
    try:
        from anthropic import Anthropic
    except Exception as exc:  # pragma: no cover
        raise RuntimeClientError("anthropic package is not installed") from exc

    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if not api_key:
        raise RuntimeClientError(runtime_setup_hint("anthropic") or "Anthropic API key is not configured")

    model = (
        os.environ.get("ANTHROPIC_MODEL")
        or os.environ.get("ANTHROPIC_DEFAULT_SONNET_MODEL")
        or os.environ.get("ANTHROPIC_DEFAULT_HAIKU_MODEL")
        or "claude-3-5-sonnet-latest"
    )

    kwargs = {"api_key": api_key, "timeout": timeout_seconds}
    base_url = os.environ.get("ANTHROPIC_BASE_URL")
    if base_url:
        kwargs["base_url"] = base_url

    client = Anthropic(**kwargs)
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system_prompt or "",
        messages=[{"role": "user", "content": prompt}],
    )

    parts: List[str] = []
    for item in response.content:
        if getattr(item, "type", None) == "text":
            text = getattr(item, "text", "")
            if text:
                parts.append(str(text))

    text = "\n".join(parts).strip()
    if text:
        return text
    raise RuntimeClientError("Anthropic response did not include text content")


def _generate_with_openai(
    prompt: str,
    system_prompt: Optional[str],
    max_tokens: int,
    temperature: float,
    timeout_seconds: int,
) -> str:
    try:
        from openai import OpenAI
    except Exception as exc:  # pragma: no cover
        raise RuntimeClientError("openai package is not installed") from exc

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeClientError(runtime_setup_hint("openai") or "OPENAI_API_KEY is not configured")

    kwargs = {"api_key": api_key, "timeout": timeout_seconds}
    base_url = os.environ.get("OPENAI_BASE_URL")
    if base_url:
        kwargs["base_url"] = base_url

    client = OpenAI(**kwargs)
    model = os.environ.get("OPENAI_MODEL") or "gpt-4o-mini"

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    response = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )

    if not response.choices:
        raise RuntimeClientError("OpenAI response did not include choices")

    text = str(response.choices[0].message.content or "").strip()
    if text:
        return text
    raise RuntimeClientError("OpenAI response did not include text content")


def generate_text(
    runtime_backend: str,
    prompt: str,
    *,
    system_prompt: Optional[str] = None,
    max_tokens: int = 4096,
    temperature: float = 0.2,
    timeout_seconds: int = 600,
) -> str:
    backend = normalize_runtime_backend(runtime_backend)
    if backend == "anthropic":
        return _generate_with_anthropic(
            prompt=prompt,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout_seconds=timeout_seconds,
        )
    if backend == "openai":
        return _generate_with_openai(
            prompt=prompt,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout_seconds=timeout_seconds,
        )
    raise RuntimeClientError(f"Unsupported runtime backend: {runtime_backend}")
