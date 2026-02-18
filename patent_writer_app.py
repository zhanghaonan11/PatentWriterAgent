#!/usr/bin/env python3
"""Streamlit frontend for PatentWriterAgent with dual runtime support."""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import streamlit as st

from runtime_client import (
    DEFAULT_RUNTIME_BACKEND,
    RUNTIME_CONFIGS,
    get_available_runtime_backends,
    is_runtime_available,
    runtime_setup_hint,
)

from app.config import (
    CLI_CONFIGS,
    DATA_DIR,
    DEFAULT_CLI_BACKEND,
    DEFAULT_EXECUTION_MODE,
    DEFAULT_DESCRIPTION_PARALLELISM,
    DESCRIPTION_PARALLELISM_MAX,
    DESCRIPTION_PARALLELISM_MIN,
    EXEC_MODE_CLI,
    EXEC_MODE_NATIVE,
    MODE_FAST,
    MODE_NORMAL,
    OUTPUT_DIR,
    PREVIEW_CHAR_LIMIT,
    ROOT_DIR,
)
from app.utils import (
    clamp_int,
    format_timestamp,
    human_file_size,
    is_valid_uuid,
    read_text_preview,
    to_display_path,
    to_positive_int,
    resolve_workspace_path,
)
from app.backend import (
    build_cli_command,
    build_runner_command,
    clamp_description_parallelism,
    get_available_cli_backends,
    get_execution_mode_label,
    get_mode_label,
    is_cli_available,
    normalize_execution_mode,
    safe_cli_label,
    safe_runtime_label,
    get_cli_label,
)
from app.session import (
    append_log_banner,
    append_log_footer,
    build_session_archive,
    get_log_path,
    get_session_dir,
    list_sessions,
    save_uploaded_file,
    tail_log_lines,
)
from app.process_manager import (
    cleanup_all_cli_processes,
    cleanup_all_runner_processes,
    cleanup_stale_pid_files,
    get_running_metadata,
    remove_pid_metadata,
    terminate_pid_tree,
    write_pid_metadata,
)
from app.fast_mode import prepare_fast_mode_input


# ---------------------------------------------------------------------------
# Helpers (UI-specific, kept here because they are tightly coupled to st)
# ---------------------------------------------------------------------------


def build_prompt(custom_prompt: str, input_path: Path) -> str:
    prompt = custom_prompt.strip()
    if prompt:
        return prompt.replace("{input_path}", to_display_path(input_path))
    return f"根据 {to_display_path(input_path)} 编写专利提案"


def get_default_input_path() -> Path:
    candidate = DATA_DIR / "input.docx"
    if candidate.exists():
        return candidate
    files = sorted(DATA_DIR.glob("*.docx"))
    return files[0] if files else candidate


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


# ---------------------------------------------------------------------------
# Core actions
# ---------------------------------------------------------------------------


def start_generation(
    session_id: str,
    input_path: Path,
    custom_prompt: str,
    execution_mode: str,
    runtime_backend: str,
    cli_backend: str,
    description_parallelism: int,
) -> tuple[bool, str]:
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
        command = build_cli_command(
            cli_backend,
            session_id,
            prompt,
            input_path=input_path,
            description_parallelism=clamp_description_parallelism(description_parallelism),
            fast_mode=False,
        )
        backend_msg = get_cli_label(cli_backend)
    else:
        if not is_runtime_available(runtime_backend):
            return (
                False,
                f"{safe_runtime_label(runtime_backend)} is not ready. {runtime_setup_hint(runtime_backend)}",
            )
        command = build_runner_command(
            runtime_backend,
            session_id,
            input_path,
            prompt,
            clamp_description_parallelism(description_parallelism),
        )
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


def stop_generation(session_id: str) -> tuple[bool, str]:
    metadata = get_running_metadata(session_id)
    if not metadata:
        remove_pid_metadata(session_id)
        return False, "No running process found for this session."

    pid = int(metadata["pid"])
    success, message = terminate_pid_tree(pid)
    remove_pid_metadata(session_id)
    append_log_footer(session_id, f"session stopped manually (pid={pid})")
    return success, f"{message} pid={pid}"


# ---------------------------------------------------------------------------
# Streamlit state & UI
# ---------------------------------------------------------------------------


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
        "description_parallelism": DEFAULT_DESCRIPTION_PARALLELISM,
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

    parallelism_raw = st.session_state.get("description_parallelism", DEFAULT_DESCRIPTION_PARALLELISM)
    parallelism = to_positive_int(parallelism_raw, DEFAULT_DESCRIPTION_PARALLELISM)
    st.session_state.description_parallelism = clamp_int(
        parallelism,
        DESCRIPTION_PARALLELISM_MIN,
        DESCRIPTION_PARALLELISM_MAX,
    )


# ---------------------------------------------------------------------------
# Main page
# ---------------------------------------------------------------------------


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
            st.slider(
                "Description parallelism",
                min_value=DESCRIPTION_PARALLELISM_MIN,
                max_value=DESCRIPTION_PARALLELISM_MAX,
                key="description_parallelism",
                help="Concurrent section generation in description stage (native runtime only).",
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
    description_parallelism = clamp_description_parallelism(
        to_positive_int(st.session_state.description_parallelism, DEFAULT_DESCRIPTION_PARALLELISM)
    )

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
        f" | Description parallelism: `{description_parallelism}`"
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
            description_parallelism=description_parallelism,
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
            archive_data: Optional[bytes] = None
            if running_metadata is None:
                archive_data = build_session_archive(session_id)
            if archive_data is not None:
                st.download_button(
                    label=f"Download session bundle ({session_id}.zip)",
                    data=archive_data,
                    file_name=f"patent_session_{session_id}.zip",
                    mime="application/zip",
                    width="stretch",
                )
            else:
                st.caption("Session bundle will be available after run completion.")
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
