#!/usr/bin/env python3
"""Streamlit frontend for PatentWriterAgent with dual runtime support."""

from __future__ import annotations

import io
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid
import zipfile
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psutil
import streamlit as st

from runtime_client import (
    DEFAULT_RUNTIME_BACKEND,
    RUNTIME_CONFIGS,
    RuntimeClientError,
    generate_text,
    get_available_runtime_backends,
    get_runtime_label,
    is_runtime_available,
    normalize_runtime_backend,
    runtime_setup_hint,
)


ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = ROOT_DIR / "data"
OUTPUT_DIR = ROOT_DIR / "output"
PIPELINE_RUNNER = ROOT_DIR / "pipeline_runner.py"

PREVIEW_CHAR_LIMIT = 20000

MODE_NORMAL = "normal"
MODE_FAST = "fast"

EXEC_MODE_NATIVE = "native"
EXEC_MODE_CLI = "cli"

DEFAULT_EXECUTION_MODE = (
    os.environ.get("PATENT_RUNTIME_MODE", EXEC_MODE_NATIVE).strip().lower() or EXEC_MODE_NATIVE
)
if DEFAULT_EXECUTION_MODE not in (EXEC_MODE_NATIVE, EXEC_MODE_CLI):
    DEFAULT_EXECUTION_MODE = EXEC_MODE_NATIVE

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

FAST_SECTION_TITLES: List[str] = [
    "发明名称",
    "要解决的技术问题",
    "现有技术方案及缺点",
    "本发明技术方案（详细描述）",
    "有益效果",
    "技术关键词",
]


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


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
    if mode == MODE_FAST:
        return "Fast mode (idea -> disclosure -> patent)"
    return "Normal mode (.docx -> patent)"


def get_log_path(session_id: str) -> Path:
    return OUTPUT_DIR / f"{session_id}.log"


def get_pid_path(session_id: str) -> Path:
    return OUTPUT_DIR / f"{session_id}.pid.json"


def get_session_dir(session_id: str) -> Path:
    return OUTPUT_DIR / f"temp_{session_id}"


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


def is_valid_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except (ValueError, TypeError):
        return False


def is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False


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


def infer_cli_backend_from_command(command: List[str]) -> str:
    if not command:
        return DEFAULT_CLI_BACKEND

    executable = Path(command[0]).name.lower()
    for backend, cfg in CLI_CONFIGS.items():
        if cfg["binary"] == executable:
            return backend

    # some shell wrappers include python first; inspect full command string
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
            try:
                return normalize_runtime_backend(command[idx + 1])
            except RuntimeClientError:
                pass
    return DEFAULT_RUNTIME_BACKEND


def safe_runtime_label(runtime_backend: str) -> str:
    try:
        return get_runtime_label(runtime_backend)
    except RuntimeClientError:
        return str(runtime_backend)


def get_running_metadata(session_id: str) -> Optional[Dict[str, Any]]:
    metadata = read_json_file(get_pid_path(session_id))
    if not metadata:
        return None

    try:
        pid = int(metadata.get("pid", 0))
    except (TypeError, ValueError):
        pid = 0

    if not is_pid_running(pid):
        try:
            get_pid_path(session_id).unlink()
        except OSError:
            pass
        return None

    metadata["pid"] = pid

    command = metadata.get("command")
    command_list = command if isinstance(command, list) else []

    if "execution_mode" not in metadata:
        metadata["execution_mode"] = infer_execution_mode_from_command(command_list)

    if metadata["execution_mode"] == EXEC_MODE_NATIVE:
        if "runtime_backend" not in metadata:
            metadata["runtime_backend"] = infer_runtime_backend_from_command(command_list)
    else:
        if "cli_backend" not in metadata:
            metadata["cli_backend"] = infer_cli_backend_from_command(command_list)

    return metadata


def cleanup_stale_pid_files() -> None:
    for pid_file in OUTPUT_DIR.glob("*.pid.json"):
        metadata = read_json_file(pid_file)
        if not metadata:
            try:
                pid_file.unlink()
            except OSError:
                pass
            continue

        try:
            pid = int(metadata.get("pid", 0))
        except (TypeError, ValueError):
            pid = 0

        if is_pid_running(pid):
            continue

        try:
            pid_file.unlink()
        except OSError:
            pass


def write_pid_metadata(
    session_id: str,
    pid: int,
    command: List[str],
    input_path: Path,
    prompt: str,
    execution_mode: str,
    runtime_backend: str,
    cli_backend: str,
) -> None:
    payload = {
        "pid": pid,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "command": command,
        "input_path": str(input_path),
        "prompt": prompt,
        "execution_mode": execution_mode,
    }
    if execution_mode == EXEC_MODE_NATIVE:
        payload["runtime_backend"] = runtime_backend
    else:
        payload["cli_backend"] = cli_backend

    write_json_file(get_pid_path(session_id), payload)


def remove_pid_metadata(session_id: str) -> None:
    try:
        get_pid_path(session_id).unlink()
    except OSError:
        pass


def terminate_pid_tree(pid: int) -> Tuple[bool, str]:
    if not is_pid_running(pid):
        return False, "Process is not running."

    if os.name != "nt":
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            time.sleep(0.4)
        except ProcessLookupError:
            return True, "Process already exited."
        except OSError:
            pass

    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return True, "Process already exited."

    targets = parent.children(recursive=True) + [parent]
    for proc in targets:
        try:
            proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    _, alive = psutil.wait_procs(targets, timeout=5)
    for proc in alive:
        try:
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    return True, "Process tree terminated."


def is_cli_process(process: psutil.Process, process_keyword: str) -> bool:
    try:
        name = (process.name() or "").lower()
        if process_keyword in name:
            return True
        cmdline = process.cmdline()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False

    for item in cmdline:
        if process_keyword in Path(item).name.lower():
            return True
    return False


def is_runner_process(process: psutil.Process) -> bool:
    try:
        cmdline = process.cmdline()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False

    for item in cmdline:
        name = Path(item).name.lower()
        if name in {"pipeline_runner.py", "pipeline_runner"}:
            return True
        if "pipeline_runner.py" in item.lower():
            return True
    return False


def cleanup_all_runner_processes() -> Tuple[int, int]:
    current_pid = os.getpid()
    killed = 0
    scanned = 0

    for process in psutil.process_iter(["pid"]):
        pid = process.info.get("pid")
        if not pid or pid == current_pid:
            continue
        scanned += 1
        if not is_runner_process(process):
            continue
        ok, _ = terminate_pid_tree(pid)
        if ok:
            killed += 1

    cleanup_stale_pid_files()
    return killed, scanned


def cleanup_all_cli_processes(cli_backend: str) -> Tuple[int, int]:
    current_pid = os.getpid()
    killed = 0
    scanned = 0
    keyword = get_cli_process_keyword(cli_backend)

    for process in psutil.process_iter(["pid"]):
        pid = process.info.get("pid")
        if not pid or pid == current_pid:
            continue
        scanned += 1
        if not is_cli_process(process, keyword):
            continue
        ok, _ = terminate_pid_tree(pid)
        if ok:
            killed += 1

    cleanup_stale_pid_files()
    return killed, scanned


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


def get_default_input_path() -> Path:
    candidate = DATA_DIR / "input.docx"
    if candidate.exists():
        return candidate
    files = sorted(DATA_DIR.glob("*.docx"))
    return files[0] if files else candidate


def build_prompt(custom_prompt: str, input_path: Path) -> str:
    prompt = custom_prompt.strip()
    if prompt:
        return prompt.replace("{input_path}", to_display_path(input_path))
    return f"根据 {to_display_path(input_path)} 编写专利提案"


def build_fast_mode_prompt(invention_idea: str) -> str:
    idea = normalize_newlines(invention_idea).strip()
    required_sections = "\n".join(f"- {title}" for title in FAST_SECTION_TITLES)
    return (
        "你是一名资深中国专利代理人。请把给定的发明构思扩写为可用于专利写作的技术交底草稿。\n\n"
        "输出要求：\n"
        "1. 仅输出中文 Markdown 正文，不要输出解释、前言或额外说明。\n"
        "2. 必须包含以下章节，并保持该顺序：\n"
        f"{required_sections}\n"
        "3. 每个章节都要给出具体技术内容，避免空泛表述。参数不明确时可合理假设，并显式标注“假设：...”。\n"
        "4. 适度补充实施细节（结构、流程、关键参数范围、可选方案），使内容可直接用于后续专利生成。\n"
        "5. 输出必须严格包裹在以下标记之间：\n"
        "<FAST_DISCLOSURE_START>\n"
        "...这里是正文...\n"
        "<FAST_DISCLOSURE_END>\n\n"
        "发明构思如下：\n"
        f"{idea}\n"
    )


def build_runner_command(
    runtime_backend: str,
    session_id: str,
    input_path: Path,
    prompt: str,
) -> List[str]:
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
    ]


def build_cli_command(cli_backend: str, session_id: str, prompt: str) -> List[str]:
    if cli_backend == "codex":
        return [
            "codex",
            "exec",
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            prompt,
        ]

    if cli_backend == "gemini":
        return [
            "gemini",
            "-p",
            prompt,
            "-o",
            "stream-json",
            "-y",
        ]

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


def save_uploaded_file(uploaded_file: Any, session_id: str) -> Path:
    target_dir = DATA_DIR / "uploads" / session_id
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = Path(uploaded_file.name).name or "input.docx"
    target_path = target_dir / filename
    target_path.write_bytes(uploaded_file.getbuffer())
    return target_path


def extract_text_chunks_from_payload(payload: Dict[str, Any]) -> List[str]:
    chunks: List[str] = []
    event_type = str(payload.get("type", ""))

    if event_type == "assistant":
        message = payload.get("message") or {}
        content = message.get("content") or []
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text":
                    text = item.get("text")
                    if isinstance(text, str):
                        chunks.append(text)
        direct_text = message.get("text")
        if isinstance(direct_text, str):
            chunks.append(direct_text)

    if event_type == "item.completed":
        item = payload.get("item") or {}
        if isinstance(item, dict) and item.get("type") == "agent_message":
            text = item.get("text")
            if isinstance(text, str):
                chunks.append(text)

    if event_type == "result":
        result = payload.get("result")
        if isinstance(result, str):
            chunks.append(result)

    if not chunks:
        for key in ("text", "content", "result", "output"):
            value = payload.get(key)
            if isinstance(value, str):
                chunks.append(value)

    return [chunk.strip() for chunk in chunks if isinstance(chunk, str) and chunk.strip()]


def extract_fast_disclosure_text(raw_output: str) -> str:
    json_chunks: List[str] = []
    plain_chunks: List[str] = []

    for line in normalize_newlines(raw_output).split("\n"):
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.startswith("{"):
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                plain_chunks.append(stripped)
                continue
            if isinstance(payload, dict):
                json_chunks.extend(extract_text_chunks_from_payload(payload))
            continue

        plain_chunks.append(stripped)

    chunks = json_chunks if json_chunks else plain_chunks
    deduped: List[str] = []
    for chunk in chunks:
        if deduped and chunk == deduped[-1]:
            continue
        deduped.append(chunk)

    merged = "\n\n".join(deduped).strip()

    marker_match = re.search(
        r"<FAST_DISCLOSURE_START>(.*?)<FAST_DISCLOSURE_END>",
        merged,
        flags=re.DOTALL,
    )
    if marker_match:
        return marker_match.group(1).strip()

    fence_match = re.search(
        r"```(?:markdown|md|text)?\s*(.*?)```",
        merged,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if fence_match:
        return fence_match.group(1).strip()

    return merged


def ensure_fast_disclosure_sections(content: str, invention_idea: str) -> str:
    text = normalize_newlines(content).strip()

    if not text:
        text = (
            "发明名称\n"
            "待补充\n\n"
            "要解决的技术问题\n"
            f"{invention_idea.strip() or '待补充'}"
        )

    missing = [title for title in FAST_SECTION_TITLES if title not in text]
    if missing:
        additions: List[str] = []
        for title in missing:
            if title == "要解决的技术问题":
                placeholder = invention_idea.strip() or "待补充"
            else:
                placeholder = "待补充（假设：后续补齐具体参数、结构和实施细节）"
            additions.append(f"{title}\n{placeholder}")

        if not text.endswith("\n"):
            text += "\n"
        text += "\n" + "\n\n".join(additions)

    return text.strip() + "\n"


def xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def write_simple_docx(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    paragraphs: List[str] = []
    for line in normalize_newlines(content).split("\n"):
        escaped = xml_escape(line)
        if not escaped:
            paragraphs.append("<w:p/>")
            continue
        paragraphs.append(f'<w:p><w:r><w:t xml:space="preserve">{escaped}</w:t></w:r></w:p>')

    document_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    {''.join(paragraphs)}
    <w:sectPr>
      <w:pgSz w:w="11906" w:h="16838"/>
      <w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:header="720" w:footer="720" w:gutter="0"/>
    </w:sectPr>
  </w:body>
</w:document>
"""

    content_types_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>
"""

    rels_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>
"""

    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    core_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>Fast Mode Disclosure</dc:title>
  <dc:creator>PatentWriterAgent</dc:creator>
  <cp:lastModifiedBy>PatentWriterAgent</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>
</cp:coreProperties>
"""

    app_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>PatentWriterAgent</Application>
</Properties>
"""

    with zipfile.ZipFile(path, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types_xml)
        archive.writestr("_rels/.rels", rels_xml)
        archive.writestr("docProps/core.xml", core_xml)
        archive.writestr("docProps/app.xml", app_xml)
        archive.writestr("word/document.xml", document_xml)


def generate_fast_disclosure_once(
    runtime_backend: str,
    invention_idea: str,
    timeout_seconds: int = 300,
) -> Tuple[bool, str, str]:
    prompt = build_fast_mode_prompt(invention_idea)
    try:
        output = generate_text(
            runtime_backend=runtime_backend,
            prompt=prompt,
            max_tokens=2200,
            temperature=0.3,
            timeout_seconds=timeout_seconds,
        )
    except RuntimeClientError as exc:
        return False, "", str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, "", f"Runtime call failed during fast-mode preprocessing: {exc}"
    return True, normalize_newlines(output), ""


def run_cli_once(
    cli_backend: str,
    session_id: str,
    prompt: str,
    timeout_seconds: int = 300,
) -> Tuple[bool, str, str, List[str]]:
    command = build_cli_command(cli_backend, session_id, prompt)
    try:
        completed = subprocess.run(
            command,
            cwd=str(ROOT_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        timeout_output_raw: Any = exc.stdout or ""
        if isinstance(timeout_output_raw, bytes):
            timeout_output = timeout_output_raw.decode("utf-8", errors="replace")
        else:
            timeout_output = str(timeout_output_raw)
        return (
            False,
            normalize_newlines(timeout_output),
            f"{get_cli_label(cli_backend)} timed out after {timeout_seconds} seconds during fast-mode preprocessing.",
            command,
        )

    output = normalize_newlines(completed.stdout or "")
    if completed.returncode != 0:
        return (
            False,
            output,
            f"{get_cli_label(cli_backend)} exited with code {completed.returncode} during fast-mode preprocessing.",
            command,
        )

    return True, output, "", command


def prepare_fast_mode_input(
    session_id: str,
    execution_mode: str,
    runtime_backend: str,
    cli_backend: str,
    invention_idea: str,
) -> Tuple[bool, str, Optional[Path]]:
    idea = invention_idea.strip()
    if not idea:
        return False, "Fast mode requires a non-empty invention idea.", None

    prompt = build_fast_mode_prompt(idea)

    if execution_mode == EXEC_MODE_CLI:
        ok, raw_output, error_message, command = run_cli_once(
            cli_backend=cli_backend,
            session_id=str(uuid.uuid4()),
            prompt=prompt,
        )
        append_log_event(
            session_id,
            "fast mode preprocessing started",
            f"Command: {' '.join(shlex.quote(item) for item in command)}",
        )
    else:
        ok, raw_output, error_message = generate_fast_disclosure_once(
            runtime_backend=runtime_backend,
            invention_idea=idea,
        )
        append_log_event(
            session_id,
            "fast mode preprocessing started",
            f"Runtime backend: {safe_runtime_label(runtime_backend)}",
        )

    if raw_output.strip():
        append_log_event(session_id, "fast mode preprocessing output", raw_output)

    if not ok:
        append_log_event(session_id, "fast mode preprocessing failed", error_message)
        return False, error_message, None

    expanded_text = extract_fast_disclosure_text(raw_output)
    expanded_text = ensure_fast_disclosure_sections(expanded_text, idea)

    if len(expanded_text.strip()) < 80:
        message = (
            "Fast mode generated insufficient disclosure content. "
            "Please provide a bit more detail and retry."
        )
        append_log_event(session_id, "fast mode preprocessing failed", message)
        return False, message, None

    target_dir = DATA_DIR / "uploads" / session_id
    target_dir.mkdir(parents=True, exist_ok=True)

    markdown_path = target_dir / "fast_disclosure.md"
    docx_path = target_dir / "fast_disclosure.docx"

    markdown_path.write_text(expanded_text, encoding="utf-8")
    write_simple_docx(docx_path, expanded_text)

    append_log_event(
        session_id,
        "fast mode preprocessing completed",
        f"Generated: {to_display_path(markdown_path)}\nGenerated: {to_display_path(docx_path)}",
    )

    return True, f"Fast mode generated disclosure file: {to_display_path(docx_path)}", docx_path


def start_generation(
    session_id: str,
    input_path: Path,
    custom_prompt: str,
    execution_mode: str,
    runtime_backend: str,
    cli_backend: str,
) -> Tuple[bool, str]:
    if not is_valid_uuid(session_id):
        return False, "Session ID must be a valid UUID."
    if get_running_metadata(session_id):
        return False, "This session is already running."
    if not input_path.exists():
        return False, f"Input file not found: {input_path}"

    execution_mode = normalize_execution_mode(execution_mode)
    prompt = build_prompt(custom_prompt, input_path)

    if execution_mode == EXEC_MODE_CLI:
        if not is_cli_available(cli_backend):
            return False, f"{get_cli_label(cli_backend)} not found in PATH."
        command = build_cli_command(cli_backend, session_id, prompt)
        backend_msg = get_cli_label(cli_backend)
    else:
        if not is_runtime_available(runtime_backend):
            return (
                False,
                f"{safe_runtime_label(runtime_backend)} is not ready. {runtime_setup_hint(runtime_backend)}",
            )
        command = build_runner_command(runtime_backend, session_id, input_path, prompt)
        backend_msg = safe_runtime_label(runtime_backend)

    append_log_banner(session_id, command)
    try:
        with get_log_path(session_id).open("ab") as log_handle:
            process = subprocess.Popen(
                command,
                cwd=str(ROOT_DIR),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                preexec_fn=os.setsid if os.name != "nt" else None,
            )
    except OSError as exc:
        return False, f"Failed to start process: {exc}"

    write_pid_metadata(
        session_id=session_id,
        pid=process.pid,
        command=command,
        input_path=input_path,
        prompt=prompt,
        execution_mode=execution_mode,
        runtime_backend=runtime_backend,
        cli_backend=cli_backend,
    )
    return True, f"Started session {session_id} with {backend_msg} (PID {process.pid})."


def stop_generation(session_id: str) -> Tuple[bool, str]:
    metadata = get_running_metadata(session_id)
    if not metadata:
        remove_pid_metadata(session_id)
        return False, "No running process found for this session."

    pid = int(metadata["pid"])
    success, message = terminate_pid_tree(pid)
    remove_pid_metadata(session_id)
    append_log_footer(session_id, f"session stopped manually (pid={pid})")
    return success, f"{message} pid={pid}"


def tail_log_lines(path: Path, max_lines: int) -> List[str]:
    if not path.exists():
        return []

    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return list(deque(handle, maxlen=max_lines))
    except OSError:
        return []


def format_stream_json_line(line: str) -> str:
    text = line.strip()
    if not text:
        return ""

    if not text.startswith("{"):
        return text

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text

    event_type = payload.get("type")

    if event_type == "assistant":
        message = payload.get("message") or {}
        content = message.get("content") or []
        rendered: List[str] = []
        for item in content:
            item_type = item.get("type")
            if item_type == "text":
                body = str(item.get("text", "")).strip()
                if body:
                    rendered.append(body)
            elif item_type == "tool_use":
                rendered.append(f"[tool_use] {item.get('name', 'unknown')}")
        return "\n".join(rendered)

    if event_type == "result":
        result = str(payload.get("result", "")).strip()
        if result:
            return f"[result] {result}"
        return "[result] completed"

    if event_type == "item.completed":
        item = payload.get("item") or {}
        if item.get("type") == "agent_message":
            body = str(item.get("text", "")).strip()
            return body

    return text


def render_formatted_logs(lines: List[str]) -> str:
    rendered: List[str] = []
    for line in lines:
        chunk = format_stream_json_line(line)
        if chunk:
            rendered.append(chunk)
    return "\n".join(rendered)


def read_text_preview(path: Path, limit: int = PREVIEW_CHAR_LIMIT) -> Tuple[str, bool]:
    content = path.read_text(encoding="utf-8", errors="replace")
    if len(content) <= limit:
        return content, False
    return content[:limit], True


def render_file_preview(title: str, path: Path, language: str) -> None:
    with st.expander(title, expanded=path.exists()):
        st.caption(to_display_path(path))
        if not path.exists():
            st.info("Not generated yet.")
            return

        preview, truncated = read_text_preview(path)
        st.download_button(
            label=f"Download {path.name}",
            data=path.read_bytes(),
            file_name=path.name,
            mime="text/plain",
            width="stretch",
        )
        st.code(preview, language=language)
        if truncated:
            st.caption(f"Preview truncated to first {PREVIEW_CHAR_LIMIT} characters.")


def ensure_directories() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "uploads").mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def safe_rerun() -> None:
    rerun = getattr(st, "rerun", None)
    if callable(rerun):
        rerun()
    else:
        st.experimental_rerun()


def inject_styles() -> None:
    st.markdown(
        """
<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;700&family=IBM+Plex+Mono:wght@400;500&display=swap');
.stApp {
    background:
        radial-gradient(1200px circle at 8% -10%, #f4f9e9 0%, transparent 45%),
        radial-gradient(900px circle at 94% 4%, #e3f6ff 0%, transparent 40%),
        linear-gradient(140deg, #f9faf4 0%, #f4f8f6 45%, #edf3f9 100%);
}
html, body, [class*="st-"] { font-family: 'Space Grotesk', 'Trebuchet MS', sans-serif; }
code, pre { font-family: 'IBM Plex Mono', 'Courier New', monospace !important; }
.main .block-container { max-width: 1320px; padding-top: 1.6rem; padding-bottom: 1.8rem; }
section[data-testid="stSidebar"] { background: linear-gradient(180deg, #ffffff 0%, #f4faf8 100%); border-right: 1px solid #dce9e4; }
.hero { margin-bottom: 0.8rem; padding: 1rem 1.15rem; border: 1px solid #d7e8df; border-radius: 14px; background: linear-gradient(115deg, rgba(249, 255, 250, 0.96), rgba(236, 247, 255, 0.92)); box-shadow: 0 8px 24px rgba(18, 46, 44, 0.07); }
.hero h1 { margin: 0; font-size: 1.7rem; color: #1d3b34; }
.hero p { margin: 0.45rem 0 0; color: #35564d; font-size: 0.95rem; }
.hero small { display: inline-block; margin-bottom: 0.35rem; color: #3b7a6b; font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase; }
@media (max-width: 900px) {
  .main .block-container { padding-top: 1rem; padding-left: 0.85rem; padding-right: 0.85rem; }
}
</style>
""",
        unsafe_allow_html=True,
    )


def initialize_state() -> None:
    default_runtime_backend = DEFAULT_RUNTIME_BACKEND
    if default_runtime_backend not in RUNTIME_CONFIGS:
        default_runtime_backend = list(RUNTIME_CONFIGS.keys())[0]

    defaults: Dict[str, Any] = {
        "session_id": str(uuid.uuid4()),
        "history_selection": "",
        "selected_execution_mode": DEFAULT_EXECUTION_MODE,
        "selected_runtime_backend": default_runtime_backend,
        "selected_cli_backend": DEFAULT_CLI_BACKEND,
        "input_mode": MODE_NORMAL,
        "input_path": to_display_path(get_default_input_path()),
        "fast_invention_idea": "",
        "custom_prompt": "",
        "show_raw_json": False,
        "max_log_lines": 500,
        "auto_refresh": True,
        "refresh_seconds": 2,
    }

    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def normalize_state_values() -> None:
    st.session_state.selected_execution_mode = normalize_execution_mode(
        str(st.session_state.get("selected_execution_mode", DEFAULT_EXECUTION_MODE))
    )

    selected_runtime_backend = str(
        st.session_state.get("selected_runtime_backend", DEFAULT_RUNTIME_BACKEND)
    )
    if selected_runtime_backend not in RUNTIME_CONFIGS:
        selected_runtime_backend = DEFAULT_RUNTIME_BACKEND
        if selected_runtime_backend not in RUNTIME_CONFIGS:
            selected_runtime_backend = list(RUNTIME_CONFIGS.keys())[0]
    st.session_state.selected_runtime_backend = selected_runtime_backend

    selected_cli_backend = str(st.session_state.get("selected_cli_backend", DEFAULT_CLI_BACKEND))
    if selected_cli_backend not in CLI_CONFIGS:
        selected_cli_backend = DEFAULT_CLI_BACKEND
    st.session_state.selected_cli_backend = selected_cli_backend

    input_mode = str(st.session_state.get("input_mode", MODE_NORMAL))
    if input_mode not in (MODE_NORMAL, MODE_FAST):
        input_mode = MODE_NORMAL
    st.session_state.input_mode = input_mode


def get_backend_display_for_metadata(metadata: Dict[str, Any]) -> str:
    mode = normalize_execution_mode(str(metadata.get("execution_mode", DEFAULT_EXECUTION_MODE)))
    if mode == EXEC_MODE_CLI:
        backend = str(metadata.get("cli_backend", DEFAULT_CLI_BACKEND))
        return safe_cli_label(backend)
    backend = str(metadata.get("runtime_backend", DEFAULT_RUNTIME_BACKEND))
    return safe_runtime_label(backend)


def build_history_rows(session_ids: List[str]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for session_id in session_ids:
        log_path = get_log_path(session_id)
        metadata = get_running_metadata(session_id)
        rows.append(
            {
                "session_id": session_id,
                "status": "running" if metadata else "idle",
                "mode": get_execution_mode_label(str(metadata.get("execution_mode", DEFAULT_EXECUTION_MODE))) if metadata else "-",
                "backend": get_backend_display_for_metadata(metadata) if metadata else "-",
                "pid": str(metadata.get("pid", "-")) if metadata else "-",
                "log_size": human_file_size(log_path.stat().st_size) if log_path.exists() else "0 B",
                "updated": format_timestamp(log_path.stat().st_mtime if log_path.exists() else None),
            }
        )
    return rows


def main() -> None:
    st.set_page_config(page_title="PatentWriterAgent Web UI", page_icon=":memo:", layout="wide")
    inject_styles()
    ensure_directories()
    cleanup_stale_pid_files()
    initialize_state()
    normalize_state_values()

    st.markdown(
        """
<div class="hero">
  <small>Patent Writer Agent</small>
  <h1>Web Control Panel</h1>
  <p>Upload a disclosure document, choose execution mode (CLI/Native), and inspect logs plus generated artifacts in one place.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    sessions = list_sessions()

    with st.sidebar:
        st.markdown("### Session")
        if st.button("Generate new session ID", width="stretch"):
            st.session_state.session_id = str(uuid.uuid4())

        st.text_input("Session ID (UUID)", key="session_id")
        st.selectbox(
            "History sessions",
            options=[""] + sessions,
            key="history_selection",
            format_func=lambda value: "Select a previous session" if value == "" else value,
        )
        if st.button("Load selected session", width="stretch"):
            selected = st.session_state.history_selection
            if selected:
                st.session_state.session_id = selected
                safe_rerun()

        st.markdown("---")
        st.markdown("### Runtime")
        st.radio(
            "Execution mode",
            options=[EXEC_MODE_NATIVE, EXEC_MODE_CLI],
            key="selected_execution_mode",
            format_func=get_execution_mode_label,
        )

        if st.session_state.selected_execution_mode == EXEC_MODE_NATIVE:
            st.selectbox(
                "Native backend",
                options=list(RUNTIME_CONFIGS.keys()),
                key="selected_runtime_backend",
                format_func=safe_runtime_label,
            )
        else:
            st.selectbox(
                "CLI backend",
                options=list(CLI_CONFIGS.keys()),
                key="selected_cli_backend",
                format_func=safe_cli_label,
            )

        st.markdown("---")
        st.markdown("### Input")
        st.radio("Input mode", options=[MODE_NORMAL, MODE_FAST], key="input_mode", format_func=get_mode_label)

        if st.session_state.input_mode == MODE_NORMAL:
            upload = st.file_uploader("Upload disclosure (.docx)", type=["docx"])
            if st.button("Save uploaded file", width="stretch"):
                current_session = st.session_state.session_id.strip()
                if not is_valid_uuid(current_session):
                    st.warning("Set a valid UUID session ID before saving the file.")
                elif upload is None:
                    st.warning("Choose a DOCX file first.")
                else:
                    saved_file = save_uploaded_file(upload, current_session)
                    st.session_state.input_path = to_display_path(saved_file)
                    st.success(f"Saved: {to_display_path(saved_file)}")

            st.text_input("Input file path", key="input_path")
        else:
            st.text_area(
                "Invention idea (fast mode)",
                key="fast_invention_idea",
                height=150,
                placeholder=(
                    "Describe your invention idea briefly. Fast mode will expand it "
                    "into a structured disclosure document automatically."
                ),
            )
            st.caption(
                "Fast mode first expands your idea into a disclosure .docx file, "
                "then runs the standard patent generation pipeline."
            )

        st.text_area(
            "Prompt override (optional)",
            key="custom_prompt",
            height=140,
            placeholder="Optional. You can use {input_path} placeholder.",
        )

        st.markdown("---")
        st.markdown("### Log options")
        st.checkbox("Show raw JSON events", key="show_raw_json")
        st.slider("Max log lines", min_value=100, max_value=3000, key="max_log_lines")
        st.checkbox("Auto refresh while running", key="auto_refresh")
        st.slider(
            "Refresh interval (seconds)",
            min_value=1,
            max_value=10,
            key="refresh_seconds",
        )

    session_id = st.session_state.session_id.strip()

    selected_execution_mode = normalize_execution_mode(str(st.session_state.selected_execution_mode))
    selected_runtime_backend = str(st.session_state.selected_runtime_backend)
    selected_cli_backend = str(st.session_state.selected_cli_backend)
    input_mode = str(st.session_state.input_mode)

    fast_idea = str(st.session_state.fast_invention_idea or "").strip()

    input_path = resolve_workspace_path(st.session_state.input_path)
    log_path = get_log_path(session_id) if is_valid_uuid(session_id) else None
    running_metadata = get_running_metadata(session_id) if is_valid_uuid(session_id) else None

    available_runtime_backends = get_available_runtime_backends()
    available_cli_backends = get_available_cli_backends()

    selected_runtime_ready = is_runtime_available(selected_runtime_backend)
    selected_cli_ready = is_cli_available(selected_cli_backend)

    selected_mode_label = get_execution_mode_label(selected_execution_mode)
    if selected_execution_mode == EXEC_MODE_CLI:
        selected_backend_label = safe_cli_label(selected_cli_backend)
        selected_ready = selected_cli_ready
    else:
        selected_backend_label = safe_runtime_label(selected_runtime_backend)
        selected_ready = selected_runtime_ready

    running_execution_mode = selected_execution_mode
    running_backend_label = selected_backend_label
    if running_metadata:
        running_execution_mode = normalize_execution_mode(
            str(running_metadata.get("execution_mode", DEFAULT_EXECUTION_MODE))
        )
        running_backend_label = get_backend_display_for_metadata(running_metadata)

    st.caption(f"Active session: `{session_id}`" if session_id else "Active session: `-`")

    status_col, pid_col, log_col, update_col = st.columns(4)
    status_col.metric("Status", "Running" if running_metadata else "Idle")
    pid_col.metric("PID", str(running_metadata.get("pid")) if running_metadata else "-")
    log_size = human_file_size(log_path.stat().st_size) if log_path and log_path.exists() else "0 B"
    log_col.metric("Log size", log_size)
    update_time = format_timestamp(log_path.stat().st_mtime if log_path and log_path.exists() else None)
    update_col.metric("Last log update", update_time)

    st.caption(
        f"Selected: `{selected_mode_label}` / `{selected_backend_label}` | "
        f"Running: `{get_execution_mode_label(running_execution_mode)}` / `{running_backend_label}`"
    )

    if not is_valid_uuid(session_id):
        st.warning("Session ID must be a valid UUID.")

    if selected_execution_mode == EXEC_MODE_NATIVE:
        if not available_runtime_backends:
            st.error(
                "No native runtime backend is ready. Configure API credentials for Anthropic-compatible or OpenAI-compatible backend."
            )
        elif not selected_runtime_ready:
            st.warning(
                f"{selected_backend_label} is not ready. {runtime_setup_hint(selected_runtime_backend)}"
            )
    else:
        if not available_cli_backends:
            st.error(
                "No supported CLI found in PATH. Install Claude CLI, OpenAI Codex CLI, or Google Gemini CLI."
            )
        elif not selected_cli_ready:
            st.warning(f"{selected_backend_label} is not found in PATH.")

    if input_mode == MODE_NORMAL and not input_path.exists():
        st.warning(f"Input file does not exist: {input_path}")
    if input_mode == MODE_FAST and not fast_idea:
        st.warning("Fast mode requires a brief invention idea.")

    start_col, stop_col, cleanup_col, refresh_col = st.columns(4)
    start_disabled = (
        running_metadata is not None
        or not is_valid_uuid(session_id)
        or not selected_ready
        or (input_mode == MODE_NORMAL and not input_path.exists())
        or (input_mode == MODE_FAST and not fast_idea)
    )

    start_clicked = start_col.button(
        "Start generation",
        type="primary",
        width="stretch",
        disabled=start_disabled,
    )
    stop_clicked = stop_col.button(
        "Stop session",
        width="stretch",
        disabled=running_metadata is None,
    )

    cleanup_mode = running_execution_mode if running_metadata else selected_execution_mode
    cleanup_cli_backend = selected_cli_backend
    if running_metadata and cleanup_mode == EXEC_MODE_CLI:
        cleanup_cli_backend = str(running_metadata.get("cli_backend", DEFAULT_CLI_BACKEND))

    cleanup_label = "Runner process" if cleanup_mode == EXEC_MODE_NATIVE else safe_cli_label(cleanup_cli_backend)

    cleanup_clicked = cleanup_col.button(
        f"Force cleanup {cleanup_label}",
        width="stretch",
    )
    refresh_clicked = refresh_col.button("Refresh", width="stretch")

    if start_clicked:
        effective_input_path = input_path

        if input_mode == MODE_FAST:
            with st.spinner("Fast mode: expanding invention idea into disclosure document..."):
                fast_ok, fast_message, generated_path = prepare_fast_mode_input(
                    session_id=session_id,
                    execution_mode=selected_execution_mode,
                    runtime_backend=selected_runtime_backend,
                    cli_backend=selected_cli_backend,
                    invention_idea=fast_idea,
                )
            if not fast_ok:
                st.error(fast_message)
                safe_rerun()
                return

            if generated_path is None:
                st.error("Fast mode did not return a generated input file.")
                safe_rerun()
                return

            effective_input_path = generated_path
            st.session_state.input_path = to_display_path(generated_path)
            st.success(fast_message)

        success, message = start_generation(
            session_id=session_id,
            input_path=effective_input_path,
            custom_prompt=st.session_state.custom_prompt,
            execution_mode=selected_execution_mode,
            runtime_backend=selected_runtime_backend,
            cli_backend=selected_cli_backend,
        )
        if success:
            st.success(message)
            safe_rerun()
        else:
            st.error(message)

    if stop_clicked:
        success, message = stop_generation(session_id)
        if success:
            st.success(message)
        else:
            st.warning(message)
        safe_rerun()

    if cleanup_clicked:
        if cleanup_mode == EXEC_MODE_CLI:
            killed, _ = cleanup_all_cli_processes(cleanup_cli_backend)
            if killed > 0:
                st.success(
                    f"Force cleanup completed, terminated {killed} {safe_cli_label(cleanup_cli_backend)} process(es)."
                )
            else:
                st.info(f"No running {safe_cli_label(cleanup_cli_backend)} process found to terminate.")
        else:
            killed, _ = cleanup_all_runner_processes()
            if killed > 0:
                st.success(f"Force cleanup completed, terminated {killed} runner process(es).")
            else:
                st.info("No running pipeline runner process found to terminate.")
        safe_rerun()

    if refresh_clicked:
        safe_rerun()

    log_tab, output_tab, history_tab = st.tabs(["Log stream", "Generated files", "History"])

    with log_tab:
        if not is_valid_uuid(session_id):
            st.info("Provide a valid session ID to inspect logs.")
        else:
            lines = tail_log_lines(get_log_path(session_id), st.session_state.max_log_lines)
            if not lines:
                st.info("No log content available yet.")
            else:
                if st.session_state.show_raw_json:
                    st.code("".join(lines), language="json")
                else:
                    rendered = render_formatted_logs(lines)
                    st.code(rendered or "No parseable messages in current log window.", language="text")

            current_log_path = get_log_path(session_id)
            if current_log_path.exists():
                st.download_button(
                    label=f"Download {current_log_path.name}",
                    data=current_log_path.read_bytes(),
                    file_name=current_log_path.name,
                    mime="text/plain",
                    width="stretch",
                )

    with output_tab:
        if not is_valid_uuid(session_id):
            st.info("Provide a valid session ID to inspect generated files.")
        else:
            session_dir = get_session_dir(session_id)
            st.caption(to_display_path(session_dir))
            archive_data = build_session_archive(session_id)
            if archive_data is not None:
                st.download_button(
                    label=f"Download session bundle ({session_id}.zip)",
                    data=archive_data,
                    file_name=f"patent_session_{session_id}.zip",
                    mime="application/zip",
                    width="stretch",
                )
            render_file_preview(
                "01_input/parsed_info.json",
                session_dir / "01_input" / "parsed_info.json",
                "json",
            )
            render_file_preview(
                "04_content/abstract.md",
                session_dir / "04_content" / "abstract.md",
                "markdown",
            )
            render_file_preview(
                "04_content/claims.md",
                session_dir / "04_content" / "claims.md",
                "markdown",
            )
            render_file_preview(
                "04_content/description.md",
                session_dir / "04_content" / "description.md",
                "markdown",
            )
            render_file_preview(
                "06_final/complete_patent.md",
                session_dir / "06_final" / "complete_patent.md",
                "markdown",
            )

    with history_tab:
        session_rows = build_history_rows(sessions)
        if not session_rows:
            st.info("No history sessions found.")
        else:
            st.dataframe(session_rows, width="stretch", hide_index=True)
            st.caption("Load a session from the sidebar to inspect or continue it.")

    if st.session_state.auto_refresh and running_metadata is not None:
        time.sleep(int(st.session_state.refresh_seconds))
        safe_rerun()


if __name__ == "__main__":
    main()
