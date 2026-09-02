from pathlib import Path

from src.browser.instagram_collector import InstagramCollector
from src.downloader.dash import parse_mpd, select_best_audio, select_best_video


MPD = """<MPD mediaPresentationDuration="PT15.07S"><Period>
<AdaptationSet contentType="video" mimeType="video/mp4" codecs="vp09.00.30.08.00.02.02.01.00">
<Representation id="v540" width="960" height="540" bandwidth="299461"><BaseURL>https://cdn.test/v540.mp4?sig=a&amp;token=b</BaseURL><SegmentBase indexRange="0-999"/></Representation>
<Representation id="v720" width="1280" height="720" bandwidth="2146608"><BaseURL>https://cdn.test/v720.mp4?sig=c&amp;token=d</BaseURL><SegmentBase indexRange="0-999"/></Representation>
<Representation id="v1080" width="1920" height="1080" bandwidth="5262302" frameRate="15360/256"><BaseURL>https://cdn.test/v1080.mp4?sig=e&amp;token=f</BaseURL><SegmentBase indexRange="0-999"/></Representation>
</AdaptationSet>
<AdaptationSet contentType="audio" mimeType="audio/mp4" codecs="mp4a.40.5" audioSamplingRate="44100">
<Representation id="a1" bandwidth="58741"><BaseURL>https://cdn.test/a.m4a?sig=g&amp;token=h</BaseURL><AudioChannelConfiguration value="2"/></Representation>
</AdaptationSet>
</Period></MPD>"""


def test_parse_embedded_mpd_preserves_signed_base_urls_and_quality() -> None:
    manifest = parse_mpd(MPD)
    assert len(manifest.video) == 3
    assert len(manifest.audio) == 1
    assert manifest.video[-1].base_url.endswith("sig=e&token=f")
    assert manifest.video[-1].segment_base == {"indexRange": "0-999"}
    assert select_best_video(manifest.video).representation_id == "v1080"
    assert select_best_audio(manifest.audio).sample_rate == 44100
    assert select_best_audio(manifest.audio).channels == 2


def test_instagram_prefers_higher_quality_dash_over_direct_mp4() -> None:
    node = {
        "is_video": True,
        "original_width": 1920,
        "original_height": 1080,
        "video_versions": [{"url": "https://cdn.test/direct.mp4", "width": 1280, "height": 720}],
        "video_dash_manifest": MPD,
    }
    media = InstagramCollector._media_from_node(node, 0)
    assert media is not None
    assert media.source == "DASH"
    assert media.width == 1920
    assert media.height == 1080
    assert media.video_sources[0].kind == "dash"
    assert media.video_sources[0].audio_url.endswith("sig=g&token=h")


def test_dash_parser_allows_representation_without_bitrate() -> None:
    manifest = parse_mpd("<MPD><Period><AdaptationSet contentType='video'><Representation id='v'><BaseURL>https://cdn.test/v.mp4</BaseURL></Representation></AdaptationSet></Period></MPD>")
    assert manifest.video[0].base_url == "https://cdn.test/v.mp4"
    assert manifest.video[0].bandwidth is None
