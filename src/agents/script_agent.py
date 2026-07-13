"""
===========================================================================
script_agent.py — Agent 3: 剪辑脚本生成
===========================================================================
功能：根据内容分析结果和用户需求，生成精确的剪辑"施工图纸"

技术原理（大白话版）：
  Agent 2 给出了"哪些片段值得用"，Agent 1 给出了"用户想要什么"。
  Agent 3 的职责是把两者结合，回答：
  - 选哪些片段？（从 highlights 中筛选）
  - 按什么顺序？（时间顺序？还是打乱？）
  - 怎么衔接？（硬切？淡入淡出？）
  - 字幕怎么配？
  - 成品大概多长？

  这就像一个剪辑师拿到素材后，先在纸上画一个"分镜表"，
  然后再动手剪。

Python 知识点：
  1. 列表操作：sum(), len(), enumerate()
  2. SRT 格式生成——一种简单的时间字幕格式
  3. LLM 作为"规划器"（Planner）的使用模式
===========================================================================
"""

from src.agents.base import BaseAgent
from src.tools.llm import call_llm
from src.models.schemas import (
    VideoRequirement,
    ContentAnalysis,
    HighlightClip,
    EditScript,
    EditOperation,
)


# ============================================================
# System Prompt —— Agent 3 的"剪辑思维"
# ============================================================

SCRIPT_PROMPT = """你是一个专业的视频剪辑师。根据内容分析结果，生成一份精确的剪辑脚本。

## 剪辑原则
1. 按时间顺序排列片段（除非用户指定要打乱）
2. 优先选择 importance 分数高的片段
3. 总时长尽量接近用户的目标时长（误差在正负 20% 内）
4. 片段之间默认用"硬切"（无转场）
5. 情绪转折处用"淡入淡出"转场
6. 第一个片段前加标题（如果有意义的话）

## 片段选择策略
- importance >= 0.8: 优先选择
- importance 0.6-0.8: 如果时长还够就选
- importance 0.4-0.6: 只在时长充裕时考虑
- importance < 0.4: 不选

## 输出格式（严格遵守）
{
  "title": "成片标题（10字以内）",
  "operations": [
    {
      "order": 1,
      "action": "title",
      "title_text": "精彩集锦",
      "duration": 2.0,
      "note": "片头标题"
    },
    {
      "order": 2,
      "action": "cut",
      "source_start": 60.0,
      "source_end": 75.5,
      "note": "冲刺瞬间"
    },
    {
      "order": 3,
      "action": "transition",
      "transition_type": "fade",
      "duration": 0.5,
      "note": ""
    },
    {
      "order": 4,
      "action": "cut",
      "source_start": 120.0,
      "source_end": 135.0,
      "note": "颁奖片段"
    }
  ],
  "srt_subtitles": "1\\n00:00:00,000 --> 00:00:02,000\\n精彩集锦\\n\\n2\\n00:00:02,000 --> 00:00:17,500\\n选手冲过终点线，全场欢呼！\\n\\n",
  "notes": "整体节奏紧凑，建议配合激昂BGM"
}

## SRT 字幕格式说明
每个字幕块格式：
序号
开始时间 --> 结束时间
字幕文本
（空行分隔）
时间格式: HH:MM:SS,mmm（时:分:秒,毫秒）

## 注意
- source_start 和 source_end 必须是原片中的实际时间戳
- 标题 duration 默认 2-3 秒
- 转场 duration 默认 0.5 秒
- 只返回 JSON，不要添加任何解释文字
"""


class ScriptAgent(BaseAgent):
    """
    Agent 3：剪辑脚本生成智能体

    用法：
        agent = ScriptAgent()
        script = agent.run(analysis, requirement)
        print(f"预估时长: {script.estimated_duration}s")
    """

    def __init__(self):
        super().__init__("ScriptAgent")

    def run(
        self,
        analysis: ContentAnalysis,
        requirement: VideoRequirement,
    ) -> EditScript:
        """
        生成剪辑脚本

        参数：
          analysis: Agent 2 的内容分析结果
          requirement: Agent 1 的用户需求

        返回值：
          EditScript: 完整的剪辑脚本
        """
        self.log(f"开始生成剪辑脚本，目标时长: {requirement.target_duration}s")
        self._start_timer()

        # ============================================
        # 第一步：筛选 + 排序高光片段
        # ============================================
        selected = self._select_clips(
            analysis.highlights,
            requirement.target_duration,
        )
        self.log(f"从 {len(analysis.highlights)} 个候选片段中选中 {len(selected)} 个")

        # ============================================
        # 第二步：用 LLM 生成剪辑脚本
        # ============================================
        self.log("调用 LLM 生成剪辑脚本...")

        user_message = self._build_script_prompt(
            selected, requirement, analysis
        )

        try:
            response = call_llm(
                system_prompt=SCRIPT_PROMPT,
                user_message=user_message,
                return_json=True,
                max_tokens=4096,
            )

            # 解析 LLM 返回的剪辑操作
            operations = []
            for op_data in response.get("operations", []):
                try:
                    operations.append(EditOperation(**op_data))
                except Exception as e:
                    self.log(f"跳过无效操作: {e}", level="warning")

            # 计算预估时长
            estimated_duration = self._estimate_duration(operations, selected)

            script = EditScript(
                title=response.get("title", ""),
                estimated_duration=estimated_duration,
                operations=operations,
                srt_subtitles=response.get("srt_subtitles", ""),
                notes=response.get("notes", ""),
            )

            self.log(f"剪辑脚本生成完成: {len(operations)} 个操作, 预估 {estimated_duration:.0f}s")
            self._end_timer()
            return script

        except Exception as e:
            self.log(f"LLM 生成脚本失败: {e}", level="error")
            # 降级：手动构建简单脚本
            return self._fallback_script(selected, requirement, analysis)

    # ============================================================
    # 内部方法
    # ============================================================

    def _select_clips(
        self,
        highlights: list[HighlightClip],
        target_duration: float,
    ) -> list[HighlightClip]:
        """
        按重要性筛选片段，直到总时长接近目标

        这是一个贪心算法：
        从最重要的片段开始选，每次加一个，直到接近目标时长
        """
        if not highlights:
            return []

        # 已按 importance 排序（在 Agent 2 中排好了）
        selected = []
        total = 0.0

        for clip in highlights:
            clip_duration = clip.end - clip.start
            # 如果加上这个片段后会超过目标的 120%，先跳过
            if total + clip_duration > target_duration * 1.3:
                # 但如果还没选够 30% 的目标时长，还是留着
                if total > target_duration * 0.3:
                    continue

            selected.append(clip)
            total += clip_duration

            # 达到目标的 80%，差不多够了
            if total >= target_duration * 0.8:
                break

        # 重新按时间排序（因为之前按 importance 排的）
        selected.sort(key=lambda x: x.start)

        return selected

    def _build_script_prompt(
        self,
        selected: list[HighlightClip],
        requirement: VideoRequirement,
        analysis: ContentAnalysis,
    ) -> str:
        """构建发给 LLM 的脚本生成请求"""
        lines = []
        lines.append(f"## 用户需求")
        lines.append(f"- 目标时长: {requirement.target_duration} 秒 ({requirement.target_duration//60}分钟)")
        lines.append(f"- 视频类型: {requirement.video_type}")
        lines.append(f"- 剪辑风格: {requirement.style}")
        lines.append(f"- 需要字幕: {requirement.need_subtitles}")
        lines.append(f"- 需要BGM建议: {requirement.need_bgm}")
        lines.append("")

        lines.append(f"## 可用片段（按时间排序，共 {len(selected)} 个）")
        for i, clip in enumerate(selected):
            lines.append(
                f"{i+1}. [{clip.start:.1f}s - {clip.end:.1f}s] "
                f"重要性:{clip.importance:.2f} | {clip.reason}"
            )
            lines.append(f"   内容: {clip.text[:80]}...")
        lines.append("")

        lines.append(f"## 视频总时长: {analysis.video_duration:.0f} 秒")

        return "\n".join(lines)

    def _estimate_duration(
        self,
        operations: list[EditOperation],
        clips: list[HighlightClip],
    ) -> float:
        """粗略估算成品时长"""
        total = 0.0
        for op in operations:
            if op.action == "cut" and op.source_start is not None and op.source_end is not None:
                total += op.source_end - op.source_start
            elif op.action == "title" and op.duration:
                total += op.duration
            elif op.action == "transition" and op.duration:
                total += op.duration
        return total

    def _fallback_script(
        self,
        clips: list[HighlightClip],
        requirement: VideoRequirement,
        analysis: ContentAnalysis,
    ) -> EditScript:
        """
        降级方案：不用 LLM，直接用规则拼接片段

        当 LLM 调用失败时启用，保证系统不崩溃。
        这就是"优雅降级"的思想——宁可功能弱一点，也不能完全不能用。
        """
        self.log("使用降级方案（纯规则拼接）", level="warning")

        operations = []

        # 加片头标题
        operations.append(EditOperation(
            order=1,
            action="title",
            title_text=f"精彩回顾",
            duration=2.0,
        ))

        # 按时间顺序拼接所有选中片段
        for i, clip in enumerate(clips):
            order = len(operations) + 1
            operations.append(EditOperation(
                order=order,
                action="cut",
                source_start=clip.start,
                source_end=clip.end,
                note=clip.reason,
            ))

            # 片段之间加硬切
            if i < len(clips) - 1:
                operations.append(EditOperation(
                    order=order + 1,
                    action="transition",
                    transition_type="none",
                    duration=0.0,
                ))

        # 简单生成 SRT 字幕
        srt = self._generate_simple_srt(clips)

        return EditScript(
            title="精彩回顾",
            estimated_duration=sum(
                (c.end - c.start) for c in clips
            ),
            operations=operations,
            srt_subtitles=srt,
            notes="（降级方案：纯规则拼接，未经 LLM 优化）",
        )

    def _generate_simple_srt(self, clips: list[HighlightClip]) -> str:
        """
        生成简单的 SRT 字幕文件

        SRT 格式示例：
        1
        00:00:00,000 --> 00:00:03,500
        选手冲过终点线

        2
        00:00:04,000 --> 00:00:08,200
        全场欢呼
        """
        lines = []
        current_time = 0.0  # 成品时间轴（不是原片时间轴）

        for i, clip in enumerate(clips):
            duration = clip.end - clip.start

            start_ts = self._format_srt_time(current_time)
            end_ts = self._format_srt_time(current_time + duration)

            lines.append(str(i + 1))
            lines.append(f"{start_ts} --> {end_ts}")
            lines.append(clip.text[:80])  # 限制字幕长度
            lines.append("")  # 空行分隔

            current_time += duration

        return "\n".join(lines)

    def _format_srt_time(self, seconds: float) -> str:
        """把秒数转成 SRT 时间格式 HH:MM:SS,mmm"""
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        ms = int((seconds % 1) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# ============================================================
# 测试代码
# ============================================================
if __name__ == "__main__":
    print("=" * 60)
    print("Agent 3 测试：剪辑脚本生成")
    print("=" * 60)

    # 造一些模拟数据来测试
    from src.models.schemas import VideoRequirement, ContentAnalysis, HighlightClip

    requirement = VideoRequirement(
        target_duration=120,
        video_type="sports",
        style="exciting",
        focus_keywords=["冲刺", "颁奖"],
    )

    mock_clips = [
        HighlightClip(start=10.0, end=25.0, text="开幕式致辞", importance=0.6, category="info", reason="开场"),
        HighlightClip(start=60.0, end=80.0, text="冲刺！冲刺！", importance=0.95, category="highlight", reason="冠军时刻"),
        HighlightClip(start=120.0, end=145.0, text="颁奖典礼", importance=0.9, category="highlight", reason="颁奖"),
    ]

    analysis = ContentAnalysis(
        video_duration=200.0,
        transcript=[],
        highlights=mock_clips,
        summary="一场精彩的运动会",
    )

    agent = ScriptAgent()
    script = agent.run(analysis, requirement)
    print(f"\n标题: {script.title}")
    print(f"预估时长: {script.estimated_duration:.0f}s")
    print(f"操作数: {len(script.operations)}")
    for op in script.operations:
        print(f"  #{op.order} {op.action}: {op.note or op.title_text or ''}")
