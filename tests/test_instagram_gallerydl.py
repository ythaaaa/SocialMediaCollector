from __future__ import annotations

import io
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.browser import ImageCandidate
from src.adapters.instagram_gallerydl import (
    GalleryDlError,
    GalleryDlWorkerClient,
    InstagramGalleryDlAdapter,
    candidate_from_gallerydl,
    gallerydl_poc_enabled,
    resolve_gallerydl_python,
)
from src.adapters.instagram_cookie_bridge import load_instagram_cookies, save_instagram_cookies
from src.adapters import gallerydl_worker


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


def test_gallerydl_hashtag_candidate_mapping_preserves_source_hashtag():
    candidate = candidate_from_gallerydl(image_payload(hashtag="aiart", source_hashtag="aiart"))

    assert candidate.hashtag == "aiart"
    assert candidate.source_hashtag == "aiart"


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


def _fake_gallerydl_python(root: Path) -> Path:
    executable = root / ".venv" / "Scripts" / "python.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"fixture")
    return executable


def test_gallerydl_python_environment_override_has_highest_priority(tmp_path: Path):
    explicit = tmp_path / "explicit-python.exe"
    explicit.write_bytes(b"fixture")
    env_home = _fake_gallerydl_python(tmp_path / "env-home")

    resolved = resolve_gallerydl_python(
        {
            "SMC_GALLERYDL_PYTHON": str(explicit),
            "SMC_GALLERYDL_HOME": str(env_home.parents[2]),
        },
        desktop_directory=tmp_path / "desktop",
        home_directory=tmp_path / "user",
    )

    assert resolved == explicit


def test_gallerydl_home_environment_override(tmp_path: Path):
    root = tmp_path / "env-home"
    expected = _fake_gallerydl_python(root)

    assert resolve_gallerydl_python(
        {"SMC_GALLERYDL_HOME": str(root)},
        desktop_directory=tmp_path / "desktop",
        home_directory=tmp_path / "user",
    ) == expected


def test_gallerydl_finds_redirected_windows_desktop(tmp_path: Path):
    desktop = tmp_path / "redirected-desktop"
    expected = _fake_gallerydl_python(desktop / "gallery-dl")

    assert resolve_gallerydl_python(
        {},
        desktop_directory=desktop,
        home_directory=tmp_path / "user",
    ) == expected


def test_gallerydl_finds_checkout_near_project(monkeypatch, tmp_path: Path):
    root = tmp_path / "nearby-gallery-dl"
    expected = _fake_gallerydl_python(root)
    monkeypatch.setattr(
        "src.adapters.instagram_gallerydl._nearby_gallerydl_roots",
        lambda: (root,),
    )

    assert resolve_gallerydl_python(
        {},
        desktop_directory=tmp_path / "desktop",
        home_directory=tmp_path / "user",
    ) == expected


def test_gallerydl_falls_back_to_legacy_home_desktop(monkeypatch, tmp_path: Path):
    legacy = tmp_path / "user" / "Desktop" / "gallery-dl"
    expected = _fake_gallerydl_python(legacy)
    monkeypatch.setattr(
        "src.adapters.instagram_gallerydl._nearby_gallerydl_roots",
        lambda: (),
    )

    assert resolve_gallerydl_python(
        {},
        desktop_directory=tmp_path / "desktop",
        home_directory=tmp_path / "user",
    ) == expected


def test_gallerydl_missing_helper_has_clear_checked_paths(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        "src.adapters.instagram_gallerydl._nearby_gallerydl_roots",
        lambda: (),
    )
    with pytest.raises(GalleryDlError) as error:
        resolve_gallerydl_python(
            {"SMC_GALLERYDL_PYTHON": str(tmp_path / "missing.exe")},
            desktop_directory=tmp_path / "desktop",
            home_directory=tmp_path / "user",
        )

    message = str(error.value)
    assert "gallery-dl helper not found" in message
    assert "checked candidate paths:" in message
    assert str(tmp_path / "missing.exe") in message
    assert str(tmp_path / "user" / "Desktop" / "gallery-dl" / ".venv" / "Scripts" / "python.exe") in message


def test_worker_jsonl_utf8_preserves_unicode_nested_metadata(monkeypatch):
    unicode_text = "测试 😂 📷 한국어 日本語"
    stdout = io.BytesIO()
    monkeypatch.setattr(gallerydl_worker.sys, "stdout", type("BinaryStdout", (), {"buffer": stdout})())

    gallerydl_worker._write(
        gallerydl_worker._candidate_payload(
            {
                "post_id": "post-unicode",
                "post_shortcode": "UNICODE1",
                "post_url": "https://www.instagram.com/reel/UNICODE1/",
                "username": unicode_text,
                "description": unicode_text,
            },
            {
                "shortcode": "UNICODE1",
                "num": 1,
                "display_url": "https://cdn.example/reel.jpg",
                "video_url": "https://cdn.example/reel.mp4",
            },
        )
    )

    event = json.loads(stdout.getvalue().decode("utf-8"))
    assert event["description"] == unicode_text
    assert event["username"] == unicode_text
    assert event["metadata"]["description"] == unicode_text
    candidate = candidate_from_gallerydl(event)
    assert candidate.author_username == unicode_text
    assert candidate.caption == unicode_text
    assert candidate.post_text == unicode_text


def test_parent_reads_utf8_jsonl_and_preserves_unicode():
    unicode_text = "测试 😂 📷 한국어 日本語"
    event = image_payload(
        username=unicode_text,
        description=unicode_text,
        caption=unicode_text,
        metadata={"nested": unicode_text},
    )
    stdout = io.TextIOWrapper(
        io.BytesIO((json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")),
        encoding="utf-8",
    )
    stderr = io.TextIOWrapper(io.BytesIO("gallery warning 😂\n".encode("utf-8")), encoding="utf-8")

    class Process:
        def __init__(self):
            self.stdin = io.StringIO()
            self.stdout = stdout
            self.stderr = stderr

        def poll(self):
            return 0

    client = GalleryDlWorkerClient(
        "https://www.instagram.com/p/UNICODE1/",
        Path("profile"),
        python_executable=Path(sys.executable),
        gallery_dl_root=Path("gallery-dl"),
    )
    client._process = Process()
    client._read_stdout()
    client._read_stderr()

    received = client.read_event(threading.Event())
    candidate = candidate_from_gallerydl(received)
    assert received["caption"] == unicode_text
    assert received["metadata"]["nested"] == unicode_text
    assert candidate.caption == unicode_text


def test_parent_forces_utf8_child_environment(monkeypatch):
    captured = {}

    class Process:
        stdin = io.StringIO()
        stdout = io.StringIO()
        stderr = io.StringIO()

        def poll(self):
            return 0

    def fake_popen(*args, **kwargs):
        captured.update(kwargs)
        return Process()

    client = GalleryDlWorkerClient(
        "https://www.instagram.com/p/UNICODE1/",
        Path("profile"),
        python_executable=Path(sys.executable),
        gallery_dl_root=Path("gallery-dl"),
        popen_factory=fake_popen,
    )
    client.start()

    assert captured["encoding"] == "utf-8"
    assert captured["errors"] == "replace"
    assert captured["env"]["PYTHONUTF8"] == "1"
    assert captured["env"]["PYTHONIOENCODING"] == "utf-8"


def test_author_message_stream_expands_carousel_with_stable_post_identity():
    post = {
        "post_id": "post-carousel",
        "post_shortcode": "CAROUSEL1",
        "post_url": "https://www.instagram.com/p/CAROUSEL1/",
        "username": "alice",
        "description": "carousel",
        "post_date": "2026-09-03T00:00:00+00:00",
    }
    items = [
        (2, "", post),
        (3, "https://cdn.example/1.jpg", {"num": 1, "display_url": "https://cdn.example/1.jpg"}),
        (3, "https://cdn.example/2.jpg", {"num": 2, "display_url": "https://cdn.example/2.jpg"}),
        (3, "https://cdn.example/3.mp4", {"num": 3, "display_url": "https://cdn.example/3.jpg", "video_url": "https://cdn.example/3.mp4"}),
    ]

    events = list(gallerydl_worker._matching_items(iter(items), "mixed"))

    assert [event["media_identity"] for event in events] == [
        "CAROUSEL1:0", "CAROUSEL1:1", "CAROUSEL1:2",
    ]
    assert [event["media_type"] for event in events] == ["image", "image", "video"]
    assert all(event["username"] == "alice" for event in events)


def test_author_adapter_filters_media_and_ownership_then_requests_next():
    fake = FakeClient([
        {"type": "ready", "cookie_loaded": True},
        image_payload(username="bob"),
        image_payload(media_type="video", video_url="https://cdn.example/video.mp4", username="alice"),
        image_payload(username="alice", shortcode="NEXT1"),
        {"type": "exhausted"},
    ])
    adapter = InstagramGalleryDlAdapter(
        profile_path=Path("profile"),
        client_factory=lambda *args, **kwargs: fake,
        cookies={"sessionid": "fixture"},
    )
    seen = []

    assert adapter.collect(
        "https://www.instagram.com/alice/",
        media_type="image",
        author_username="alice",
        on_candidate=lambda candidate: seen.append(candidate) or True,
    ) == 1
    assert [candidate.shortcode for candidate in seen] == ["NEXT1"]
    assert fake.commands == ["start", "next", "next", "next", "next"]


def test_author_adapter_pause_resume_keeps_candidate_position():
    fake = FakeClient([
        {"type": "ready", "cookie_loaded": True},
        image_payload(username="alice"),
        image_payload(username="alice", shortcode="NEXT1"),
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
        "https://www.instagram.com/alice/",
        author_username="alice",
        on_candidate=callback,
        pause_event=pause_event,
    ) == 2
    assert fake.commands.index("pause") < fake.commands.index("resume") < fake.commands.index("next", 2)


def test_hashtag_adapter_does_not_apply_author_ownership_and_filters_media_type():
    fake = FakeClient([
        {"type": "ready", "cookie_loaded": True},
        image_payload(username="creator", media_type="video", video_url="https://cdn.example/video.mp4"),
        image_payload(username="another_creator", shortcode="TAGNEXT"),
        {"type": "exhausted"},
    ])
    adapter = InstagramGalleryDlAdapter(
        profile_path=Path("profile"),
        client_factory=lambda *args, **kwargs: fake,
        cookies={"sessionid": "fixture"},
    )
    seen = []

    assert adapter.collect(
        "https://www.instagram.com/explore/tags/aiart/",
        media_type="image",
        source_hashtag="aiart",
        on_candidate=lambda candidate: seen.append(candidate) or True,
    ) == 1
    assert [candidate.shortcode for candidate in seen] == ["TAGNEXT"]
    assert fake.commands == ["start", "next", "next", "next"]


def test_adapter_reports_each_mapped_candidate_before_ownership_or_media_filter():
    fake = FakeClient([
        {"type": "ready", "cookie_loaded": True},
        image_payload(shortcode="WRONG_OWNER", username="bob"),
        image_payload(shortcode="FILTERED_VIDEO", media_type="video", video_url="https://cdn.example/video.mp4"),
        image_payload(shortcode="ACCEPTED", username="alice"),
        {"type": "exhausted"},
    ])
    adapter = InstagramGalleryDlAdapter(
        profile_path=Path("profile"),
        client_factory=lambda *args, **kwargs: fake,
        cookies={"sessionid": "fixture"},
    )
    discovered: list[str] = []
    skipped: list[str] = []
    accepted: list[str] = []

    assert adapter.collect(
        "https://www.instagram.com/alice/",
        media_type="image",
        author_username="alice",
        on_discovered=lambda candidate: discovered.append(candidate.shortcode),
        on_skipped=lambda candidate: skipped.append(candidate.shortcode),
        on_candidate=lambda candidate: accepted.append(candidate.shortcode) or True,
    ) == 1
    assert discovered == ["WRONG_OWNER", "FILTERED_VIDEO", "ACCEPTED"]
    assert skipped == ["WRONG_OWNER", "FILTERED_VIDEO"]
    assert accepted == ["ACCEPTED"]


def test_adapter_control_and_error_messages_do_not_report_discovered():
    for event in ({"type": "exhausted"}, {"type": "error", "message": "fixture error"}):
        fake = FakeClient([{"type": "ready", "cookie_loaded": True}, event])
        adapter = InstagramGalleryDlAdapter(
            profile_path=Path("profile"),
            client_factory=lambda *args, _fake=fake, **kwargs: _fake,
            cookies={"sessionid": "fixture"},
        )
        discovered: list[ImageCandidate] = []
        collect = lambda: adapter.collect(
            "https://www.instagram.com/p/ABC123/",
            on_discovered=discovered.append,
            on_candidate=lambda _candidate: True,
        )
        if event["type"] == "error":
            with pytest.raises(GalleryDlError, match="fixture error"):
                collect()
        else:
            assert collect() == 0
        assert discovered == []


def test_worker_author_uses_posts_extractor_and_streams_candidate(monkeypatch, tmp_path: Path, capsys):
    profile = tmp_path / "profile"
    profile.mkdir()
    find_urls = []
    unicode_text = "作者 😂"

    class Instance:
        category = "instagram"
        subcategory = "posts"
        cookies_names = ("sessionid",)
        cookies_domain = ".instagram.com"

        def initialize(self):
            return None

        def cookies_check(self, *_args):
            return True

        def request(self, *_args, **_kwargs):
            return SimpleNamespace(url="https://www.instagram.com/", status_code=200)

        def items(self):
            return iter([
                (2, "", {
                    "post_id": "post-author",
                    "post_shortcode": "AUTHOR1",
                    "post_url": "https://www.instagram.com/p/AUTHOR1/",
                    "username": "alice",
                    "description": unicode_text,
                }),
                (3, "https://cdn.example/author.jpg", {
                    "num": 1,
                    "shortcode": "AUTHOR1",
                    "display_url": "https://cdn.example/author.jpg",
                }),
            ])

    instance = Instance()
    fake_gallery_dl = SimpleNamespace(
        config=SimpleNamespace(set=lambda *_args, **_kwargs: None),
        extractor=SimpleNamespace(
            find=lambda url: find_urls.append(url) or instance,
        ),
    )
    monkeypatch.setitem(sys.modules, "gallery_dl", fake_gallery_dl)
    monkeypatch.setattr(gallerydl_worker.sys, "stdin", io.StringIO('{"type":"next"}\n{"type":"stop"}\n'))

    assert gallerydl_worker.run({
        "url": "https://www.instagram.com/alice/",
        "profile_path": str(profile),
        "cookies": {"sessionid": "fixture"},
        "media_type": "mixed",
        "gallery_dl_root": str(tmp_path),
    }) == 0

    output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert find_urls == ["https://www.instagram.com/alice/posts/"]
    assert output[0] == {"type": "ready", "cookie_loaded": True}
    assert output[1]["type"] == "candidate"
    assert output[1]["media_identity"] == "AUTHOR1:0"
    assert output[1]["description"] == unicode_text


def test_worker_hashtag_uses_official_tag_extractor_and_keeps_source_metadata(monkeypatch, tmp_path: Path, capsys):
    profile = tmp_path / "profile"
    profile.mkdir()
    find_urls = []

    class Instance:
        category = "instagram"
        subcategory = "tag"
        cookies_names = ("sessionid",)
        cookies_domain = ".instagram.com"

        def initialize(self):
            return None

        def cookies_check(self, *_args):
            return True

        def request(self, *_args, **_kwargs):
            return SimpleNamespace(url="https://www.instagram.com/", status_code=200)

        def items(self):
            return iter([
                (2, "", {
                    "post_id": "post-tag",
                    "post_shortcode": "TAGPOST1",
                    "post_url": "https://www.instagram.com/p/TAGPOST1/",
                    "username": "creator",
                    "description": "旅行 😂",
                }),
                (3, "https://cdn.example/tag.jpg", {
                    "num": 1,
                    "shortcode": "TAGPOST1",
                    "display_url": "https://cdn.example/tag.jpg",
                }),
            ])

    instance = Instance()
    fake_gallery_dl = SimpleNamespace(
        config=SimpleNamespace(set=lambda *_args, **_kwargs: None),
        extractor=SimpleNamespace(find=lambda url: find_urls.append(url) or instance),
    )
    monkeypatch.setitem(sys.modules, "gallery_dl", fake_gallery_dl)
    monkeypatch.setattr(gallerydl_worker.sys, "stdin", io.StringIO('{"type":"next"}\n{"type":"stop"}\n'))

    assert gallerydl_worker.run({
        "url": "https://www.instagram.com/explore/tags/aiart/",
        "source_hashtag": "aiart",
        "profile_path": str(profile),
        "cookies": {"sessionid": "fixture"},
        "media_type": "mixed",
        "gallery_dl_root": str(tmp_path),
    }) == 0

    output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert find_urls == ["https://www.instagram.com/explore/tags/aiart/"]
    assert output[1]["hashtag"] == "aiart"
    assert output[1]["source_hashtag"] == "aiart"
    assert output[1]["description"] == "旅行 😂"


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
