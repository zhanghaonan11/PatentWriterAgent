#!/usr/bin/env python3
"""Bootstrap script for PatentWriterAgent Streamlit frontend."""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

from runtime_client import RUNTIME_CONFIGS, get_available_runtime_backends, runtime_setup_hint
from app.config import CLI_CONFIGS


ROOT_DIR = Path(__file__).resolve().parent
REQUIREMENTS_FILE = ROOT_DIR / "requirements.txt"
APP_FILE = ROOT_DIR / "patent_writer_app.py"
PIPELINE_RUNNER = ROOT_DIR / "pipeline_runner.py"

REQUIRED_MODULES = ["streamlit", "psutil", "markitdown"]


def log(message: str) -> None:
    print(f"[run_app] {message}")


def ensure_python_version() -> None:
    if sys.version_info < (3, 8):
        raise SystemExit("Python 3.8+ is required.")


def ensure_directories() -> None:
    (ROOT_DIR / "data").mkdir(parents=True, exist_ok=True)
    (ROOT_DIR / "data" / "uploads").mkdir(parents=True, exist_ok=True)
    (ROOT_DIR / "output").mkdir(parents=True, exist_ok=True)


def module_exists(module_name: str) -> bool:
    return importlib.util.find_spec(module_name) is not None


def install_requirements() -> None:
    if not REQUIREMENTS_FILE.exists():
        raise SystemExit(f"Missing requirements file: {REQUIREMENTS_FILE}")

    cmd = [sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS_FILE)]
    log("Installing Python dependencies from requirements.txt ...")
    result = subprocess.run(cmd, cwd=str(ROOT_DIR), check=False)
    if result.returncode != 0:
        raise SystemExit("Dependency installation failed.")


def ensure_dependencies(auto_install: bool) -> None:
    missing: List[str] = [name for name in REQUIRED_MODULES if not module_exists(name)]
    if not missing:
        log("Python dependencies ready.")
        return

    log(f"Missing modules: {', '.join(missing)}")
    if auto_install:
        install_requirements()
        still_missing: List[str] = [name for name in REQUIRED_MODULES if not module_exists(name)]
        if still_missing:
            raise SystemExit(f"Modules still missing after installation: {', '.join(still_missing)}")
        log("Dependencies installed successfully.")
        return

    raise SystemExit("Install dependencies first: pip install -r requirements.txt")


def check_runtime_backends() -> None:
    available = get_available_runtime_backends()

    for backend, cfg in RUNTIME_CONFIGS.items():
        if backend in available:
            log(f"{cfg.label} detected.")
        else:
            log(f"Warning: {cfg.label} not ready. {runtime_setup_hint(backend)}")

    if available:
        labels = [RUNTIME_CONFIGS[b].label for b in available]
        log(f"Available native runtimes: {', '.join(labels)}")
        return

    log("Warning: No native runtime backend is ready.")


def check_cli_backends() -> None:
    detected = []
    for cfg in CLI_CONFIGS.values():
        cli_path = shutil.which(cfg["binary"])
        if cli_path:
            detected.append(cfg["label"])
            log(f"{cfg['label']} detected: {cli_path}")
        else:
            log(f"Warning: {cfg['label']} not found in PATH.")

    if detected:
        log(f"Available CLI runtimes: {', '.join(detected)}")
    else:
        log("Warning: No CLI runtime detected.")


def check_required_files() -> None:
    missing: List[Path] = []
    for path in (APP_FILE, PIPELINE_RUNNER):
        if not path.exists():
            missing.append(path)

    if missing:
        raise SystemExit("Missing required files:\n" + "\n".join(str(p) for p in missing))


def launch_streamlit(host: str, port: int) -> int:
    cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(APP_FILE),
        "--server.address",
        host,
        "--server.port",
        str(port),
    ]
    env = os.environ.copy()
    env.setdefault("STREAMLIT_BROWSER_GATHER_USAGE_STATS", "false")

    log(f"Starting Streamlit on http://{host}:{port}")
    return subprocess.call(cmd, cwd=str(ROOT_DIR), env=env)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start the PatentWriterAgent web app")
    parser.add_argument("--host", default=os.environ.get("STREAMLIT_SERVER_ADDRESS", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("STREAMLIT_SERVER_PORT", "8501")))
    parser.add_argument("--skip-install", action="store_true")
    parser.add_argument("--check-only", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    ensure_python_version()
    ensure_directories()
    check_required_files()
    ensure_dependencies(auto_install=not args.skip_install)
    check_runtime_backends()
    check_cli_backends()

    if args.check_only:
        log("Environment checks completed.")
        return

    code = launch_streamlit(args.host, args.port)
    if code != 0:
        raise SystemExit(code)


if __name__ == "__main__":
    main()
