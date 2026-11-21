from __future__ import annotations

import json
from pathlib import Path

from src.ui import main_window
from src.models import TaskConfig
from src.downloader.engine import DownloadEngine
from src.storage import CollectorDatabase
from src.utils.diagnostics import initialize_diagnostics, log_task_stage


def test_runtime_exception_log_keeps_missing_path_and_traceback(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SMC_DIAGNOSTICS_HOME", str(tmp_path))
    log_path = tmp_path / "runtime-crash.log"
    missing = tmp_path / "missing" / "ffmpeg.exe"
    try:
        missing.open("rb")
    except FileNotFoundError as exc:
        main_window._write_runtime_exception(
            exc,
            TaskConfig("", 1, tmp_path),
            {"stage": "external process startup", "resource_path": str(missing)},
        )

    contents = log_path.read_text(encoding="utf-8")
    assert f"filename={missing}" in contents
    assert "errno=2" in contents
    assert "stage=external process startup" in contents
    assert "Traceback (most recent call last)" in contents
    assert "FileNotFoundError" in contents
    assert "certifi_where=" in contents
    assert "certifi_exists=True" in contents
    assert "certifi_readable=True" in contents
    assert "httpx_ca_source=certifi.where()" in contents
    assert "SSL_CERT_FILE_configured=False" in contents


def test_httpx_tls_context_uses_readable_certifi_bundle(tmp_path: Path, monkeypatch) -> None:
    import certifi
    import httpx

    ca_file = Path(certifi.where())
    assert ca_file.is_file()
    assert ca_file.stat().st_size > 0
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    with httpx.Client(trust_env=True, timeout=2) as client:
        assert client._transport is not None


def test_pyinstaller_build_rejects_host_specific_ssl_policy_before_cleanup() -> None:
    build_script = Path(__file__).resolve().parents[1] / "scripts" / "build_exe.ps1"
    contents = build_script.read_text(encoding="utf-8")
    reject = contents.index("if ($sslSource.Contains('codex-rejected-cas.pem'))")
    cleanup = contents.index("# Validate all inputs before replacing any previous build output.")
    assert reject < cleanup
    assert "请使用未修改的官方 CPython 3.12 环境构建" in contents
    assert "$sslPolicyPem" not in contents


def test_httpx_environment_ca_override_is_reported_without_value(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SMC_DIAGNOSTICS_HOME", str(tmp_path))
    log_path = tmp_path / "runtime-crash.log"
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "private-ca.pem"))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "private-certs"))
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(tmp_path / "requests-ca.pem"))
    main_window._write_runtime_exception(RuntimeError("test"))
    contents = log_path.read_text(encoding="utf-8")
    assert "SSL_CERT_FILE_configured=True" in contents
    assert "SSL_CERT_DIR_configured=True" in contents
    assert "REQUESTS_CA_BUNDLE_configured=True" in contents
    assert "httpx_ca_source=SSL_CERT_FILE" in contents
    assert str(tmp_path / "private-ca.pem") not in contents


def test_startup_and_environment_logs_are_created_immediately(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SMC_DIAGNOSTICS_HOME", str(tmp_path))

    result = initialize_diagnostics()

    startup = tmp_path / "startup.log"
    environment = tmp_path / "environment.log"
    assert result == tmp_path.resolve()
    assert startup.is_file() and startup.stat().st_size > 0
    assert environment.is_file() and environment.stat().st_size > 0
    startup_text = startup.read_text(encoding="utf-8")
    environment_text = environment.read_text(encoding="utf-8")
    assert "executable=" in startup_text
    assert "pid=" in startup_text
    assert "cwd=" in startup_text
    assert f"diagnostics_dir={tmp_path.resolve()}" in startup_text
    assert "python_version=" in startup_text
    assert "app_version=" in startup_text
    assert "certifi_where=" in environment_text
    assert "certifi_exists=True" in environment_text
    assert "certifi_readable=True" in environment_text
    assert "httpx_ca_source=" in environment_text
    assert "SSL_CERT_FILE_configured=False" in environment_text
    assert "SSL_CERT_DIR_configured=False" in environment_text
    assert "REQUESTS_CA_BUNDLE_configured=False" in environment_text
    assert "httpx_loaded_ca_count=" in environment_text


def test_task_stage_is_written_as_jsonl(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SMC_DIAGNOSTICS_HOME", str(tmp_path))

    path = log_task_stage("HTTP/TLS client initialization", task_id="task-123", platform="pinterest", mode="home")

    event = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert event["event"] == "stage"
    assert event["stage"] == "HTTP/TLS client initialization"
    assert event["task_id"] == "task-123"
    assert event["platform"] == "pinterest"
    assert event["mode"] == "home"
    assert event["timestamp"]


def test_download_engine_stage_transition_is_persisted(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SMC_DIAGNOSTICS_HOME", str(tmp_path / "diagnostics-output"))
    config = TaskConfig("diagnostic stage", 1, tmp_path / "output")
    engine = DownloadEngine(
        config,
        tmp_path / "profile",
        database=CollectorDatabase(tmp_path / "collector.db"),
    )

    engine.diagnostic_stage = "browser connection"

    event = json.loads((tmp_path / "diagnostics-output" / "task-events.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert event["stage"] == "browser connection"
    assert event["task_id"] == config.task_id
    assert event["platform"] == "pinterest"


def test_runtime_exception_redacts_credentials(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SMC_DIAGNOSTICS_HOME", str(tmp_path))
    credential_url = "https://" + "user" + ":" + "pass" + "@example.invalid/private?access_token=url-secret"
    main_window._write_runtime_exception(RuntimeError(
        "token=private-value password=hunter2 "
        + credential_url
    ))

    contents = (tmp_path / "runtime-crash.log").read_text(encoding="utf-8")
    assert "private-value" not in contents
    assert "hunter2" not in contents
    assert "user:pass@" not in contents
    assert "url-secret" not in contents
    assert "token=<redacted>" in contents
    assert "password=<redacted>" in contents
