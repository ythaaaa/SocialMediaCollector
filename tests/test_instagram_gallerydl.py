from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from src.adapters.instagram_gallerydl import (
    GalleryDlError,
    GalleryDlWorkerClient,
    InstagramGalleryDlAdapter,
    candidate_from_gallerydl,
    gallerydl_poc_enabled,
)
from src.adapters.instagram_cookie_bridge import load_instagram_cookies, save_instagram_cookies


def image_payload(**extra):
    return {
        "type": "candidate",
        "media_type": "image",
        "post_id": "post-1",
        "shortcode": "ABC123",
        "media_index": 0,
        "post_url": "https://www.instagram.com/p/ABC123/",
        "username": "creator",
        "description": "caption",
        "date": "2026-09-02T00:00:00+00:00",
        "display_url": "https://cdn.example/image.jpg",
        "width": 1200,
        "height": 800,
        **extra,
    }


def test_gallerydl_candidate_mapping_preserves_smc_identity_and_metadata_fields():
    candidate = candidate_from_gallerydl(image_payload())

    assert candidate.platform == "instagram"
    assert candidate.media_identity == "ABC123:0"
    assert candidate.media_url.endswith("image.jpg")
    assert candidate.post_url.endswith("ABC123/")
    assert candidate.caption == "caption"
    assert candidate.metadata_source == "gallery-dl"


def test_gallerydl_video_mapping_preserves_dash_manifest_payload():
    candidate = candidate_from_gallerydl(
        image_payload(
            media_type="video",
            media_index=1,
            display_url="https://cdn.example/thumb.jpg",
            video_url="https://cdn.example/video.mp4",
            dash_manifest="<MPD/>",
            width=1080,
            height=1920,
        )
    )

    assert candidate.media_type == "video"
    assert candidate.image_url == ""
    assert candidate.media_url.endswith("video.mp4")
    assert candidate.video_width == 1080
    assert candidate.video_height == 1920


def test_gallerydl_raw_dash_manifest_is_converted_to_existing_dash_source():
    manifest = """
    <MPD mediaPresentationDuration="PT2S">
      <Period>
        <AdaptationSet contentType="video" mimeType="video/mp4">
          <Representation id="v1080" width="1080" height="1920" bandwidth="4000000">
            <BaseURL>https://cdn.example/video-1080.mp4</BaseURL>
          </Representation>
        </AdaptationSet>
        <AdaptationSet contentType="audio" mimeType="audio/mp4">
          <Representation id="a128" bandwidth="128000">
            <BaseURL>https://cdn.example/audio.m4a</BaseURL>
          </Representation>
        </AdaptationSet>
      </Period>
    </MPD>
    """
    candidate = candidate_from_gallerydl(
        image_payload(media_type="video", video_url="https://cdn.example/fallback.mp4", dash_manifest=manifest)
    )

    assert len(candidate.video_sources) == 1
    source = candidate.video_sources[0]
    assert source.kind == "dash"
    assert source.url.endswith("video-1080.mp4")
    assert source.audio_url.endswith("audio.m4a")


def test_feature_flag_is_opt_in():
    assert not gallerydl_poc_enabled({})
    assert gallerydl_poc_enabled({"SMC_INSTAGRAM_GALLERYDL_POC": "1"})
    assert gallerydl_poc_enabled({"SMC_INSTAGRAM_GALLERYDL_POC": "true"})


def test_cookie_store_is_dpapi_protected_and_round_trips(tmp_path: Path):
    path = tmp_path / "auth" / "instagram.cookies"

    save_instagram_cookies(
        {"sessionid": "fixture-session", "csrftoken": "fixture-csrf", "ds_user_id": "42"},
        path,
    )

    assert b"fixture-session" not in path.read_bytes()
    assert load_instagram_cookies(path) == {
        "sessionid": "fixture-session",
        "csrftoken": "fixture-csrf",
        "ds_user_id": "42",
    }


class FakeClient:
    def __init__(self, events):
        self.events = list(events)
        self.commands = []
        self.stopped = False

    def start(self):
        self.commands.append("start")

    def send(self, command):
        self.commands.append(command)

    def read_event(self, stop_event):
        if stop_event.is_set():
            raise RuntimeError("unexpected read after stop")
        return self.events.pop(0)

    def stop(self):
        self.stopped = True


def test_adapter_requests_one_candidate_at_a_time_and_stops_at_callback_boundary():
    fake = FakeClient([
        {"type": "ready", "cookie_loaded": True},
        image_payload(),
    ])
    adapter = InstagramGalleryDlAdapter(
        profile_path=Path("profile"),
        client_factory=lambda *args, **kwargs: fake,
        cookies={"sessionid": "fixture"},
    )
    seen = []

    assert adapter.collect(
        "https://www.instagram.com/p/ABC123/",
        on_candidate=lambda candidate: seen.append(candidate) or False,
    ) == 1
    assert len(seen) == 1
    assert fake.commands == ["start", "next"]
    assert fake.stopped


def test_adapter_pause_resume_sends_no_next_while_paused():
    fake = FakeClient([
        {"type": "ready", "cookie_loaded": True},
        image_payload(),
        {"type": "exhausted"},
    ])
    pause_event = threading.Event()
    pause_event.set()
    adapter = InstagramGalleryDlAdapter(
        profile_path=Path("profile"),
        client_factory=lambda *args, **kwargs: fake,
        cookies={"sessionid": "fixture"},
    )

    def callback(_candidate):
        pause_event.clear()
        threading.Timer(0.12, pause_event.set).start()
        return True

    assert adapter.collect(
        "https://www.instagram.com/p/ABC123/",
        pause_event=pause_event,
        on_candidate=callback,
    ) == 1
    assert fake.commands.index("pause") < fake.commands.index("resume") < fake.commands.index("next", 2)


def test_adapter_converts_helper_error_to_local_error_and_cleans_up():
    fake = FakeClient([
        {"type": "ready", "cookie_loaded": True},
        {"type": "error", "message": "request failed"},
    ])
    adapter = InstagramGalleryDlAdapter(
        profile_path=Path("profile"),
        client_factory=lambda *args, **kwargs: fake,
        cookies={"sessionid": "fixture"},
    )

    with pytest.raises(GalleryDlError, match="request failed"):
        adapter.collect(
            "https://www.instagram.com/p/ABC123/",
            on_candidate=lambda _candidate: True,
        )
    assert fake.stopped


def test_worker_client_uses_hidden_child_process_and_terminates_tree(monkeypatch):
    calls = []

    class Process:
        pid = 321

        def poll(self):
            return None

        def wait(self, timeout=None):
            calls.append(("wait", timeout))
            return 0

    monkeypatch.setattr("src.adapters.instagram_gallerydl.subprocess.run", lambda *args, **kwargs: calls.append((args, kwargs)))
    GalleryDlWorkerClient._terminate_process_tree(Process())
    assert calls[0][0][0][0:4] == ["taskkill", "/PID", "321", "/T"]


def test_duplicate_and_failed_callbacks_continue_until_success_callback_stops():
    fake = FakeClient([
        {"type": "ready", "cookie_loaded": True},
        image_payload(media_index=0),
        image_payload(media_index=1),
        image_payload(media_index=2),
    ])
    adapter = InstagramGalleryDlAdapter(
        profile_path=Path("profile"),
        client_factory=lambda *args, **kwargs: fake,
        cookies={"sessionid": "fixture"},
    )
    callback_results = iter((True, True, False))
    accepted = []

    delivered = adapter.collect(
        "https://www.instagram.com/p/ABC123/",
        on_candidate=lambda candidate: accepted.append(candidate.media_index) or next(callback_results),
    )

    assert delivered == 3
    assert accepted == [0, 1, 2]
    assert fake.commands == ["start", "next", "next", "next"]
