"""
===========================================================================
orchestrator.py — Agent 编排器
===========================================================================
功能：串联 4 个 Agent，管理整个视频剪辑流水线的执行

技术原理（大白话版）：
  如果把 4 个 Agent 比作工厂流水线的 4 个工位：
  - Orchestrator 就是"生产调度员"——它不亲自干活，
    但决定了"谁在什么时候做什么，做完交给谁"。

  编排模式 vs 对话模式：
  - 对话模式：Agent A 直接跟 Agent B 聊天 → 混乱、难调试
  - 编排模式：所有 Agent 只跟 Orchestrator 交互 → 清晰、可控
    这是多 Agent 系统设计的"最佳实践"。

Python 知识点：
  1. 鸭子类型（Duck Typing）——不关心你是哪个类，只关心你有没有 run() 方法
  2. PipelineStatus ——用数据模型追踪状态，而不是用一堆变量
  3. 异常处理的"兜底"策略——每一步都可能失败，都要有应对
===========================================================================
"""

from __future__ import annotations

import time
import logging
import json
import re
import uuid
import hashlib
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.agents.requirement_agent import RequirementAgent
from src.agents.clarification_agent import RequirementClarificationAgent
from src.agents.alignment_agent import RequirementAlignmentAgent
from src.agents.material_interview_agent import MaterialInterviewAgent
from src.agents.analysis_agent import AnalysisAgent
from src.agents.candidate_agent import CandidateAgent
from src.agents.style_agent import StyleRecommendationAgent
from src.agents.script_agent import ScriptAgent
from src.agents.executor_agent import ExecutorAgent
from src.config import DATABASE_URL, OUTPUT_DIR, REDIS_URL, WHISPER_MODEL_SIZE
from src.tools.ffmpeg import FFmpegTool
from src.tools.llm import model_call_context
from src.models.schemas import (
    VideoRequirement,
    AuditableEditPlan,
    AuditEvent,
    ContentAnalysis,
    EditScript,
    Evidence,
    ExecutionBrief,
    ExecutionResult,
    EditorExportResult,
    PipelineStatus,
    RequirementBrief,
    RequirementCompilation,
    RequirementItem,
    RequirementSlot,
    RequirementSpec,
    HighlightClip,
    DeliveryReport,
    MaterialAlignmentDecision,
    MaterialAlignmentProposal,
    MobileReviewLink,
    MobileReviewSubmission,
    PlanApprovalException,
    EntityReviewQueue,
    ReferenceDocumentCategory,
    ReferenceLibrary,
    SourceAsset,
    TaskMaterialSet,
)
from src.services.media_assets import MediaAssetService
from src.services.evidence import BM25EvidenceRetriever
from src.services.mobile_review import MobileReviewService
from src.services.infrastructure import (
    PostgresTaskMetadataRepository,
    build_idempotency_backend,
)
from src.services.editor_adapters import JianyingHandoffAdapter, OTIOAdapter
from src.services.plans import EditPlanService
from src.services.rendering import FFmpegRenderBackend
from src.services.requirements import RequirementClarificationService
from src.services.reference_documents import ReferenceDocumentService
from src.services.state_machine import TaskStateMachine
from src.services.task_store import TaskStore
from src.services.timeline_ir import TimelineCompiler
from src.services.verification import VerificationEngine

logger = logging.getLogger("Orchestrator")


class VideoEditOrchestrator:
    """
    视频剪辑编排器——整个系统的"总指挥"

    用法（最简单的端到端流程）：
        orchestrator = VideoEditOrchestrator()
        result = orchestrator.run(
            video_path="data/my_video.mp4",
            user_input="帮我把运动会视频剪成3分钟精彩集锦",
        )
        print(f"成品: {result.output_path}")
    """

    def __init__(self):
        """初始化编排器和 4 个 Agent"""
        logger.info("=" * 50)
        logger.info("初始化 VideoEditOrchestrator")
        logger.info("=" * 50)

        # 创建领域组件；内容分析与候选判断明确分离。
        self.agent1 = RequirementAgent()
        self.clarification_agent = RequirementClarificationAgent()
        self.alignment_agent = RequirementAlignmentAgent()
        self.material_interview_agent = MaterialInterviewAgent()
        self.agent2 = AnalysisAgent(whisper_model_size=WHISPER_MODEL_SIZE)
        self.candidate_agent = CandidateAgent()
        # 两个阶段复用同一份素材向量矩阵，避免同一批转录重复请求 Embedding。
        self.candidate_agent.retriever = self.material_interview_agent.retriever
        self.style_agent = StyleRecommendationAgent()
        self.agent3 = ScriptAgent()
        self.agent4 = ExecutorAgent()
        self.plan_service = EditPlanService()
        self.media_asset_service = MediaAssetService(
            cache_root=OUTPUT_DIR / "cache" / "source_analysis",
            cache_namespace=WHISPER_MODEL_SIZE,
        )
        self.timeline_compiler = TimelineCompiler()
        self.reference_document_service = ReferenceDocumentService()
        self.mobile_review_service = MobileReviewService()
        self.clarification_service = RequirementClarificationService()
        self.verification_engine = VerificationEngine()
        self.render_backend = FFmpegRenderBackend(self.agent4)
        self.metadata_repository = (
            PostgresTaskMetadataRepository(DATABASE_URL) if DATABASE_URL else None
        )
        self.idempotency_backend = None

        self.status = PipelineStatus(step="init")
        self.task_dir: Path | None = None
        self.store: TaskStore | None = None
        self.state_machine: TaskStateMachine | None = None
        self.preview_paths: dict[str, str] = {}
        self.video_path: str | None = None
        self._render_lock = threading.Lock()
        logger.info("需求、转录、证据候选、规划与执行组件初始化完成")

    @classmethod
    def open_task(
        cls,
        task_id: str,
        runtime: Optional["VideoEditOrchestrator"] = None,
    ) -> "VideoEditOrchestrator":
        """从版本化磁盘产物恢复任务；不依赖 UI 进程内全局状态。"""
        tasks_root = (OUTPUT_DIR / "tasks").resolve()
        task_dir = (tasks_root / task_id).resolve()
        if task_dir.parent != tasks_root or not task_dir.is_dir():
            raise ValueError("任务不存在或 task_id 非法")
        if runtime is None:
            orchestrator = cls()
        else:
            orchestrator = cls.__new__(cls)
            for name in (
                "agent1", "clarification_agent", "alignment_agent", "material_interview_agent", "agent2", "candidate_agent", "style_agent", "agent3", "agent4",
                "plan_service", "media_asset_service", "timeline_compiler", "reference_document_service", "mobile_review_service", "clarification_service", "verification_engine", "render_backend", "metadata_repository",
            ):
                value = getattr(runtime, name, None)
                if name == "clarification_agent" and value is None:
                    value = RequirementClarificationAgent()
                if name == "alignment_agent" and value is None:
                    value = RequirementAlignmentAgent()
                if name == "material_interview_agent" and value is None:
                    value = MaterialInterviewAgent()
                if name == "media_asset_service" and value is None:
                    value = MediaAssetService()
                if name == "timeline_compiler" and value is None:
                    value = TimelineCompiler()
                if name == "reference_document_service" and value is None:
                    value = ReferenceDocumentService()
                if name == "mobile_review_service" and value is None:
                    value = MobileReviewService()
                setattr(orchestrator, name, value)
        orchestrator.task_dir = task_dir
        orchestrator.store = TaskStore(task_dir)
        orchestrator.state_machine = TaskStateMachine(task_dir)
        snapshot = orchestrator.state_machine.load()
        orchestrator.status = PipelineStatus(step=snapshot.state, task_snapshot=snapshot)
        orchestrator.preview_paths = {
            path.stem.split("_")[-1].lstrip("0") or "0": str(path.resolve())
            for path in (task_dir / "previews").glob("clip_*.mp4")
        } if (task_dir / "previews").exists() else {}
        manifest_path = task_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
        orchestrator.video_path = manifest.get("video_path")
        if (task_dir / "material_set.json").exists():
            orchestrator.status.material_set = orchestrator.store.read_model(
                "material_set.json", TaskMaterialSet
            )
        orchestrator._render_lock = threading.Lock()
        orchestrator.idempotency_backend = build_idempotency_backend(
            task_dir, REDIS_URL
        )

        def latest(prefix: str, suffix: str = ".json") -> Optional[Path]:
            matches = list(task_dir.glob(f"{prefix}_v*{suffix}"))
            if not matches:
                return None
            def version(path: Path) -> int:
                match = re.search(r"_v(\d+)\.json$", path.name)
                return int(match.group(1)) if match else -1
            return max(matches, key=version)

        requirement_path = latest("requirement_spec")
        execution_path = latest("execution_brief")
        legacy_path = latest("legacy_requirement")
        plan_path = latest("edit_plan")
        if requirement_path:
            orchestrator.status.requirement_spec = orchestrator.store.read_model(
                requirement_path.name, RequirementSpec
            )
        if execution_path:
            orchestrator.status.execution_brief = orchestrator.store.read_model(
                execution_path.name, ExecutionBrief
            )
        if legacy_path:
            orchestrator.status.requirement = orchestrator.store.read_model(
                legacy_path.name, VideoRequirement
            )
        gates = orchestrator.store.load_gates()
        orchestrator.status.requirement_gate = next(
            (gate for gate in reversed(gates) if gate.stage == "requirement"), None
        )
        if (task_dir / "analysis.json").exists():
            orchestrator.status.analysis = orchestrator.store.read_model(
                "analysis.json", ContentAnalysis
            )
        if plan_path:
            orchestrator.status.edit_plan = orchestrator.store.read_model(
                plan_path.name, AuditableEditPlan
            )
            orchestrator.status.script = orchestrator.status.edit_plan.execution_script
            orchestrator.status.plan_gate = next(
                (
                    gate for gate in reversed(gates)
                    if gate.stage == "edit_plan"
                    and gate.target_id == orchestrator.status.edit_plan.id
                    and gate.target_version == orchestrator.status.edit_plan.version
                ),
                None,
            )
        style_path = latest("style_proposals")
        if style_path:
            from src.models.schemas import StyleProposal
            payload = json.loads(style_path.read_text(encoding="utf-8"))
            orchestrator.status.style_proposals = [
                StyleProposal.model_validate(item) for item in payload.get("proposals", [])
            ]
        if (task_dir / "execution_result.json").exists():
            orchestrator.status.result = orchestrator.store.read_model(
                "execution_result.json", ExecutionResult
            )
        if (
            snapshot.state in {"awaiting_delivery_resolution", "succeeded"}
            and (task_dir / "delivery_report.json").exists()
        ):
            orchestrator.status.delivery_report = orchestrator.store.read_model(
                "delivery_report.json", DeliveryReport
            )
        return orchestrator

    def run(
        self,
        video_path: str,
        user_input: str,
        output_path: str = "",
    ) -> ExecutionResult:
        """
        运行完整的视频剪辑流水线

        参数：
          video_path: 原视频路径
          user_input: 用户的自然语言需求
          output_path: 输出路径（可选）

        返回值：
          ExecutionResult: 最终执行结果

        流程：
          Agent 1 (需求理解) → Agent 2 (内容分析) →
          Agent 3 (脚本生成) → Agent 4 (执行处理)
        """
        print("\n" + "=" * 60)
        print("  多 Agent 视频自动剪辑系统")
        print("=" * 60)
        print(f"  视频: {video_path}")
        print(f"  需求: {user_input[:50]}...")
        print("=" * 60 + "\n")

        message = "全自动 run() 已停用：请先创建并确认需求任务书，再分析、审核候选和导出。"
        logger.warning(message)
        return self._error_result(message)

    def prepare(
        self,
        video_path: str,
        user_input: str,
        overrides: Optional[dict] = None,
        generate_previews: bool = False,
    ) -> EditScript:
        """已停用的旧接口，防止调用方绕过需求批准门禁。"""
        raise RuntimeError(
            "prepare() 已停用；请使用 create_requirement_draft()、"
            "confirm_requirement_draft() 和 analyze_confirmed_requirement()"
        )

    def _generate_clarification_questions(
        self,
        compilation: RequirementCompilation,
    ) -> RequirementCompilation:
        """让澄清 Agent 只提问，不允许其直接修改任务书值。"""
        clarification_agent = getattr(self, "clarification_agent", None)
        if clarification_agent is None:
            return compilation
        version = compilation.spec.version
        try:
            with model_call_context(
                self.task_dir,
                stage="requirement_clarification",
                prompt_template_version="clarification-question-v1",
                input_spec_id=compilation.spec.id,
                input_spec_version=version,
            ):
                questions = clarification_agent.run(
                    raw_text=compilation.brief.raw_text,
                    spec=compilation.spec,
                    slots=compilation.slots,
                    scenario=compilation.brief.scenario,
                )
            revised_spec, revised_slots = clarification_agent.apply_questions(
                compilation.spec,
                compilation.slots,
                questions,
            )
            self._write_json(
                f"clarification_questions_v{version}.json",
                {
                    "source": "ai",
                    "prompt_template_version": "clarification-question-v1",
                    "questions": [item.model_dump(mode="json") for item in questions],
                },
            )
            return compilation.model_copy(update={
                "spec": revised_spec,
                "slots": revised_slots,
            })
        except Exception as error:
            self._write_json(
                f"clarification_questions_v{version}.json",
                {
                    "source": "ai_failed",
                    "prompt_template_version": "clarification-question-v1",
                    "questions": [],
                    "error_type": type(error).__name__,
                },
            )
            return compilation.model_copy(update={
                "warnings": [
                    *compilation.warnings,
                    "AI 未能完成澄清问题分析；系统没有用固定问题冒充 AI 结果，请直接核对并修改任务书。",
                ]
            })

    def _analyze_material_alignment(
        self,
        analysis: ContentAnalysis,
        compilation: RequirementCompilation,
    ) -> RequirementCompilation:
        """使用已独立转录的聚合只读视图生成需求对齐建议。"""
        alignment_agent = getattr(self, "alignment_agent", None)
        if alignment_agent is None or not self.store:
            return compilation
        try:
            if not any(item.type == "transcript" for item in analysis.evidence):
                return compilation.model_copy(update={
                    "warnings": [
                        *compilation.warnings,
                        "素材中没有识别出可用于需求对齐的语音证据，请直接核对任务书。",
                    ]
                })
            with model_call_context(
                self.task_dir,
                stage="requirement_alignment",
                prompt_template_version="material-alignment-v1",
                input_spec_id=compilation.spec.id,
                input_spec_version=compilation.spec.version,
            ):
                proposal = alignment_agent.run(
                    raw_text=compilation.brief.raw_text,
                    spec=compilation.spec,
                    analysis=analysis,
                    scenario=compilation.brief.scenario,
                )
            self.store.write_model("material_alignment_proposal.json", proposal)
            return compilation.model_copy(update={"alignment_proposal": proposal})
        except Exception as error:
            self._write_json(
                "material_alignment_failure.json",
                {"error_type": type(error).__name__, "stage": "material_alignment"},
            )
            return compilation.model_copy(update={
                "warnings": [
                    *compilation.warnings,
                    "素材理解与需求对齐本次未完成；你仍可直接审核任务书，候选阶段会继续使用已成功保存的转录。",
                ]
            })

    def _generate_material_interview_questions(
        self,
        compilation: RequirementCompilation,
        analysis: ContentAnalysis,
    ) -> RequirementCompilation:
        """把素材证据驱动的问题转换为 UI 已支持的逐题确认槽位。"""
        interview_agent = getattr(self, "material_interview_agent", None)
        if interview_agent is None or not self.store:
            return compilation
        pending_count = sum(bool(slot.question) for slot in compilation.slots)
        remaining = max(0, 5 - pending_count)
        if remaining == 0:
            return compilation
        labels = {
            source.id: source.filename
            for source in (self.status.material_set.sources if self.status.material_set else [])
        }
        version = compilation.spec.version
        try:
            with model_call_context(
                self.task_dir,
                stage="material_interview",
                prompt_template_version="material-interview-v2-batch",
                input_spec_id=compilation.spec.id,
                input_spec_version=version,
            ):
                questions = interview_agent.run(
                    raw_text=compilation.brief.raw_text,
                    spec=compilation.spec,
                    analysis=analysis,
                    scenario=compilation.brief.scenario,
                    source_labels=labels,
                    max_questions=remaining,
                )
            label_names = {
                "retain": "希望保留的内容",
                "exclude": "不希望出现的内容",
                "order": "内容顺序",
                "emphasis": "重点展开方式",
                "risk": "需人工核对的信息",
            }
            dynamic_slots = []
            for question in questions:
                suffix = hashlib.sha256(
                    (question.question + "|" + "|".join(question.supporting_evidence_ids)).encode("utf-8")
                ).hexdigest()[:10]
                dynamic_slots.append(
                    RequirementSlot(
                        key=f"material_{question.decision_type}_{suffix}",
                        label=label_names[question.decision_type],
                        status="missing",
                        risk_level="high" if question.decision_type == "risk" else "medium",
                        source_type="llm",
                        source_ref=f"material-interview:{question.id}",
                        question=question.question,
                        question_reason=question.reason,
                        question_impact=question.impact,
                        answer_hint=question.answer_hint,
                        question_source=f"material-interview-agent:{compilation.spec.id}:v{version}",
                        supporting_evidence_ids=question.supporting_evidence_ids,
                        source_asset_ids=question.source_asset_ids,
                    )
                )
            revised_slots = [*compilation.slots, *dynamic_slots]
            revised_spec = compilation.spec.model_copy(update={
                "open_questions": [
                    slot.question for slot in revised_slots if slot.question
                ][:5]
            })
            self._write_json(
                f"material_interview_questions_v{version}.json",
                {
                    "source": "ai_with_retrieved_evidence",
                    "prompt_template_version": "material-interview-v2-batch",
                    "questions": [item.model_dump(mode="json") for item in questions],
                },
            )
            return compilation.model_copy(
                update={"spec": revised_spec, "slots": revised_slots}
            )
        except Exception as error:
            self._write_json(
                f"material_interview_questions_v{version}.json",
                {
                    "source": "ai_failed",
                    "prompt_template_version": "material-interview-v2-batch",
                    "questions": [],
                    "error_type": type(error).__name__,
                },
            )
            return compilation.model_copy(update={
                "warnings": [
                    *compilation.warnings,
                    "AI 本次未能根据素材生成进一步问题；这不会阻止你直接审核任务书。",
                ]
            })

    def create_requirement_draft(
        self,
        video_path: str | list[str],
        user_input: str,
        scenario: str = "school",
        overrides: Optional[dict] = None,
        submitted_by: Optional[str] = None,
        progress_callback=None,
    ) -> RequirementCompilation:
        """创建任务书，并预先理解素材以生成可审核的需求对齐建议。"""
        self.status = PipelineStatus(step="created", started_at=datetime.now().isoformat())
        self.task_dir = OUTPUT_DIR / "tasks" / uuid.uuid4().hex
        self.task_dir.mkdir(parents=True, exist_ok=False)
        self.store = TaskStore(self.task_dir)
        self.idempotency_backend = build_idempotency_backend(
            self.task_dir, REDIS_URL
        )
        metadata_repository = getattr(self, "metadata_repository", None)
        if metadata_repository is not None:
            try:
                metadata_repository.initialise()
            except Exception as error:
                logger.warning(
                    "PostgreSQL 元数据仓库初始化失败，继续使用本地任务文件: %s",
                    error,
                )
        self.state_machine = TaskStateMachine(self.task_dir)
        self.status.task_snapshot = self.state_machine.initialise()
        self.preview_paths = {}

        def reject_preflight(message: str) -> None:
            self._transition(
                "task_failed",
                "preflight-failed",
                payload={
                    "error_code": "media_preflight_failed",
                    "error_message": message,
                },
            )
            self._write_manifest("failed", error=message)
            raise ValueError(message)

        raw_paths = [video_path] if isinstance(video_path, (str, Path)) else list(video_path)
        source_paths = [str(Path(path).resolve()) for path in raw_paths if str(path).strip()]
        if not source_paths:
            reject_preflight("请至少上传一段视频素材")

        media_asset_service = getattr(self, "media_asset_service", None) or MediaAssetService()
        self.media_asset_service = media_asset_service
        try:
            material_set = media_asset_service.register(self.store.task_id, source_paths)
        except ValueError as error:
            reject_preflight(str(error))
            raise
        self.status.material_set = material_set
        self.store.write_model("material_set.json", material_set)
        self.video_path = source_paths[0]

        self._write_json(
            "source_media.json",
            {
                "schema_version": 3,
                "analysis_strategy": "independent_sources",
                "merged_video_path": None,
                "sources": [item.model_dump(mode="json") for item in material_set.sources],
            },
        )
        self._write_json(
            "media_info.json",
            {
                "source_count": len(material_set.sources),
                "total_duration": sum(item.duration for item in material_set.sources),
                "audio_source_count": sum(item.has_audio for item in material_set.sources),
            },
        )
        self._transition("preflight_passed", "preflight-passed")
        self._write_manifest(
            "preflight_ok",
            video_path=self.video_path,
            source_count=len(source_paths),
        )

        try:
            compilation = self.agent1.compile(
                user_input,
                scenario=scenario,
                submitted_by=submitted_by,
                overrides=overrides,
            )
            aggregate_analysis: ContentAnalysis | None = None
            analysis_agent = getattr(self, "agent2", None)
            if analysis_agent is not None:
                def analyze_source(source: SourceAsset) -> ContentAnalysis:
                    with model_call_context(
                        self.task_dir,
                        stage="material_understanding",
                        prompt_template_version="material-summary-v2-per-source",
                        input_spec_id=compilation.spec.id,
                        input_spec_version=compilation.spec.version,
                    ):
                        return analysis_agent.run(
                            source.source_path,
                            compilation.legacy_requirement,
                            source_asset_id=source.id,
                        )

                material_set, aggregate_analysis = media_asset_service.analyze(
                    material_set,
                    compilation.legacy_requirement,
                    analysis_agent,
                    self.store,
                    run_in_context=analyze_source,
                    progress_callback=progress_callback,
                )
                self.status.material_set = material_set
                compilation = self._generate_material_interview_questions(
                    compilation, aggregate_analysis
                )
        except Exception as error:
            self._fail_task("requirement_compilation_failed", error)
            raise
        gate = self.store.save_requirement_draft(
            compilation.brief,
            compilation.spec,
            compilation.execution_brief,
            actor_id=submitted_by,
            generation_mode=compilation.mode,
            slots=compilation.slots,
        )
        self.store.write_model(f"legacy_requirement_v{compilation.spec.version}.json", compilation.legacy_requirement)
        self.status.requirement = compilation.legacy_requirement
        self.status.requirement_spec = compilation.spec
        self.status.execution_brief = compilation.execution_brief
        self.status.requirement_gate = gate
        self._transition("requirement_drafted", "requirement-drafted-v1")
        needs_clarification = bool(compilation.spec.open_questions)
        if needs_clarification:
            self._transition("clarification_required", "clarification-required-v1")
        self.status.step = self.status.task_snapshot.state
        self._write_manifest(
            self.status.step,
            requirement_version=str(compilation.spec.version),
            requirement_compilation_mode=compilation.mode,
        )
        return compilation

    def resolve_material_alignment(
        self,
        action: str,
        *,
        actor_id: Optional[str] = None,
        edited_requirement_text: Optional[str] = None,
    ) -> RequirementCompilation:
        """记录用户对素材建议的决定，并生成可继续澄清的新任务书版本。"""
        if action not in {"adopt", "edit_and_adopt", "keep_original"}:
            raise ValueError("未知的素材对齐决定")
        if not self.store or not self.task_dir or not self.status.requirement_spec:
            raise ValueError("当前没有可处理的素材对齐建议")
        if (self.task_dir / "material_alignment_decision.json").exists():
            raise ValueError("这条素材对齐建议已经处理，请继续审核当前任务书")
        proposal = self.store.read_model(
            "material_alignment_proposal.json", MaterialAlignmentProposal
        )
        previous_spec = self.status.requirement_spec
        previous_execution = self.status.execution_brief
        if not previous_execution or (
            proposal.requirement_spec_id != previous_spec.id
            or proposal.requirement_spec_version != previous_spec.version
        ):
            raise ValueError("素材建议与当前任务书版本不一致，请重新生成任务书")
        brief = self.store.read_model("requirement_brief.json", RequirementBrief)
        previous_legacy = self.store.read_model(
            f"legacy_requirement_v{previous_spec.version}.json", VideoRequirement
        )
        try:
            previous_slots = self.store.read_requirement_slots(previous_spec.version)
        except (FileNotFoundError, ValueError):
            previous_slots = []

        decision_text = brief.raw_text
        requirements = list(previous_spec.requirements)
        updates = {
            "purpose": previous_spec.purpose,
            "audience": previous_spec.audience,
            "video_type": previous_spec.video_type,
            "style": previous_spec.style,
            "target_duration": previous_spec.target_duration,
            "duration_tolerance": previous_spec.duration_tolerance,
            "need_subtitles": previous_spec.need_subtitles,
            "need_bgm": previous_spec.need_bgm,
        }
        legacy = previous_legacy

        if action == "adopt":
            decision_text = proposal.suggested_requirement_text
            preserved = [
                item for item in previous_spec.requirements
                if item.category != "content" or item.priority in {"must", "prohibited"}
            ]
            suggested = [
                RequirementItem(
                    category="content",
                    description=item,
                    priority="should",
                    status="confirmed",
                )
                for item in proposal.suggested_focus_items
            ]
            quality_requirement = RequirementItem(
                category="quality",
                description="人物发言或问答必须保留完整，不在表达中途截断",
                priority="should",
                status="confirmed",
            )
            requirements = [*preserved, *suggested, quality_requirement]
            updates.update({
                "purpose": proposal.suggested_purpose,
                "video_type": proposal.suggested_video_type,
                "style": proposal.suggested_style,
                "target_duration": proposal.suggested_target_duration,
                "duration_tolerance": max(
                    10.0, min(30.0, proposal.suggested_target_duration * 0.1)
                ),
            })
            legacy = previous_legacy.model_copy(update={
                "target_duration": int(proposal.suggested_target_duration),
                "video_type": proposal.suggested_video_type,
                "style": proposal.suggested_style,
                "focus_keywords": proposal.suggested_focus_items,
            })
        elif action == "edit_and_adopt":
            decision_text = str(edited_requirement_text or "").strip()
            if not decision_text:
                raise ValueError("请先修改 AI 建议，再选择“修改后采用”")
            with model_call_context(
                self.task_dir,
                stage="alignment_user_revision",
                prompt_template_version="requirement-compiler-v1",
                input_spec_id=previous_spec.id,
                input_spec_version=previous_spec.version,
            ):
                edited = self.agent1.compile(
                    decision_text,
                    scenario=brief.scenario,
                    submitted_by=actor_id,
                )
            requirements = [
                item.model_copy(update={"status": "confirmed"})
                for item in edited.spec.requirements
            ]
            updates.update({
                "purpose": edited.spec.purpose,
                "audience": edited.spec.audience,
                "video_type": edited.spec.video_type,
                "style": edited.spec.style,
                "target_duration": edited.spec.target_duration,
                "duration_tolerance": edited.spec.duration_tolerance,
                "need_subtitles": edited.spec.need_subtitles,
                "need_bgm": edited.spec.need_bgm,
            })
            legacy = edited.legacy_requirement

        revised_spec = previous_spec.model_copy(update={
            **updates,
            "version": previous_spec.version + 1,
            "requirements": requirements,
            "open_questions": [],
            "status": "draft",
            "confirmed_at": None,
            "created_at": datetime.now(),
        })
        revised_execution = previous_execution.model_copy(update={
            "version": previous_execution.version + 1,
            "requirement_spec_version": revised_spec.version,
            "visible_instruction": RequirementAgent._build_visible_instruction(revised_spec),
            "included_requirement_ids": [item.id for item in requirements],
            "status": "draft",
            "confirmed_at": None,
        })
        if action == "keep_original":
            slots = [
                slot.model_copy(update={
                    "question": None,
                    "question_reason": None,
                    "question_impact": None,
                    "answer_hint": None,
                    "question_source": None,
                })
                for slot in previous_slots
            ]
        else:
            slots = RequirementAgent._build_slots(
                brief.raw_text + "\n" + decision_text,
                revised_spec,
                scenario=brief.scenario,
                overrides={
                    "target_duration": revised_spec.target_duration,
                    "style": revised_spec.style,
                    "focus_keywords": [
                        item.description for item in requirements if item.category == "content"
                    ],
                },
                manual_required=False,
            )
        compilation = RequirementCompilation(
            brief=brief,
            spec=revised_spec,
            execution_brief=revised_execution,
            legacy_requirement=legacy,
            slots=slots,
            alignment_proposal=proposal,
        )
        compilation = self._generate_clarification_questions(compilation)
        material_analysis_path = self.task_dir / "material_analysis.json"
        if material_analysis_path.exists():
            material_analysis = self.store.read_model(
                "material_analysis.json", ContentAnalysis
            )
            compilation = self._generate_material_interview_questions(
                compilation, material_analysis
            )
        revised_spec = compilation.spec
        revised_execution = compilation.execution_brief.model_copy(update={
            "visible_instruction": RequirementAgent._build_visible_instruction(revised_spec),
            "included_requirement_ids": [item.id for item in revised_spec.requirements],
        })
        gate = self.store.save_requirement_revision(
            brief,
            previous_spec,
            revised_spec,
            revised_execution,
            actor_id,
            slots=compilation.slots,
        )
        self.store.write_model(
            f"legacy_requirement_v{revised_spec.version}.json", legacy
        )
        decision = MaterialAlignmentDecision(
            task_id=self.store.task_id,
            proposal_id=proposal.id,
            action=action,
            edited_requirement_text=(decision_text if action == "edit_and_adopt" else None),
            resulting_requirement_version=revised_spec.version,
            actor_id=actor_id,
        )
        self.store.write_model("material_alignment_decision.json", decision)
        self.store.append_audit(
            AuditEvent(
                task_id=self.store.task_id,
                action=f"material_alignment_{action}",
                subject_type="MaterialAlignmentProposal",
                subject_id=proposal.id,
                subject_version=proposal.requirement_spec_version,
                actor_type="user",
                actor_id=actor_id,
                summary={
                    "adopt": "用户采用了素材驱动的需求优化建议。",
                    "edit_and_adopt": "用户修改后采用了素材驱动的需求优化建议。",
                    "keep_original": "用户选择保持原任务书。",
                }[action],
            )
        )
        self.status.requirement_spec = revised_spec
        self.status.execution_brief = revised_execution
        self.status.requirement_gate = gate
        self.status.requirement = legacy
        if revised_spec.open_questions:
            self._transition(
                "clarification_required",
                f"alignment-clarification-required-v{revised_spec.version}",
                actor_id=actor_id,
            )
        self.status.step = self.status.task_snapshot.state
        self._write_manifest(
            self.status.step,
            requirement_version=str(revised_spec.version),
            material_alignment_action=action,
        )
        return compilation.model_copy(update={"execution_brief": revised_execution})

    def confirm_requirement_draft(
        self,
        gate_id: str,
        actor_id: Optional[str] = None,
        clarification_answers: str | dict[str, str] = "",
        allow_confirmed_override: bool = False,
    ) -> RequirementSpec:
        """确认任务书后才允许后续转录和候选分析。"""
        if not self.store or not self.state_machine:
            raise ValueError("没有可确认的需求任务书，请先创建任务")
        spec = self.status.requirement_spec
        execution = self.status.execution_brief
        if not spec or not execution:
            raise ValueError("缺少当前需求任务书或执行说明")
        proposal_path = self.task_dir / "material_alignment_proposal.json"
        decision_path = self.task_dir / "material_alignment_decision.json"
        if proposal_path.exists() and not decision_path.exists():
            proposal = self.store.read_model(
                "material_alignment_proposal.json", MaterialAlignmentProposal
            )
            if proposal.requires_user_decision:
                raise ValueError(
                    "请先处理素材与需求对齐建议：采用、修改后采用，或保持原任务书"
                )
        if self.status.task_snapshot and self.status.task_snapshot.state == "awaiting_requirement_clarification":
            if not clarification_answers:
                raise ValueError("任务书仍有待确认问题，请填写补充说明后再确认")
            slots = self.store.read_requirement_slots(spec.version)
            if isinstance(clarification_answers, dict):
                answer_map = clarification_answers
            else:
                text = clarification_answers.strip()
                unresolved = [
                    slot for slot in slots if slot.status in {"missing", "unknown"}
                ]
                answer_map = {
                    slot.key: (
                        text if slot.key in {"focus_keywords", "high_risk_review"} else "不知道"
                    )
                    for slot in unresolved
                }
            clarification_service = getattr(self, "clarification_service", None) or RequirementClarificationService()
            revised_spec, revised_execution, revised_slots, turn = clarification_service.apply(
                task_id=self.store.task_id,
                spec=spec,
                execution=execution,
                slots=slots,
                answers=answer_map,
                actor_id=actor_id,
                allow_confirmed_override=allow_confirmed_override,
            )
            revised_execution = revised_execution.model_copy(update={
                "visible_instruction": RequirementAgent._build_visible_instruction(revised_spec)
            })
            brief = self.store.read_model("requirement_brief.json", RequirementBrief)
            gate = self.store.save_requirement_revision(
                brief,
                spec,
                revised_spec,
                revised_execution,
                actor_id,
                slots=revised_slots,
            )
            self.store.append_clarification(turn)
            previous_legacy = self.store.read_model(
                f"legacy_requirement_v{spec.version}.json", VideoRequirement
            )
            revised_legacy = previous_legacy.model_copy(
                update={
                    "target_duration": int(revised_spec.target_duration),
                    "style": revised_spec.style,
                    "need_subtitles": revised_spec.need_subtitles,
                    "need_bgm": revised_spec.need_bgm,
                    "focus_keywords": [
                        item.description for item in revised_spec.requirements
                        if item.category == "content"
                    ][:8],
                }
            )
            self.store.write_model(
                f"legacy_requirement_v{revised_spec.version}.json", revised_legacy
            )
            self._transition(
                "clarification_applied",
                f"clarification-applied-v{revised_spec.version}",
                actor_id=actor_id,
            )
            spec, execution, gate_id = revised_spec, revised_execution, gate.id
            self.status.requirement_spec = spec
            self.status.execution_brief = execution
            self.status.requirement_gate = gate
            if spec.open_questions:
                self._transition(
                    "clarification_required",
                    f"clarification-required-v{spec.version}",
                    actor_id=actor_id,
                )
                raise ValueError("仍有未解决或冲突的需求槽位，请继续补充")

        if spec.open_questions:
            raise ValueError("请先解决任务书中的待确认问题")
        if any(item.status == "needs_confirmation" for item in spec.requirements):
            spec = spec.model_copy(
                update={
                    "requirements": [
                        item.model_copy(update={"status": "confirmed"})
                        if item.status == "needs_confirmation" else item
                        for item in spec.requirements
                    ]
                }
            )
            self.store.write_model(f"requirement_spec_v{spec.version}.json", spec)
        spec, execution, gate = self.store.confirm_requirement(gate_id, actor_id)
        self.status.requirement_spec = spec
        self.status.execution_brief = execution
        self.status.requirement_gate = gate
        self._transition(
            "requirement_confirmed",
            f"requirement-confirmed-v{spec.version}",
            actor_id=actor_id,
        )
        # 风格、字幕和音乐已经在同一次任务书确认中由用户选择，不再新增一次
        # LLM 推荐和审批步骤。渲染细节仍可在方案页高级设置中修改。
        self.status.style_proposals = []
        self._transition("style_skipped", f"style-confirmed-in-taskbook-v{spec.version}")
        self.status.step = self.status.task_snapshot.state
        self._write_manifest(self.status.step, requirement_version=str(spec.version))
        return spec

    def revise_requirement_draft(
        self,
        *,
        updates: Optional[dict] = None,
        requirements: Optional[list[dict]] = None,
        actor_id: Optional[str] = None,
        clear_open_questions: bool = False,
    ) -> RequirementSpec:
        """把用户在审核页的修改保存为新版本，不原地覆盖 AI 草稿。"""
        if not self.store or not self.state_machine:
            raise ValueError("没有可修改的需求任务书")
        spec = self.status.requirement_spec
        execution = self.status.execution_brief
        gate = self.status.requirement_gate
        if not spec or not execution or not gate:
            raise ValueError("缺少当前需求任务书、执行说明或 Gate")
        state = self.status.task_snapshot.state if self.status.task_snapshot else ""
        if state not in {"requirement_draft", "awaiting_requirement_clarification"}:
            raise ValueError(f"当前状态 {state} 不能修改需求任务书")

        allowed_fields = {
            "purpose", "audience", "video_type", "style", "need_subtitles",
            "need_bgm", "target_duration", "duration_tolerance",
        }
        patch = {
            key: value for key, value in (updates or {}).items()
            if key in allowed_fields and value is not None
        }
        if requirements is None:
            revised_items = list(spec.requirements)
        else:
            revised_items = []
            for raw in requirements:
                payload = {
                    key: value for key, value in raw.items()
                    if key in RequirementItem.model_fields and value is not None and value != ""
                }
                if "id" not in payload:
                    payload.pop("id", None)
                payload.setdefault("status", "confirmed")
                revised_items.append(RequirementItem.model_validate(payload))
        patch.update({
            "version": spec.version + 1,
            "status": "draft",
            "confirmed_at": None,
            "requirements": revised_items,
            "open_questions": [] if clear_open_questions else spec.open_questions,
            "created_at": datetime.now(),
        })
        revised_spec = spec.model_copy(update=patch)
        visible_instruction = RequirementAgent._build_visible_instruction(revised_spec)
        revised_execution = execution.model_copy(update={
            "version": execution.version + 1,
            "requirement_spec_version": revised_spec.version,
            "visible_instruction": visible_instruction,
            "included_requirement_ids": [item.id for item in revised_items],
            "status": "draft",
            "confirmed_at": None,
        })
        try:
            slots = self.store.read_requirement_slots(spec.version)
        except (FileNotFoundError, ValueError):
            slots = []
        slot_updates = {
            "purpose": revised_spec.purpose,
            "audience": revised_spec.audience,
            "target_duration": revised_spec.target_duration,
            "style": revised_spec.style,
            "need_subtitles": revised_spec.need_subtitles,
            "need_bgm": revised_spec.need_bgm,
        }
        revised_slots = [
            slot.model_copy(update={
                "value": slot_updates[slot.key],
                "status": "confirmed",
                "source_type": "clarification",
                "source_ref": f"requirement_v{revised_spec.version}",
                "question": None,
            }) if slot.key in slot_updates else slot
            for slot in slots
        ]
        brief = self.store.read_model("requirement_brief.json", RequirementBrief)
        revised_gate = self.store.save_requirement_revision(
            brief, spec, revised_spec, revised_execution, actor_id, slots=revised_slots,
        )
        previous_legacy = self.store.read_model(
            f"legacy_requirement_v{spec.version}.json", VideoRequirement
        )
        revised_legacy = previous_legacy.model_copy(update={
            "target_duration": int(revised_spec.target_duration),
            "style": revised_spec.style,
            "need_subtitles": revised_spec.need_subtitles,
            "need_bgm": revised_spec.need_bgm,
            "focus_keywords": [
                item.description for item in revised_items if item.category == "content"
            ][:8],
        })
        self.store.write_model(
            f"legacy_requirement_v{revised_spec.version}.json", revised_legacy
        )
        if state == "awaiting_requirement_clarification":
            self._transition(
                "clarification_applied",
                f"requirement-editor-applied-v{revised_spec.version}",
                actor_id=actor_id,
            )
        if revised_spec.open_questions:
            self._transition(
                "clarification_required",
                f"clarification-required-v{revised_spec.version}",
                actor_id=actor_id,
            )
        self.status.requirement_spec = revised_spec
        self.status.execution_brief = revised_execution
        self.status.requirement_gate = revised_gate
        self.status.requirement = revised_legacy
        self._write_manifest(
            self.status.task_snapshot.state,
            requirement_version=str(revised_spec.version),
        )
        return revised_spec

    def analyze_confirmed_requirement(
        self,
        video_path: str,
        generate_previews: bool = False,
    ) -> EditScript:
        """只接受已确认需求版本，生成候选与粗剪计划。"""
        if not self.store or not self.state_machine or not self.status.requirement_spec:
            raise ValueError("没有已确认的需求任务书")
        spec = self.status.requirement_spec
        if not self.store.is_approved("requirement", spec.id, spec.version):
            raise ValueError("需求任务书尚未确认，不能开始素材分析")
        if self.status.task_snapshot and self.status.task_snapshot.state not in {
            "style_recommended", "style_skipped"
        }:
            raise ValueError(f"当前状态 {self.status.task_snapshot.state} 不能开始素材分析")
        self._transition("analysis_started", f"analysis-started-v{spec.version}")
        requirement = self.store.read_model(f"legacy_requirement_v{spec.version}.json", VideoRequirement)
        self.status.step = self.status.task_snapshot.state
        self._write_manifest(self.status.step, requirement_version=str(spec.version))
        try:
            cached_material = self.task_dir / "material_analysis.json"
            if cached_material.exists():
                analysis = self.store.read_model("material_analysis.json", ContentAnalysis)
                logger.info("复用任务书确认前生成的素材转录与证据，不重复运行 Whisper")
            else:
                with model_call_context(
                    self.task_dir,
                    stage="transcription_summary",
                    prompt_template_version="content-summary-v1",
                    input_spec_id=spec.id,
                    input_spec_version=spec.version,
                ):
                    analysis = self.agent2.run(video_path, requirement)
        except Exception as error:
            self._fail_task("transcription_failed", error)
            raise
        if not self.status.execution_brief:
            raise ValueError("缺少已确认的 AI 执行说明")
        try:
            with model_call_context(
                self.task_dir,
                stage="candidate_analysis",
                prompt_template_version="candidate-ranker-v3-single-call",
                input_spec_id=spec.id,
                input_spec_version=spec.version,
            ):
                generation = self.candidate_agent.run(
                    self.status.execution_brief,
                    spec.requirements,
                    analysis.evidence,
                    analysis.source_durations or analysis.video_duration,
                    target_duration=spec.target_duration,
                    duration_tolerance=spec.duration_tolerance,
                    transcript=analysis.transcript,
                )
        except Exception as error:
            self._fail_task("candidate_analysis_failed", error)
            raise
        highlights = [
            HighlightClip(
                start=candidate.source_start,
                end=candidate.source_end,
                source_asset_id=candidate.source_asset_id,
                text=" / ".join(citation.quote for citation in candidate.citations),
                importance=candidate.confidence,
                category="highlight",
                reason=candidate.selection_reason,
                suggestion="；".join(candidate.risk_flags) or None,
                candidate_id=candidate.id,
                matched_requirement_ids=candidate.matched_requirement_ids,
                evidence_ids=[citation.evidence_id for citation in candidate.citations],
            )
            for candidate in generation.valid_candidates
        ]
        valid_candidates = list(generation.valid_candidates)
        valid_candidate_ids = {candidate.id for candidate in valid_candidates}
        rejected_candidates = [
            candidate for candidate in generation.candidates
            if candidate.id not in valid_candidate_ids
        ]
        analysis = analysis.model_copy(
            update={"candidate_clips": valid_candidates, "highlights": highlights}
        )
        self.store.write_payload(
            "candidate_generation.json",
            {
                "valid_candidate_ids": generation.valid_ids,
                "retrieval_trace": generation.retrieval_trace,
                "missing_must_requirement_ids": getattr(
                    generation, "missing_must_requirement_ids", []
                ),
                "degraded": getattr(generation, "degraded", False),
                "failure_reason": getattr(generation, "failure_reason", None),
                "failed_requirement_ids": getattr(
                    generation, "failed_requirement_ids", []
                ),
                "duration_plan": (
                    generation.duration_plan.model_dump(mode="json")
                    if getattr(generation, "duration_plan", None) else None
                ),
                "retrieval_degradation_reason": getattr(
                    generation, "retrieval_degradation_reason", None
                ),
                "rejected_candidate_ids": [item.id for item in rejected_candidates],
            },
        )
        if getattr(generation, "duration_plan", None):
            self.store.write_model("duration_budget.json", generation.duration_plan)
        self.store.write_payload(
            "retrieval_trace.json",
            {
                "retrieval_trace": generation.retrieval_trace,
                "missing_must_requirement_ids": getattr(
                    generation, "missing_must_requirement_ids", []
                ),
            },
        )
        self.store.write_payload(
            "candidate_clips.json",
            {"candidates": [candidate.model_dump(mode="json") for candidate in valid_candidates]},
        )
        self.store.write_payload(
            "rejected_candidate_suggestions.json",
            {"candidates": [candidate.model_dump(mode="json") for candidate in rejected_candidates]},
        )
        self.store.write_payload(
            "evidence.json",
            {"evidence": [item.model_dump(mode="json") for item in analysis.evidence]},
        )
        self.store.write_payload(
            "transcript.json",
            {"segments": [item.model_dump(mode="json") for item in analysis.transcript]},
        )
        self.store.write_model("analysis.json", analysis)
        if (self.task_dir / "reference_library.json").exists():
            library = self.store.read_model("reference_library.json", ReferenceLibrary)
            previous_queue = (
                self.store.read_model("entity_review_queue.json", EntityReviewQueue)
                if (self.task_dir / "entity_review_queue.json").exists()
                else None
            )
            queue = self.reference_document_service.build_entity_queue(
                task_id=self.store.task_id,
                analysis=analysis,
                library=library,
                previous=previous_queue,
            )
            self.store.write_model("entity_review_queue.json", queue)
        try:
            script = self.agent3.run(
                analysis,
                requirement,
                spec.requirements,
                duration_tolerance=spec.duration_tolerance,
                transition_duration=0.35,
            )
        except Exception as error:
            self._fail_task("planning_failed", error)
            raise
        self.status.requirement = requirement
        self.status.analysis = analysis
        self.status.script = script
        plan_service = getattr(self, "plan_service", None) or EditPlanService()
        try:
            plan = plan_service.create(spec=spec, analysis=analysis, script=script)
            if getattr(generation, "duration_plan", None):
                actual_budget = self.candidate_agent.duration_planner.complete(
                    generation.duration_plan, [plan.estimated_duration]
                )
                self.store.write_model("duration_budget.json", actual_budget)
                generation_payload = json.loads(
                    (self.task_dir / "candidate_generation.json").read_text(encoding="utf-8")
                )
                generation_payload["duration_plan"] = actual_budget.model_dump(mode="json")
                self.store.write_payload("candidate_generation.json", generation_payload)
            plan_gate = self.store.save_plan_draft(plan)
        except Exception as error:
            self._fail_task("planning_failed", error)
            raise
        self.status.edit_plan = plan
        self.status.plan_gate = plan_gate
        self.store.write_model("edit_plan.json", script)
        if generate_previews:
            self.preview_paths = self._create_previews_for_sources(script)
        self._transition("review_ready", f"review-ready-plan-v{plan.version}")
        self.status.step = self.status.task_snapshot.state
        self._write_manifest(
            self.status.step,
            requirement_version=str(spec.version),
            edit_plan_version=str(plan.version),
        )
        return script

    def add_reference_document(
        self,
        path: str,
        category: ReferenceDocumentCategory,
        *,
        actor_id: Optional[str] = None,
    ) -> ReferenceLibrary:
        """上传活动资料并生成可定位证据；不会自动改写字幕或专有名词。"""
        if not self.store or not self.task_dir:
            raise ValueError("请先创建视频任务")
        service = (
            getattr(self, "reference_document_service", None)
            or ReferenceDocumentService()
        )
        previous = (
            self.store.read_model("reference_library.json", ReferenceLibrary)
            if (self.task_dir / "reference_library.json").exists()
            else None
        )
        library = service.ingest(
            task_id=self.store.task_id,
            path=path,
            category=category,
            library=previous,
        )
        self.store.write_model("reference_library.json", library)
        analysis = self.status.analysis
        if analysis is None and (self.task_dir / "material_analysis.json").exists():
            analysis = self.store.read_model("material_analysis.json", ContentAnalysis)
        if analysis is not None:
            previous_queue = (
                self.store.read_model("entity_review_queue.json", EntityReviewQueue)
                if (self.task_dir / "entity_review_queue.json").exists()
                else None
            )
            queue = service.build_entity_queue(
                task_id=self.store.task_id,
                analysis=analysis,
                library=library,
                previous=previous_queue,
            )
            self.store.write_model("entity_review_queue.json", queue)
        self.store.append_audit(
            AuditEvent(
                task_id=self.store.task_id,
                action="reference_document_added",
                subject_type="ReferenceLibrary",
                subject_id=self.store.task_id,
                subject_version=library.version,
                actor_type="user",
                actor_id=actor_id,
                summary=f"用户上传了 {category} 类活动资料，系统生成了可定位文字证据。",
                metadata={"category": category, "filename": Path(path).name},
            )
        )
        return library

    def retry_source_analysis(
        self,
        source_asset_id: str,
        *,
        retry_reason: str,
        actor_id: Optional[str] = None,
    ) -> TaskMaterialSet:
        """在候选审核前只重试一段失败素材，其他转录和索引不重复生成。"""
        if not self.store or not self.task_dir or not self.status.material_set:
            raise ValueError("当前任务没有可重试的素材分析")
        state = self.status.task_snapshot.state if self.status.task_snapshot else ""
        if state not in {
            "requirement_draft", "awaiting_requirement_clarification",
            "requirement_confirmed", "style_recommended", "style_skipped",
        }:
            raise ValueError("候选计划生成后不能直接替换素材分析；请先创建新的分析版本")
        requirement = self.status.requirement
        if requirement is None:
            raise ValueError("缺少当前任务书的兼容执行参数")
        service = getattr(self, "media_asset_service", None) or MediaAssetService()

        def analyze_source(source: SourceAsset) -> ContentAnalysis:
            spec = self.status.requirement_spec
            with model_call_context(
                self.task_dir,
                stage="source_analysis_retry",
                prompt_template_version="material-summary-v2-per-source",
                input_spec_id=spec.id if spec else None,
                input_spec_version=spec.version if spec else None,
            ):
                return self.agent2.run(
                    source.source_path,
                    requirement,
                    source_asset_id=source.id,
                )

        material_set, _ = service.retry_source(
            self.status.material_set,
            source_asset_id=source_asset_id,
            requirement=requirement,
            analysis_agent=self.agent2,
            store=self.store,
            retry_reason=retry_reason,
            run_in_context=analyze_source,
        )
        self.status.material_set = material_set
        self.store.append_audit(
            AuditEvent(
                task_id=self.store.task_id,
                action="source_analysis_retried",
                subject_type="SourceAsset",
                subject_id=source_asset_id,
                actor_type="user",
                actor_id=actor_id,
                summary="系统只重新分析了用户指定的来源素材，其他素材结果保持不变。",
                metadata={"retry_reason": retry_reason},
            )
        )
        return material_set

    def resolve_entity_review(
        self,
        item_id: str,
        action: str,
        *,
        actor_id: Optional[str] = None,
        edited_value: Optional[str] = None,
    ) -> EntityReviewQueue:
        """确认、拒绝或修改一个专有名词建议，不静默改写 ASR 原文。"""
        if not self.store or not self.task_dir:
            raise ValueError("请先创建视频任务")
        if not (self.task_dir / "entity_review_queue.json").exists():
            raise ValueError("当前没有待核对的专有名词")
        queue = self.store.read_model("entity_review_queue.json", EntityReviewQueue)
        updated = ReferenceDocumentService.resolve(
            queue,
            item_id=item_id,
            action=action,
            actor_id=actor_id,
            edited_value=edited_value,
        )
        self.store.write_model("entity_review_queue.json", updated)
        self.store.write_model(f"entity_review_queue_v{updated.version}.json", updated)
        self.store.append_audit(
            AuditEvent(
                task_id=self.store.task_id,
                action=f"entity_review_{action}",
                subject_type="EntityReviewItem",
                subject_id=item_id,
                subject_version=updated.version,
                actor_type="user",
                actor_id=actor_id,
                summary="用户处理了一项专有名词核对建议。",
            )
        )
        return updated

    def render(self, script: EditScript, video_path: str, output_path: str = "") -> ExecutionResult:
        """兼容入口；只接受当前已批准计划完全匹配的执行视图。"""
        plan = self.status.edit_plan
        if not plan or plan.status != "approved" or plan.execution_script != script:
            raise ValueError("未批准计划不能渲染；请先审核并批准当前计划版本")
        return self.render_approved_plan(plan, video_path, output_path)

    def revise_edit_plan(
        self,
        *,
        selected_candidate_ids: Optional[list[str]] = None,
        segment_updates: Optional[dict[str, dict]] = None,
        manual_segments: Optional[list[dict]] = None,
        delivery_updates: Optional[dict] = None,
        actor_id: Optional[str] = None,
    ) -> AuditableEditPlan:
        if not self.store or not self.status.edit_plan or not self.status.analysis:
            raise ValueError("没有可修订的剪辑计划")
        state = self.status.task_snapshot.state if self.status.task_snapshot else ""
        if state not in {"awaiting_review", "plan_approved", "awaiting_delivery_resolution"}:
            raise ValueError(f"当前状态 {state} 不能修改剪辑计划")
        previous = self.status.edit_plan
        prepared_manual_segments = []
        if manual_segments:
            valid_requirement_ids = {
                item.id for item in self.status.requirement_spec.requirements
            } if self.status.requirement_spec else set()
            evidence = list(self.status.analysis.evidence)
            evidence_ids = {item.id for item in evidence}
            for raw in manual_segments:
                matched_ids = list(dict.fromkeys(raw.get("matched_requirement_ids", [])))
                if not matched_ids or not set(matched_ids).issubset(valid_requirement_ids):
                    raise ValueError("人工补片必须关联至少一个当前任务书中的需求 ID")
                start = float(raw["source_start"])
                end = float(raw["source_end"])
                annotation = str(
                    raw.get("annotation")
                    or raw.get("subtitle_text")
                    or raw.get("title_text")
                    or "用户人工确认该时间段应进入成片"
                ).strip()
                fingerprint = (
                    f"{self.store.task_id}|{raw.get('source_asset_id') or 'legacy'}|"
                    f"{start:.3f}|{end:.3f}|{annotation}"
                ).encode("utf-8")
                digest = hashlib.sha256(fingerprint).hexdigest()
                evidence_id = f"user_evidence_{digest[:16]}"
                if evidence_id not in evidence_ids:
                    evidence.append(
                        Evidence(
                            id=evidence_id,
                            source_asset_id=raw.get("source_asset_id"),
                            type="user_annotation",
                            source_start=start,
                            source_end=end,
                            content=annotation,
                            content_hash=digest,
                            metadata={
                                "actor_id": actor_id,
                                "source": "manual_timeline_add",
                                "source_asset_id": raw.get("source_asset_id"),
                            },
                        )
                    )
                    evidence_ids.add(evidence_id)
                    self.store.append_audit(
                        AuditEvent(
                            task_id=self.store.task_id,
                            action="user_annotation_created",
                            subject_type="Evidence",
                            subject_id=evidence_id,
                            actor_type="user",
                            actor_id=actor_id,
                            summary=f"用户为 {start:.1f}–{end:.1f} 秒人工补片创建了可追溯标注证据。",
                        )
                    )
                prepared_manual_segments.append({
                    **raw,
                    "matched_requirement_ids": matched_ids,
                    "evidence_ids": [
                        *list(dict.fromkeys(raw.get("evidence_ids", []))),
                        evidence_id,
                    ],
                })
            self.status.analysis = self.status.analysis.model_copy(
                update={"evidence": evidence}
            )
            self.store.write_model("analysis.json", self.status.analysis)
            self.store.write_payload(
                "evidence.json",
                {"evidence": [item.model_dump(mode="json") for item in evidence]},
            )
        plan_service = getattr(self, "plan_service", None) or EditPlanService()
        revised, decisions = plan_service.revise(
            task_id=self.store.task_id,
            plan=previous,
            analysis=self.status.analysis,
            selected_candidate_ids=selected_candidate_ids,
            segment_updates=segment_updates,
            manual_segments=prepared_manual_segments,
            delivery_updates=delivery_updates,
            actor_id=actor_id,
        )
        gate = self.store.save_plan_draft(
            revised,
            actor_id=actor_id,
            previous_plan=previous,
        )
        self.store.append_decisions(decisions)
        if previous.delivery_spec != revised.delivery_spec:
            self.store.append_audit(
                AuditEvent(
                    task_id=self.store.task_id,
                    action="delivery_spec_revised",
                    subject_type="DeliverySpec",
                    subject_id=revised.delivery_spec.id,
                    subject_version=revised.delivery_spec.version,
                    actor_type="user",
                    actor_id=actor_id,
                    summary="用户修改了字幕、转场、片头片尾、标题、BGM 或风格组合，系统生成新的交付规格版本。",
                    metadata={
                        "style_bundle_id": revised.delivery_spec.style_bundle_id,
                    },
                )
            )
        if state == "plan_approved":
            self._transition(
                "review_invalidated",
                f"review-invalidated-plan-v{revised.version}",
                actor_id=actor_id,
            )
        elif state == "awaiting_delivery_resolution":
            self._transition(
                "delivery_revision_requested",
                f"delivery-revision-plan-v{revised.version}",
                actor_id=actor_id,
            )
            self.status.delivery_report = None
        self.status.edit_plan = revised
        self.status.plan_gate = gate
        self.status.script = revised.execution_script
        self._write_manifest(
            "awaiting_review",
            edit_plan_version=str(revised.version),
        )
        return revised

    def supplement_plan_duration(
        self,
        *,
        excluded_ranges: Optional[list[dict]] = None,
        actor_id: Optional[str] = None,
    ) -> AuditableEditPlan:
        """复用转录补选备用片段；保留用户当前选择，不重新调用模型或恢复已删除画面。"""
        if not self.store or not self.status.analysis or not self.status.edit_plan:
            raise ValueError("请先生成剪辑方案")
        analysis, plan, spec = (
            self.status.analysis, self.status.edit_plan, self.status.requirement_spec
        )
        if spec is None or self.status.execution_brief is None:
            raise ValueError("当前任务书尚未确认")
        generator = CandidateAgent(retriever=BM25EvidenceRetriever())
        reserve = generator.run(
            self.status.execution_brief, spec.requirements, analysis.evidence,
            analysis.source_durations or analysis.video_duration,
            target_duration=spec.target_duration,
            duration_tolerance=spec.duration_tolerance,
            transcript=analysis.transcript,
            rank_with_model=False,
        )
        candidates = list(analysis.candidate_clips)
        known_ranges = {
            (item.source_asset_id, round(item.source_start, 3), round(item.source_end, 3))
            for item in candidates
        }
        for candidate in reserve.valid_candidates:
            key = (candidate.source_asset_id, round(candidate.source_start, 3), round(candidate.source_end, 3))
            if key not in known_ranges:
                candidates.append(candidate)
                known_ranges.add(key)
        def as_highlight(item):
            return HighlightClip(
                start=item.source_start, end=item.source_end,
                source_asset_id=item.source_asset_id,
                text=" ".join(citation.quote for citation in item.citations),
                importance=item.confidence, category="highlight",
                reason=item.selection_reason, candidate_id=item.id,
                matched_requirement_ids=item.matched_requirement_ids,
                evidence_ids=[citation.evidence_id for citation in item.citations],
            )
        kept = [HighlightClip(
            start=item.source_start, end=item.source_end,
            source_asset_id=item.source_asset_id, text=item.subtitle_text or "",
            importance=1.0, category="highlight", reason="保留你已审核的片段",
            candidate_id=item.candidate_id, matched_requirement_ids=item.matched_requirement_ids,
            evidence_ids=item.evidence_ids,
        ) for item in plan.timeline_segments]
        choices = [
            as_highlight(item) for item in candidates
            if not any(
                item.source_asset_id == blocked.get("source_asset_id")
                and max(item.source_start, blocked["start"]) < min(item.source_end, blocked["end"])
                for blocked in (excluded_ranges or [])
            )
        ]
        card_seconds = 2.0 * sum(
            style != "none" for style in (plan.delivery_spec.intro_style, plan.delivery_spec.outro_style)
        )
        selected = self.agent3._select_clips(
            choices, max(0.0, spec.target_duration - card_seconds),
            analysis.source_durations or analysis.video_duration,
            must_requirement_ids={item.id for item in spec.requirements if item.priority == "must"},
            duration_tolerance=spec.duration_tolerance,
            transition_duration=plan.delivery_spec.transition_duration,
            already_selected=kept,
        )
        self.status.analysis = analysis.model_copy(update={"candidate_clips": candidates})
        self.store.write_model("analysis.json", self.status.analysis)
        self.store.write_payload("candidate_clips.json", {
            "candidates": [item.model_dump(mode="json") for item in candidates]
        })
        revised = self.revise_edit_plan(
            selected_candidate_ids=[item.candidate_id for item in selected], actor_id=actor_id,
        )
        budget = generator.duration_planner.complete(reserve.duration_plan, [revised.estimated_duration])
        self.store.write_model("duration_budget.json", budget)
        self.store.append_audit(AuditEvent(
            task_id=self.store.task_id, action="duration_supplemented",
            subject_type="AuditableEditPlan", subject_id=revised.id,
            subject_version=revised.version, actor_type="user", actor_id=actor_id,
            summary=f"用户请求自动补选，预计时长从 {plan.estimated_duration:.1f} 秒调整为 {revised.estimated_duration:.1f} 秒；未调用 LLM 或重复转录。",
            metadata={"excluded_ranges": json.dumps(excluded_ranges or [], ensure_ascii=False)},
        ))
        self.preview_paths = {}
        return revised

    def create_mobile_review_link(
        self,
        *,
        base_url: str = "http://127.0.0.1:7961",
        ttl_hours: int = 24,
        actor_id: Optional[str] = None,
    ) -> MobileReviewLink:
        """为当前待审计划创建可过期、可撤销的手机审批链接。"""
        if not self.store or not self.status.edit_plan:
            raise ValueError("当前没有可发起手机审核的计划")
        service = getattr(self, "mobile_review_service", None) or MobileReviewService()
        link = service.issue(
            store=self.store,
            plan=self.status.edit_plan,
            base_url=base_url,
            ttl_hours=ttl_hours,
            created_by=actor_id,
        )
        self.store.write_model("latest_mobile_review_link.json", link)
        self.store.append_audit(
            AuditEvent(
                task_id=self.store.task_id,
                action="mobile_review_link_created",
                subject_type="AuditableEditPlan",
                subject_id=self.status.edit_plan.id,
                subject_version=self.status.edit_plan.version,
                actor_type="user",
                actor_id=actor_id,
                summary=f"用户创建了有效期 {ttl_hours} 小时的手机审核链接。",
            )
        )
        return link

    def revoke_latest_mobile_review_link(
        self,
        *,
        actor_id: Optional[str] = None,
    ) -> MobileReviewLink:
        """撤销桌面端最近创建的审核链接，并保留审计记录。"""
        if not self.store or not self.task_dir:
            raise ValueError("当前没有可撤销的手机审核链接")
        path = self.task_dir / "latest_mobile_review_link.json"
        if not path.exists():
            raise ValueError("当前没有可撤销的手机审核链接")
        link = self.store.read_model("latest_mobile_review_link.json", MobileReviewLink)
        service = getattr(self, "mobile_review_service", None) or MobileReviewService()
        service.revoke(self.store, link.token_record_id)
        self.store.append_audit(
            AuditEvent(
                task_id=self.store.task_id,
                action="mobile_review_link_revoked",
                subject_type="AuditableEditPlan",
                subject_id=link.plan_id,
                subject_version=link.plan_version,
                actor_type="user",
                actor_id=actor_id,
                summary="用户撤销了当前手机审核链接。",
            )
        )
        return link

    def apply_latest_mobile_review(self) -> MobileReviewSubmission:
        """在桌面端应用手机提交；同一提交只应用一次。"""
        if not self.store or not self.task_dir or not self.status.edit_plan:
            raise ValueError("当前没有可应用移动审核的计划")
        submissions_path = self.task_dir / "mobile_review_submissions.json"
        if not submissions_path.exists():
            raise ValueError("尚未收到手机审核结果")
        payload = json.loads(submissions_path.read_text(encoding="utf-8"))
        submissions = [
            MobileReviewSubmission.model_validate(item)
            for item in payload.get("submissions", [])
        ]
        plan = self.status.edit_plan
        submission = next(
            (
                item for item in reversed(submissions)
                if item.plan_id == plan.id and item.plan_version == plan.version
            ),
            None,
        )
        if submission is None:
            raise ValueError("手机审核结果不属于当前计划版本")
        applied_path = self.task_dir / "mobile_review_applied.json"
        applied = (
            json.loads(applied_path.read_text(encoding="utf-8"))
            if applied_path.exists() else {"submission_ids": []}
        )
        if submission.id in applied.get("submission_ids", []):
            return submission
        actor = submission.reviewer_name or "mobile-reviewer"
        if submission.decision == "approve":
            self.approve_current_plan(actor)
        else:
            if not submission.comment.strip():
                raise ValueError("手机端退回修改必须填写意见")
            gate = self.store.resolve_gate(
                self.status.plan_gate.id,
                "changes_requested",
                actor,
                submission.comment,
            )
            self.status.plan_gate = gate
        applied["submission_ids"] = [
            *applied.get("submission_ids", []), submission.id
        ]
        self._write_json("mobile_review_applied.json", applied)
        return submission

    def approve_current_plan(
        self,
        actor_id: Optional[str] = None,
        *,
        allow_duration_exception: bool = False,
    ) -> AuditableEditPlan:
        if not self.store or not self.status.edit_plan or not self.status.plan_gate:
            raise ValueError("没有可批准的剪辑计划")
        if not self.status.requirement_spec or not self.status.analysis:
            raise ValueError("缺少需求或分析结果")
        plan_service = getattr(self, "plan_service", None) or EditPlanService()
        validation = plan_service.validate(
            self.status.edit_plan,
            self.status.requirement_spec,
            self.status.analysis.candidate_clips,
            self.status.analysis.source_durations or self.status.analysis.video_duration,
            known_evidence_ids=[item.id for item in self.status.analysis.evidence],
            user_annotation_evidence_ids=[
                item.id for item in self.status.analysis.evidence
                if item.type == "user_annotation"
            ],
        )
        approval_exceptions: list[PlanApprovalException] = []
        duration_only = set(validation.errors) == {"plan_duration_too_short"}
        if not validation.valid and allow_duration_exception and duration_only:
            lower = max(
                0.0,
                self.status.edit_plan.delivery_spec.target_duration
                - self.status.edit_plan.delivery_spec.duration_tolerance,
            )
            approval_exceptions.append(
                PlanApprovalException(
                    code="plan_duration_too_short",
                    reason=(
                        f"用户在生成前确认接受当前约 {self.status.edit_plan.estimated_duration:.1f} 秒的短版，"
                        f"不再补足至任务书最低 {lower:.1f} 秒。"
                    ),
                    planned_duration=self.status.edit_plan.estimated_duration,
                    required_minimum=lower,
                    target_duration=self.status.edit_plan.delivery_spec.target_duration,
                    approved_by=actor_id,
                )
            )
        elif not validation.valid:
            segment_number_by_id = {
                segment.id: index
                for index, segment in enumerate(self.status.edit_plan.timeline_segments, start=1)
            }
            messages = []
            for error in validation.errors:
                if error == "plan_duration_too_short":
                    lower = max(
                        0.0,
                        self.status.edit_plan.delivery_spec.target_duration
                        - self.status.edit_plan.delivery_spec.duration_tolerance,
                    )
                    messages.append(
                        f"当前方案约 {self.status.edit_plan.estimated_duration:.1f} 秒，"
                        f"至少需要 {lower:.1f} 秒；请补充候选片段后再生成"
                    )
                elif error == "plan_duration_too_long":
                    upper = (
                        self.status.edit_plan.delivery_spec.target_duration
                        + self.status.edit_plan.delivery_spec.duration_tolerance
                    )
                    messages.append(
                        f"当前方案约 {self.status.edit_plan.estimated_duration:.1f} 秒，"
                        f"最多允许 {upper:.1f} 秒；请删减片段后再生成"
                    )
                elif error.startswith("segment_exceeds_candidate_context:"):
                    segment_id = error.split(":", 1)[1]
                    number = segment_number_by_id.get(segment_id, "未知")
                    messages.append(
                        f"片段 {number} 的起止时间超出 AI 候选证据范围；"
                        "请缩短时间，或使用人工补片添加更大范围"
                    )
                elif error.startswith("missing_must:"):
                    messages.append("仍有必须内容没有进入时间线")
                else:
                    messages.append("当前时间线存在未通过的证据或一致性检查")
            raise ValueError("当前方案还不能生成成片：" + "；".join(dict.fromkeys(messages)))
        approved, gate = self.store.approve_plan(
            self.status.plan_gate.id,
            actor_id,
            approval_exceptions=approval_exceptions,
        )
        self.status.edit_plan = approved
        self.status.plan_gate = gate
        self.status.script = approved.execution_script
        self._write_json(
            "confirmed_edit_plan.json",
            approved.execution_script.model_dump(),
        )
        if self.status.material_set:
            timeline_compiler = (
                getattr(self, "timeline_compiler", None) or TimelineCompiler()
            )
            canonical_timeline = timeline_compiler.compile(
                task_id=self.store.task_id,
                plan=approved,
                material_set=self.status.material_set,
            )
            self.store.write_model(
                f"canonical_timeline_v{canonical_timeline.version}.json",
                canonical_timeline,
            )
            self.store.write_model("canonical_timeline.json", canonical_timeline)
        self._transition(
            "plan_approved",
            f"plan-approved-v{approved.version}",
            actor_id=actor_id,
        )
        self.status.step = self.status.task_snapshot.state
        self._write_manifest(self.status.step, edit_plan_version=str(approved.version))
        return approved

    def export_editor_package(
        self,
        adapter: str,
        output_dir: str = "",
        *,
        render_clips: bool = True,
    ) -> EditorExportResult:
        """从已批准通用时间线导出独立交接包，失败不改变任务状态。"""
        if not self.store or not self.task_dir or not self.status.edit_plan:
            raise ValueError("当前任务没有可导出的剪辑计划")
        plan = self.status.edit_plan
        if plan.status != "approved" or not self.status.material_set:
            raise ValueError("请先批准当前剪辑计划")
        compiler = getattr(self, "timeline_compiler", None) or TimelineCompiler()
        timeline = compiler.compile(
            task_id=self.store.task_id,
            plan=plan,
            material_set=self.status.material_set,
        )
        self.store.write_model("canonical_timeline.json", timeline)
        if not output_dir:
            output_dir = str(
                self.task_dir / "editor_exports" / f"{adapter}_v{plan.version}"
            )
        if adapter == "jianying":
            exporter = JianyingHandoffAdapter(render_clips=render_clips)
        elif adapter == "otio":
            exporter = OTIOAdapter()
        else:
            raise ValueError("当前支持的导出目标为 jianying 或 otio")
        try:
            result = exporter.export(timeline, Path(output_dir))
        except Exception as error:
            result = EditorExportResult(
                adapter=adapter,
                timeline_id=timeline.id,
                timeline_version=timeline.version,
                success=False,
                output_path=str(Path(output_dir).resolve()),
                errors=[f"adapter_exception:{type(error).__name__}:{error}"],
            )
        self.store.write_model(
            f"editor_export_{adapter}_v{plan.version}.json", result
        )
        self.store.append_audit(
            AuditEvent(
                task_id=self.store.task_id,
                action="editor_export_completed" if result.success else "editor_export_failed",
                subject_type="CanonicalTimeline",
                subject_id=timeline.id,
                subject_version=timeline.version,
                summary=(
                    f"系统生成了 {adapter} 编辑器交接结果。"
                    if result.success else f"{adapter} 编辑器交接导出失败，已批准计划未改变。"
                ),
                metadata={"adapter": adapter, "success": result.success},
            )
        )
        return result

    def render_approved_plan(
        self,
        plan: AuditableEditPlan,
        video_path: str,
        output_path: str = "",
    ) -> ExecutionResult:
        render_lock = getattr(self, "_render_lock", None)
        if render_lock is None:
            render_lock = threading.Lock()
            self._render_lock = render_lock
        if not render_lock.acquire(blocking=False):
            raise ValueError("当前任务正在渲染，请勿重复提交")
        idempotency = getattr(self, "idempotency_backend", None)
        key = f"render:{self.store.task_id if self.store else 'unknown'}:{plan.version}"
        if idempotency is not None and not idempotency.acquire(key, 3600):
            render_lock.release()
            raise ValueError("当前计划正在渲染，请勿重复提交")
        try:
            return self._render_approved_plan_locked(plan, video_path, output_path)
        finally:
            if idempotency is not None:
                idempotency.release(key)
            render_lock.release()

    def _render_approved_plan_locked(
        self,
        plan: AuditableEditPlan,
        video_path: str,
        output_path: str = "",
    ) -> ExecutionResult:
        if not self.store or not self.state_machine or not self.task_dir:
            raise ValueError("任务尚未初始化")
        if not self.status.requirement_spec or not self.status.analysis:
            raise ValueError("缺少验收所需的需求或分析结果")
        if plan.status != "approved":
            raise ValueError("未批准计划不能渲染")
        if not self.store.is_approved("edit_plan", plan.id, plan.version):
            raise ValueError("当前计划版本没有有效审核 Gate")
        if self.status.task_snapshot and self.status.task_snapshot.state != "plan_approved":
            raise ValueError(f"当前状态 {self.status.task_snapshot.state} 不能渲染")
        if not output_path:
            output_path = str(self.task_dir / f"final_v{plan.version}.mp4")
        self._transition("render_started", f"render-started-plan-v{plan.version}")
        self._write_manifest("rendering", edit_plan_version=str(plan.version))
        if plan.execution_script.srt_subtitles:
            (self.task_dir / "subtitles.srt").write_text(
                plan.execution_script.srt_subtitles,
                encoding="utf-8",
            )
            (self.task_dir / f"subtitles_v{plan.version}.srt").write_text(
                plan.execution_script.srt_subtitles,
                encoding="utf-8",
            )
        backend = getattr(self, "render_backend", None) or FFmpegRenderBackend(self.agent4)
        try:
            render_sources: str | dict[str, str] = video_path
            if self.status.material_set:
                render_sources = {
                    source.id: source.source_path
                    for source in self.status.material_set.sources
                }
            result = backend.render(plan.execution_script, render_sources, output_path)
        except Exception as error:
            result = ExecutionResult(
                success=False,
                output_path=output_path,
                output_duration=0.0,
                operations_done=0,
                operations_failed=len(plan.execution_script.operations),
                errors=[f"render_backend_exception:{type(error).__name__}:{error}"],
                log="渲染后端抛出未处理异常，Harness 已将任务置为 failed。",
            )
        self.status.result = result
        self._write_json("execution_result.json", result.model_dump())
        self._write_json(f"execution_result_v{plan.version}.json", result.model_dump())
        (self.task_dir / "render.log").write_text(result.log, encoding="utf-8")
        (self.task_dir / f"render_v{plan.version}.log").write_text(result.log, encoding="utf-8")
        self.store.append_audit(
            AuditEvent(
                task_id=self.store.task_id,
                action="render_completed" if result.success else "render_failed",
                subject_type="AuditableEditPlan",
                subject_id=plan.id,
                subject_version=plan.version,
                summary="系统完成了批准计划的渲染。" if result.success else "批准计划渲染失败。",
            )
        )
        if not result.success:
            self._transition(
                "task_failed",
                f"render-failed-plan-v{plan.version}",
                payload={"error_code": "render_failed", "error_message": "；".join(result.errors)},
            )
            self.status.step = "failed"
            self._write_manifest("failed", output_path=result.output_path)
            return result

        self._transition("render_completed", f"render-completed-plan-v{plan.version}")
        self._write_manifest("verifying", output_path=result.output_path)
        verifier = getattr(self, "verification_engine", None) or VerificationEngine()
        try:
            report = verifier.verify(
                task_id=self.store.task_id,
                execution_result=result,
                spec=self.status.requirement_spec,
                plan=plan,
                candidates=self.status.analysis.candidate_clips,
            )
        except Exception as error:
            self._fail_task("verification_failed", error)
            failed_result = result.model_copy(update={
                "success": False,
                "errors": [
                    *result.errors,
                    f"verification_exception:{type(error).__name__}:{error}",
                ],
            })
            self.status.result = failed_result
            self._write_json("execution_result.json", failed_result.model_dump())
            self._write_json(
                f"execution_result_v{plan.version}.json",
                failed_result.model_dump(),
            )
            return failed_result
        self.store.write_model("verification_report.json", report)
        self.store.write_model(f"verification_report_v{plan.version}.json", report)
        self.store.write_model("delivery_report.json", report)
        self.store.write_model(f"delivery_report_v{plan.version}.json", report)
        self.status.delivery_report = report
        self.status.completed_at = datetime.now().isoformat()
        if report.status == "passed":
            self._transition(
                "verification_passed",
                f"verification-passed-plan-v{plan.version}",
            )
        else:
            self._transition(
                "delivery_resolution_required",
                f"delivery-resolution-plan-v{plan.version}",
            )
        self.status.step = self.status.task_snapshot.state
        self._write_manifest(
            self.status.step,
            output_path=result.output_path,
            delivery_report_id=report.id,
        )
        return result

    def resolve_delivery(
        self,
        *,
        actor_id: str,
        exception_reason: str,
    ) -> DeliveryReport:
        if not self.store or not self.status.delivery_report:
            raise ValueError("没有待处理的交付报告")
        verifier = getattr(self, "verification_engine", None) or VerificationEngine()
        approved = verifier.approve_exceptions(
            self.status.delivery_report,
            actor_id=actor_id,
            reason=exception_reason,
        )
        self.store.write_model("delivery_report.json", approved)
        self.store.write_model(
            f"delivery_report_v{approved.edit_plan_version}.json",
            approved,
        )
        self.store.append_audit(
            AuditEvent(
                task_id=self.store.task_id,
                action="delivery_exception_approved",
                subject_type="DeliveryReport",
                subject_id=approved.id,
                actor_type="user",
                actor_id=actor_id,
                summary="用户填写原因并批准了交付例外。",
                metadata={"reason": exception_reason},
            )
        )
        self.status.delivery_report = approved
        self._transition(
            "delivery_approved",
            f"delivery-approved-{approved.id}",
            actor_id=actor_id,
        )
        self.status.step = self.status.task_snapshot.state
        self._write_manifest("succeeded", output_path=approved.output_path)
        return approved

    def confirm_and_render(
        self,
        script: EditScript,
        selected_orders: list[int],
        video_path: str,
        output_path: str = "",
        transition_duration: float = 0.0,
        subtitle_style: str = "classic",
        bgm_path: Optional[str] = None,
        bgm_volume: float = 0.15,
        intro_style: str = "none",
        outro_style: str = "none",
        title_text: str = "",
    ) -> ExecutionResult:
        """兼容 UI 的单按钮操作：修订 → 校验 → 批准 → 渲染。"""
        if self.status.analysis is None or self.status.edit_plan is None:
            raise ValueError("没有可确认的分析结果，请先生成剪辑方案")
        selected_ids = [
            segment.candidate_id
            for segment in self.status.edit_plan.timeline_segments
            if segment.order in set(selected_orders)
        ]
        revised = self.revise_edit_plan(
            selected_candidate_ids=selected_ids,
            delivery_updates={
                "transition_duration": transition_duration,
                "subtitle_style": subtitle_style,
                "bgm_path": bgm_path or None,
                "bgm_volume": bgm_volume,
                "intro_style": intro_style,
                "outro_style": outro_style,
                "title_text": title_text.strip() or script.title,
            },
            actor_id="local-user",
        )
        if not revised.timeline_segments:
            raise ValueError("请至少保留一个片段")
        approved = self.approve_current_plan("local-user")
        return self.render_approved_plan(approved, video_path, output_path)

    def _create_previews(self, video_path: str, script: EditScript) -> dict[str, str]:
        """生成候选片段预览；失败不影响主剪辑流程。"""
        if not self.task_dir:
            return {}
        preview_dir = self.task_dir / "previews"
        preview_dir.mkdir(exist_ok=True)
        previews: dict[str, str] = {}
        for operation in script.operations:
            if operation.action != "cut" or operation.source_start is None or operation.source_end is None:
                continue
            preview_path = preview_dir / f"clip_{operation.order:02d}.mp4"
            if FFmpegTool.create_preview(video_path, operation.source_start, operation.source_end, str(preview_path)):
                previews[str(operation.order)] = str(preview_path.resolve())
        return previews

    def _create_previews_for_sources(self, script: EditScript) -> dict[str, str]:
        """按每个操作的来源素材生成预览；单素材旧任务仍使用兼容路径。"""
        if not self.status.material_set:
            return self._create_previews(self.video_path or "", script)
        source_paths = {
            source.id: source.source_path for source in self.status.material_set.sources
        }
        if not self.task_dir:
            return {}
        preview_dir = self.task_dir / "previews"
        preview_dir.mkdir(exist_ok=True)
        previews: dict[str, str] = {}
        for operation in script.operations:
            if (
                operation.action != "cut"
                or operation.source_start is None
                or operation.source_end is None
            ):
                continue
            source_path = source_paths.get(operation.source_asset_id or "")
            if not source_path:
                continue
            preview_path = preview_dir / f"clip_{operation.order:02d}.mp4"
            if FFmpegTool.create_preview(
                source_path,
                operation.source_start,
                operation.source_end,
                str(preview_path),
            ):
                previews[str(operation.order)] = str(preview_path.resolve())
        return previews

    def create_preview_for_order(self, order: int, context_seconds: float = 0.0) -> str:
        """用户点击候选时才生成单段预览，并复用已经存在的文件。"""
        if not self.task_dir or not self.status.edit_plan:
            raise ValueError("当前没有可预览的剪辑方案")
        segment = next(
            (
                item for item in self.status.edit_plan.timeline_segments
                if item.order == int(order)
            ),
            None,
        )
        if segment is None:
            raise ValueError("找不到这个候选片段")
        source_path = self.video_path or ""
        source_duration = (
            self.status.analysis.source_durations.get(segment.source_asset_id or "", self.status.analysis.video_duration)
            if self.status.analysis else segment.source_end
        )
        if self.status.material_set:
            source = next(
                (
                    item for item in self.status.material_set.sources
                    if item.id == segment.source_asset_id
                ),
                None,
            )
            if source:
                source_path = source.source_path
                source_duration = source.duration
        candidate = next((
            item for item in (self.status.analysis.candidate_clips if self.status.analysis else [])
            if item.id == segment.candidate_id
        ), None)
        base_start = candidate.source_start if candidate else segment.source_start
        base_end = candidate.source_end if candidate else segment.source_end
        start = max(0.0, base_start - context_seconds) if context_seconds else segment.source_start
        end = min(source_duration, base_end + context_seconds) if context_seconds else segment.source_end
        preview_dir = self.task_dir / "previews"
        preview_dir.mkdir(exist_ok=True)
        preview_path = preview_dir / (
            f"clip_{segment.order:02d}_{start:.2f}_{end:.2f}.mp4"
        )
        if not preview_path.exists() and not FFmpegTool.create_preview(
            source_path,
            start,
            end,
            str(preview_path),
        ):
            raise ValueError("片段预览生成失败，请直接在原素材中核对")
        resolved = str(preview_path.resolve())
        self.preview_paths[str(segment.order)] = resolved
        return resolved

    def _transition(
        self,
        event: str,
        idempotency_key: str,
        *,
        actor_id: Optional[str] = None,
        payload: Optional[dict] = None,
    ):
        if not self.state_machine:
            raise ValueError("任务状态机尚未初始化")
        snapshot = self.state_machine.load()
        updated, _, _ = self.state_machine.apply(
            event,
            expected_version=snapshot.version,
            idempotency_key=idempotency_key,
            actor_id=actor_id,
            payload=payload,
        )
        self.status.task_snapshot = updated
        self.status.step = updated.state
        metadata_repository = getattr(self, "metadata_repository", None)
        if metadata_repository is not None and self.store:
            try:
                metadata_repository.upsert(
                    task_id=self.store.task_id,
                    state=updated.state,
                    state_version=updated.version,
                    plan_version=(
                        self.status.edit_plan.version
                        if self.status.edit_plan else None
                    ),
                )
            except Exception as error:
                logger.warning(
                    "PostgreSQL 元数据同步失败，本地状态仍有效: %s", error
                )
        return updated

    def _fail_task(self, error_code: str, error: Exception) -> None:
        """把未处理阶段异常转成唯一业务失败状态，同时保留已有产物。"""
        message = f"{type(error).__name__}: {error}"[:1000]
        if self.state_machine:
            snapshot = self.state_machine.load()
            if snapshot.state not in {"succeeded", "failed"}:
                self._transition(
                    "task_failed",
                    f"task-failed-{error_code}-v{snapshot.version}",
                    payload={"error_code": error_code, "error_message": message},
                )
        if self.store:
            self.store.append_audit(
                AuditEvent(
                    task_id=self.store.task_id,
                    action="task_failed",
                    subject_type="Task",
                    subject_id=self.store.task_id,
                    summary=f"任务因 {error_code} 停止；已完成产物保留用于恢复和排查。",
                    metadata={"error_code": error_code},
                )
            )
        self._write_manifest("failed", error_code=error_code, error_message=message)

    def _write_json(self, filename: str, payload: dict) -> None:
        if self.task_dir:
            destination = self.task_dir / filename
            temporary = destination.with_name(
                f".{destination.name}.{uuid.uuid4().hex}.tmp"
            )
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(destination)

    def _write_manifest(self, state: str, **extra: str) -> None:
        if self.task_dir:
            existing = {}
            destination = self.task_dir / "manifest.json"
            if destination.exists():
                try:
                    existing = json.loads(destination.read_text(encoding="utf-8"))
                except Exception:
                    existing = {}
            self._write_json(
                "manifest.json",
                {
                    **existing,
                    "task_id": self.task_dir.name,
                    "state": state,
                    "updated_at": datetime.now().isoformat(),
                    **extra,
                },
            )

    def run_interactive(self, video_path: str, user_input: str):
        """
        交互式运行——每一步完成后暂停，让用户确认

        适合：用户想看中间结果、手动调整参数
        不适合：全自动场景
        """
        # 实现略（以后可以加）
        pass

    # ============================================================
    # 私有方法：格式化输出
    # ============================================================

    def _print_requirement(self, req: VideoRequirement):
        """打印需求理解结果"""
        print(f"\n{'='*40}")
        print("  [Agent 1] 需求理解完成")
        print(f"  类型: {req.video_type} | 风格: {req.style}")
        print(f"  目标时长: {req.target_duration//60}分{req.target_duration%60}秒")
        print(f"  关键词: {', '.join(req.focus_keywords)}")
        print(f"  字幕: {'是' if req.need_subtitles else '否'} | BGM: {'是' if req.need_bgm else '否'}")

    def _print_analysis(self, analysis: ContentAnalysis):
        """打印内容分析结果"""
        print(f"\n{'='*40}")
        print("  [Agent 2] 内容分析完成")
        print(f"  视频时长: {analysis.video_duration/60:.1f} 分钟")
        print(f"  转录片段: {len(analysis.transcript)} 个")
        print(f"  高光片段: {len(analysis.highlights)} 个")
        print(f"  摘要: {analysis.summary[:80]}...")
        if analysis.highlights:
            print("  Top 3 高光:")
            for i, h in enumerate(analysis.highlights[:3]):
                print(f"    {i+1}. [{h.importance:.2f}] {h.start:.0f}s-{h.end:.0f}s | {h.reason[:30]}")

    def _print_script(self, script: EditScript):
        """打印剪辑脚本摘要"""
        cuts = [op for op in script.operations if op.action == "cut"]
        transitions = [op for op in script.operations if op.action == "transition"]
        print(f"\n{'='*40}")
        print("  [Agent 3] 剪辑脚本生成完成")
        print(f"  标题: {script.title}")
        print(f"  操作: {len(script.operations)} 个 ({len(cuts)} 个裁剪, {len(transitions)} 个转场)")
        print(f"  预估时长: {script.estimated_duration:.0f} 秒")
        if script.notes:
            print(f"  备注: {script.notes[:80]}")

    def _print_result(self, result: ExecutionResult, total_start: float):
        """打印最终结果"""
        total_time = time.time() - total_start
        print(f"\n{'='*60}")
        if result.success:
            print("  剪辑完成！")
            print(f"  成品: {result.output_path}")
            print(f"  时长: {result.output_duration:.0f} 秒")
            print(f"  总耗时: {total_time:.0f} 秒 ({total_time/60:.1f} 分钟)")
            if result.errors:
                print(f"  警告: {len(result.errors)} 个非致命错误")
        else:
            print("  剪辑失败！")
            for err in result.errors:
                print(f"  错误: {err}")
        print(f"{'='*60}\n")

    def _error_result(self, message: str) -> ExecutionResult:
        """生成错误结果"""
        self.status.step = "error"
        self.status.error_message = message
        if self.task_dir:
            self._write_manifest("failed", error=message)
        return ExecutionResult(
            success=False,
            output_path="",
            output_duration=0,
            operations_done=0,
            operations_failed=0,
            errors=[message],
        )


# ============================================================
# 便捷函数：一行调用
# ============================================================

def auto_edit(video_path: str, user_input: str, output_path: str = "") -> ExecutionResult:
    """
    一行代码完成视频自动剪辑

    用法：
        from src.orchestrator import auto_edit
        result = auto_edit("my_video.mp4", "帮我剪成3分钟精华版")

    这是最简洁的调用方式，适合在其他脚本中集成。
    """
    orch = VideoEditOrchestrator()
    return orch.run(video_path, user_input, output_path)


# ============================================================
# 测试代码
# ============================================================
if __name__ == "__main__":
    print("=" * 60)
    print("Orchestrator 测试：完整流程")
    print("=" * 60)

    import sys

    # 检查命令行参数
    if len(sys.argv) < 2:
        print("用法: python orchestrator.py <视频路径> [需求描述]")
        print("示例: python orchestrator.py data/test.mp4 '剪成3分钟精华版'")
        sys.exit(0)

    video_path = sys.argv[1]
    user_input = sys.argv[2] if len(sys.argv) > 2 else "帮我把这个视频剪成3分钟精彩集锦"

    if not Path(video_path).exists():
        print(f"视频文件不存在: {video_path}")
        sys.exit(1)

    orch = VideoEditOrchestrator()
    result = orch.run(video_path, user_input)

    if result.success:
        print(f"\n成品视频: {result.output_path}")
    else:
        print(f"\n剪辑失败: {result.errors}")
