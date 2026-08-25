from src.downloader.hls import parse_attributes, select_variant


def test_hls_selects_highest_bandwidth_variant_and_audio_group() -> None:
    master = """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio-128000",URI="audio.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=500000,RESOLUTION=640x360,AUDIO="audio-128000"
low.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1500000,RESOLUTION=1280x720,AUDIO="audio-128000"
high.m3u8
"""
    selected = select_variant(master, "https://video.twimg.com/amplify_video/1/pl/master.m3u8")
    assert selected[0].endswith("high.m3u8")
    assert selected[1:4] == (1280, 720, 1500000)
    assert selected[4].endswith("audio.m3u8")


def test_hls_attributes_strip_quotes() -> None:
    assert parse_attributes('BANDWIDTH=1234,RESOLUTION="640x360",AUDIO="a"') == {
        "BANDWIDTH": "1234",
        "RESOLUTION": "640x360",
        "AUDIO": "a",
    }
