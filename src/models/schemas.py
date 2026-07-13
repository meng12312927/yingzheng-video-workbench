"""
===========================================================================
schemas.py — 数据模型定义（Agent 之间的"通信协议"）
===========================================================================
功能：定义系统中所有数据结构，保证 Agent 间传递的信息格式统一

技术原理（大白话版）：
  4 个 Agent 之间需要传递信息。比如 Agent 1 要告诉 Agent 2
  "用户想要一个 3 分钟的运动风格视频"。怎么保证 Agent 2 收到
  的信息格式正确？用 Pydantic！

  Pydantic 就像一个"数据质检员"：
  - 你定义好数据长什么样（字段名、类型）
  - Pydantic 自动检查传入的数据是否符合要求
  - 不符合就报错，防止错误数据传染到后续步骤

Python 知识点：
  1. BaseModel — Pydantic 的核心，所有数据模型都继承它
  2. Field(description=...) — 字段描述，既是文档也是给 LLM 的提示
  3. Optional[str] — 可选字段，可以不给值（默认为 None）
  4. from __future__ import annotations — 让类型提示更好用

面试会问：
  为什么不用 dataclass 而用 Pydantic？
  → Pydantic 支持自动校验、JSON序列化、嵌套模型，
    还能把 schema 转成 JSON Schema 给 LLM 的 function calling 用。
===========================================================================
"""

from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Optional


# ============================================================
# Agent 1 → Agent 2：需求理解结果
# ============================================================

class VideoRequirement(BaseModel):
    """
    用户需求的结构化表示

    Agent 1（需求理解）解析用户的自然语言后，输出这个结构。
    例如用户说"把运动会视频剪成 3 分钟精彩集锦"，Agent 1 输出：
    VideoRequirement(
        target_duration=180,
        video_type="sports",
        style="exciting",
        focus_keywords=["冲刺", "欢呼", "颁奖"],
        need_subtitles=True,
        need_bgm=True,
    )
    """
    target_duration: int = Field(
        description="目标成品时长（秒），如 180 表示 3 分钟"
    )
    video_type: str = Field(
        description="视频类型：sports(运动会/体育) / competition(竞赛/演讲) / meeting(会议) / general(通用)"
    )
    style: str = Field(
        description="剪辑风格：exciting(燃向/快节奏) / formal(正式/庄重) / warm(温馨) / funny(搞笑)"
    )
    focus_keywords: list[str] = Field(
        default_factory=list,
        description="重点关注的关键词，如 ['冲刺', '颁奖', '欢呼']"
    )
    need_subtitles: bool = Field(
        default=True,
        description="是否需要烧录字幕"
    )
    need_bgm: bool = Field(
        default=True,
        description="是否建议添加背景音乐（实际添加需用户提供音乐文件）"
    )
    avoid_keywords: list[str] = Field(
        default_factory=list,
        description="要避免的内容关键词，如 ['广告', '停顿过长']"
    )
    output_format: str = Field(
        default="mp4",
        description="输出格式：mp4 / mov"
    )


# ============================================================
# Agent 2 内部 / Agent 2 → Agent 3：内容分析结果
# ============================================================

class TranscriptSegment(BaseModel):
    """
    单个转录片段

    Whisper 输出的一小段文字，包含起止时间。
    """
    start: float = Field(description="开始时间（秒）")
    end: float = Field(description="结束时间（秒）")
    text: str = Field(description="转录文本")
    speaker_id: int = Field(default=0, description="说话人编号（0-based）")


class HighlightClip(BaseModel):
    """
    一个"高光片段"——被认为值得放进成片的部分

    例如运动会中观众欢呼的那 15 秒、
    知识竞赛中选手答对题的那 10 秒。
    """
    start: float = Field(description="片段开始时间（秒）")
    end: float = Field(description="片段结束时间（秒）")
    text: str = Field(description="片段中的转录文本")
    importance: float = Field(
        ge=0.0, le=1.0,
        description="重要性打分（0.0-1.0），1.0 表示最重要"
    )
    category: str = Field(
        description="片段类型：highlight(高光) / info(信息) / emotion(情感) / transition(过渡)"
    )
    reason: str = Field(
        description="为什么选这个片段？如'选手冲刺瞬间+全场欢呼'"
    )
    suggestion: Optional[str] = Field(
        default=None,
        description="剪辑建议，如'建议慢放'"
    )


class ContentAnalysis(BaseModel):
    """
    Agent 2（内容分析）的完整输出

    包含完整的转录文本和筛选出的高光片段列表。
    """
    video_duration: float = Field(description="视频总时长（秒）")
    transcript: list[TranscriptSegment] = Field(
        default_factory=list,
        description="完整转录文本"
    )
    highlights: list[HighlightClip] = Field(
        default_factory=list,
        description="筛选出的高光片段"
    )
    summary: str = Field(
        default="",
        description="视频内容的一句话摘要"
    )
    keyword_timeline: dict[str, list[float]] = Field(
        default_factory=dict,
        description="关键词出现的时间点，如 {'冲刺': [120.5, 300.2], '颁奖': [500.0]}"
    )


# ============================================================
# Agent 3 → Agent 4：剪辑脚本
# ============================================================

class EditOperation(BaseModel):
    """
    一个剪辑操作

    例如："从原片的第 60 秒裁到第 75 秒，作为成片的第 1 个片段"
    """
    order: int = Field(description="操作顺序号（从 1 开始）")
    action: str = Field(
        description="操作类型：cut(裁剪) / transition(转场) / title(标题) / subtitle(字幕段)"
    )
    source_start: Optional[float] = Field(
        default=None,
        description="原片中的开始时间（秒），仅 cut 操作需要"
    )
    source_end: Optional[float] = Field(
        default=None,
        description="原片中的结束时间（秒），仅 cut 操作需要"
    )
    transition_type: Optional[str] = Field(
        default=None,
        description="转场类型：fade(淡入淡出) / dissolve(叠化) / none(硬切)"
    )
    subtitle_text: Optional[str] = Field(
        default=None,
        description="字幕文本，仅 subtitle 操作需要"
    )
    title_text: Optional[str] = Field(
        default=None,
        description="标题文本，如'精彩回顾'"
    )
    duration: Optional[float] = Field(
        default=None,
        description="该操作的持续时长（秒），转场/标题需要"
    )
    note: Optional[str] = Field(
        default=None,
        description="备注，如'这里加 BGM 渐入'"
    )


class EditScript(BaseModel):
    """
    Agent 3（脚本生成）输出的完整剪辑脚本

    这就是"施工图纸"，Agent 4 按照它来执行 FFmpeg 命令。
    """
    title: str = Field(
        default="",
        description="成片标题"
    )
    estimated_duration: float = Field(
        description="预估成品时长（秒）"
    )
    operations: list[EditOperation] = Field(
        default_factory=list,
        description="按顺序排列的剪辑操作列表"
    )
    srt_subtitles: str = Field(
        default="",
        description="SRT 格式的字幕内容（完整的文本块）"
    )
    notes: str = Field(
        default="",
        description="整体说明，如'建议配 BGM：轻快活泼'"
    )


# ============================================================
# Agent 4 → 外部：执行结果
# ============================================================

class ExecutionResult(BaseModel):
    """
    Agent 4（执行）的输出结果
    """
    success: bool = Field(description="是否成功")
    output_path: str = Field(description="输出文件路径")
    output_duration: float = Field(description="实际成品时长（秒）")
    operations_done: int = Field(description="完成的剪辑操作数")
    operations_failed: int = Field(description="失败的剪辑操作数")
    errors: list[str] = Field(
        default_factory=list,
        description="错误信息列表"
    )
    log: str = Field(
        default="",
        description="详细的执行日志"
    )


# ============================================================
# 管道状态（Orchestrator 用）
# ============================================================

class PipelineStatus(BaseModel):
    """
    整个管道的运行状态（Orchestrator 追踪用）

    这个不是 Agent 之间传递的，而是 Orchestrator 用来
    追踪整个流程进度的。
    """
    step: str = Field(description="当前步骤：init/requirement/analysis/script/execution/done/error")
    requirement: Optional[VideoRequirement] = None
    analysis: Optional[ContentAnalysis] = None
    script: Optional[EditScript] = None
    result: Optional[ExecutionResult] = None
    error_message: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None


# ============================================================
# 测试代码
# ============================================================
if __name__ == "__main__":
    print("=" * 60)
    print("数据模型测试")
    print("=" * 60)

    # 测试 1：创建需求
    req = VideoRequirement(
        target_duration=180,
        video_type="sports",
        style="exciting",
        focus_keywords=["冲刺", "欢呼", "颁奖"],
    )
    print(f"[OK] VideoRequirement: {req.model_dump()}")

    # 测试 2：创建高光片段
    clip = HighlightClip(
        start=60.0,
        end=75.5,
        text="选手冲过终点线，全场欢呼！",
        importance=0.95,
        category="highlight",
        reason="冠军冲刺瞬间+全场欢呼",
        suggestion="建议慢放处理",
    )
    print(f"[OK] HighlightClip: score={clip.importance}, reason={clip.reason}")

    # 测试 3：创建剪辑操作
    op = EditOperation(
        order=1,
        action="cut",
        source_start=60.0,
        source_end=75.5,
        note="高光片段1",
    )
    print(f"[OK] EditOperation: #{op.order} {op.action} [{op.source_start}-{op.source_end}]")

    # 测试 4：验证功能——错误的输入会被拒绝
    try:
        bad_clip = HighlightClip(
            start=10.0,
            end=20.0,
            text="test",
            importance=99.9,  # 超出 0.0-1.0 范围！
            category="highlight",
            reason="test",
        )
        print("[FAIL] 应该抛出验证错误但没有！")
    except Exception as e:
        print(f"[OK] 正确拒绝了无效数据: {type(e).__name__}")

    print("\n所有数据模型测试通过！")
