from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))

sys.path.insert(0, os.path.join(ROOT, "backend", "src"))

with contextlib.suppress(AttributeError, OSError):
    sys.stdout.reconfigure(line_buffering=True)

FRONTEND = os.path.join(ROOT, "frontend")
DIST_INDEX = os.path.join(FRONTEND, "dist", "index.html")
FRONTEND_INPUTS = ("src", "index.html", "package.json", "vite.config.ts", "tsconfig.json")


def free_port(preferred: int = 8765) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", preferred))
        except OSError:
            probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def wait_for_server(url: str, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310
                if response.status == 200:
                    return True
        except Exception:
            time.sleep(0.2)
    return False


def frontend_stale() -> bool:
    if not os.path.isfile(DIST_INDEX):
        return True
    built = os.path.getmtime(DIST_INDEX)
    for name in FRONTEND_INPUTS:
        target = os.path.join(FRONTEND, name)
        if os.path.isdir(target):
            for root, _dirs, files in os.walk(target):
                if any(os.path.getmtime(os.path.join(root, item)) > built for item in files):
                    return True
        elif os.path.isfile(target) and os.path.getmtime(target) > built:
            return True
    return False


def run_npm(npm: str, *args: str) -> bool:
    print(f"npm {' '.join(args)}", flush=True)
    result = subprocess.run([npm, *args], cwd=FRONTEND, check=False)
    if result.returncode != 0:
        print(f"npm {' '.join(args)}: код {result.returncode}", file=sys.stderr)
        return False
    return True


def build_frontend() -> None:
    npm = shutil.which("npm")
    if npm is None:
        print("npm не найден — интерфейс не собран, доступен только API", file=sys.stderr)
        return
    if not os.path.isdir(os.path.join(FRONTEND, "node_modules")) and not run_npm(npm, "install"):
        return
    run_npm(npm, "run", "build")


def main() -> int:
    parser = argparse.ArgumentParser(description="Запуск FandomTitleSorter")
    parser.add_argument("--port", type=int, default=0, help="0 — выбрать свободный")
    parser.add_argument("--no-open", action="store_true", help="не открывать браузер")
    parser.add_argument("--no-build", action="store_true", help="не собирать интерфейс")
    args = parser.parse_args()

    if not args.no_build and frontend_stale():
        build_frontend()

    import uvicorn

    from fts.api.app import create_app, token_for_display
    from fts.config import get_settings

    settings = get_settings()
    port = args.port or free_port(settings.fts_port or 8765)
    app = create_app(settings)
    token = token_for_display(settings)
    base = f"http://{settings.fts_host}:{port}"
    url = f"{base}/?token={token}"

    config = uvicorn.Config(app, host=settings.fts_host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="fts-api", daemon=True)
    thread.start()

    if not wait_for_server(f"{base}/api/health"):
        print("сервер не поднялся", file=sys.stderr)
        return 1

    print(f"FandomTitleSorter: {url}")
    print(f"лог: {settings.logs_dir / 'fts.log'}")
    if not args.no_open:
        webbrowser.open(url)

    try:
        while thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nостанавливаю…")
        server.should_exit = True
        thread.join(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
