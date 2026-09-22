from pathlib import Path

import pytest

from src.models import TaskConfig
from src.platforms import PLATFORM_REGISTRY, get_platform_spec
from src.ui.input_state import ModeInputState
from src.version import APP_VERSION


def test_input_state_isolated_by_platform_and_mode() -> None:
    state = ModeInputState()
    x_author = ("x", "author")
    pinterest_search = ("pinterest", "search")

    assert state.switch(x_author, "") == ""
    assert state.switch(pinterest_search, "https://x.com/AIVideoHub_") == ""
    assert state.switch(x_author, "AI character design") == "https://x.com/AIVideoHub_"
    assert state.switch(pinterest_search, "https://x.com/AIVideoHub_") == "AI character design"


def test_pinterest_search_rejects_url_input() -> None:
    config = TaskConfig("https://x.com/AIVideoHub_", 1, Path("out"))
    with pytest.raises(ValueError, match="Pinterest 关键词搜索请输入搜索关键词，不要填写网页地址。"):
        config.validate()


@pytest.mark.parametrize(
    "value",
    ["AIVideoHub_", "@AIVideoHub_", "https://x.com/AIVideoHub_", "https://twitter.com/AIVideoHub_"],
)
def test_x_author_inputs_are_normalized_before_collection(value: str) -> None:
    config = TaskConfig("", 1, Path("out"), platform="x", collection_mode="author", source_identifier=value)
    config.validate()
    assert config.canonical_source_value == "AIVideoHub_"


def test_task_config_exposes_v2_queue_fields_and_reads_legacy_data() -> None:
    config = TaskConfig("design", 2, Path("out"), interval_seconds=2.5, only_author_content=False)
    assert config.task_id
    assert config.source_value == "design"
    assert config.download_interval == 2.5
    assert config.save_dir == Path("out")
    assert config.only_author_posts is False
    assert config.status == "pending"

    legacy = TaskConfig.from_dict({"keyword": "old", "target_count": 1, "output_root": "out"})
    assert legacy.status == "pending"

    queued = TaskConfig.from_dict(
        {
            "platform": "pinterest",
            "collection_mode": "search",
            "source_value": "queued design",
            "target_count": 1,
            "save_dir": "queued-out",
            "download_interval": 3,
            "only_author_posts": False,
        }
    )
    assert queued.keyword == "queued design"
    assert queued.save_dir == Path("queued-out")
    assert queued.download_interval == 3
    assert queued.only_author_posts is False


def test_v2_release_candidate_version() -> None:
    assert APP_VERSION == "2.1.0-dev"


def test_platform_registry_declares_current_and_reserved_capabilities() -> None:
    assert set(PLATFORM_REGISTRY) == {"pinterest", "x", "instagram", "youtube"}

    pinterest = get_platform_spec("pinterest")
    assert pinterest is not None
    assert pinterest.enabled is True
    assert pinterest.collection_modes == ("search", "single_post", "user", "board", "home", "current_page")
    assert pinterest.media_types == ("image", "video", "mixed")

    x = get_platform_spec("x")
    assert x is not None
    assert x.enabled is True
    assert x.collection_modes == ("author", "search", "home")
    assert x.media_types == ("image", "video", "mixed")

    instagram = get_platform_spec("instagram")
    assert instagram is not None
    assert instagram.enabled is True
    assert instagram.collection_modes == ("author", "single_post", "hashtag")

    youtube = get_platform_spec("youtube")
    assert youtube is not None
    assert youtube.enabled is True
    assert youtube.collection_modes == ("auto", "single_video", "playlist", "channel")
    assert youtube.media_types == ("video", "audio", "thumbnail")


def test_platform_resources_are_source_and_pyinstaller_inputs() -> None:
    for spec in PLATFORM_REGISTRY.values():
        assert Path("src/ui/resources", spec.icon).is_file()

    build_script = Path("scripts/build_exe.ps1").read_text(encoding="utf-8")
    assert "'--add-data', \"$uiResources;src\\ui\\resources\"" in build_script
