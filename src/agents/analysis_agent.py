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
from src.models.schemas import (
    VideoRequirement,
    TranscriptSegment,
    HighlightClip,
    ContentAnalysis,
)


# ============================================================
# System Prompt —— Agent 2 的"眼睛"
# ============================================================

ANALYSIS_PROMPT = """你是一个专业的视频内容分析师。你的任务是从视频的文字记录中找出最值得放进成片的片段。

## 评分标准（0.0 - 1.0）
- 0.9-1.0: 绝对要保留的高光时刻（冲刺、获奖、掌声雷动、关键结论）
- 0.7-0.8: 重要内容（比赛过程、主要论点、精彩发言）
- 0.5-0.6: 可选内容（过渡、介绍、一般性讨论）
- 0.3-0.4: 可省略（寒暄、沉默、重复内容）
- 0.0-0.2: 应该删除（广告、无关闲聊、冗长停顿）

## 特别关注
- 观众反应：欢呼、掌声、笑声 → 高重要性
- 情绪波动：激动、紧张、感动 → 中高重要性
- 关键信息：人名、数字、结论、决定 → 中高重要性
- 重复内容：同一件事说第二遍 → 低重要性

## 输出格式（严格遵守）
返回一个 JSON，包含 highlights 数组：
{
  "highlights": [
    {
      "start": 60.0,
      "end": 75.5,
      "text": "片段中的文字内容",
      "importance": 0.95,
      "category": "highlight",
      "reason": "选手冲刺瞬间，全场欢呼",
      "suggestion": "建议慢放 + 特写"
    }
  ],
  "segment_summary": "这段内容主要讲了..."
}
"""


class AnalysisAgent(BaseAgent):
    """
    Agent 2：内容分析智能体

    用法：
        agent = AnalysisAgent()
        analysis = agent.run("video.mp4", requirement)
        for clip in analysis.highlights:
            print(f"[{clip.importance}] {clip.start}-{clip.end}: {clip.reason}")
    """

    def __init__(self, whisper_model_size: str = "large-v3"):
        super().__init__("AnalysisAgent")
        self.whisper = WhisperTool(
            model_size=whisper_model_size,
            device="cuda",  # 用你的 RTX 4060
        )

    def run(
        self,
        video_path: str,
        requirement: VideoRequirement,
    ) -> ContentAnalysis:
        """
        分析视频内容

        参数：
          video_path: 视频文件路径
          requirement: Agent 1 输出的结构化需求

        返回值：
          ContentAnalysis: 包含完整转录 + 高光片段列表
        """
        self.log(f"开始分析视频: {video_path}")
        self._start_timer()

        # ============================================
        # 第一步：Whisper 语音转文字
        # ============================================
        self.log("Step 1/3: Whisper 转录中...")
        raw_segments = self.whisper.transcribe(video_path)
        self.log(f"转录完成，共 {len(raw_segments)} 个片段")

        # 包装成 Pydantic 模型
        transcript = [
            TranscriptSegment(**seg) for seg in raw_segments
        ]

        # 计算视频总时长
        video_duration = transcript[-1].end if transcript else 0
        self.log(f"视频总时长: {video_duration:.0f} 秒 ({video_duration/60:.1f} 分钟)")

        # ============================================
        # 第二步：文本分块 + LLM 分析
        # ============================================
        self.log("Step 2/3: LLM 分析关键片段...")

        # 分块策略：每 20 个转录片段为一组，重叠 5 个（防止关键内容被切断）
        CHUNK_SIZE = 20
        OVERLAP = 5

        all_highlights = []
        chunks = self._split_transcript(transcript, CHUNK_SIZE, OVERLAP)

        self.log(f"分为 {len(chunks)} 个文本块进行分析")

        for i, chunk in enumerate(chunks):
            # 构建发给 LLM 的文本
            chunk_text = self._format_chunk(chunk, requirement)

            try:
                response = call_llm(
                    system_prompt=ANALYSIS_PROMPT,
                    user_message=chunk_text,
                    return_json=True,
                    temperature=0.3,
                )

                # 提取高光片段
                if "highlights" in response:
                    for h in response["highlights"]:
                        # 只保留在合理时间范围内的片段
                        if 0 <= h.get("start", 0) <= video_duration:
                            try:
                                clip = HighlightClip(**h)
                                all_highlights.append(clip)
                            except Exception:
                                pass  # 跳过格式不对的

            except Exception as e:
                self.log(f"分析第 {i+1} 块时出错: {e}", level="warning")
                continue

        self.log(f"共识别 {len(all_highlights)} 个候选片段")

        # ============================================
        # 第三步：排序 + 去重 + 筛选
        # ============================================
        self.log("Step 3/3: 去重排序...")

        # 用需求中的关键词加权
        for clip in all_highlights:
            for keyword in requirement.focus_keywords:
                if keyword in clip.text:
                    clip.importance = min(1.0, clip.importance + 0.1)

        # 去重：时间重叠超过 50% 的只保留重要性更高的
        highlights = self._deduplicate(all_highlights)

        # 按重要性排序
        highlights.sort(key=lambda x: x.importance, reverse=True)

        self.log(f"去重后剩余 {len(highlights)} 个高光片段")
        if highlights:
            self.log(f"Top 3: {[(h.importance, h.reason[:30]) for h in highlights[:3]]}")

        # ============================================
        # 第四步：生成摘要
        # ============================================
        summary = self._generate_summary(transcript, video_duration, requirement)

        self._end_timer()

        return ContentAnalysis(
            video_duration=video_duration,
            transcript=transcript,
            highlights=highlights,
            summary=summary,
            keyword_timeline=self._build_keyword_timeline(transcript, requirement),
        )

    # ============================================================
    # 内部辅助方法
    # ============================================================

    def _split_transcript(
        self,
        transcript: list[TranscriptSegment],
        chunk_size: int,
        overlap: int,
    ) -> list[list[TranscriptSegment]]:
        """
        把转录文本切成小块（滑动窗口分块）

        为什么要重叠？
        如果不重叠，一个关键片段可能刚好被切在两块的边界上。
        重叠 5 个片段可以保证边界处的内容也被分析到。
        """
        chunks = []
        step = chunk_size - overlap  # 实际步长
        for i in range(0, len(transcript), step):
            chunk = transcript[i : i + chunk_size]
            if chunk:
                chunks.append(chunk)
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

        # 按开始时间排序
        sorted_clips = sorted(clips, key=lambda x: x.start)
        kept = [sorted_clips[0]]

        for clip in sorted_clips[1:]:
            last = kept[-1]
            overlap_start = max(clip.start, last.start)
            overlap_end = min(clip.end, last.end)

            if overlap_start < overlap_end:
                # 有重叠
                overlap_duration = overlap_end - overlap_start
                shorter_duration = min(clip.end - clip.start, last.end - last.start)
                if shorter_duration > 0 and overlap_duration / shorter_duration > overlap_threshold:
                    # 重叠超过阈值，保留更重要的那个
                    if clip.importance > last.importance:
                        kept[-1] = clip
                else:
                    kept.append(clip)
            else:
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

    test_video = Path("C:/Users/22307/video-agent-pipeline/data/test.mp4")
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
