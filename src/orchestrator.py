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
from src.agents.analysis_agent import AnalysisAgent
from src.agents.candidate_agent import CandidateAgent
from src.agents.style_agent import StyleRecommendationAgent
from src.agents.script_agent import ScriptAgent
from src.agents.executor_agent import ExecutorAgent
from src.config import OUTPUT_DIR, WHISPER_MODEL_SIZE
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
    PipelineStatus,
    RequirementBrief,
    RequirementCompilation,
    RequirementItem,
    RequirementSpec,
    HighlightClip,
    DeliveryReport,
    MaterialAlignmentDecision,
    MaterialAlignmentProposal,
    PlanApprovalException,
)
from src.services.plans import EditPlanService
from src.services.rendering import FFmpegRenderBackend
from src.services.requirements import RequirementClarificationService
from src.services.state_machine import TaskStateMachine
from src.services.task_store import TaskStore
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
        self.agent2 = AnalysisAgent(whisper_model_size=WHISPER_MODEL_SIZE)
        self.candidate_agent = CandidateAgent()
        self.style_agent = StyleRecommendationAgent()
        self.agent3 = ScriptAgent()
        self.agent4 = ExecutorAgent()
        self.plan_service = EditPlanService()
        self.clarification_service = RequirementClarificationService()
        self.verification_engine = VerificationEngine()
        self.render_backend = FFmpegRenderBackend(self.agent4)

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
                "agent1", "clarification_agent", "alignment_agent", "agent2", "candidate_agent", "style_agent", "agent3", "agent4",
                "plan_service", "clarification_service", "verification_engine", "render_backend",
            ):
                value = getattr(runtime, name, None)
                if name == "clarification_agent" and value is None:
                    value = RequirementClarificationAgent()
                if name == "alignment_agent" and value is None:
                    value = RequirementAlignmentAgent()
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
        orchestrator._render_lock = threading.Lock()

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
        video_path: str,
        compilation: RequirementCompilation,
    ) -> RequirementCompilation:
        """在任务书确认前理解素材，并缓存转录供正式候选分析复用。"""
        alignment_agent = getattr(self, "alignment_agent", None)
        analysis_agent = getattr(self, "agent2", None)
        if alignment_agent is None or analysis_agent is None or not self.store:
            return compilation
        try:
            with model_call_context(
                self.task_dir,
                stage="material_understanding",
                prompt_template_version="material-summary-v1",
                input_spec_id=compilation.spec.id,
                input_spec_version=compilation.spec.version,
            ):
                analysis = analysis_agent.run(video_path, compilation.legacy_requirement)
            self.store.write_model("material_analysis.json", analysis)
            self.store.write_payload(
                "transcript.json",
                {"segments": [item.model_dump(mode="json") for item in analysis.transcript]},
            )
            self.store.write_payload(
                "evidence.json",
                {"evidence": [item.model_dump(mode="json") for item in analysis.evidence]},
            )
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

    def create_requirement_draft(
        self,
        video_path: str | list[str],
        user_input: str,
        scenario: str = "school",
        overrides: Optional[dict] = None,
        submitted_by: Optional[str] = None,
    ) -> RequirementCompilation:
        """创建任务书，并预先理解素材以生成可审核的需求对齐建议。"""
        self.status = PipelineStatus(step="created", started_at=datetime.now().isoformat())
        self.task_dir = OUTPUT_DIR / "tasks" / uuid.uuid4().hex
        self.task_dir.mkdir(parents=True, exist_ok=False)
        self.store = TaskStore(self.task_dir)
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

        source_entries = []
        timeline_offset = 0.0
        for index, source_path in enumerate(source_paths, start=1):
            source_info = FFmpegTool.get_video_info(source_path)
            if not source_info:
                reject_preflight(f"第 {index} 段素材无法读取，请重新上传")
            duration = float(source_info.get("duration", 0))
            if duration <= 0:
                reject_preflight(f"第 {index} 段素材时长无效，请重新上传")
            source_entries.append({
                "order": index,
                "source_path": source_path,
                "timeline_start": timeline_offset,
                "timeline_end": timeline_offset + duration,
                "duration": duration,
                "has_audio": bool(source_info.get("has_audio")),
            })
            timeline_offset += duration

        if not any(item["has_audio"] for item in source_entries):
            reject_preflight("视频预检失败：上传的素材都没有可用音频流")

        if len(source_paths) > 1:
            merged_path = self.task_dir / "merged_source.mp4"
            if not FFmpegTool.concatenate_source_videos(source_paths, str(merged_path)):
                reject_preflight("多段素材合并失败，请检查素材格式后重试")
            self.video_path = str(merged_path.resolve())
        else:
            self.video_path = source_paths[0]

        self._write_json(
            "source_media.json",
            {
                "merge_strategy": "upload_order",
                "merged_video_path": self.video_path,
                "sources": source_entries,
            },
        )
        media_info = FFmpegTool.get_video_info(self.video_path)
        if not media_info or not media_info.get("has_audio"):
            reject_preflight("视频预检失败：需要可解码的视频和音频流")
        self._write_json("media_info.json", media_info)
        self._transition("preflight_passed", "preflight-passed")
        self._write_manifest(
            "preflight_ok",
            video_path=self.video_path,
            source_count=len(source_paths),
        )

        try:
            with model_call_context(
                self.task_dir,
                stage="requirement_compilation",
                prompt_template_version="requirement-compiler-v1",
            ):
                compilation = self.agent1.compile(
                    user_input,
                    scenario=scenario,
                    submitted_by=submitted_by,
                    overrides=overrides,
                )
            compilation = self._analyze_material_alignment(self.video_path, compilation)
            proposal = compilation.alignment_proposal
            # 有明显模糊或错位时，先让用户决定是否采用素材驱动建议；决定后再动态追问。
            if proposal is None or not proposal.requires_user_decision:
                compilation = self._generate_clarification_questions(compilation)
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
        proposals = []
        style_agent = getattr(self, "style_agent", None)
        if style_agent:
            brief = self.store.read_model("requirement_brief.json", RequirementBrief)
            with model_call_context(
                self.task_dir,
                stage="style_recommendation",
                prompt_template_version="style-recommender-v1",
                input_spec_id=spec.id,
                input_spec_version=spec.version,
            ):
                proposals = style_agent.run(spec, brief.scenario)
        self.status.style_proposals = proposals
        if proposals:
            self.store.write_payload(
                f"style_proposals_v{spec.version}.json",
                {"proposals": [item.model_dump(mode="json") for item in proposals]},
            )
            self.store.append_audit(
                AuditEvent(
                    task_id=self.store.task_id,
                    action="style_recommended",
                    subject_type="RequirementSpec",
                    subject_id=spec.id,
                    subject_version=spec.version,
                    summary=f"系统从受控样式库推荐了 {len(proposals)} 组方案。",
                )
            )
            self._transition("style_recommended", f"style-recommended-v{spec.version}")
        else:
            self._transition("style_skipped", f"style-skipped-v{spec.version}")
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
                prompt_template_version="candidate-tools-v2-batched",
                input_spec_id=spec.id,
                input_spec_version=spec.version,
            ):
                generation = self.candidate_agent.run(
                    self.status.execution_brief,
                    spec.requirements,
                    analysis.evidence,
                    analysis.video_duration,
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
                "rejected_candidate_ids": [item.id for item in rejected_candidates],
            },
        )
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
        try:
            script = self.agent3.run(
                analysis,
                requirement,
                spec.requirements,
                duration_tolerance=spec.duration_tolerance,
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
            plan_gate = self.store.save_plan_draft(plan)
        except Exception as error:
            self._fail_task("planning_failed", error)
            raise
        self.status.edit_plan = plan
        self.status.plan_gate = plan_gate
        self.store.write_model("edit_plan.json", script)
        if generate_previews:
            self.preview_paths = self._create_previews(video_path, script)
        self._transition("review_ready", f"review-ready-plan-v{plan.version}")
        self.status.step = self.status.task_snapshot.state
        self._write_manifest(
            self.status.step,
            requirement_version=str(spec.version),
            edit_plan_version=str(plan.version),
        )
        return script

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
                    f"{self.store.task_id}|{start:.3f}|{end:.3f}|{annotation}"
                ).encode("utf-8")
                digest = hashlib.sha256(fingerprint).hexdigest()
                evidence_id = f"user_evidence_{digest[:16]}"
                if evidence_id not in evidence_ids:
                    evidence.append(
                        Evidence(
                            id=evidence_id,
                            type="user_annotation",
                            source_start=start,
                            source_end=end,
                            content=annotation,
                            content_hash=digest,
                            metadata={
                                "actor_id": actor_id,
                                "source": "manual_timeline_add",
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
            self.status.analysis.video_duration,
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
        self._transition(
            "plan_approved",
            f"plan-approved-v{approved.version}",
            actor_id=actor_id,
        )
        self.status.step = self.status.task_snapshot.state
        self._write_manifest(self.status.step, edit_plan_version=str(approved.version))
        return approved

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
        try:
            return self._render_approved_plan_locked(plan, video_path, output_path)
        finally:
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
            result = backend.render(plan.execution_script, video_path, output_path)
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
