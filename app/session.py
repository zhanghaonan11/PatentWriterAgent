"""Session management: listing, archiving, log I/O."""

from __future__ import annotations

import io
import shlex
import zipfile
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import OUTPUT_DIR
from app.utils import is_valid_uuid, read_json_file, human_file_size


def get_log_path(session_id: str) -> Path:
    return OUTPUT_DIR / f"{session_id}.log"


def get_pid_path(session_id: str) -> Path:
    return OUTPUT_DIR / f"{session_id}.pid.json"


def get_session_dir(session_id: str) -> Path:
    return OUTPUT_DIR / f"temp_{session_id}"


def list_sessions() -> List[str]:
    scores: Dict[str, float] = {}

    for log_file in OUTPUT_DIR.glob("*.log"):
        session_id = log_file.stem
        if is_valid_uuid(session_id):
            scores[session_id] = max(scores.get(session_id, 0.0), log_file.stat().st_mtime)

    for temp_dir in OUTPUT_DIR.glob("temp_*"):
        session_id = temp_dir.name.replace("temp_", "", 1)
        if is_valid_uuid(session_id):
            scores[session_id] = max(scores.get(session_id, 0.0), temp_dir.stat().st_mtime)

    for pid_file in OUTPUT_DIR.glob("*.pid.json"):
        session_id = pid_file.name[: -len(".pid.json")]
        if is_valid_uuid(session_id):
            scores[session_id] = max(scores.get(session_id, 0.0), pid_file.stat().st_mtime)

    return [
        session_id
        for session_id, _ in sorted(scores.items(), key=lambda item: item[1], reverse=True)
    ]


def build_session_archive(session_id: str) -> Optional[bytes]:
    session_dir = get_session_dir(session_id)
    log_path = get_log_path(session_id)
    if not session_dir.exists() and not log_path.exists():
        return None

    buffer = io.BytesIO()
    file_count = 0
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        if session_dir.exists():
            for file_path in sorted(session_dir.rglob("*")):
                if not file_path.is_file():
                    continue
                arcname = Path(f"temp_{session_id}") / file_path.relative_to(session_dir)
                archive.write(file_path, arcname.as_posix())
                file_count += 1

        if log_path.exists():
            archive.write(log_path, log_path.name)
            file_count += 1

    if file_count == 0:
        return None
    return buffer.getvalue()


def append_log_banner(session_id: str, command: List[str]) -> None:
    log_path = get_log_path(session_id)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(
            f"\n=== [{datetime.now().isoformat(timespec='seconds')}] session {session_id} started ===\n"
        )
        handle.write(f"Command: {' '.join(shlex.quote(item) for item in command)}\n")


def append_log_event(session_id: str, note: str, body: str = "") -> None:
    log_path = get_log_path(session_id)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n=== [{datetime.now().isoformat(timespec='seconds')}] {note} ===\n")
        if body:
            handle.write(body)
            if not body.endswith("\n"):
                handle.write("\n")


def append_log_footer(session_id: str, note: str) -> None:
    log_path = get_log_path(session_id)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n=== [{datetime.now().isoformat(timespec='seconds')}] {note} ===\n")


def tail_log_lines(path: Path, max_lines: int) -> List[str]:
    if not path.exists():
        return []

    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return list(deque(handle, maxlen=max_lines))
    except OSError:
        return []


def save_uploaded_file(uploaded_file: Any, session_id: str) -> Path:
    from app.config import DATA_DIR

    target_dir = DATA_DIR / "uploads" / session_id
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = Path(uploaded_file.name).name or "input.docx"
    target_path = target_dir / filename
    target_path.write_bytes(uploaded_file.getbuffer())
    return target_path
