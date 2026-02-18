#!/usr/bin/env python3
"""Streamlit frontend for PatentWriterAgent without external AI CLI dependencies."""

from __future__ import annotations

import io
import json
import os
import re
import shlex
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


def build_fast_mode_prompt(invention_idea: str) -> str:
    return f"""你是一名中国专利技术交底书助手。
请把用户提供的“简要发明构思”扩写为可直接用于专利生成流程的技术交底书草稿。

严格要求：
1. 只输出最终正文，不要输出分析过程、前言、结语。
2. 使用以下固定标题并按顺序输出（每个标题单独一行）：
发明名称
要解决的技术问题
现有技术方案及缺点
本发明技术方案（详细描述）
有益效果
技术关键词
3. 内容必须具体、工程化，不要空泛宣传。
4. 对用户没有提供的关键细节，使用“假设：...”补齐。
5. 技术关键词提供 5-10 个，使用中文逗号分隔。
6. 最终输出必须包裹在以下标记之间：
<FAST_DISCLOSURE_START>
...正文...
<FAST_DISCLOSURE_END>

用户的简要发明构思：
{invention_idea.strip()}
"""


def extract_fast_disclosure_text(raw_output: str) -> str:
    merged = normalize_newlines(raw_output).strip()
    m = re.search(r"<FAST_DISCLOSURE_START>(.*?)<FAST_DISCLOSURE_END>", merged, flags=re.DOTALL)
    if m:
        return m.group(1).strip()

    fence = re.search(r"```(?:markdown|md|text)?\s*(.*?)```", merged, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        return fence.group(1).strip()

    return merged


def ensure_fast_disclosure_sections(content: str, invention_idea: str) -> str:
    text = normalize_newlines(content).strip()
    if not text:
        text = "发明名称\n待补充\n\n要解决的技术问题\n" + (invention_idea.strip() or "待补充")

    missing = [title for title in FAST_SECTION_TITLES if title not in text]
    if missing:
        parts: List[str] = []
        for title in missing:
            placeholder = invention_idea.strip() if title == "要解决的技术问题" else "待补充（假设：后续补齐参数、结构与实施细节）"
            parts.append(f"{title}\n{placeholder or '待补充'}")
        text = text.rstrip() + "\n\n" + "\n\n".join(parts)

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
        else:
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


def get_mode_label(mode: str) -> str:
    return "Fast mode (idea -> disclosure -> patent)" if mode == MODE_FAST else "Normal mode (.docx -> patent)"


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
    if "runtime_backend" not in metadata:
        cmd = metadata.get("command")
        metadata["runtime_backend"] = infer_runtime_backend_from_command(cmd if isinstance(cmd, list) else [])
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

        if not is_pid_running(pid):
            try:
                pid_file.unlink()
            except OSError:
                pass


def write_pid_metadata(session_id: str, pid: int, command: List[str], input_path: Path, prompt: str, runtime_backend: str) -> None:
    write_json_file(
        get_pid_path(session_id),
        {
            "pid": pid,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "command": command,
            "input_path": str(input_path),
            "prompt": prompt,
            "runtime_backend": runtime_backend,
        },
    )


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


def build_session_archive(session_id: str) -> Optional[bytes]:
    session_dir = get_session_dir(session_id)
    log_path = get_log_path(session_id)
    if not session_dir.exists() and not log_path.exists():
        return None

    buf = io.BytesIO()
    count = 0
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        if session_dir.exists():
            for file_path in sorted(session_dir.rglob("*")):
                if not file_path.is_file():
                    continue
                zf.write(file_path, (Path(f"temp_{session_id}") / file_path.relative_to(session_dir)).as_posix())
                count += 1
        if log_path.exists():
            zf.write(log_path, log_path.name)
            count += 1

    if count == 0:
        return None
    return buf.getvalue()


def list_sessions() -> List[str]:
    scores: Dict[str, float] = {}

    for log_file in OUTPUT_DIR.glob("*.log"):
        sid = log_file.stem
        if is_valid_uuid(sid):
            scores[sid] = max(scores.get(sid, 0.0), log_file.stat().st_mtime)

    for temp_dir in OUTPUT_DIR.glob("temp_*"):
        sid = temp_dir.name.replace("temp_", "", 1)
        if is_valid_uuid(sid):
            scores[sid] = max(scores.get(sid, 0.0), temp_dir.stat().st_mtime)

    for pid_file in OUTPUT_DIR.glob("*.pid.json"):
        sid = pid_file.name[: -len(".pid.json")]
        if is_valid_uuid(sid):
            scores[sid] = max(scores.get(sid, 0.0), pid_file.stat().st_mtime)

    return [sid for sid, _ in sorted(scores.items(), key=lambda x: x[1], reverse=True)]


def human_file_size(num_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(num_bytes)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{num_bytes} B"


def format_timestamp(ts: Optional[float]) -> str:
    if ts is None:
        return "-"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


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


def build_runner_command(runtime_backend: str, session_id: str, input_path: Path, prompt: str) -> List[str]:
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


def append_log_banner(session_id: str, command: List[str]) -> None:
    path = get_log_path(session_id)
    with path.open("a", encoding="utf-8") as h:
        h.write(f"\n=== [{datetime.now().isoformat(timespec='seconds')}] session {session_id} started ===\n")
        h.write("Command: " + " ".join(shlex.quote(x) for x in command) + "\n")


def append_log_footer(session_id: str, note: str) -> None:
    path = get_log_path(session_id)
    with path.open("a", encoding="utf-8") as h:
        h.write(f"\n=== [{datetime.now().isoformat(timespec='seconds')}] {note} ===\n")


def save_uploaded_file(uploaded_file: Any, session_id: str) -> Path:
    target = DATA_DIR / "uploads" / session_id
    target.mkdir(parents=True, exist_ok=True)
    filename = Path(uploaded_file.name).name or "input.docx"
    path = target / filename
    path.write_bytes(uploaded_file.getbuffer())
    return path


def generate_fast_disclosure_once(runtime_backend: str, invention_idea: str, timeout_seconds: int = 300) -> Tuple[bool, str, str]:
    prompt = build_fast_mode_prompt(invention_idea)
    try:
        out = generate_text(runtime_backend=runtime_backend, prompt=prompt, max_tokens=2200, temperature=0.3, timeout_seconds=timeout_seconds)
        return True, normalize_newlines(out), ""
    except RuntimeClientError as exc:
        return False, "", str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, "", f"Runtime call failed during fast-mode preprocessing: {exc}"


def prepare_fast_mode_input(session_id: str, runtime_backend: str, invention_idea: str) -> Tuple[bool, str, Optional[Path]]:
    idea = invention_idea.strip()
    if not idea:
        return False, "Fast mode requires a non-empty invention idea.", None

    append_log_footer(session_id, f"fast mode preprocessing started (backend={safe_runtime_label(runtime_backend)})")

    ok, raw, err = generate_fast_disclosure_once(runtime_backend, idea)
    if raw.strip():
        append_log_footer(session_id, "fast mode preprocessing output")
        with get_log_path(session_id).open("a", encoding="utf-8") as h:
            h.write(raw + "\n")

    if not ok:
        append_log_footer(session_id, f"fast mode preprocessing failed: {err}")
        return False, err, None

    expanded = ensure_fast_disclosure_sections(extract_fast_disclosure_text(raw), idea)
    if len(expanded.strip()) < 80:
        msg = "Fast mode generated insufficient disclosure content. Please provide more detail and retry."
        append_log_footer(session_id, f"fast mode preprocessing failed: {msg}")
        return False, msg, None

    target = DATA_DIR / "uploads" / session_id
    target.mkdir(parents=True, exist_ok=True)
    md_path = target / "fast_disclosure.md"
    docx_path = target / "fast_disclosure.docx"
    md_path.write_text(expanded, encoding="utf-8")
    write_simple_docx(docx_path, expanded)

    append_log_footer(session_id, f"fast mode preprocessing completed: {to_display_path(docx_path)}")
    return True, f"Fast mode generated disclosure file: {to_display_path(docx_path)}", docx_path


def start_generation(session_id: str, input_path: Path, custom_prompt: str, runtime_backend: str) -> Tuple[bool, str]:
    if not is_valid_uuid(session_id):
        return False, "Session ID must be a valid UUID."
    if get_running_metadata(session_id):
        return False, "This session is already running."
    if not input_path.exists():
        return False, f"Input file not found: {input_path}"
    if not is_runtime_available(runtime_backend):
        return False, f"{safe_runtime_label(runtime_backend)} is not ready. {runtime_setup_hint(runtime_backend)}"

    prompt = build_prompt(custom_prompt, input_path)
    command = build_runner_command(runtime_backend, session_id, input_path, prompt)

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
        return False, f"Failed to start pipeline process: {exc}"

    write_pid_metadata(session_id, process.pid, command, input_path, prompt, runtime_backend)
    return True, f"Started session {session_id} with {safe_runtime_label(runtime_backend)} (PID {process.pid})."


def stop_generation(session_id: str) -> Tuple[bool, str]:
    metadata = get_running_metadata(session_id)
    if not metadata:
        remove_pid_metadata(session_id)
        return False, "No running process found for this session."

    pid = int(metadata["pid"])
    ok, msg = terminate_pid_tree(pid)
    remove_pid_metadata(session_id)
    append_log_footer(session_id, f"session stopped manually (pid={pid})")
    return ok, f"{msg} pid={pid}"


def tail_log_lines(path: Path, max_lines: int) -> List[str]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as h:
            return list(deque(h, maxlen=max_lines))
    except OSError:
        return []


def render_formatted_logs(lines: List[str]) -> str:
    rendered: List[str] = []
    for line in lines:
        t = line.strip()
        if not t:
            continue
        if not t.startswith("{"):
            rendered.append(t)
            continue
        try:
            payload = json.loads(t)
        except json.JSONDecodeError:
            rendered.append(t)
            continue
        event_type = payload.get("type")
        if event_type == "assistant":
            msg = payload.get("message") or {}
            content = msg.get("content") or []
            parts: List[str] = []
            for item in content:
                if item.get("type") == "text":
                    body = str(item.get("text", "")).strip()
                    if body:
                        parts.append(body)
            if parts:
                rendered.append("\n".join(parts))
        elif event_type == "result":
            result = str(payload.get("result", "")).strip()
            rendered.append(f"[result] {result}" if result else "[result] completed")
        else:
            rendered.append(t)
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
            use_container_width=True,
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
    default_backend = DEFAULT_RUNTIME_BACKEND if DEFAULT_RUNTIME_BACKEND in RUNTIME_CONFIGS else list(RUNTIME_CONFIGS.keys())[0]
    defaults: Dict[str, Any] = {
        "session_id": str(uuid.uuid4()),
        "history_selection": "",
        "selected_runtime_backend": default_backend,
        "input_mode": MODE_NORMAL,
        "input_path": to_display_path(get_default_input_path()),
        "fast_invention_idea": "",
        "custom_prompt": "",
        "show_raw_json": False,
        "max_log_lines": 500,
        "auto_refresh": True,
        "refresh_seconds": 2,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def build_history_rows(session_ids: List[str]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for sid in session_ids:
        log_path = get_log_path(sid)
        metadata = get_running_metadata(sid)
        backend = str(metadata.get("runtime_backend", DEFAULT_RUNTIME_BACKEND)) if metadata else "-"
        rows.append(
            {
                "session_id": sid,
                "status": "running" if metadata else "idle",
                "runtime": safe_runtime_label(backend) if metadata else "-",
                "pid": metadata.get("pid", "-") if metadata else "-",
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

    st.markdown(
        """
<div class="hero">
  <small>Patent Writer Agent</small>
  <h1>Web Control Panel</h1>
  <p>Upload a disclosure document, choose a runtime backend, and inspect logs plus generated artifacts in one place.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    sessions = list_sessions()

    with st.sidebar:
        st.markdown("### Session")
        if st.button("Generate new session ID", use_container_width=True):
            st.session_state.session_id = str(uuid.uuid4())

        st.text_input("Session ID (UUID)", key="session_id")
        st.selectbox(
            "History sessions",
            options=[""] + sessions,
            key="history_selection",
            format_func=lambda value: "Select a previous session" if value == "" else value,
        )
        if st.button("Load selected session", use_container_width=True):
            selected = st.session_state.history_selection
            if selected:
                st.session_state.session_id = selected
                safe_rerun()

        st.markdown("---")
        st.markdown("### Runtime")
        st.selectbox(
            "Runtime backend",
            options=list(RUNTIME_CONFIGS.keys()),
            key="selected_runtime_backend",
            format_func=get_runtime_label,
        )

        st.markdown("---")
        st.markdown("### Input")
        st.radio("Input mode", options=[MODE_NORMAL, MODE_FAST], key="input_mode", format_func=get_mode_label)

        if st.session_state.input_mode == MODE_NORMAL:
            upload = st.file_uploader("Upload disclosure (.docx)", type=["docx"])
            if st.button("Save uploaded file", use_container_width=True):
                sid = st.session_state.session_id.strip()
                if not is_valid_uuid(sid):
                    st.warning("Set a valid UUID session ID before saving the file.")
                elif upload is None:
                    st.warning("Choose a DOCX file first.")
                else:
                    saved = save_uploaded_file(upload, sid)
                    st.session_state.input_path = to_display_path(saved)
                    st.success(f"Saved: {to_display_path(saved)}")

            st.text_input("Input file path", key="input_path")
        else:
            st.text_area(
                "Invention idea (fast mode)",
                key="fast_invention_idea",
                height=150,
                placeholder="Describe your invention idea briefly. Fast mode will expand it into a structured disclosure document automatically.",
            )
            st.caption("Fast mode first expands your idea into a disclosure .docx file, then runs the standard patent generation pipeline.")

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
        st.slider("Refresh interval (seconds)", min_value=1, max_value=10, key="refresh_seconds")

    session_id = st.session_state.session_id.strip()
    selected_runtime_backend = str(st.session_state.selected_runtime_backend)
    if selected_runtime_backend not in RUNTIME_CONFIGS:
        selected_runtime_backend = DEFAULT_RUNTIME_BACKEND if DEFAULT_RUNTIME_BACKEND in RUNTIME_CONFIGS else list(RUNTIME_CONFIGS.keys())[0]
        st.session_state.selected_runtime_backend = selected_runtime_backend

    input_mode = str(st.session_state.input_mode)
    if input_mode not in (MODE_NORMAL, MODE_FAST):
        input_mode = MODE_NORMAL
        st.session_state.input_mode = input_mode

    fast_idea = str(st.session_state.fast_invention_idea or "").strip()

    input_path = resolve_workspace_path(st.session_state.input_path)
    log_path = get_log_path(session_id) if is_valid_uuid(session_id) else None
    running_metadata = get_running_metadata(session_id) if is_valid_uuid(session_id) else None

    available_backends = get_available_runtime_backends()
    selected_runtime_ready = is_runtime_available(selected_runtime_backend)
    selected_runtime_label = safe_runtime_label(selected_runtime_backend)

    running_runtime_backend = selected_runtime_backend
    if running_metadata:
        running_runtime_backend = str(running_metadata.get("runtime_backend") or infer_runtime_backend_from_command(running_metadata.get("command") or []))

    st.caption(f"Active session: `{session_id}`" if session_id else "Active session: `-`")

    status_col, pid_col, log_col, update_col = st.columns(4)
    status_col.metric("Status", "Running" if running_metadata else "Idle")
    pid_col.metric("PID", str(running_metadata.get("pid")) if running_metadata else "-")
    log_col.metric("Log size", human_file_size(log_path.stat().st_size) if log_path and log_path.exists() else "0 B")
    update_col.metric("Last log update", format_timestamp(log_path.stat().st_mtime if log_path and log_path.exists() else None))

    st.caption(f"Selected runtime: `{selected_runtime_label}` | Running runtime: `{safe_runtime_label(running_runtime_backend)}`")

    if not is_valid_uuid(session_id):
        st.warning("Session ID must be a valid UUID.")

    if not available_backends:
        st.error("No runtime backend is ready. Configure API credentials for Anthropic-compatible or OpenAI-compatible backend.")
    elif not selected_runtime_ready:
        st.warning(f"{selected_runtime_label} is not ready. {runtime_setup_hint(selected_runtime_backend)}")

    if input_mode == MODE_NORMAL and not input_path.exists():
        st.warning(f"Input file does not exist: {input_path}")
    if input_mode == MODE_FAST and not fast_idea:
        st.warning("Fast mode requires a brief invention idea.")

    start_col, stop_col, cleanup_col, refresh_col = st.columns(4)
    start_disabled = (
        running_metadata is not None
        or not is_valid_uuid(session_id)
        or not selected_runtime_ready
        or (input_mode == MODE_NORMAL and not input_path.exists())
        or (input_mode == MODE_FAST and not fast_idea)
    )

    start_clicked = start_col.button("Start generation", type="primary", use_container_width=True, disabled=start_disabled)
    stop_clicked = stop_col.button("Stop session", use_container_width=True, disabled=running_metadata is None)
    cleanup_clicked = cleanup_col.button("Force cleanup runner", use_container_width=True, help="Terminate all local pipeline runner processes.")
    refresh_clicked = refresh_col.button("Refresh", use_container_width=True)

    if start_clicked:
        effective_input_path = input_path
        if input_mode == MODE_FAST:
            with st.spinner("Fast mode: expanding invention idea into disclosure document..."):
                ok, msg, generated = prepare_fast_mode_input(session_id, selected_runtime_backend, fast_idea)
            if not ok:
                st.error(msg)
                safe_rerun()
                return
            if generated is None:
                st.error("Fast mode did not return a generated input file.")
                safe_rerun()
                return
            effective_input_path = generated
            st.session_state.input_path = to_display_path(generated)
            st.success(msg)

        ok, msg = start_generation(session_id, effective_input_path, st.session_state.custom_prompt, selected_runtime_backend)
        if ok:
            st.success(msg)
            safe_rerun()
        else:
            st.error(msg)

    if stop_clicked:
        ok, msg = stop_generation(session_id)
        st.success(msg) if ok else st.warning(msg)
        safe_rerun()

    if cleanup_clicked:
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
                    st.code(render_formatted_logs(lines) or "No parseable messages in current log window.", language="text")

            current_log = get_log_path(session_id)
            if current_log.exists():
                st.download_button(
                    label=f"Download {current_log.name}",
                    data=current_log.read_bytes(),
                    file_name=current_log.name,
                    mime="text/plain",
                    use_container_width=True,
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
                    use_container_width=True,
                )

            render_file_preview("01_input/parsed_info.json", session_dir / "01_input" / "parsed_info.json", "json")
            render_file_preview("04_content/abstract.md", session_dir / "04_content" / "abstract.md", "markdown")
            render_file_preview("04_content/claims.md", session_dir / "04_content" / "claims.md", "markdown")
            render_file_preview("04_content/description.md", session_dir / "04_content" / "description.md", "markdown")
            render_file_preview("06_final/complete_patent.md", session_dir / "06_final" / "complete_patent.md", "markdown")

    with history_tab:
        rows = build_history_rows(sessions)
        if not rows:
            st.info("No history sessions found.")
        else:
            st.dataframe(rows, use_container_width=True, hide_index=True)
            st.caption("Load a session from the sidebar to inspect or continue it.")

    if st.session_state.auto_refresh and running_metadata is not None:
        time.sleep(int(st.session_state.refresh_seconds))
        safe_rerun()


if __name__ == "__main__":
    main()
