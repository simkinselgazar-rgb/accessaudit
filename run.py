#!/usr/bin/env python3
"""WCAG Trusted Tester v6 -- Single entry point launcher.

Usage:
    python run.py              # Start the application
    python run.py --setup      # Install dependencies and setup
    python run.py --port 8080  # Custom port
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).parent


# ── Python checks ────────────────────────────────────────────────────────────

def check_python_version() -> None:
    if sys.version_info < (3, 11):
        print(f"ERROR: Python 3.11+ required (you have {sys.version})")
        sys.exit(1)
    print(f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")


# ── Virtual environment ──────────────────────────────────────────────────────

def _venv_dir() -> Path:
    return PROJECT_DIR / "venv"


def _pip() -> str:
    venv = _venv_dir()
    return str(venv / ("Scripts" if os.name == "nt" else "bin") / ("pip.exe" if os.name == "nt" else "pip"))


def _python() -> str:
    venv = _venv_dir()
    if venv.exists():
        return str(venv / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python"))
    return sys.executable


def create_venv() -> None:
    venv = _venv_dir()
    if not venv.exists():
        print("Creating virtual environment...")
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        print("Virtual environment created.")


def install_dependencies() -> None:
    print("Installing dependencies...")
    subprocess.run([_pip(), "install", "-r", str(PROJECT_DIR / "requirements.txt")], check=True)
    print("Dependencies installed.")


def install_playwright() -> None:
    print("Installing Playwright Chromium browser...")
    subprocess.run([_python(), "-m", "playwright", "install", "chromium"], check=True)
    print("Playwright browser installed.")


def install_ffmpeg() -> None:
    print("Installing ffmpeg (via Playwright)...")
    try:
        subprocess.run([_python(), "-m", "playwright", "install", "ffmpeg"], check=True, capture_output=False)
    except subprocess.CalledProcessError:
        print("  Playwright ffmpeg install failed -- trying pip imageio-ffmpeg...")
        try:
            subprocess.run([_pip(), "install", "imageio-ffmpeg"], check=True, capture_output=False)
        except subprocess.CalledProcessError:
            pass
    # Also try ffmpeg-python as fallback
    try:
        subprocess.run([_pip(), "install", "ffmpeg-python"], check=True, capture_output=True)
    except subprocess.CalledProcessError:
        pass


def check_ffmpeg() -> bool:
    try:
        result = subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
        if result.returncode == 0:
            print("ffmpeg: FOUND")
            return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    import shutil
    if shutil.which("ffmpeg"):
        print("ffmpeg: FOUND (in PATH)")
        return True
    print("ffmpeg: NOT FOUND")
    if os.name == "nt":
        print("  Install via: winget install ffmpeg")
    elif sys.platform == "darwin":
        print("  Install via: brew install ffmpeg")
    else:
        print("  Install via: sudo apt install ffmpeg")
    return False


# ── AI connectivity ──────────────────────────────────────────────────────────

def check_ai_connectivity() -> bool:
    settings_path = PROJECT_DIR / "settings.json"
    settings = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
        except Exception as exc:
            print(f"WARN: could not parse {settings_path} ({exc}); proceeding with defaults")

    backend = settings.get("ai_backend", "vllm")
    base_url = settings.get("api_base_url", "")

    if backend in ("openrouter", "openai", "gemini", "anthropic"):
        api_key = settings.get("api_key", "")
        if api_key:
            print(f"AI backend: {backend.upper()} (API key configured)")
            print(f"  Model: {settings.get('ai_model', 'default')}")
            return True
        print(f"AI backend: {backend.upper()} (NO API KEY -- add one in Settings)")
        return False

    # Local vLLM -- check health endpoint
    try:
        import httpx
        health_url = base_url.rsplit("/v1", 1)[0] + "/health" if "/v1" in base_url else base_url + "/health"
        with httpx.Client(timeout=5) as client:
            resp = client.get(health_url)
            if resp.status_code == 200:
                print("AI backend: LOCAL vLLM (connected)")
                return True
    except Exception as exc:
        print(f"  Local vLLM probe at {base_url} failed: {exc}")

    print("AI backend: NOT REACHABLE")
    print("  The app will still start, but AI analysis will fail.")
    print("  Configure your AI backend in Settings or settings.json.")
    return False


# ── Directory setup ──────────────────────────────────────────────────────────

def ensure_directories() -> None:
    for d in ["reviews", "guidelines", "static", "templates"]:
        (PROJECT_DIR / d).mkdir(parents=True, exist_ok=True)


# ── Commands ─────────────────────────────────────────────────────────────────

def setup(args: argparse.Namespace) -> None:
    check_python_version()
    create_venv()
    install_dependencies()
    install_playwright()
    install_ffmpeg()
    ensure_directories()
    check_ffmpeg()
    check_ai_connectivity()
    print("\nSetup complete! Run 'python run.py' to start.")


def start(args: argparse.Namespace) -> None:
    check_python_version()
    ensure_directories()

    # Check dependencies
    try:
        subprocess.run(
            [_python(), "-c", "import fastapi, uvicorn, playwright, httpx"],
            check=True, capture_output=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("Dependencies not installed. Running setup first...")
        setup(args)

    check_ai_connectivity()

    host = getattr(args, "host", "127.0.0.1")
    port = getattr(args, "port", 5050)

    print(f"\n{'=' * 60}")
    print(f"  WCAG Trusted Tester v6.0.0")
    print(f"  Starting on http://{host}:{port}")
    print(f"{'=' * 60}\n")

    uvicorn_args = [
        _python(), "-m", "uvicorn",
        "app:app",
        "--host", host,
        "--port", str(port),
        "--log-level", "info",
    ]
    # --reload disabled on Windows to avoid event loop conflicts with Playwright
    if os.name != "nt":
        uvicorn_args.append("--reload")

    try:
        subprocess.run(uvicorn_args, cwd=str(PROJECT_DIR))
    except KeyboardInterrupt:
        pass
    print("\nServer stopped.")


def main() -> None:
    parser = argparse.ArgumentParser(description="WCAG Trusted Tester v6")
    parser.add_argument("--setup", action="store_true", help="Install dependencies and setup")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind to")
    parser.add_argument("--port", type=int, default=5050, help="Port to bind to")
    args = parser.parse_args()

    try:
        if args.setup:
            setup(args)
        else:
            start(args)
    except KeyboardInterrupt:
        print("\nShutdown complete.")


if __name__ == "__main__":
    main()
