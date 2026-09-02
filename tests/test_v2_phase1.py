from pathlib import Path

import pytest

from src.models import TaskConfig
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
    assert APP_VERSION == "2.0.0"
