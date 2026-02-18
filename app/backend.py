"""Backend helpers: CLI/Native command building, availability checks, label helpers."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any, List

from app.config import (
    CLI_CONFIGS,
    DEFAULT_CLI_BACKEND,
    DEFAULT_EXECUTION_MODE,
    EXEC_MODE_CLI,
    EXEC_MODE_NATIVE,
    PIPELINE_RUNNER,
    DESCRIPTION_PARALLELISM_MAX,
    DESCRIPTION_PARALLELISM_MIN,
)
from runtime_client import (
    DEFAULT_RUNTIME_BACKEND,
    RuntimeClientError,
    get_runtime_label,
    normalize_runtime_backend,
)


# --- Execution mode helpers ---

def normalize_execution_mode(execution_mode: str) -> str:
    mode = (execution_mode or "").strip().lower()
    if mode in (EXEC_MODE_NATIVE, EXEC_MODE_CLI):
        return mode
    return DEFAULT_EXECUTION_MODE


def get_execution_mode_label(execution_mode: str) -> str:
    mode = normalize_execution_mode(execution_mode)
    if mode == EXEC_MODE_CLI:
        return "CLI runtime"
    return "Native runtime"


def get_mode_label(mode: str) -> str:
    from app.config import MODE_FAST

    if mode == MODE_FAST:
        return "Fast mode (idea -> disclosure -> patent)"
    return "Normal mode (.docx -> patent)"


# --- CLI backend helpers ---

def get_cli_binary(cli_backend: str) -> str:
    cfg = CLI_CONFIGS.get(cli_backend, CLI_CONFIGS[DEFAULT_CLI_BACKEND])
    return cfg["binary"]


def get_cli_label(cli_backend: str) -> str:
    cfg = CLI_CONFIGS.get(cli_backend, CLI_CONFIGS[DEFAULT_CLI_BACKEND])
    return cfg["label"]


def safe_cli_label(cli_backend: str) -> str:
    if cli_backend in CLI_CONFIGS:
        return get_cli_label(cli_backend)
    return str(cli_backend)


def get_cli_process_keyword(cli_backend: str) -> str:
    cfg = CLI_CONFIGS.get(cli_backend, CLI_CONFIGS[DEFAULT_CLI_BACKEND])
    return cfg["process_keyword"]


def is_cli_available(cli_backend: str) -> bool:
    return shutil.which(get_cli_binary(cli_backend)) is not None


def get_available_cli_backends() -> List[str]:
    return [backend for backend in CLI_CONFIGS if is_cli_available(backend)]


# --- Runtime backend helpers ---

def safe_runtime_label(runtime_backend: str) -> str:
    try:
        return get_runtime_label(runtime_backend)
    except RuntimeClientError:
        return str(runtime_backend)


def clamp_description_parallelism(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = DESCRIPTION_PARALLELISM_MIN

    if parsed < DESCRIPTION_PARALLELISM_MIN:
        return DESCRIPTION_PARALLELISM_MIN
    if parsed > DESCRIPTION_PARALLELISM_MAX:
        return DESCRIPTION_PARALLELISM_MAX
    return parsed


# --- Command inference ---

def infer_cli_backend_from_command(command: List[str]) -> str:
    if not command:
        return DEFAULT_CLI_BACKEND

    executable = Path(command[0]).name.lower()
    for backend, cfg in CLI_CONFIGS.items():
        if cfg["binary"] == executable:
            return backend

    joined = " ".join(command).lower()
    for backend, cfg in CLI_CONFIGS.items():
        if f" {cfg['binary']}" in f" {joined}":
            return backend

    return DEFAULT_CLI_BACKEND


def infer_execution_mode_from_command(command: List[str]) -> str:
    if not command:
        return DEFAULT_EXECUTION_MODE
    joined = " ".join(command)
    if "pipeline_runner.py" in joined:
        return EXEC_MODE_NATIVE
    return EXEC_MODE_CLI


def infer_runtime_backend_from_command(command: List[str]) -> str:
    if "--runtime-backend" in command:
        idx = command.index("--runtime-backend")
        if idx + 1 < len(command):
            raw_backend = (command[idx + 1] or "").strip().lower()
            if raw_backend in {"codex-cli", "gemini-cli"}:
                return raw_backend
            try:
                return normalize_runtime_backend(command[idx + 1])
            except RuntimeClientError:
                pass
    return DEFAULT_RUNTIME_BACKEND


# --- Command building ---

def build_runner_command(
    runtime_backend: str,
    session_id: str,
    input_path: Path,
    prompt: str,
    description_parallelism: int,
) -> List[str]:
    parallelism = clamp_description_parallelism(description_parallelism)
    return [
        sys.executable,
        str(PIPELINE_RUNNER),
        "--session-id",
        session_id,
        "--input-path",
        str(input_path),
        "--runtime-backend",
        runtime_backend,
        "--task-prompt",
        prompt,
        "--description-parallelism",
        str(parallelism),
    ]


def build_cli_command(
    cli_backend: str,
    session_id: str,
    prompt: str,
    *,
    input_path: Path | None = None,
    description_parallelism: int = DESCRIPTION_PARALLELISM_MIN,
    fast_mode: bool = False,
) -> List[str]:
    if cli_backend in {"codex", "gemini"}:
        # Fast-mode preprocessing expects raw model text output from the CLI.
        if fast_mode:
            if cli_backend == "codex":
                return [
                    "codex",
                    "exec",
                    "--json",
                    "--dangerously-bypass-approvals-and-sandbox",
                    prompt,
                ]

            return [
                "gemini",
                "-p",
                prompt,
                "-o",
                "stream-json",
                "-y",
            ]

        # Normal patent generation needs deterministic staged outputs. Reuse the
        # existing 8-stage pipeline and route model calls through CLI bridge runtime.
        if input_path is None:
            raise ValueError(f"input_path is required for {cli_backend} CLI patent generation")

        runtime_backend = "codex-cli" if cli_backend == "codex" else "gemini-cli"
        return build_runner_command(
            runtime_backend=runtime_backend,
            session_id=session_id,
            input_path=input_path,
            prompt=prompt,
            description_parallelism=description_parallelism,
        )

    return [
        "claude",
        "--dangerously-skip-permissions",
        "--session-id",
        session_id,
        prompt,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
    ]
