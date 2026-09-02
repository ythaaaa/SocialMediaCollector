"""One-shot Chrome/CDP cookie bridge; never prints cookie values."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from http.client import HTTPConnection
from pathlib import Path


COOKIE_NAMES = {"sessionid", "csrftoken", "ds_user_id"}


def _write(message):
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _ready(port):
    connection = None
    try:
        connection = HTTPConnection("127.0.0.1", port, timeout=0.5)
        connection.request("GET", "/json/version")
        response = connection.getresponse()
        response.read()
        return 200 <= response.status < 300
    except Exception:
        return False
    finally:
        if connection:
            connection.close()


def _terminate(process):
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def main():
    try:
        request = json.loads(sys.stdin.readline())
        profile = Path(request["profile_path"])
        chrome = str(request["chrome_path"])
        if not profile.is_dir():
            raise RuntimeError("Instagram browser profile does not exist")
        port = _free_port()
        command = [chrome, f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1", f"--user-data-dir={profile}", "https://www.instagram.com/"]
        process = subprocess.Popen(command, cwd=str(Path(chrome).parent), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline and not _ready(port):
                if process.poll() is not None:
                    raise RuntimeError(f"Chrome exited during Cookie sync (code={process.poll()})")
                time.sleep(0.2)
            if not _ready(port):
                raise RuntimeError("Cookie sync CDP endpoint not ready")
            from playwright.sync_api import sync_playwright

            playwright = sync_playwright().start()
            try:
                browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                try:
                    contexts = browser.contexts
                    if not contexts:
                        raise RuntimeError("Cookie sync Chrome has no browser context")
                    cookies = contexts[0].cookies(["https://www.instagram.com/"])
                finally:
                    browser.close()
            finally:
                playwright.stop()
            selected = {
                item["name"]: item["value"]
                for item in cookies
                if item.get("name") in COOKIE_NAMES
                and str(item.get("domain") or "").lstrip(".").endswith("instagram.com")
                and item.get("value")
            }
            if "sessionid" not in selected:
                _write({"ok": False, "code": "AUTH_REQUIRED", "message": "Instagram cookie loaded=no; sessionid 未找到"})
                return 2
            _write({"ok": True, "cookies": selected})
            return 0
        finally:
            _terminate(process)
    except Exception as exc:
        _write({"ok": False, "message": f"Cookie sync failed: {type(exc).__name__}: {exc}"})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
