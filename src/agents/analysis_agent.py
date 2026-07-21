"""
===========================================================================
analysis_agent.py — Agent 2: 内容分析
===========================================================================
功能：分析视频内容，识别值得放进成片的关键片段

技术原理（大白话版）：
  Agent 2 做三件事：
  1. 用 Whisper 把视频中的所有语音转成文字（带时间戳）
  2. 把文字分段发给 GPT，"这段里面有什么精彩内容？"
  3. 汇总所有 GPT 的评分，排序选出最重要的片段

为什么不能一次性发给 GPT？
  一段 2 小时视频的转录文本可能有 3 万字。GPT 的上下文窗口
  虽然能容纳，但分析质量会下降（"迷失在中间"效应）。
  分段分析 + 汇总排序是更可靠的策略。

Python 知识点：
  1. 文本分块（chunking）策略——把长文本切成小段
  2. sorted(key=...) —— 自定义排序规则
  3. list slicing: transcript[i:i+chunk_size] —— 列表切片
===========================================================================
"""

from src.agents.base import BaseAgent
from src.tools.llm import call_llm
from src.tools.whisper import WhisperTool
from src.tools.ffmpeg import FFmpegTool
from src.config import WHISPER_COMPUTE_TYPE, WHISPER_DEVICE, WHISPER_MODEL_SIZE
from src.models.schemas import (
    VideoRequirement,
    TranscriptSegment,
    HighlightClip,
    ContentAnalysis,
)
from src.services.evidence import EvidenceBuilder


class AnalysisAgent(BaseAgent):
    """
    Agent 2：内容分析智能体

    用法：
        agent = AnalysisAgent()
        analysis = agent.run("video.mp4", requirement)
        for clip in analysis.highlights:
            print(f"[{clip.importance}] {clip.start}-{clip.end}: {clip.reason}")
    """

    def __init__(self, whisper_model_size: str = WHISPER_MODEL_SIZE):
        super().__init__("AnalysisAgent")
        self.whisper = WhisperTool(
            model_size=whisper_model_size,
            device=WHISPER_DEVICE,
            compute_type=WHISPER_COMPUTE_TYPE,
        )

    def run(
        self,
        video_path: str,
        requirement: VideoRequirement,
    ) -> ContentAnalysis:
        """兼容分析接口：只转录与构建证据，不再自由生成高光候选。

        候选必须由 ``CandidateAgent`` 经受限检索和引文校验生成；该方法保留
        是为了让已有调用方仍可获得转录和摘要。
        """
        transcript, evidence, video_duration = self.transcribe(video_path)
        summary = self._generate_summary(transcript, video_duration, requirement)
        return ContentAnalysis(
            video_duration=video_duration,
            transcript=transcript,
            evidence=evidence,
            highlights=[],
            summary=summary,
            keyword_timeline=self._build_keyword_timeline(transcript, requirement),
        )

    def transcribe(self, video_path: str) -> tuple[list[TranscriptSegment], list, float]:
        """产生带稳定证据的转录，不在此阶段作内容价值判断。"""
        self.log(f"开始转录视频: {video_path}")
        self._start_timer()
        raw_segments = self.whisper.transcribe(video_path)
        transcript = [TranscriptSegment(**segment) for segment in raw_segments]
        evidence = EvidenceBuilder.from_transcript(transcript)
        media_info = FFmpegTool.get_video_info(video_path)
        video_duration = float(media_info["duration"]) if media_info else (transcript[-1].end if transcript else 0.0)
        self._end_timer()
        self.log(f"转录完成，共 {len(transcript)} 段、{len(evidence)} 条可引用证据")
        return transcript, evidence, video_duration

    # ============================================================
    # 内部辅助方法
    # ============================================================

    def _split_transcript(
        self,
        transcript: list[TranscriptSegment],
        max_duration: float,
        overlap_duration: float,
    ) -> list[list[TranscriptSegment]]:
        """
        把转录文本切成小块（滑动窗口分块）

        为什么要重叠？
        如果不重叠，一个关键片段可能刚好被切在两块的边界上。
        重叠 5 个片段可以保证边界处的内容也被分析到。
        """
        chunks: list[list[TranscriptSegment]] = []
        start_index = 0
        while start_index < len(transcript):
            chunk: list[TranscriptSegment] = []
            chunk_start = transcript[start_index].start
            index = start_index
            while index < len(transcript) and transcript[index].end - chunk_start <= max_duration:
                chunk.append(transcript[index])
                index += 1
            if not chunk:
                chunk = [transcript[start_index]]
                index = start_index + 1
            chunks.append(chunk)
            if index >= len(transcript):
                break
            next_start = chunk[-1].end - overlap_duration
            start_index = next((i for i, segment in enumerate(transcript) if i > start_index and segment.start >= next_start), index)
        return chunks

    def _format_chunk(
        self,
        chunk: list[TranscriptSegment],
        requirement: VideoRequirement,
    ) -> str:
        """把一组转录片段格式化成 LLM 能理解的文本"""
        lines = []
        lines.append(f"视频类型: {requirement.video_type}")
        lines.append(f"关注重点: {', '.join(requirement.focus_keywords)}")
        lines.append(f"剪辑风格: {requirement.style}")
        lines.append(f"允许输出的时间范围: {chunk[0].start:.2f}s - {chunk[-1].end:.2f}s")
        lines.append("")
        lines.append("以下是视频的语音转录（带时间戳）：")
        lines.append("---")

        for seg in chunk:
            timestamp = f"[{int(seg.start//60):02d}:{seg.start%60:04.1f}]"
            speaker = f"说话人{seg.speaker_id}: " if seg.speaker_id > 0 else ""
            lines.append(f"{timestamp} {speaker}{seg.text}")

        return "\n".join(lines)

    def _deduplicate(
        self,
        clips: list[HighlightClip],
        overlap_threshold: float = 0.5,
    ) -> list[HighlightClip]:
        """
        去除时间重叠的重复片段

        逻辑：如果两个片段的时间重叠超过 50%，只保留重要性更高的那个
        """
        if not clips:
            return []

        kept: list[HighlightClip] = []
        for clip in sorted(clips, key=lambda item: item.importance, reverse=True):
            duplicate = False
            for existing in kept:
                intersection = max(0.0, min(clip.end, existing.end) - max(clip.start, existing.start))
                shorter = min(clip.end - clip.start, existing.end - existing.start)
                if shorter > 0 and intersection / shorter > overlap_threshold:
                    duplicate = True
                    break
            if not duplicate:
                kept.append(clip)

        return kept

    def _generate_summary(
        self,
        transcript: list[TranscriptSegment],
        duration: float,
        requirement: VideoRequirement,
    ) -> str:
        """用 LLM 生成视频内容的一句话摘要"""
        # 取开头、中间、结尾各一段文字作为样本
        sample_indices = [0, len(transcript) // 2, len(transcript) - 1]
        sample_texts = []
        for idx in sample_indices:
            if 0 <= idx < len(transcript):
                sample_texts.append(transcript[idx].text)

        sample = " | ".join(sample_texts)

        try:
            summary = call_llm(
                system_prompt="用一句话概括视频内容，不超过 50 字。",
                user_message=f"视频时长 {duration/60:.0f} 分钟，类型 {requirement.video_type}。片段样本: {sample}",
                return_json=False,
                temperature=0.3,
            )
            return summary[:100]  # 截断
        except Exception:
            return f"一段 {duration/60:.0f} 分钟的{requirement.video_type}视频"

    def _build_keyword_timeline(
        self,
        transcript: list[TranscriptSegment],
        requirement: VideoRequirement,
    ) -> dict[str, list[float]]:
        """构建关键词时间轴：{关键词: [出现的时间点列表]}"""
        timeline = {}
        for keyword in requirement.focus_keywords:
            times = []
            for seg in transcript:
                if keyword in seg.text:
                    times.append(seg.start)
            if times:
                timeline[keyword] = times
        return timeline


# ============================================================
# 测试代码
# ============================================================
if __name__ == "__main__":
    print("=" * 60)
    print("Agent 2 测试：内容分析")
    print("=" * 60)

    from pathlib import Path

    # 用 tiny 模型测试（更快），不用加载 large-v3
    agent = AnalysisAgent(whisper_model_size="tiny")

    test_video = Path("data/test.mp4")
    if test_video.exists():
        from src.models.schemas import VideoRequirement

        req = VideoRequirement(
            target_duration=180,
            video_type="sports",
            style="exciting",
            focus_keywords=["冲刺", "欢呼", "颁奖"],
        )

        result = agent.run(str(test_video), req)
        print(f"\n摘要: {result.summary}")
        print(f"高光片段数: {len(result.highlights)}")
        for clip in result.highlights[:5]:
            print(f"  [{clip.importance:.2f}] {clip.start:.0f}s-{clip.end:.0f}s: {clip.reason}")
    else:
        print(f"请先放一个测试视频到: {test_video}")
