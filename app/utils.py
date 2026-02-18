"""Pure utility functions with no side effects."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from app.config import ROOT_DIR


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def is_valid_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except (ValueError, TypeError):
        return False


def to_display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT_DIR))
    except ValueError:
        return str(path)


def resolve_workspace_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = ROOT_DIR / path
    return path


def read_json_file(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def write_json_file(path: Path, payload: Dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def human_file_size(num_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(num_bytes)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{num_bytes} B"


def format_timestamp(timestamp: Optional[float]) -> str:
    if timestamp is None:
        return "-"
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def read_text_preview(path: Path, limit: int = 20000) -> tuple[str, bool]:
    if limit <= 0:
        return "", False

    try:
        size = path.stat().st_size
    except OSError:
        size = None

    # Fast path for large artifacts: only read the first chunk we need.
    if size is not None and size > limit:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                preview = handle.read(limit)
            return preview, True
        except OSError:
            return "", False

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", False

    if len(content) <= limit:
        return content, False
    return content[:limit], True


def to_positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def clamp_int(value: int, minimum: int, maximum: int) -> int:
    if value < minimum:
        return minimum
    if value > maximum:
        return maximum
    return value


def tail_text_lines(path: Path, max_lines: int) -> list[str]:
    if max_lines <= 0:
        return []

    try:
        size = path.stat().st_size
    except OSError:
        return []

    if size == 0:
        return []

    chunk_size = 8192
    max_bytes = 1024 * 1024
    read_size = min(size, min(max(size // 8, chunk_size), max_bytes))

    try:
        with path.open("rb") as handle:
            if read_size < size:
                handle.seek(-read_size, os.SEEK_END)
            data = handle.read(read_size)
    except OSError:
        return []

    text = data.decode("utf-8", errors="replace")

    # Drop partial first line when reading from middle of file.
    if read_size < size and "\n" in text:
        text = text.split("\n", 1)[1]

    lines = text.splitlines(keepends=True)
    if len(lines) <= max_lines:
        return lines
    return lines[-max_lines:]
