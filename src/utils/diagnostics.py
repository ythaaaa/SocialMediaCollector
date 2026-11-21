"""Small, privacy-conscious persistent diagnostics for the desktop app."""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from src.utils.paths import diagnostics_directory
from src.version import APP_VERSION


_LOCK = threading.RLock()
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(cookie|token|password|passwd|secret|authorization|auth)[\s=:]+[^\s,;]+"
)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _safe_text(value: object) -> str:
    text = "" if value is None else str(value)
    for token in re.findall(r"https?://[^\s)]+", text):
        parsed = urlsplit(token)
        safe_netloc = parsed.netloc.rsplit("@", 1)[-1]
        safe = urlunsplit((parsed.scheme, safe_netloc, parsed.path, "", ""))
        text = text.replace(token, safe)
    return _SECRET_ASSIGNMENT.sub(r"\1=<redacted>", text)


def _append_text(name: str, value: str) -> Path:
    path = diagnostics_directory() / name
    with _LOCK:
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(value.rstrip("\n") + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    return path


def _append_jsonl(name: str, payload: dict[str, Any]) -> Path:
    path = diagnostics_directory() / name
    record = {"timestamp": _now(), **payload}
    safe_record = json.loads(_safe_text(json.dumps(record, ensure_ascii=False, default=str)))
    with _LOCK:
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(safe_record, ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    return path


def _application_executable() -> str:
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable).resolve())
    script = Path(sys.argv[0] or "app.py")
    return str(script.resolve())


def _ca_diagnostics() -> dict[str, object]:
    try:
        import certifi

        certifi_path = Path(certifi.where()).resolve()
        certifi_exists = certifi_path.is_file()
        try:
            with certifi_path.open("rb") as stream:
                certifi_readable = bool(stream.read(1))
        except OSError:
            certifi_readable = False
        certifi_error = ""
    except Exception as exc:  # startup diagnostics must survive a broken certifi import
        certifi_path = Path()
        certifi_exists = False
        certifi_readable = False
        certifi_error = f"{type(exc).__name__}: {_safe_text(exc)}"

    ssl_cert_file = os.environ.get("SSL_CERT_FILE", "")
    ssl_cert_dir = os.environ.get("SSL_CERT_DIR", "")
    requests_ca_bundle = os.environ.get("REQUESTS_CA_BUNDLE", "")
    # httpx 0.27+ create_ssl_context(verify=True, trust_env=True) uses this
    # exact precedence: SSL_CERT_FILE, SSL_CERT_DIR, then certifi.where().
    if ssl_cert_file:
        source, effective = "SSL_CERT_FILE", Path(ssl_cert_file).expanduser()
        is_directory = False
    elif ssl_cert_dir:
        source, effective = "SSL_CERT_DIR", Path(ssl_cert_dir).expanduser()
        is_directory = True
    else:
        source, effective = "certifi.where()", certifi_path
        is_directory = False
    effective_exists = effective.is_dir() if is_directory else effective.is_file()
    try:
        if is_directory:
            with os.scandir(effective) as entries:
                next(entries, None)
            effective_readable = True
        else:
            with effective.open("rb") as stream:
                effective_readable = bool(stream.read(1))
    except OSError:
        effective_readable = False

    try:
        import httpx

        # Build the same verified context used by httpx.Client(verify=True,
        # trust_env=True), then report what it actually loaded.
        context = httpx._config.create_ssl_context(verify=True, trust_env=True)
        httpx_version = httpx.__version__
        loaded_ca_count = len(context.get_ca_certs())
        httpx_context_error = ""
    except Exception as exc:
        httpx_version = "unknown"
        loaded_ca_count = 0
        httpx_context_error = f"{type(exc).__name__}: {_safe_text(exc)}"

    return {
        "httpx_version": httpx_version,
        "httpx_loaded_ca_count": loaded_ca_count,
        "httpx_context_error": httpx_context_error,
        "certifi_where": str(certifi_path) if certifi_exists else "",
        "certifi_exists": certifi_exists,
        "certifi_readable": certifi_readable,
        "certifi_error": certifi_error,
        "SSL_CERT_FILE_configured": bool(ssl_cert_file),
        "SSL_CERT_DIR_configured": bool(ssl_cert_dir),
        "REQUESTS_CA_BUNDLE_configured": bool(requests_ca_bundle),
        "httpx_ca_source": source,
        "httpx_effective_ca_path": str(effective),
        "httpx_effective_ca_is_directory": is_directory,
        "httpx_effective_ca_exists": effective_exists,
        "httpx_effective_ca_readable": effective_readable,
    }


def initialize_diagnostics() -> Path:
    """Create startup/environment logs before the UI and worker modules load."""
    root = diagnostics_directory()
    timestamp = _now()
    executable = _application_executable()
    startup = [
        f"timestamp={timestamp}",
        f"executable={_safe_text(executable)}",
        f"pid={os.getpid()}",
        f"cwd={_safe_text(Path.cwd().resolve())}",
        f"diagnostics_dir={_safe_text(root)}",
        f"python_version={_safe_text(sys.version.split()[0])}",
        f"app_version={_safe_text(APP_VERSION)}",
    ]
    _append_text("startup.log", "\n".join(startup))

    environment = {
        "timestamp": timestamp,
        "executable": _safe_text(executable),
        "pid": os.getpid(),
        "cwd": _safe_text(Path.cwd().resolve()),
        "diagnostics_dir": _safe_text(root),
        "python_version": _safe_text(sys.version),
        "app_version": _safe_text(APP_VERSION),
        **_ca_diagnostics(),
    }
    _append_text(
        "environment.log",
        "\n".join(f"{key}={_safe_text(value)}" for key, value in environment.items()),
    )
    return root


def runtime_log_path() -> Path:
    return diagnostics_directory() / "runtime-crash.log"


def log_exception(
    exc: BaseException,
    *,
    config: object | None = None,
    context: dict[str, object] | None = None,
) -> Path:
    """Append the real exception and traceback; never allow logging to hide it."""
    details = dict(context or {})
    task_id = getattr(config, "task_id", "") if config is not None else ""
    platform = ""
    mode = ""
    if config is not None:
        try:
            platform = config.canonical_platform()
            mode = config.canonical_collection_mode()
        except Exception:
            pass
    tb = exc.__traceback__
    frames = traceback.extract_tb(tb) if tb else []
    failed_function = f"{frames[-1].name} ({frames[-1].filename}:{frames[-1].lineno})" if frames else "unknown"
    filename = getattr(exc, "filename", "")
    ca_info = _ca_diagnostics()
    ca_path_for_log = (
        "<configured path redacted>"
        if ca_info["httpx_ca_source"] == "SSL_CERT_FILE"
        else "<configured directory redacted>"
        if ca_info["httpx_ca_source"] == "SSL_CERT_DIR"
        else ca_info["certifi_where"]
    )
    lines = [
        f"timestamp={_now()}",
        f"exception_type={type(exc).__name__}",
        f"message={_safe_text(exc)}",
        f"filename={_safe_text(filename or '')}",
        f"errno={getattr(exc, 'errno', '')}",
        f"failed_function={_safe_text(details.get('failed_function', failed_function))}",
        f"stage={_safe_text(details.get('stage', ''))}",
        f"task_id={_safe_text(task_id or details.get('task_id', ''))}",
        f"platform={_safe_text(platform or details.get('platform', ''))}",
        f"collection_mode={_safe_text(mode or details.get('collection_mode', ''))}",
        f"certifi_where={_safe_text(ca_info['certifi_where'])}",
        f"certifi_exists={ca_info['certifi_exists']}",
        f"certifi_readable={ca_info['certifi_readable']}",
        f"SSL_CERT_FILE_configured={ca_info['SSL_CERT_FILE_configured']}",
        f"SSL_CERT_DIR_configured={ca_info['SSL_CERT_DIR_configured']}",
        f"REQUESTS_CA_BUNDLE_configured={ca_info['REQUESTS_CA_BUNDLE_configured']}",
        f"httpx_ca_source={ca_info['httpx_ca_source']}",
        f"httpx_ca_path={_safe_text(ca_path_for_log)}",
        "traceback:",
        _safe_text("".join(traceback.format_exception(type(exc), exc, tb))),
        "-" * 80,
    ]
    path = runtime_log_path()
    try:
        _append_text(path.name, "\n".join(lines))
    except OSError:
        # Preserve the diagnostic exception path even when storage is unavailable.
        pass
    return path


def log_task_stage(stage: str, *, task_id: str = "", platform: str = "", mode: str = "") -> Path:
    return _append_jsonl(
        "task-events.jsonl",
        {
            "event": "stage",
            "stage": stage,
            "task_id": task_id,
            "platform": platform,
            "mode": mode,
        },
    )
