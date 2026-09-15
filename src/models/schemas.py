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

模型选型：
  Pydantic 提供自动校验、JSON 序列化和嵌套模型支持，
  并可导出 JSON Schema，约束 LLM 结构化输出和工具调用参数。
===========================================================================
"""

from __future__ import annotations
from datetime import datetime, timezone
from typing import Any, Dict, Literal, Optional, Union
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


def new_id(prefix: str) -> str:
    """生成可读、可跨任务引用的领域对象 ID。"""
    return f"{prefix}_{uuid4().hex}"


def utc_now() -> datetime:
    """统一使用带时区的 UTC 时间，便于审计记录排序。"""
    return datetime.now(timezone.utc)


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
        need_bgm=False,
    )
    """
    target_duration: int = Field(
        ge=30,
        le=600,
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
        default=False,
        description="是否建议添加背景音乐；MVP 不会自动添加，需用户自行提供已授权音频"
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
# 映证领域层：需求、审核与审计
# ============================================================

class RequirementBrief(BaseModel):
    """用户提交的原始业务需求；原文始终保留，不被模型输出覆盖。"""

    id: str = Field(default_factory=lambda: new_id("brief"))
    scenario: Literal["school", "enterprise"] = "school"
    raw_text: str = Field(min_length=1)
    submitted_by: Optional[str] = None
    submitted_at: datetime = Field(default_factory=utc_now)


class RequirementItem(BaseModel):
    """一条可审核的业务要求，而不是仅供模型阅读的关键词。"""

    id: str = Field(default_factory=lambda: new_id("req"))
    category: str = Field(default="content")
    description: str = Field(min_length=1)
    priority: Literal["must", "should", "optional", "prohibited"] = "should"
    status: Literal["draft", "confirmed", "needs_confirmation"] = "draft"
    acceptance_rule: Optional[str] = None

    @model_validator(mode="after")
    def require_acceptance_rule_for_high_risk_items(self):
        if self.priority in {"must", "prohibited"} and not self.acceptance_rule:
            raise ValueError("must 和 prohibited 要求必须提供验收规则")
        return self


class RequirementSpec(BaseModel):
    """可版本化的任务书；已确认版本不得原地修改。"""

    id: str = Field(default_factory=lambda: new_id("requirement"))
    version: int = Field(default=1, ge=1)
    brief_id: str
    purpose: str = Field(default="活动回顾视频")
    audience: str = Field(default="活动参与者与相关负责人")
    video_type: str = "general"
    style: str = "formal"
    need_subtitles: bool = True
    need_bgm: bool = False
    target_duration: float = Field(ge=1.0, le=3600.0)
    duration_tolerance: float = Field(default=15.0, ge=0.0, le=300.0)
    requirements: list[RequirementItem] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list, max_length=5)
    status: Literal["draft", "confirmed", "superseded"] = "draft"
    created_at: datetime = Field(default_factory=utc_now)
    confirmed_at: Optional[datetime] = None

    @model_validator(mode="after")
    def confirmed_spec_cannot_have_open_questions(self):
        if self.status == "confirmed" and self.open_questions:
            raise ValueError("仍有待确认问题的任务书不能标记为 confirmed")
        return self


class ExecutionBrief(BaseModel):
    """给用户看的 AI 执行说明，不包含内部提示词、安全规则或密钥。"""

    id: str = Field(default_factory=lambda: new_id("execution"))
    requirement_spec_id: str
    requirement_spec_version: int = Field(ge=1)
    version: int = Field(default=1, ge=1)
    visible_instruction: str = Field(min_length=1)
    included_requirement_ids: list[str] = Field(default_factory=list)
    evidence_scope: list[str] = Field(default_factory=lambda: ["transcript", "user_annotation"])
    model_task: Literal["candidate_analysis", "subtitle_review", "semantic_verification"] = "candidate_analysis"
    prompt_template_version: str = "requirement-compiler-v1"
    status: Literal["draft", "confirmed", "superseded"] = "draft"
    confirmed_at: Optional[datetime] = None


class RequirementCompilation(BaseModel):
    """需求编译器产物：保留兼容执行视图，供旧剪辑流水线逐步迁移。"""

    brief: RequirementBrief
    spec: RequirementSpec
    execution_brief: ExecutionBrief
    legacy_requirement: VideoRequirement
    mode: Literal["llm_generated", "manual_required", "material_assisted"] = "material_assisted"
    warnings: list[str] = Field(default_factory=list)
    slots: list["RequirementSlot"] = Field(default_factory=list)
    alignment_proposal: Optional["MaterialAlignmentProposal"] = None


SlotStatus = Literal["missing", "inferred", "confirmed", "conflict", "unknown"]


class RequirementSlot(BaseModel):
    """多轮需求澄清的最小槽位；每个值都保留来源与确认状态。"""

    id: str = Field(default_factory=lambda: new_id("slot"))
    key: str = Field(min_length=1)
    label: str = Field(min_length=1)
    value: Any = None
    status: SlotStatus = "missing"
    risk_level: Literal["low", "medium", "high"] = "low"
    source_type: Literal["user_input", "form", "llm", "clarification", "domain_config"] = "llm"
    source_ref: Optional[str] = None
    question: Optional[str] = None
    question_reason: Optional[str] = None
    question_impact: Optional[str] = None
    answer_hint: Optional[str] = None
    question_source: Optional[str] = None
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    source_asset_ids: list[str] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=utc_now)


class ClarificationQuestion(BaseModel):
    """澄清 Agent 的受约束输出；问题必须绑定现有任务书槽位。"""

    slot_key: str = Field(min_length=1, max_length=64)
    question: str = Field(min_length=4, max_length=160)
    reason: str = Field(min_length=2, max_length=240)
    impact: str = Field(min_length=2, max_length=240)
    answer_hint: Optional[str] = Field(default=None, max_length=160)


class MaterialInterviewQuestion(BaseModel):
    """由素材证据触发的业务选择题；不要求用户理解或填写时间码。"""

    id: str = Field(default_factory=lambda: new_id("material_question"))
    decision_type: Literal["retain", "exclude", "order", "emphasis", "risk"]
    question: str = Field(min_length=4, max_length=180)
    reason: str = Field(min_length=2, max_length=300)
    impact: str = Field(min_length=2, max_length=300)
    answer_hint: Optional[str] = Field(default=None, max_length=180)
    supporting_evidence_ids: list[str] = Field(default_factory=list, min_length=1, max_length=8)
    source_asset_ids: list[str] = Field(default_factory=list, min_length=1, max_length=8)


class MaterialAlignmentProposal(BaseModel):
    """素材转录与初步任务书的对齐建议；只有用户选择后才能写入任务书。"""

    id: str = Field(default_factory=lambda: new_id("alignment"))
    requirement_spec_id: str
    requirement_spec_version: int = Field(ge=1)
    alignment_status: Literal["aligned", "too_vague", "mismatch"]
    confidence: float = Field(ge=0.0, le=1.0)
    material_summary: str = Field(min_length=1, max_length=500)
    detected_topics: list[str] = Field(default_factory=list, max_length=8)
    rationale: str = Field(min_length=1, max_length=600)
    suggested_requirement_text: str = Field(min_length=1, max_length=1500)
    suggested_purpose: str = Field(min_length=1, max_length=200)
    suggested_video_type: str = Field(default="general", max_length=50)
    suggested_style: Literal["formal", "exciting", "warm", "funny", "general"] = "general"
    suggested_target_duration: float = Field(ge=30.0, le=600.0)
    suggested_focus_items: list[str] = Field(default_factory=list, min_length=1, max_length=6)
    supporting_evidence_ids: list[str] = Field(default_factory=list, max_length=12)
    created_at: datetime = Field(default_factory=utc_now)

    @property
    def requires_user_decision(self) -> bool:
        return (
            self.alignment_status in {"too_vague", "mismatch"}
            and self.confidence >= 0.65
        )


class MaterialAlignmentDecision(BaseModel):
    """用户对素材—需求对齐建议的选择。"""

    id: str = Field(default_factory=lambda: new_id("alignment_decision"))
    task_id: str
    proposal_id: str
    action: Literal["adopt", "edit_and_adopt", "keep_original"]
    edited_requirement_text: Optional[str] = Field(default=None, max_length=1500)
    resulting_requirement_version: int = Field(ge=1)
    actor_id: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)


class ClarificationTurn(BaseModel):
    """一轮可重放的需求澄清，只更新本轮涉及的槽位。"""

    id: str = Field(default_factory=lambda: new_id("clarification"))
    task_id: str
    requirement_spec_id: str
    from_version: int = Field(ge=1)
    to_version: int = Field(ge=1)
    answers: Dict[str, str] = Field(default_factory=dict)
    changed_slot_ids: list[str] = Field(default_factory=list)
    actor_id: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)


TaskState = Literal[
    "created", "preflight_ok", "requirement_draft",
    "awaiting_requirement_clarification", "requirement_confirmed",
    "style_recommended", "style_skipped", "analyzing", "awaiting_review",
    "plan_approved", "rendering", "verifying",
    "awaiting_delivery_resolution", "succeeded", "failed",
]

TaskEvent = Literal[
    "preflight_passed", "requirement_drafted", "clarification_required",
    "clarification_applied", "requirement_confirmed", "style_recommended",
    "style_skipped", "analysis_started", "review_ready", "plan_approved",
    "review_invalidated", "render_started", "render_completed",
    "verification_passed", "delivery_resolution_required", "delivery_approved",
    "delivery_revision_requested",
    "task_failed",
]


class TaskSnapshot(BaseModel):
    """可从磁盘恢复的任务状态快照；version 用于拒绝并发覆盖。"""

    task_id: str
    state: TaskState = "created"
    version: int = Field(default=1, ge=1)
    last_event_id: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    updated_at: datetime = Field(default_factory=utc_now)


class TaskEventRecord(BaseModel):
    """幂等、带期望版本的状态事件。"""

    id: str = Field(default_factory=lambda: new_id("task_event"))
    task_id: str
    event: TaskEvent
    from_state: TaskState
    to_state: TaskState
    expected_state_version: int = Field(ge=1)
    resulting_state_version: int = Field(ge=2)
    idempotency_key: str = Field(min_length=1)
    actor_id: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class StyleAsset(BaseModel):
    """当前渲染器可稳定执行的单项视觉样式。"""

    id: str
    version: int = Field(default=1, ge=1)
    category: Literal["intro", "outro", "subtitle", "transition"]
    name: str
    description: str
    config: Dict[str, Any] = Field(default_factory=dict)
    active: bool = True


class StyleBundle(BaseModel):
    """只引用受控 StyleAsset ID 的组合方案。"""

    id: str
    version: int = Field(default=1, ge=1)
    name: str
    scenario: Literal["common", "school", "enterprise"] = "common"
    intro_style_id: str
    outro_style_id: str
    subtitle_style_id: str
    transition_style_id: str


class StyleProposal(BaseModel):
    """LLM 只能推荐已有 bundle；选择仍由用户完成。"""

    id: str = Field(default_factory=lambda: new_id("style_proposal"))
    requirement_spec_id: str
    requirement_spec_version: int = Field(ge=1)
    bundle_id: str
    rationale: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    source: Literal["llm", "deterministic_fallback"] = "llm"
    status: Literal["suggested", "selected", "rejected"] = "suggested"


class DeliverySpec(BaseModel):
    """渲染和验收共同读取的版本化交付规则。"""

    id: str = Field(default_factory=lambda: new_id("delivery_spec"))
    version: int = Field(default=1, ge=1)
    requirement_spec_id: str
    requirement_spec_version: int = Field(ge=1)
    target_duration: float = Field(gt=0.0)
    duration_tolerance: float = Field(ge=0.0)
    need_subtitles: bool = True
    subtitle_style: Literal["classic", "clean", "highlight"] = "classic"
    transition_duration: float = Field(default=0.0, ge=0.0, le=1.0)
    intro_style: Literal["none", "title", "fade_black"] = "none"
    outro_style: Literal["none", "title", "fade_black"] = "none"
    title_text: str = ""
    bgm_path: Optional[str] = None
    bgm_volume: float = Field(default=0.15, ge=0.0, le=1.0)
    style_bundle_id: Optional[str] = None
    output_format: Literal["mp4"] = "mp4"


ReviewStage = Literal[
    "media_preflight", "requirement", "style", "evidence",
    "candidate", "edit_plan", "render", "delivery",
]
ReviewStatus = Literal["pending", "approved", "changes_requested", "rejected", "superseded"]


class ReviewGate(BaseModel):
    """版本绑定的人工审核门禁；下游只能使用 approved 版本。"""

    id: str = Field(default_factory=lambda: new_id("gate"))
    task_id: str
    stage: ReviewStage
    target_type: str
    target_id: str
    target_version: int = Field(ge=1)
    status: ReviewStatus = "pending"
    actor_id: Optional[str] = None
    comment: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    resolved_at: Optional[datetime] = None


class AuditEvent(BaseModel):
    """面向业务用户的追加式审计事件；技术日志不写入这里。"""

    id: str = Field(default_factory=lambda: new_id("audit"))
    task_id: str
    action: str
    subject_type: str
    subject_id: str
    subject_version: Optional[int] = Field(default=None, ge=1)
    actor_type: Literal["system", "user"] = "system"
    actor_id: Optional[str] = None
    summary: str
    metadata: Dict[str, Optional[Union[str, int, float, bool]]] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


# ============================================================
# Agent 2 内部 / Agent 2 → Agent 3：内容分析结果
# ============================================================

SourceAnalysisState = Literal[
    "pending", "preflight_ok", "transcribing", "ready",
    "no_audio", "failed", "excluded",
]


class SourceAsset(BaseModel):
    """任务中的一段原始素材；分析事实始终使用该文件的本地时间。"""

    id: str = Field(default_factory=lambda: new_id("asset"))
    order: int = Field(ge=1)
    filename: str = Field(min_length=1)
    source_path: str = Field(min_length=1)
    content_hash: str = Field(min_length=1)
    duration: float = Field(gt=0.0)
    width: int = Field(default=0, ge=0)
    height: int = Field(default=0, ge=0)
    fps: Optional[float] = Field(default=None, gt=0.0)
    codec: str = ""
    size_mb: Optional[float] = Field(default=None, ge=0.0)
    has_audio: bool = False
    analysis_state: SourceAnalysisState = "pending"
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)


class TranscriptSegment(BaseModel):
    """
    单个转录片段

    Whisper 输出的一小段文字，包含起止时间。
    """
    id: str = Field(default_factory=lambda: new_id("transcript"))
    source_asset_id: Optional[str] = Field(
        default=None,
        description="所属原素材 ID；旧单素材任务可以为空",
    )
    start: float = Field(description="开始时间（秒）")
    end: float = Field(description="结束时间（秒）")
    text: str = Field(description="转录文本")
    speaker_id: int = Field(default=0, description="说话人编号（0-based）")


class Evidence(BaseModel):
    """可供模型引用、并由代码验证的不可变证据片段。"""

    id: str = Field(default_factory=lambda: new_id("evidence"))
    source_asset_id: Optional[str] = None
    type: Literal["transcript", "ocr", "scene", "audio", "user_annotation"] = "transcript"
    source_start: float = Field(ge=0.0)
    source_end: float = Field(gt=0.0)
    content: str = Field(min_length=1)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    segment_ids: list[str] = Field(default_factory=list)
    content_hash: str = ""
    metadata: Dict[str, Optional[Union[str, int, float, bool]]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def end_must_follow_start(self):
        if self.source_end <= self.source_start:
            raise ValueError("Evidence 的 source_end 必须大于 source_start")
        return self


class EvidenceCitation(BaseModel):
    """候选片段对一项需求的可核验引用。"""

    requirement_id: str = Field(min_length=1)
    evidence_id: str = Field(min_length=1)
    quote: str = Field(min_length=1)
    relation: Literal["direct", "supporting"] = "direct"
    retrieval_score: float = Field(default=0.0, ge=0.0, le=1.0)


class CandidateClip(BaseModel):
    """证据化候选；模型建议不会因缺少有效引用进入自动选段。"""

    id: str = Field(default_factory=lambda: new_id("candidate"))
    source_asset_id: Optional[str] = None
    source_start: float = Field(ge=0.0)
    source_end: float = Field(gt=0.0)
    matched_requirement_ids: list[str] = Field(default_factory=list)
    citations: list[EvidenceCitation] = Field(default_factory=list)
    selection_reason: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    suggested_duration: float = Field(gt=0.0)
    risk_flags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def candidate_end_must_follow_start(self):
        if self.source_end <= self.source_start:
            raise ValueError("CandidateClip 的 source_end 必须大于 source_start")
        return self


class RequirementDurationBudget(BaseModel):
    """一项任务书要求在候选阶段应获得的确定性时长预算。"""

    requirement_id: str
    priority: Literal["must", "should", "optional"]
    target_seconds: float = Field(ge=0.0)
    minimum_seconds: float = Field(ge=0.0)
    desired_candidate_count: int = Field(ge=1, le=8)


class DurationBudgetPlan(BaseModel):
    """分析前分配、分析后回填的时长预算与缺口。"""

    target_duration: float = Field(gt=0.0)
    duration_tolerance: float = Field(ge=0.0)
    required_minimum: float = Field(ge=0.0)
    content_budget: float = Field(ge=0.0)
    atmosphere_budget: float = Field(ge=0.0)
    requirement_budgets: list[RequirementDurationBudget] = Field(default_factory=list)
    planned_candidate_duration: float = Field(default=0.0, ge=0.0)
    shortage_seconds: float = Field(default=0.0, ge=0.0)
    status: Literal["planned", "sufficient", "short"] = "planned"
    recommendations: list[str] = Field(default_factory=list)


class HighlightClip(BaseModel):
    """
    一个"高光片段"——被认为值得放进成片的部分

    例如运动会中观众欢呼的那 15 秒、
    知识竞赛中选手答对题的那 10 秒。
    """
    start: float = Field(description="片段开始时间（秒）")
    end: float = Field(description="片段结束时间（秒）")
    source_asset_id: Optional[str] = None
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
    candidate_id: Optional[str] = None
    matched_requirement_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class ContentAnalysis(BaseModel):
    """
    Agent 2（内容分析）的完整输出

    包含完整的转录文本和筛选出的高光片段列表。
    """
    video_duration: float = Field(description="视频总时长（秒）")
    source_durations: dict[str, float] = Field(
        default_factory=dict,
        description="各原素材的本地时长；多素材任务不能用总时长校验局部时间",
    )
    transcript: list[TranscriptSegment] = Field(
        default_factory=list,
        description="完整转录文本"
    )
    evidence: list[Evidence] = Field(
        default_factory=list,
        description="由转录或人工标注生成、可供后续候选引用的证据",
    )
    candidate_clips: list[CandidateClip] = Field(
        default_factory=list,
        description="证据校验后的候选；迁移期 highlights 仍保留给旧渲染器",
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


class SourceAnalysisResult(BaseModel):
    """一段原素材的独立分析结果；失败不会污染其他素材。"""

    source: SourceAsset
    analysis: Optional[ContentAnalysis] = None
    status: SourceAnalysisState = "pending"
    warnings: list[str] = Field(default_factory=list)
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    attempt_count: int = Field(default=0, ge=0)
    last_retry_reason: Optional[str] = None
    completed_at: Optional[datetime] = None


class TaskMaterialSet(BaseModel):
    """一个任务内的全部独立素材及聚合分析状态。"""

    schema_version: int = Field(default=3, ge=3)
    task_id: str
    sources: list[SourceAsset] = Field(default_factory=list, min_length=1)
    results: list[SourceAnalysisResult] = Field(default_factory=list)
    status: Literal["registered", "analyzing", "ready", "partial", "failed"] = "registered"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


# ============================================================
# Agent 3 → Agent 4：剪辑脚本
# ============================================================

class EditOperation(BaseModel):
    """
    一个剪辑操作

    例如："从原片的第 60 秒裁到第 75 秒，作为成片的第 1 个片段"
    """
    order: int = Field(description="操作顺序号（从 1 开始）")
    source_asset_id: Optional[str] = Field(
        default=None,
        description="cut 操作引用的原素材 ID；旧单素材脚本可以为空",
    )
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
    subtitle_style: str = Field(
        default="classic",
        description="字幕样式：classic / clean / highlight"
    )
    transition_duration: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="相邻片段淡转场时长（秒）；0 表示硬切"
    )
    bgm_path: Optional[str] = Field(
        default=None,
        description="用户提供的背景音乐文件路径；为空则不添加 BGM"
    )
    bgm_volume: float = Field(
        default=0.15,
        ge=0.0,
        le=1.0,
        description="背景音乐相对音量"
    )
    intro_style: str = Field(
        default="none",
        description="片头样式：none / title / fade_black"
    )
    outro_style: str = Field(
        default="none",
        description="片尾样式：none / title / fade_black"
    )
    title_text: str = Field(
        default="",
        description="片头主标题；片尾默认显示感谢观看"
    )
    notes: str = Field(
        default="",
        description="整体说明，如'建议配 BGM：轻快活泼'"
    )


class TimelineSegment(BaseModel):
    """审核视图中的逐段时间线，可追溯到候选、需求和证据。"""

    id: str = Field(default_factory=lambda: new_id("timeline"))
    order: int = Field(ge=1)
    candidate_id: str
    source_asset_id: Optional[str] = None
    source_start: float = Field(ge=0.0)
    source_end: float = Field(gt=0.0)
    output_start: float = Field(ge=0.0)
    output_end: float = Field(gt=0.0)
    matched_requirement_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    title_text: Optional[str] = None
    subtitle_text: Optional[str] = None
    transition_style: Literal["cut", "fade"] = "cut"
    transition_duration: float = Field(default=0.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_timeline_ranges(self):
        if self.source_end <= self.source_start:
            raise ValueError("TimelineSegment 源时间范围非法")
        if self.output_end <= self.output_start:
            raise ValueError("TimelineSegment 成片时间范围非法")
        return self


class PlanApprovalException(BaseModel):
    """用户在生成前明确接受的计划级例外；只覆盖声明的校验项。"""

    code: Literal["plan_duration_too_short"]
    reason: str = Field(min_length=1, max_length=500)
    planned_duration: float = Field(ge=0.0)
    required_minimum: float = Field(ge=0.0)
    target_duration: float = Field(ge=0.0)
    approved_by: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)


class AuditableEditPlan(BaseModel):
    """用户审核的事实来源；EditScript 只是批准计划的执行视图。"""

    id: str = Field(default_factory=lambda: new_id("edit_plan"))
    version: int = Field(default=1, ge=1)
    requirement_spec_id: str
    requirement_spec_version: int = Field(ge=1)
    delivery_spec: DeliverySpec
    timeline_segments: list[TimelineSegment] = Field(default_factory=list)
    candidate_ids: list[str] = Field(default_factory=list)
    execution_script: EditScript
    estimated_duration: float = Field(ge=0.0)
    approval_exceptions: list[PlanApprovalException] = Field(default_factory=list)
    status: Literal["draft", "approved", "superseded"] = "draft"
    created_at: datetime = Field(default_factory=utc_now)
    approved_at: Optional[datetime] = None


class CanonicalTimelineSource(BaseModel):
    """编辑器无关时间线引用的一段原素材。"""

    source_asset_id: str
    filename: str
    source_path: str
    content_hash: str = ""
    duration: float = Field(gt=0.0)
    fps: float = Field(default=30.0, gt=0.0)


class CanonicalTimelineClip(BaseModel):
    """所有 Adapter 共享的最小剪辑事实。"""

    id: str = Field(default_factory=lambda: new_id("canonical_clip"))
    order: int = Field(ge=1)
    source_asset_id: str
    source_in: float = Field(ge=0.0)
    source_out: float = Field(gt=0.0)
    timeline_in: float = Field(ge=0.0)
    timeline_out: float = Field(gt=0.0)
    candidate_id: str
    matched_requirement_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    subtitle_text: Optional[str] = None
    title_text: Optional[str] = None
    transition_style: Literal["cut", "fade"] = "cut"
    transition_duration: float = Field(default=0.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_ranges(self):
        if self.source_out <= self.source_in:
            raise ValueError("CanonicalTimelineClip 源范围非法")
        if self.timeline_out <= self.timeline_in:
            raise ValueError("CanonicalTimelineClip 成片范围非法")
        return self


class CanonicalTimeline(BaseModel):
    """审核计划编译出的编辑器无关事实来源。"""

    schema_version: int = Field(default=1, ge=1)
    id: str = Field(default_factory=lambda: new_id("canonical_timeline"))
    version: int = Field(default=1, ge=1)
    task_id: str
    requirement_spec_id: str
    requirement_spec_version: int = Field(ge=1)
    approved_plan_id: str
    approved_plan_version: int = Field(ge=1)
    title: str = ""
    sources: list[CanonicalTimelineSource] = Field(default_factory=list, min_length=1)
    clips: list[CanonicalTimelineClip] = Field(default_factory=list)
    subtitles_srt: str = ""
    estimated_duration: float = Field(ge=0.0)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class EditorExportResult(BaseModel):
    """一个 Adapter 的独立导出结果；失败不改变已批准计划。"""

    adapter: str
    timeline_id: str
    timeline_version: int = Field(ge=1)
    success: bool
    output_path: str
    artifacts: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


ReferenceDocumentCategory = Literal[
    "agenda", "host_script", "people", "awards", "products",
    "organizations", "terminology", "other",
]


class ReferenceDocument(BaseModel):
    """用户上传的活动资料；原文件与解析证据分别版本化。"""

    id: str = Field(default_factory=lambda: new_id("reference_document"))
    task_id: str
    version: int = Field(default=1, ge=1)
    category: ReferenceDocumentCategory
    filename: str
    source_path: str
    content_hash: str
    created_at: datetime = Field(default_factory=utc_now)


class ReferenceDocumentEvidence(BaseModel):
    """活动资料中的可核对文字及其行、单元格或 JSON 路径。"""

    id: str = Field(default_factory=lambda: new_id("document_evidence"))
    document_id: str
    document_version: int = Field(ge=1)
    category: ReferenceDocumentCategory
    location: str
    content: str = Field(min_length=1, max_length=1000)
    content_hash: str


class ReferenceLibrary(BaseModel):
    """一个任务的辅助资料索引。"""

    schema_version: int = Field(default=1, ge=1)
    task_id: str
    version: int = Field(default=1, ge=1)
    documents: list[ReferenceDocument] = Field(default_factory=list)
    evidence: list[ReferenceDocumentEvidence] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=utc_now)


class EntityReviewItem(BaseModel):
    """ASR 与权威资料之间的专有名词候选；永远等待用户确认。"""

    id: str = Field(default_factory=lambda: new_id("entity_review"))
    entity_type: Literal["person", "title", "award", "product", "organization", "term"]
    source_asset_id: Optional[str] = None
    transcript_segment_id: str
    source_start: float = Field(ge=0.0)
    source_end: float = Field(gt=0.0)
    observed_text: str = Field(min_length=1)
    suggested_text: str = Field(min_length=1)
    reference_evidence_ids: list[str] = Field(default_factory=list, min_length=1)
    similarity: float = Field(ge=0.0, le=1.0)
    status: Literal["pending", "confirmed", "rejected", "edited"] = "pending"
    confirmed_value: Optional[str] = None
    actor_id: Optional[str] = None
    resolved_at: Optional[datetime] = None


class EntityReviewQueue(BaseModel):
    task_id: str
    version: int = Field(default=1, ge=1)
    items: list[EntityReviewItem] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=utc_now)


OrganizationMemoryKind = Literal[
    "brand_rule", "proper_noun", "publishing_restriction", "template_rule",
]


class OrganizationMemoryItem(BaseModel):
    """由组织明确维护的规则；每项都能查看来源、停用或删除。"""

    id: str = Field(default_factory=lambda: new_id("organization_memory"))
    kind: OrganizationMemoryKind
    label: str = Field(min_length=1, max_length=120)
    value: str = Field(min_length=1, max_length=1000)
    aliases: list[str] = Field(default_factory=list)
    source: str = Field(min_length=1, max_length=500)
    status: Literal["active", "deleted"] = "active"
    created_at: datetime = Field(default_factory=utc_now)
    deleted_at: Optional[datetime] = None


class OrganizationProfile(BaseModel):
    """学校或企业的版本化品牌、术语、发布限制和模板配置。"""

    schema_version: int = Field(default=1, ge=1)
    id: str = Field(default_factory=lambda: new_id("organization"))
    version: int = Field(default=1, ge=1)
    name: str = Field(min_length=1, max_length=120)
    scenario: Literal["school", "enterprise"]
    items: list[OrganizationMemoryItem] = Field(default_factory=list)
    status: Literal["active", "deleted"] = "active"
    created_by: Optional[str] = Field(default=None, max_length=120)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    deleted_at: Optional[datetime] = None


class MobileReviewTokenRecord(BaseModel):
    """移动审核令牌只保存哈希，可过期和撤销。"""

    id: str = Field(default_factory=lambda: new_id("mobile_token"))
    task_id: str
    plan_id: str
    plan_version: int = Field(ge=1)
    token_hash: str
    status: Literal["active", "revoked", "used", "expired"] = "active"
    expires_at: datetime
    created_by: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)


class MobileReviewLink(BaseModel):
    token_record_id: str
    task_id: str
    plan_id: str
    plan_version: int = Field(ge=1)
    url: str
    expires_at: datetime
    qr_code_path: Optional[str] = None


class MobileReviewSubmission(BaseModel):
    id: str = Field(default_factory=lambda: new_id("mobile_submission"))
    token_record_id: str
    task_id: str
    plan_id: str
    plan_version: int = Field(ge=1)
    decision: Literal["approve", "changes_requested"]
    comment: str = Field(default="", max_length=1000)
    reviewer_name: Optional[str] = Field(default=None, max_length=100)
    idempotency_key: str = Field(min_length=1, max_length=200)
    created_at: datetime = Field(default_factory=utc_now)


class CandidateDecision(BaseModel):
    """用户对候选或文字的显式决定，用于重放审核结果和计算修改率。"""

    id: str = Field(default_factory=lambda: new_id("decision"))
    task_id: str
    plan_id: str
    plan_version: int = Field(ge=1)
    candidate_id: str
    action: Literal[
        "keep", "delete", "trim", "replace", "reorder",
        "subtitle_edit", "title_edit", "manual_add", "auto_add",
    ]
    before: Dict[str, Any] = Field(default_factory=dict)
    after: Dict[str, Any] = Field(default_factory=dict)
    actor_id: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)


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


class VerificationResult(BaseModel):
    """单条任务要求的验收结论；确定性失败不能被语义建议覆盖。"""

    requirement_id: str
    status: Literal["passed", "failed", "warning", "manual_review"]
    method: Literal["deterministic", "semantic", "human"]
    summary: str
    evidence_ids: list[str] = Field(default_factory=list)
    code: Optional[str] = None


class DeliveryReport(BaseModel):
    """绑定当前需求、计划和输出版本的逐项交付报告。"""

    id: str = Field(default_factory=lambda: new_id("delivery_report"))
    task_id: str
    output_path: str
    requirement_spec_id: str
    requirement_spec_version: int = Field(ge=1)
    edit_plan_id: str
    edit_plan_version: int = Field(ge=1)
    results: list[VerificationResult] = Field(default_factory=list)
    status: Literal["passed", "needs_resolution", "approved_with_exceptions"]
    approved_by: Optional[str] = None
    exception_reason: Optional[str] = None
    generated_at: datetime = Field(default_factory=utc_now)


class ModelCallRecord(BaseModel):
    """不含隐藏推理和密钥的模型/工具可观测记录。"""

    id: str = Field(default_factory=lambda: new_id("model_call"))
    task_id: Optional[str] = None
    trace_id: str = Field(default_factory=lambda: new_id("trace"))
    stage: str
    provider: str
    model: str
    prompt_template_version: str
    input_spec_id: Optional[str] = None
    input_spec_version: Optional[int] = Field(default=None, ge=1)
    duration_ms: int = Field(ge=0)
    input_tokens: Optional[int] = Field(default=None, ge=0)
    output_tokens: Optional[int] = Field(default=None, ge=0)
    tool_names: list[str] = Field(default_factory=list)
    error_code: Optional[str] = None
    fallback_used: bool = False
    created_at: datetime = Field(default_factory=utc_now)


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
    requirement_spec: Optional[RequirementSpec] = None
    execution_brief: Optional[ExecutionBrief] = None
    requirement_gate: Optional[ReviewGate] = None
    task_snapshot: Optional[TaskSnapshot] = None
    style_proposals: list[StyleProposal] = Field(default_factory=list)
    material_set: Optional[TaskMaterialSet] = None
    analysis: Optional[ContentAnalysis] = None
    script: Optional[EditScript] = None
    edit_plan: Optional[AuditableEditPlan] = None
    plan_gate: Optional[ReviewGate] = None
    result: Optional[ExecutionResult] = None
    delivery_report: Optional[DeliveryReport] = None
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
