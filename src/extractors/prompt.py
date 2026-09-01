from __future__ import annotations

import re
from dataclasses import dataclass


# Keep the longest/composite labels first so ``GPT IMAGE2 提示词`` is not
# reduced to the shorter ``提示词`` label.
_PROMPT_LABEL = re.compile(
    r"(?ix)"
    r"(?:"
    r"gpt\s*-?\s*image\s*\d*\s*(?:prompt|提示词)"
    r"|(?:image\s+prompt|negative\s+prompt|prompt|parameters?)\b"
    r"|(?:正向|反向|生图|绘图|图片|AI\s*)提示词"
    r"|提示词"
    r")"
)
_PROMPT_SEPARATOR = re.compile(r"^(?:[\s:：,，;；|•·~～_\-–—]*)", re.IGNORECASE)
_PROMPT_DIRECTION = re.compile(r"^(?:below|down|如下|如下所示|在下方|见下|👇|⬇️|↓)+", re.IGNORECASE)
_OTHER_LABEL = re.compile(
    r"(?ix)^\s*(?:negative\s+prompt|parameters?|cfg\s*scale|steps?|提示词|参数)\b\s*[:：]?"
)
_PARAMETER = re.compile(r"(?:^|\s)--(?:ar|stylize|style|v|chaos|seed|sref)\b", re.IGNORECASE)
_STRUCTURED_PARAMETER = re.compile(
    r"(?ix)(?:negative\s+prompt|parameters?|cfg\s*scale|steps?|提示词|参数)\s*[:：]"
)
_CALL_TO_ACTION = re.compile(
    r"(?ix)^\s*(?:"
    r"喜欢(?:的话|就)?(?:点个赞|点赞|收藏|关注)|"
    r"点个赞|欢迎(?:点赞|关注|收藏)|"
    r"follow\s+for\s+more|like\s+and\s+follow|"
    r"感谢支持|谢谢支持|转发支持|求赞|求关注"
    r")"
)


@dataclass(frozen=True)
class PromptDetection:
    prompt_detected: bool
    prompt: str = ""
    label_matched: bool = False
    parameter_matched: bool = False


class PromptDetector:
    """Conservative, local-only detection and extraction for generated-image prompts."""

    def detect(self, text: str) -> PromptDetection:
        if not text or not text.strip():
            return PromptDetection(False)
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        label_matched = False
        for index, line in enumerate(lines):
            match = self._find_label(line)
            if not match:
                continue
            label_matched = True
            prompt_lines: list[str] = []
            remainder = self._clean_label_remainder(line[match.end() :])
            if self._is_substantive(remainder):
                prompt_lines.append(remainder)
            for following in lines[index + 1 :]:
                if not following.strip() or _OTHER_LABEL.match(following):
                    break
                if _CALL_TO_ACTION.match(following):
                    break
                prompt_lines.append(following.rstrip())
            prompt = self._clean_prompt("\n".join(prompt_lines))
            if self._is_substantive(prompt):
                return PromptDetection(True, prompt, True, bool(_PARAMETER.search(prompt)))

        non_empty = [line.strip() for line in lines if line.strip()]
        candidate = "\n".join(non_empty).strip()
        parameter_matched = bool(_PARAMETER.search(candidate) or _STRUCTURED_PARAMETER.search(candidate))
        if parameter_matched:
            without_parameters = _PARAMETER.sub("", candidate).strip(" ,，")
            if self._is_substantive(without_parameters) and (
                len(re.findall(r"[A-Za-z0-9\u3400-\u9fff]", without_parameters)) >= 15
                or len(non_empty) >= 2
            ):
                return PromptDetection(True, candidate, label_matched, True)
        return PromptDetection(False, "", label_matched, parameter_matched)

    @staticmethod
    def _find_label(line: str) -> re.Match[str] | None:
        for match in _PROMPT_LABEL.finditer(line):
            prefix = line[: match.start()].strip()
            label = match.group(0)
            # A generic word such as ``prompt`` in a sentence is not a label.
            # Composite/Chinese labels remain valid after a short caption.
            composite = bool(re.search(r"(?i)image|提示词|参数|生图|绘图|图片", label))
            if prefix and not composite and not re.search(r"[：:|•·,，;；!?！？]$", prefix):
                continue
            return match
        return None

    @staticmethod
    def _clean_label_remainder(value: str) -> str:
        remainder = value.strip()
        previous = None
        while remainder and remainder != previous:
            previous = remainder
            remainder = _PROMPT_SEPARATOR.sub("", remainder)
            remainder = _PROMPT_DIRECTION.sub("", remainder).strip()
            remainder = _PROMPT_SEPARATOR.sub("", remainder)
        return remainder.strip()

    @staticmethod
    def _clean_prompt(value: str) -> str:
        lines = [line.rstrip() for line in value.splitlines()]
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        return "\n".join(lines).strip()

    @staticmethod
    def _is_substantive(value: str) -> bool:
        return len(re.findall(r"[A-Za-z0-9\u3400-\u9fff]", value)) >= 3
