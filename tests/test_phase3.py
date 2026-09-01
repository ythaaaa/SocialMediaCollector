from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from src.browser import ImageCandidate, XCollector
from src.extractors import PromptDetector
from src.models import DownloadRecord, TaskConfig
from src.storage import QueueStore, TaskStore


def test_prompt_detector_extracts_inline_prompt() -> None:
    result = PromptDetector().detect("Prompt: a cute cat")
    assert result.prompt_detected is True
    assert result.prompt == "a cute cat"


def test_prompt_detector_rejects_generic_model_mention() -> None:
    result = PromptDetector().detect("This image was made with Midjourney is great.")
    assert result.prompt_detected is False
    assert result.prompt == ""


def test_prompt_detector_extracts_multiline_prompt_without_following_caption() -> None:
    text = """Made with GPT Image.

Prompt:
A cute strawberry cake character,
soft studio lighting,
3D cartoon style.

Amazing result."""
    result = PromptDetector().detect(text)
    assert result.prompt_detected is True
    assert result.prompt == "A cute strawberry cake character,\nsoft studio lighting,\n3D cartoon style."


def test_prompt_detector_extracts_chinese_and_parameters() -> None:
    chinese = PromptDetector().detect("提示词：一个可爱的草莓蛋糕IP角色")
    assert chinese.prompt_detected is True
    assert chinese.prompt == "一个可爱的草莓蛋糕IP角色"
    parameterized = PromptDetector().detect("cinematic portrait of a woman, soft light, 85mm lens --ar 2:3")
    assert parameterized.prompt_detected is True
    assert "--ar 2:3" in parameterized.prompt
    labeled_parameters = PromptDetector().detect("Parameters: --ar 2:3 --stylize 100")
    assert labeled_parameters.prompt_detected is True


@pytest.mark.parametrize(
    "text",
    [
        "GPT IMAGE2 提示词\na cute 3d character",
        "GPT IMAGE2 提示词👇\na cute 3d character",
        "提示词如下：\n一个可爱的草莓蛋糕角色",
        "生图提示词\ncinematic portrait...",
        "Prompt ⬇️\ncinematic portrait...",
        "GPT Image Prompt:\ncinematic portrait...",
    ],
)
def test_prompt_detector_accepts_common_chinese_english_prompt_labels(text: str) -> None:
    result = PromptDetector().detect(text)
    assert result.prompt_detected is True
    assert "提示词" not in result.prompt
    assert "Prompt" not in result.prompt


@pytest.mark.parametrize(
    "text",
    [
        "今天分享 GPT Image2 的使用感受",
        "Midjourney 很好用",
        "GPT Image\n",
        "Prompt👇",
    ],
)
def test_prompt_detector_remains_conservative_for_mentions_and_empty_labels(text: str) -> None:
    result = PromptDetector().detect(text)
    assert result.prompt_detected is False


def test_prompt_detector_extracts_multiline_body_and_keeps_parameters_without_tail_copy() -> None:
    result = PromptDetector().detect(
        "效果太棒了！\nGPT IMAGE2 提示词👇\nA cute character...\nsoft lighting...\n--ar 1:1\n喜欢的话点个赞～"
    )
    assert result.prompt_detected is True
    assert result.prompt == "A cute character...\nsoft lighting...\n--ar 1:1"
    assert "效果太棒了" not in result.prompt
    assert "点个赞" not in result.prompt
    assert result.parameter_matched is True


def test_prompt_detector_reports_label_and_parameter_matches() -> None:
    result = PromptDetector().detect("Parameters: cinematic portrait --stylize 500")
    assert result.label_matched is True
    assert result.parameter_matched is True


def test_x_post_metadata_is_shared_by_media_from_one_post() -> None:
    class Locator:
        def evaluate_all(self, script):
            return [{
                "status": "https://x.com/example/status/1234567890",
                "authorName": "example",
                "repost": False,
                "promoted": False,
                "video": False,
                "tweetText": "Prompt: 一个可爱的角色 🌟",
                "postedAt": "2026-09-01T01:02:03.000Z",
                "media": [
                    "https://pbs.twimg.com/media/one.jpg?name=small",
                    "https://pbs.twimg.com/media/two.jpg?name=small",
                ],
            }]

    class Page:
        url = "https://x.com/example"

        def locator(self, selector):
            return Locator()

    candidates: dict[str, ImageCandidate] = {}
    XCollector(Path("profile"), lambda _: None)._collect_visible_tweets(
        Page(), "example", Page.url, True, candidates, None
    )
    assert len(candidates) == 2
    assert {candidate.post_id for candidate in candidates.values()} == {"1234567890"}
    assert {candidate.post_url for candidate in candidates.values()} == {"https://x.com/example/status/1234567890"}
    assert {candidate.author_username for candidate in candidates.values()} == {"example"}
    assert {candidate.prompt for candidate in candidates.values()} == {"一个可爱的角色 🌟"}


def test_only_prompt_posts_filters_candidates_before_counting() -> None:
    class Locator:
        def evaluate_all(self, script):
            return [
                {
                    "status": "https://x.com/example/status/1",
                    "authorName": "example",
                    "repost": False,
                    "promoted": False,
                    "video": False,
                    "tweetText": "普通风景照片",
                    "media": ["https://pbs.twimg.com/media/plain.jpg?name=small"],
                },
                {
                    "status": "https://x.com/example/status/2",
                    "authorName": "example",
                    "repost": False,
                    "promoted": False,
                    "video": False,
                    "tweetText": "Prompt: neon city at night --ar 16:9",
                    "media": ["https://pbs.twimg.com/media/prompt.jpg?name=small"],
                },
            ]

    class Page:
        url = "https://x.com/home"

        def locator(self, selector):
            return Locator()

    candidates: dict[str, ImageCandidate] = {}
    XCollector(Path("profile"), lambda _: None)._collect_visible_tweets(
        Page(), "", Page.url, False, candidates, None, "image", True
    )
    assert list(candidates) == ["https://pbs.twimg.com/media/prompt.jpg?name=orig"]


def test_metadata_csv_round_trips_quotes_newlines_and_emoji(tmp_path: Path) -> None:
    config = TaskConfig("", 1, tmp_path, platform="x", collection_mode="home")
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    store = TaskStore(task_dir, config)
    record = DownloadRecord(
        "0001.jpg",
        "",
        "https://x.com/example/status/1",
        "https://pbs.twimg.com/media/one.jpg?name=orig",
        status="downloaded",
        platform="x",
        author_username="example",
        post_url="https://x.com/example/status/1",
        post_id="1",
        post_text='中文, "quoted"\nemoji 🌟',
        prompt_detected=True,
        prompt="一个可爱的角色",
        collected_at="2026-09-01T00:00:00",
    )
    store.append(record)
    with (task_dir / "metadata.csv").open("r", newline="", encoding="utf-8-sig") as handle:
        row = next(csv.DictReader(handle))
    assert row["post_text"] == '中文, "quoted"\nemoji 🌟'
    assert row["post_id"] == "1"
    assert row["prompt"] == "一个可爱的角色"


def test_old_task_and_queue_default_only_prompt_posts_to_false(tmp_path: Path) -> None:
    old = TaskConfig.from_dict({"keyword": "old", "target_count": 1, "output_root": "out"})
    assert old.only_prompt_posts is False

    queue_path = tmp_path / "queue.json"
    queue_path.write_text(
        json.dumps({"version": 1, "tasks": [{"status": "pending", "config": {
            "keyword": "old", "target_count": 1, "output_root": "out"
        }}]}),
        encoding="utf-8",
    )
    loaded = QueueStore(queue_path).load()
    assert len(loaded) == 1
    assert loaded[0].config.only_prompt_posts is False
