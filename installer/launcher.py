"""Standalone launcher for the packaged Savings Coach demo (built by installer/build.ps1).

Starts the app on this computer only (127.0.0.1), opens the browser, and reads an
optional Anthropic API key from the user's own folder. The key is NEVER part of the
installer. Without a key, Penny uses her pre-written messages and everything else works.
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path

FIRST_PORT = 8001
ENV_HELP = """# Savings Coach settings. Optional.
# To get live AI messages from Penny, add your Anthropic API key on the next line
# (remove the leading #), save this file, and restart Savings Coach.
# Without a key the app still works and Penny uses her pre-written messages.
#
# ANTHROPIC_API_KEY=sk-ant-your-key-here
"""


def user_env_file() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "SavingsCoach" / ".env"


def free_port(start: int = FIRST_PORT) -> int:
    for port in range(start, start + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:  # nothing is listening there
                return port
    raise RuntimeError("No free port found between 8001 and 8050.")


def main() -> None:
    env = user_env_file()
    env.parent.mkdir(parents=True, exist_ok=True)
    if not env.exists():
        env.write_text(ENV_HELP, encoding="utf-8")
    from dotenv import load_dotenv

    load_dotenv(env)  # must happen before the app is imported

    import uvicorn
    from app.main import app

    port = free_port()
    url = f"http://127.0.0.1:{port}/"
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
    print("=" * 60)
    print(" Savings Coach (GESA hackathon demo, synthetic data only)")
    print(f" Open:  {url}")
    print(f" Penny: {'live AI messages' if has_key else 'pre-written messages (no API key set)'}")
    if not has_key:
        print(f" To use live AI, add a key to: {env}")
    print(" Close this window to stop the app.")
    print("=" * 60)
    threading.Timer(2.0, webbrowser.open, args=(url,)).start()
    log_config = None if sys.stdout is None else uvicorn.config.LOGGING_CONFIG
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", log_config=log_config)


if __name__ == "__main__":
    main()
