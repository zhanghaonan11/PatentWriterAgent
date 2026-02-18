"""Shared constants, paths, and configuration for PatentWriterAgent."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List


ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
OUTPUT_DIR = ROOT_DIR / "output"
PIPELINE_RUNNER = ROOT_DIR / "pipeline_runner.py"

PREVIEW_CHAR_LIMIT = 20000


def _positive_int_from_env(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


# --- Input modes ---
MODE_NORMAL = "normal"
MODE_FAST = "fast"

# --- Execution modes ---
EXEC_MODE_NATIVE = "native"
EXEC_MODE_CLI = "cli"

DEFAULT_EXECUTION_MODE = (
    os.environ.get("PATENT_RUNTIME_MODE", EXEC_MODE_NATIVE).strip().lower() or EXEC_MODE_NATIVE
)
if DEFAULT_EXECUTION_MODE not in (EXEC_MODE_NATIVE, EXEC_MODE_CLI):
    DEFAULT_EXECUTION_MODE = EXEC_MODE_NATIVE

# --- Native pipeline tuning ---
DESCRIPTION_PARALLELISM_MIN = 1
DESCRIPTION_PARALLELISM_MAX = 6
DEFAULT_DESCRIPTION_PARALLELISM = _positive_int_from_env("PATENT_DESCRIPTION_PARALLELISM", 2)
if DEFAULT_DESCRIPTION_PARALLELISM < DESCRIPTION_PARALLELISM_MIN:
    DEFAULT_DESCRIPTION_PARALLELISM = DESCRIPTION_PARALLELISM_MIN
if DEFAULT_DESCRIPTION_PARALLELISM > DESCRIPTION_PARALLELISM_MAX:
    DEFAULT_DESCRIPTION_PARALLELISM = DESCRIPTION_PARALLELISM_MAX

# --- CLI backend configs ---
CLI_CONFIGS: Dict[str, Dict[str, str]] = {
    "claude": {
        "label": "Claude CLI",
        "binary": "claude",
        "process_keyword": "claude",
    },
    "codex": {
        "label": "OpenAI Codex CLI",
        "binary": "codex",
        "process_keyword": "codex",
    },
    "gemini": {
        "label": "Google Gemini CLI",
        "binary": "gemini",
        "process_keyword": "gemini",
    },
}

DEFAULT_CLI_BACKEND = os.environ.get("PATENT_CLI_BACKEND", "claude").strip().lower() or "claude"
if DEFAULT_CLI_BACKEND not in CLI_CONFIGS:
    DEFAULT_CLI_BACKEND = "claude"

# --- Fast mode section titles ---
FAST_SECTION_TITLES: List[str] = [
    "发明名称",
    "要解决的技术问题",
    "现有技术方案及缺点",
    "本发明技术方案（详细描述）",
    "有益效果",
    "技术关键词",
]
