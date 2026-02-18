"""Process lifecycle management: PID tracking, termination, cleanup."""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psutil

from app.config import (
    CLI_CONFIGS,
    DEFAULT_CLI_BACKEND,
    DEFAULT_EXECUTION_MODE,
    EXEC_MODE_CLI,
    EXEC_MODE_NATIVE,
    OUTPUT_DIR,
)
from app.session import get_pid_path
from app.utils import is_valid_uuid, read_json_file, write_json_file
from app.backend import (
    infer_cli_backend_from_command,
    infer_execution_mode_from_command,
    infer_runtime_backend_from_command,
)

from datetime import datetime


def is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False


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
    from app.backend import get_cli_process_keyword

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
