"""Agent 3：将候选高光片段转换为可执行、可审核的剪辑计划。"""

from __future__ import annotations

from src.agents.base import BaseAgent
from src.tools.llm import call_llm
import re
from typing import Iterable, Optional
from src.models.schemas import (
    ContentAnalysis,
    EditOperation,
    EditScript,
    HighlightClip,
    RequirementItem,
    TranscriptSegment,
    VideoRequirement,
)


class ScriptAgent(BaseAgent):
    """用确定性规则生成剪辑计划，避免 LLM 改写合法的时间边界。"""

    MIN_CLIP_DURATION = 3.0
    MAX_CLIP_DURATION = 60.0

    def __init__(self):
        super().__init__("ScriptAgent")

    def run(
        self,
        analysis: ContentAnalysis,
        requirement: VideoRequirement,
        requirement_items: Optional[list[RequirementItem]] = None,
        duration_tolerance: Optional[float] = None,
        transition_duration: float = 0.0,
    ) -> EditScript:
        self._start_timer()
        must_ids = {
            item.id for item in (requirement_items or [])
            if item.priority == "must"
        }
        selected = self._select_clips(
            analysis.highlights,
            requirement.target_duration,
            analysis.source_durations or analysis.video_duration,
            must_requirement_ids=must_ids,
            duration_tolerance=duration_tolerance,
            transition_duration=transition_duration,
        )
        operations = [
            EditOperation(
                order=index,
                source_asset_id=clip.source_asset_id,
                action="cut",
                source_start=clip.start,
                source_end=clip.end,
                note=clip.reason,
            )
            for index, clip in enumerate(selected, start=1)
        ]
        estimated_duration = sum(clip.end - clip.start for clip in selected)
        estimated_duration -= transition_duration * max(0, len(selected) - 1)
        subtitles = (
            self._build_remapped_srt(analysis.transcript, selected)
            if requirement.need_subtitles
            else ""
        )
        self._end_timer()
        self.log(f"生成 {len(operations)} 个可确认片段，预计 {estimated_duration:.1f}s")
        return EditScript(
            title="精彩回顾",
            estimated_duration=estimated_duration,
            operations=operations,
            srt_subtitles=subtitles,
            transition_duration=transition_duration,
            notes="片段已按原始时间顺序排列；导出前可删除不需要的片段。",
        )

    def _select_clips(
        self,
        highlights: list[HighlightClip],
        target_duration: float,
        video_duration: float | dict[str, float],
        must_requirement_ids: Optional[set[str]] = None,
        duration_tolerance: Optional[float] = None,
        transition_duration: float = 0.0,
        already_selected: Optional[list[HighlightClip]] = None,
    ) -> list[HighlightClip]:
        """先覆盖 must，再按评分补足预算；普通预算不能静默删除 must。"""
        tolerance = target_duration * 0.1 if duration_tolerance is None else duration_tolerance
        budget = target_duration + tolerance
        selected: list[HighlightClip] = list(already_selected or [])
        total = sum(item.end - item.start for item in selected)
        total -= transition_duration * max(0, len(selected) - 1)
        must_ids = set(must_requirement_ids or set())

        def normalise_clip(clip: HighlightClip) -> Optional[HighlightClip]:
            if isinstance(video_duration, dict):
                duration = video_duration.get(clip.source_asset_id or "")
                if clip.source_asset_id is None and len(video_duration) == 1:
                    duration = next(iter(video_duration.values()))
                source_duration = float(duration or 0.0)
            else:
                source_duration = float(video_duration)
            start = max(0.0, clip.start)
            # 不能为了满足单段上限重新截断已经通过完整表达校验的候选。
            end = min(source_duration, clip.end)
            if end - start < self.MIN_CLIP_DURATION:
                return None
            return clip.model_copy(update={"start": start, "end": end})

        uncovered = set(must_ids) - {
            requirement_id for item in selected for requirement_id in item.matched_requirement_ids
        }
        must_candidates = sorted(
            highlights,
            key=lambda item: (
                -len(set(item.matched_requirement_ids).intersection(must_ids)),
                -item.importance,
                item.start,
            ),
        )
        for clip in must_candidates:
            covers = set(clip.matched_requirement_ids).intersection(uncovered)
            if not covers:
                continue
            candidate = normalise_clip(clip)
            if candidate is None or any(self._overlap(candidate, kept) for kept in selected):
                continue
            overlap_cost = transition_duration if selected else 0.0
            selected.append(candidate)
            uncovered.difference_update(covers)
            total += candidate.end - candidate.start - overlap_cost
            if not uncovered:
                break

        for clip in self._diverse_order(highlights, selected):
            if total >= target_duration:
                break
            candidate = normalise_clip(clip)
            if candidate is None:
                continue
            duration = candidate.end - candidate.start
            overlap_cost = transition_duration if selected else 0.0
            if total + duration - overlap_cost > budget and selected:
                continue
            if any(self._overlap(candidate, kept) for kept in selected):
                continue
            selected.append(candidate)
            total += duration - overlap_cost

        return sorted(
            selected,
            key=lambda item: (item.source_asset_id or "", item.start),
        )

    @classmethod
    def _diverse_order(
        cls,
        highlights: Iterable[HighlightClip],
        already_selected: Iterable[HighlightClip] = (),
    ) -> list[HighlightClip]:
        """MMR 风格贪心排序，降低重复讲话和单一来源连续占满预算的概率。"""
        remaining = list(highlights)
        chosen = list(already_selected)
        ranked: list[HighlightClip] = []
        while remaining:
            source_counts = {
                source_id: sum(item.source_asset_id == source_id for item in chosen)
                for source_id in {item.source_asset_id for item in remaining}
            }

            def score(item: HighlightClip) -> tuple[float, float, float]:
                repetition = max(
                    (cls._text_similarity(item.text, kept.text) for kept in chosen),
                    default=0.0,
                )
                source_penalty = 0.04 * source_counts.get(item.source_asset_id, 0)
                return (
                    item.importance - 0.35 * repetition - source_penalty,
                    item.importance,
                    -(item.end - item.start),
                )

            best = max(remaining, key=score)
            remaining.remove(best)
            ranked.append(best)
            chosen.append(best)
        return ranked

    @staticmethod
    def _text_similarity(left: str, right: str) -> float:
        def tokens(value: str) -> set[str]:
            compact = re.sub(r"\s+", "", value.lower())
            chinese = re.findall(r"[\u4e00-\u9fff]", compact)
            bigrams = {
                "".join(chinese[index:index + 2])
                for index in range(max(0, len(chinese) - 1))
            }
            words = set(re.findall(r"[a-z0-9]{2,}", compact))
            return bigrams | words

        left_tokens, right_tokens = tokens(left), tokens(right)
        union = left_tokens | right_tokens
        if not union:
            return 0.0
        return len(left_tokens & right_tokens) / len(union)

    @staticmethod
    def _overlap(left: HighlightClip, right: HighlightClip) -> bool:
        if left.source_asset_id != right.source_asset_id:
            return False
        return max(left.start, right.start) < min(left.end, right.end)

    def _build_remapped_srt(
        self,
        transcript: list[TranscriptSegment],
        clips: list[HighlightClip],
        initial_offset: float = 0.0,
    ) -> str:
        """仅保留入选片段中的转录，并映射到拼接后的成片时间轴。"""
        lines: list[str] = []
        output_offset = initial_offset
        subtitle_index = 1

        for clip in clips:
            for segment in transcript:
                if (
                    clip.source_asset_id is not None
                    and segment.source_asset_id != clip.source_asset_id
                ):
                    continue
                start = max(segment.start, clip.start)
                end = min(segment.end, clip.end)
                if end <= start or not segment.text.strip():
                    continue
                lines.extend(
                    [
                        str(subtitle_index),
                        f"{self._format_srt_time(output_offset + start - clip.start)} --> "
                        f"{self._format_srt_time(output_offset + end - clip.start)}",
                        segment.text.strip(),
                        "",
                    ]
                )
                subtitle_index += 1
            output_offset += clip.end - clip.start
        return "\n".join(lines)

    def apply_selection(
        self,
        script: EditScript,
        analysis: ContentAnalysis,
        selected_orders: list[int],
        transition_duration: float = 0.0,
        subtitle_style: str = "classic",
        bgm_path: Optional[str] = None,
        bgm_volume: float = 0.15,
        intro_style: str = "none",
        outro_style: str = "none",
        title_text: str = "",
        correct_subtitles: bool = True,
    ) -> EditScript:
        """根据用户确认的片段重新计算时长和字幕时间轴。"""
        selected_set = set(selected_orders)
        operations = [
            operation
            for operation in script.operations
            if operation.action == "cut" and operation.order in selected_set
        ]
        operations = [
            operation.model_copy(update={"order": index})
            for index, operation in enumerate(operations, start=1)
        ]
        clips = [
            HighlightClip(
                start=operation.source_start,
                end=operation.source_end,
                source_asset_id=operation.source_asset_id,
                text="",
                importance=0.0,
                category="transition",
                reason=operation.note or "用户确认的片段",
            )
            for operation in operations
            if operation.source_start is not None and operation.source_end is not None
        ]
        corrected_transcript = (
            self._correct_subtitles(analysis.transcript, clips)
            if correct_subtitles
            else analysis.transcript
        )
        estimated_duration = sum(clip.end - clip.start for clip in clips)
        if len(clips) > 1:
            estimated_duration -= transition_duration * (len(clips) - 1)
        estimated_duration += 2.0 * sum(
            style != "none" for style in (intro_style, outro_style)
        )
        return script.model_copy(
            update={
                "operations": operations,
                "estimated_duration": estimated_duration,
                "srt_subtitles": self._build_remapped_srt(
                    corrected_transcript,
                    clips,
                    initial_offset=2.0 if intro_style != "none" else 0.0,
                ),
                "subtitle_style": subtitle_style,
                "transition_duration": transition_duration,
                "bgm_path": bgm_path or None,
                "bgm_volume": bgm_volume,
                "intro_style": intro_style,
                "outro_style": outro_style,
                "title_text": title_text.strip() or script.title,
                "notes": "已按用户确认的片段重新生成字幕时间轴，并应用所选导出样式。",
            }
        )

    def _correct_subtitles(
        self,
        transcript: list[TranscriptSegment],
        clips: list[HighlightClip],
    ) -> list[TranscriptSegment]:
        """用 LLM 仅修复错别字、同音字和断句，不改动时间轴或扩写内容。"""
        # 未配置模型时直接保留转写结果，避免离线使用时出现无意义的重试等待。
        from src.config import LLM_API_KEY

        if not LLM_API_KEY:
            return transcript
        selected = [
            segment for segment in transcript
            if any(
                (
                    clip.source_asset_id is None
                    or segment.source_asset_id == clip.source_asset_id
                )
                and max(segment.start, clip.start) < min(segment.end, clip.end)
                for clip in clips
            )
        ]
        if not selected:
            return transcript

        lines = [f"{index}: {segment.text}" for index, segment in enumerate(selected)]
        prompt = "\n".join(lines)
        try:
            response = call_llm(
                system_prompt=(
                    "你是中文视频字幕校对员。只修正错别字、同音字、明显缺失的标点和断句；"
                    "不得扩写、概括、删减事实或改变原意。返回 JSON："
                    '{"segments":[{"index":0,"text":"校正后的文本"}]}'
                ),
                user_message=f"请校对以下带编号字幕：\n{prompt}",
                return_json=True,
                temperature=0.0,
                max_tokens=2048,
            )
            replacements = {
                int(item["index"]): str(item["text"]).strip()
                for item in response.get("segments", [])
                if isinstance(item, dict) and str(item.get("index", "")).isdigit() and item.get("text")
            }
            corrected = list(transcript)
            positions = {id(segment): index for index, segment in enumerate(transcript)}
            for selected_index, segment in enumerate(selected):
                text = replacements.get(selected_index)
                if text and len(text) <= max(4, len(segment.text) * 2):
                    corrected[positions[id(segment)]] = segment.model_copy(update={"text": text})
            return corrected
        except Exception as error:
            self.log(f"字幕语义纠错失败，保留 ASR 原文: {error}", level="warning")
            return transcript

    @staticmethod
    def _format_srt_time(seconds: float) -> str:
        milliseconds = round(max(0.0, seconds) * 1000)
        hours, milliseconds = divmod(milliseconds, 3_600_000)
        minutes, milliseconds = divmod(milliseconds, 60_000)
        seconds, milliseconds = divmod(milliseconds, 1000)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"
