from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.adapters.x_cookie_bridge import (
    XAuthRequired,
    XCookieBridge,
    filter_x_cookies,
    load_x_cookies,
    save_x_cookies,
    x_cookie_presence,
    x_cookie_store_path,
)


def test_x_cookie_store_and_profile_are_platform_specific(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    from src.utils.paths import profile_directory_for_platform

    assert x_cookie_store_path() == tmp_path / "PinterestLocalDownloader" / "auth" / "x.cookies"
    assert profile_directory_for_platform("x").name == "browser-data-x"
    assert profile_directory_for_platform("x") != profile_directory_for_platform("pinterest")


def test_filter_x_cookies_keeps_x_and_twitter_nonempty_values_only():
    selected = filter_x_cookies([
        {"domain": ".x.com", "name": "auth_token", "value": "auth-secret"},
        {"domain": "twitter.com", "name": "ct0", "value": "csrf-secret"},
        {"domain": ".x.com", "name": "guest_id", "value": "guest"},
        {"domain": ".pinterest.com", "name": "_pinterest_sess", "value": "wrong"},
        {"domain": ".x.com", "name": "empty", "value": ""},
    ])
    assert selected == {"auth_token": "auth-secret", "ct0": "csrf-secret", "guest_id": "guest"}
    assert x_cookie_presence(selected) == (True, True)


@pytest.mark.skipif(os.name != "nt", reason="X Cookie store uses Windows DPAPI")
def test_x_dpapi_round_trip_has_no_plaintext_or_other_platform_store(tmp_path: Path):
    path = tmp_path / "auth" / "x.cookies"
    cookies = {"auth_token": "auth-secret", "ct0": "csrf-secret", "twid": "user-secret"}
    save_x_cookies(cookies, path)
    assert load_x_cookies(path) == cookies
    raw = path.read_bytes()
    assert b"auth-secret" not in raw and b"csrf-secret" not in raw
    assert not (tmp_path / "auth" / "pinterest.cookies").exists()
    assert not (tmp_path / "auth" / "instagram.cookies").exists()


@pytest.mark.parametrize("cookies", [{}, {"auth_token": "a"}, {"ct0": "c"}])
def test_x_cookie_store_requires_auth_token_and_ct0(cookies, tmp_path: Path):
    with pytest.raises(XAuthRequired, match="AUTH_REQUIRED"):
        save_x_cookies(cookies, tmp_path / "x.cookies")


@pytest.mark.skipif(os.name != "nt", reason="X Cookie store uses Windows DPAPI")
def test_x_cookie_bridge_reports_presence_without_exposing_values(tmp_path: Path):
    bridge = XCookieBridge(profile_path=tmp_path / "browser-data-x", cookies_reader=lambda: [
        {"domain": ".x.com", "name": "auth_token", "value": "auth-secret"},
        {"domain": ".x.com", "name": "ct0", "value": "csrf-secret"},
        {"domain": ".x.com", "name": "personalization_id", "value": "personal-secret"},
    ])
    target = bridge.sync(tmp_path / "auth" / "x.cookies")
    assert bridge.cookie_count == 3
    assert bridge.auth_token_present is True and bridge.ct0_present is True
    assert b"secret" not in target.read_bytes()
