"""Agent 3：将候选高光片段转换为可执行、可审核的剪辑计划。"""

from src.agents.base import BaseAgent
from src.tools.llm import call_llm
from typing import Optional
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
    MAX_CLIP_DURATION = 30.0

    def __init__(self):
        super().__init__("ScriptAgent")

    def run(
        self,
        analysis: ContentAnalysis,
        requirement: VideoRequirement,
        requirement_items: Optional[list[RequirementItem]] = None,
    ) -> EditScript:
        self._start_timer()
        must_ids = {
            item.id for item in (requirement_items or [])
            if item.priority == "must"
        }
        selected = self._select_clips(
            analysis.highlights,
            requirement.target_duration,
            analysis.video_duration,
            must_requirement_ids=must_ids,
        )
        operations = [
            EditOperation(
                order=index,
                action="cut",
                source_start=clip.start,
                source_end=clip.end,
                note=clip.reason,
            )
            for index, clip in enumerate(selected, start=1)
        ]
        estimated_duration = sum(clip.end - clip.start for clip in selected)
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
            notes="片段已按原始时间顺序排列；导出前可删除不需要的片段。",
        )

    def _select_clips(
        self,
        highlights: list[HighlightClip],
        target_duration: float,
        video_duration: float,
        must_requirement_ids: Optional[set[str]] = None,
    ) -> list[HighlightClip]:
        """先覆盖 must，再按评分补足预算；普通预算不能静默删除 must。"""
        budget = target_duration * 1.15
        selected: list[HighlightClip] = []
        total = 0.0
        must_ids = set(must_requirement_ids or set())

        def normalise_clip(clip: HighlightClip) -> Optional[HighlightClip]:
            start = max(0.0, clip.start)
            end = min(video_duration, clip.end, start + self.MAX_CLIP_DURATION)
            if end - start < self.MIN_CLIP_DURATION:
                return None
            return clip.model_copy(update={"start": start, "end": end})

        uncovered = set(must_ids)
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
            selected.append(candidate)
            uncovered.difference_update(covers)
            total += candidate.end - candidate.start
            if not uncovered:
                break

        for clip in sorted(highlights, key=lambda item: item.importance, reverse=True):
            candidate = normalise_clip(clip)
            if candidate is None:
                continue
            duration = candidate.end - candidate.start
            if total + duration > budget and selected:
                continue
            if any(self._overlap(candidate, kept) for kept in selected):
                continue
            selected.append(candidate)
            total += duration
            if total >= target_duration * 0.85:
                break

        return sorted(selected, key=lambda item: item.start)

    @staticmethod
    def _overlap(left: HighlightClip, right: HighlightClip) -> bool:
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
            if any(max(segment.start, clip.start) < min(segment.end, clip.end) for clip in clips)
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
