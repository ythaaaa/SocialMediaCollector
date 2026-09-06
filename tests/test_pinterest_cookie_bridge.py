from __future__ import annotations

import io
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from src.adapters import gallerydl_worker
from src.adapters import instagram_cookie_bridge
from src.adapters import instagram_gallerydl as gallerydl_adapter_module
from src.adapters.pinterest_cookie_bridge import (
    PinterestAuthRequired,
    PinterestCookieBridge,
    filter_pinterest_cookies,
    load_pinterest_cookies,
    pinterest_cookie_store_path,
    save_pinterest_cookies,
)
from src.adapters.instagram_gallerydl import GalleryDlError, PinterestGalleryDlAdapter


def test_pinterest_store_is_independent_from_instagram_store(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("src.utils.paths.application_data_directory", lambda: tmp_path)

    assert pinterest_cookie_store_path() == tmp_path / "auth" / "pinterest.cookies"
    assert pinterest_cookie_store_path() != instagram_cookie_bridge.cookie_store_path()


def test_filter_pinterest_cookies_keeps_only_non_empty_pinterest_domains():
    selected = filter_pinterest_cookies(
        [
            {"domain": ".pinterest.com", "name": "_pinterest_sess", "value": "session"},
            {"domain": "www.pinterest.com", "name": "csrftoken", "value": "csrf"},
            {"domain": ".pinimg.com", "name": "cdn", "value": "ignored"},
            {"domain": ".instagram.com", "name": "sessionid", "value": "ignored"},
            {"domain": ".pinterest.com", "name": "empty", "value": ""},
        ]
    )

    assert selected == {"_pinterest_sess": "session", "csrftoken": "csrf"}


@pytest.mark.skipif(os.name != "nt", reason="Pinterest Cookie store uses Windows DPAPI")
def test_pinterest_dpapi_round_trip_does_not_store_plaintext(tmp_path: Path):
    path = tmp_path / "auth" / "pinterest.cookies"
    cookies = {"_pinterest_sess": "session-secret", "csrftoken": "csrf-secret", "locale": "zh-CN"}

    save_pinterest_cookies(cookies, path)

    raw = path.read_bytes()
    assert b"session-secret" not in raw
    assert b"csrf-secret" not in raw
    assert load_pinterest_cookies(path) == cookies
    assert not (tmp_path / "auth" / "instagram.cookies").exists()


@pytest.mark.skipif(os.name != "nt", reason="Pinterest Cookie store uses Windows DPAPI")
def test_malformed_pinterest_store_requires_auth(tmp_path: Path):
    path = tmp_path / "auth" / "pinterest.cookies"
    path.parent.mkdir()
    path.write_bytes(b"not-a-dpapi-cookie-store")

    with pytest.raises(PinterestAuthRequired, match="Pinterest Search requires login"):
        load_pinterest_cookies(path)


@pytest.mark.skipif(os.name != "nt", reason="Pinterest Cookie store uses Windows DPAPI")
def test_profile_sync_filters_domains_and_never_logs_cookie_values(tmp_path: Path):
    path = tmp_path / "auth" / "pinterest.cookies"
    bridge = PinterestCookieBridge(
        profile_path=tmp_path / "browser-data",
        cookies_reader=lambda: [
            {"domain": ".pinterest.com", "name": "_pinterest_sess", "value": "session-secret"},
            {"domain": ".pinterest.com", "name": "csrftoken", "value": "csrf-secret"},
            {"domain": ".instagram.com", "name": "sessionid", "value": "must-not-copy"},
        ],
    )

    bridge.sync(path)

    assert bridge.cookie_count == 2
    assert load_pinterest_cookies(path) == {
        "_pinterest_sess": "session-secret",
        "csrftoken": "csrf-secret",
    }
    assert b"must-not-copy" not in path.read_bytes()


class _FakeClient:
    is_packaged_helper = True

    def __init__(self, events):
        self.events = list(events)
        self.stopped = False
        self.commands: list[str] = []

    def start(self):
        self.commands.append("start")

    def send(self, command):
        self.commands.append(command)

    def read_event(self, _stop_event):
        return self.events.pop(0)

    def stop(self):
        self.stopped = True


def test_search_missing_store_fails_auth_before_starting_helper(monkeypatch):
    monkeypatch.setattr(
        gallerydl_adapter_module,
        "load_pinterest_cookies",
        lambda: (_ for _ in ()).throw(PinterestAuthRequired("Pinterest Search requires login")),
    )
    started = False

    def client_factory(*_args, **_kwargs):
        nonlocal started
        started = True
        return _FakeClient([])

    adapter = PinterestGalleryDlAdapter(client_factory=client_factory)
    with pytest.raises(PinterestAuthRequired, match="Pinterest Search requires login"):
        adapter.collect(
            "https://www.pinterest.com/search/pins/?q=AI+art",
            on_candidate=lambda _candidate: True,
        )
    assert not started


def test_search_passes_loaded_cookie_dict_and_never_logs_values():
    secret = "session-secret"
    fake = _FakeClient([
        {"type": "ready", "cookie_loaded": True, "gallery_dl_version": "1.32.10"},
        {"type": "exhausted"},
    ])
    captured: dict[str, object] = {}

    def client_factory(*_args, **kwargs):
        captured.update(kwargs)
        return fake

    logs: list[str] = []
    adapter = PinterestGalleryDlAdapter(
        cookies={"_pinterest_sess": secret, "csrftoken": "csrf"},
        client_factory=client_factory,
    )
    assert adapter.collect(
        "https://www.pinterest.com/search/pins/?q=AI+art",
        on_candidate=lambda _candidate: True,
        on_log=logs.append,
    ) == 0
    assert captured["cookies"] == {"_pinterest_sess": secret, "csrftoken": "csrf"}
    assert any("cookie loaded=yes" in line for line in logs)
    assert all(secret not in line for line in logs)


def test_single_pin_stays_anonymous_when_cookie_store_is_missing():
    fake = _FakeClient([
        {"type": "ready", "cookie_loaded": False},
        {"type": "exhausted"},
    ])
    captured: dict[str, object] = {}

    def client_factory(*_args, **kwargs):
        captured.update(kwargs)
        return fake

    logs: list[str] = []
    adapter = PinterestGalleryDlAdapter(client_factory=client_factory)
    assert adapter.collect(
        "https://www.pinterest.com/pin/123456789/",
        on_candidate=lambda _candidate: True,
        on_log=logs.append,
    ) == 0
    assert captured["cookies"] == {}
    assert any("cookie loaded=no" in line for line in logs)


def test_worker_converts_pinterest_auth_exception_to_auth_required(monkeypatch, tmp_path: Path):
    class Instance:
        category = "pinterest"
        subcategory = "search"
        cookies_names = None
        cookies_domain = ""

        def initialize(self):
            return None

        def items(self):
            raise RuntimeError("403 challenge: login required")
            yield  # pragma: no cover

    instance = Instance()
    fake_extractor = SimpleNamespace(find=lambda _url: instance)
    fake_config = SimpleNamespace(set=lambda *_args, **_kwargs: None)
    fake_gallery = ModuleType("gallery_dl")
    fake_gallery.__version__ = "1.32.10"
    fake_gallery.config = fake_config
    fake_gallery.extractor = fake_extractor
    monkeypatch.setitem(sys.modules, "gallery_dl", fake_gallery)
    monkeypatch.setitem(sys.modules, "gallery_dl.config", fake_config)
    monkeypatch.setitem(sys.modules, "gallery_dl.extractor", fake_extractor)
    monkeypatch.setattr(gallerydl_worker.sys, "stdin", io.StringIO('{"type":"next"}\n'))
    output: list[dict] = []
    monkeypatch.setattr(gallerydl_worker, "_write", output.append)

    assert gallerydl_worker.run(
        {
            "platform": "pinterest",
            "url": "https://www.pinterest.com/search/pins/?q=AI+art",
            "profile_path": str(tmp_path),
            "cookies": {"_pinterest_sess": "session-secret"},
        }
    ) == 2
    assert output[0]["type"] == "ready"
    assert output[1] == {
        "type": "error",
        "code": "AUTH_REQUIRED",
        "message": "Pinterest Search requires login",
    }


def test_instagram_cookie_store_api_remains_separate():
    assert instagram_cookie_bridge.COOKIE_STORE_PATH == "auth/instagram.cookies"
    assert pinterest_cookie_store_path().name == "pinterest.cookies"
    assert GalleryDlError.__name__ == "GalleryDlError"
