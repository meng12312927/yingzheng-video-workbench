import subprocess
from pathlib import Path

import pytest

from src.agents.script_agent import ScriptAgent
from src.agents.executor_agent import ExecutorAgent
from src.agents.analysis_agent import AnalysisAgent
from src.agents.candidate_agent import CandidateAgent
from src.agents.requirement_agent import RequirementAgent
from src.agents.style_agent import StyleRecommendationAgent
from src.models.schemas import (
    ContentAnalysis,
    CandidateClip,
    EditOperation,
    EditScript,
    EvidenceCitation,
    ExecutionBrief,
    HighlightClip,
    PipelineStatus,
    RequirementBrief,
    RequirementCompilation,
    RequirementItem,
    RequirementSlot,
    RequirementSpec,
    TranscriptSegment,
    VideoRequirement,
    AuditableEditPlan,
    DeliverySpec,
    ExecutionResult,
)
from src.services.task_store import TaskStore
from src.services.requirements import RequirementClarificationService
from src.services.state_machine import (
    IdempotencyConflictError,
    InvalidTransitionError,
    StateVersionConflictError,
    TaskStateMachine,
)
from src.services.evidence import (
    EvidenceBuilder,
    EvidenceValidator,
    HybridEvidenceRetriever,
    LexicalEvidenceRetriever,
)
from src.services.plans import EditPlanService
from src.services.verification import VerificationEngine
from src.orchestrator import VideoEditOrchestrator
from src.tools.ffmpeg import FFmpegTool


def test_requirement_compiler_creates_visible_execution_instruction(monkeypatch):
    agent = RequirementAgent()
    parsed = VideoRequirement(
        target_duration=180,
        video_type="sports",
        style="exciting",
        focus_keywords=["开幕", "冲刺", "颁奖"],
        need_subtitles=True,
    )
    monkeypatch.setattr(agent, "run", lambda _: parsed)

    compilation = agent.compile(
        "把学校运动会剪成 3 分钟回顾，必须包含颁奖，发布到公众号。",
        scenario="school",
        submitted_by="teacher-1",
    )

    assert compilation.spec.brief_id == compilation.brief.id
    assert compilation.spec.version == 1
    assert any(item.priority == "must" and "颁奖" in item.description for item in compilation.spec.requirements)
    assert "目标成片时长 180 秒" in compilation.execution_brief.visible_instruction
    assert "颁奖" in compilation.execution_brief.visible_instruction
    assert len(compilation.spec.open_questions) <= 5
    assert {slot.key for slot in compilation.slots} >= {
        "target_duration", "focus_keywords", "publish_channel", "high_risk_review"
    }
    assert next(slot for slot in compilation.slots if slot.key == "target_duration").source_type == "llm"


def test_multi_turn_clarification_updates_only_answered_slots_and_protects_confirmed_values():
    spec = RequirementSpec(
        brief_id="brief-1",
        target_duration=180,
        open_questions=["发布到哪里？", "重点内容是什么？"],
    )
    execution = ExecutionBrief(
        requirement_spec_id=spec.id,
        requirement_spec_version=1,
        visible_instruction="制作活动回顾。",
    )
    slots = [
        RequirementSlot(
            key="target_duration",
            label="目标时长",
            value=180,
            status="confirmed",
            source_type="form",
        ),
        RequirementSlot(
            key="publish_channel",
            label="发布渠道",
            status="missing",
            source_type="llm",
            question="发布到哪里？",
        ),
        RequirementSlot(
            key="focus_keywords",
            label="重点内容",
            status="missing",
            source_type="llm",
            question="重点内容是什么？",
        ),
    ]
    service = RequirementClarificationService()

    second, second_execution, second_slots, first_turn = service.apply(
        task_id="task-1",
        spec=spec,
        execution=execution,
        slots=slots,
        answers={"publish_channel": "公众号"},
        actor_id="teacher-1",
    )

    assert second.version == 2
    assert second_execution.version == 2
    assert second.open_questions == ["重点内容是什么？"]
    assert first_turn.answers == {"publish_channel": "公众号"}
    assert next(slot for slot in second_slots if slot.key == "publish_channel").status == "confirmed"

    third, _, third_slots, _ = service.apply(
        task_id="task-1",
        spec=second,
        execution=second_execution,
        slots=second_slots,
        answers={"focus_keywords": "开幕，颁奖"},
    )
    assert third.version == 3
    assert third.open_questions == []
    assert len([item for item in third.requirements if item.category == "content"]) == 2

    conflicted, _, conflict_slots, conflict_turn = service.apply(
        task_id="task-1",
        spec=third,
        execution=second_execution.model_copy(
            update={"version": 3, "requirement_spec_version": 3}
        ),
        slots=third_slots,
        answers={"target_duration": "240秒"},
    )
    assert conflicted.target_duration == 180
    assert conflict_turn.answers == {}
    target_slot = next(slot for slot in conflict_slots if slot.key == "target_duration")
    assert target_slot.status == "conflict"
    assert "已有确认值" in target_slot.question


def test_task_state_machine_rejects_illegal_transitions_and_recovers(tmp_path: Path):
    machine = TaskStateMachine(tmp_path / "task-state")
    initial = machine.initialise()
    assert initial.state == "created"
    assert initial.version == 1

    with pytest.raises(InvalidTransitionError):
        machine.apply(
            "requirement_drafted",
            expected_version=1,
            idempotency_key="draft-too-early",
        )

    preflight, first_event, duplicate = machine.apply(
        "preflight_passed",
        expected_version=1,
        idempotency_key="preflight-1",
    )
    assert preflight.state == "preflight_ok"
    assert not duplicate

    same_snapshot, same_event, duplicate = machine.apply(
        "preflight_passed",
        expected_version=1,
        idempotency_key="preflight-1",
    )
    assert duplicate
    assert same_event.id == first_event.id
    assert same_snapshot.version == preflight.version

    with pytest.raises(IdempotencyConflictError):
        machine.apply(
            "requirement_drafted",
            expected_version=preflight.version,
            idempotency_key="preflight-1",
        )
    with pytest.raises(StateVersionConflictError):
        machine.apply(
            "requirement_drafted",
            expected_version=1,
            idempotency_key="draft-stale",
        )

    draft, _, _ = machine.apply(
        "requirement_drafted",
        expected_version=preflight.version,
        idempotency_key="draft-1",
    )
    recovered = TaskStateMachine(tmp_path / "task-state").load()
    assert recovered.state == "requirement_draft"
    assert recovered.version == draft.version
    assert len(machine.events()) == 2

    # 模拟“事件已追加、快照尚未来得及替换”的进程中断；事件日志可重建快照。
    machine.snapshot_path.write_text(initial.model_dump_json(indent=2), encoding="utf-8")
    replayed = TaskStateMachine(tmp_path / "task-state").load()
    assert replayed.state == "requirement_draft"
    assert replayed.version == draft.version


def test_requirement_compiler_failure_creates_visible_manual_draft(monkeypatch):
    def fail_llm(*args, **kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr("src.agents.requirement_agent.call_llm", fail_llm)
    agent = RequirementAgent()

    try:
        agent.run("制作企业年会回顾")
        assert False, "解析失败时不应返回默认正式需求"
    except RuntimeError as error:
        assert "没有生成默认正式需求" in str(error)

    compilation = agent.compile(
        "制作企业年会回顾，重点内容稍后人工填写",
        scenario="enterprise",
        submitted_by="operator-1",
        overrides={
            "target_duration": 120,
            "style": "formal",
            "focus_keywords": [],
            "need_subtitles": False,
        },
    )

    assert compilation.mode == "manual_required"
    assert compilation.brief.raw_text == "制作企业年会回顾，重点内容稍后人工填写"
    assert compilation.legacy_requirement.target_duration == 120
    assert compilation.legacy_requirement.focus_keywords == []
    assert not compilation.legacy_requirement.need_subtitles
    assert compilation.warnings
    assert "请核对目标时长" in compilation.spec.open_questions[0]
    assert compilation.execution_brief.visible_instruction.startswith("注意：AI 需求解析失败")


def test_requirement_item_requires_acceptance_rule_for_must_and_prohibited():
    try:
        RequirementItem(description="必须包含颁奖", priority="must")
        assert False, "must 项缺少验收规则应被拒绝"
    except ValueError:
        pass


def test_task_store_confirms_versioned_requirement_and_writes_audit(tmp_path: Path):
    store = TaskStore(tmp_path / "task-001")
    brief = RequirementBrief(raw_text="剪成活动回顾", scenario="school")
    spec = RequirementSpec(
        brief_id=brief.id,
        target_duration=180,
        open_questions=[],
        requirements=[
            RequirementItem(
                description="必须包含颁奖",
                priority="must",
                acceptance_rule="成片有已确认的颁奖片段",
            )
        ],
    )
    execution = ExecutionBrief(
        requirement_spec_id=spec.id,
        requirement_spec_version=spec.version,
        visible_instruction="成片必须包含颁奖。",
        included_requirement_ids=[spec.requirements[0].id],
    )

    gate = store.save_requirement_draft(brief, spec, execution)
    confirmed_spec, confirmed_execution, resolved_gate = store.confirm_requirement(gate.id, "teacher-1")

    assert resolved_gate.status == "approved"
    assert store.is_approved("requirement", spec.id, 1)
    assert confirmed_spec.status == "confirmed"
    assert confirmed_execution.status == "confirmed"
    assert (store.task_dir / "requirement_brief.json").exists()
    assert (store.task_dir / "requirement_spec_v1.json").exists()
    assert (store.task_dir / "execution_brief_v1.json").exists()
    audit_lines = (store.task_dir / "audit_events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(audit_lines) >= 3


def test_task_store_audits_manual_requirement_fallback(tmp_path: Path):
    store = TaskStore(tmp_path / "task-manual")
    brief = RequirementBrief(raw_text="制作活动回顾")
    spec = RequirementSpec(brief_id=brief.id, target_duration=180)
    execution = ExecutionBrief(
        requirement_spec_id=spec.id,
        requirement_spec_version=1,
        visible_instruction="等待人工核对的执行说明",
    )

    store.save_requirement_draft(
        brief,
        spec,
        execution,
        generation_mode="manual_required",
    )

    audit_text = (store.task_dir / "audit_events.jsonl").read_text(encoding="utf-8")
    assert "AI 需求解析失败" in audit_text
    assert '"generation_mode":"manual_required"' in audit_text


def test_style_recommendation_only_accepts_catalog_bundle_ids():
    spec = RequirementSpec(
        brief_id="brief-style",
        target_duration=180,
        status="confirmed",
    )

    def fake_llm(**_):
        return {
            "proposals": [
                {"bundle_id": "invented_effect", "rationale": "模型编造", "confidence": 1.0},
                {"bundle_id": "bundle_formal_clean", "rationale": "适合正式受众", "confidence": 0.9},
            ]
        }

    proposals = StyleRecommendationAgent(llm_runner=fake_llm).run(spec, "school")

    assert [proposal.bundle_id for proposal in proposals] == ["bundle_formal_clean"]
    assert proposals[0].source == "llm"


def test_auditable_plan_revision_invalidates_missing_must_and_supersedes_gate(tmp_path: Path):
    must = RequirementItem(
        description="必须包含颁奖",
        priority="must",
        acceptance_rule="成片包含颁奖候选",
    )
    spec = RequirementSpec(
        brief_id="brief-plan",
        target_duration=10,
        duration_tolerance=5,
        requirements=[must],
        status="confirmed",
    )
    evidence = EvidenceBuilder.from_transcript(
        [TranscriptSegment(start=0, end=5, text="下面进行颁奖")]
    )
    candidate = CandidateClip(
        source_start=0,
        source_end=5,
        matched_requirement_ids=[must.id],
        citations=[EvidenceCitation(
            requirement_id=must.id,
            evidence_id=evidence[0].id,
            quote="进行颁奖",
        )],
        selection_reason="颁奖证据",
        confidence=0.9,
        suggested_duration=5,
    )
    analysis = ContentAnalysis(
        video_duration=20,
        transcript=[TranscriptSegment(start=0, end=5, text="下面进行颁奖")],
        evidence=evidence,
        candidate_clips=[candidate],
    )
    script = EditScript(
        estimated_duration=5,
        operations=[EditOperation(order=1, action="cut", source_start=0, source_end=5)],
    )
    service = EditPlanService()
    plan = service.create(spec=spec, analysis=analysis, script=script)
    assert service.validate(plan, spec, [candidate], 20).valid

    store = TaskStore(tmp_path / "task-plan")
    gate = store.save_plan_draft(plan)
    approved, _ = store.approve_plan(gate.id, "reviewer")
    revised, decisions = service.revise(
        task_id=store.task_id,
        plan=approved,
        analysis=analysis,
        selected_candidate_ids=[],
        actor_id="reviewer",
    )
    new_gate = store.save_plan_draft(revised, "reviewer", approved)
    store.append_decisions(decisions)

    validation = service.validate(revised, spec, [candidate], 20)
    assert not validation.valid
    assert validation.missing_must_requirement_ids == (must.id,)
    assert not store.is_approved("edit_plan", plan.id, 1)
    assert new_gate.status == "pending"
    assert decisions[0].action == "delete"


def test_verification_report_requires_resolution_for_deterministic_failure(tmp_path: Path, monkeypatch):
    output = tmp_path / "final.mp4"
    output.write_bytes(b"video")
    must = RequirementItem(
        description="必须包含颁奖",
        priority="must",
        acceptance_rule="成片包含颁奖",
    )
    spec = RequirementSpec(
        brief_id="brief-verify",
        target_duration=30,
        duration_tolerance=2,
        requirements=[must],
        status="confirmed",
    )
    delivery = DeliverySpec(
        requirement_spec_id=spec.id,
        requirement_spec_version=1,
        target_duration=30,
        duration_tolerance=2,
        need_subtitles=False,
    )
    script = EditScript(estimated_duration=5, operations=[])
    plan = AuditableEditPlan(
        requirement_spec_id=spec.id,
        requirement_spec_version=1,
        delivery_spec=delivery,
        execution_script=script,
        estimated_duration=5,
        status="approved",
    )
    result = ExecutionResult(
        success=True,
        output_path=str(output),
        output_duration=5,
        operations_done=1,
        operations_failed=0,
    )
    monkeypatch.setattr(
        "src.services.verification.FFmpegTool.get_video_info",
        lambda _: {"duration": 5.0, "has_audio": True},
    )

    report = VerificationEngine().verify(
        task_id="task-verify",
        execution_result=result,
        spec=spec,
        plan=plan,
        candidates=[],
    )

    assert report.status == "needs_resolution"
    assert any(item.code == "duration_out_of_range" for item in report.results)
    assert any(item.code == "requirement_must_failed" for item in report.results)
    with pytest.raises(ValueError):
        VerificationEngine.approve_exceptions(report, actor_id="leader", reason="")
    approved = VerificationEngine.approve_exceptions(
        report,
        actor_id="leader",
        reason="活动负责人确认接受短版",
    )
    assert approved.status == "approved_with_exceptions"


def test_requirement_revision_supersedes_approved_gate(tmp_path: Path):
    store = TaskStore(tmp_path / "task-002")
    brief = RequirementBrief(raw_text="剪成活动回顾")
    first = RequirementSpec(brief_id=brief.id, target_duration=180, open_questions=[])
    first_execution = ExecutionBrief(
        requirement_spec_id=first.id,
        requirement_spec_version=1,
        visible_instruction="第一版执行说明",
    )
    first_gate = store.save_requirement_draft(brief, first, first_execution)
    confirmed_first, _, _ = store.confirm_requirement(first_gate.id, "teacher-1")
    second = confirmed_first.model_copy(
        update={"version": 2, "status": "draft", "confirmed_at": None, "target_duration": 240}
    )
    second_execution = first_execution.model_copy(
        update={"version": 2, "requirement_spec_version": 2, "visible_instruction": "第二版执行说明"}
    )

    second_gate = store.save_requirement_revision(brief, confirmed_first, second, second_execution, "teacher-1")

    assert not store.is_approved("requirement", first.id, 1)
    assert second_gate.status == "pending"
    assert store.read_model("requirement_spec_v1.json", RequirementSpec).status == "superseded"


def test_evidence_builder_retrieval_and_citation_validation():
    transcript = [
        TranscriptSegment(start=10, end=14, text="现在进行一等奖颁奖仪式"),
        TranscriptSegment(start=30, end=34, text="接下来是短跑冲刺"),
    ]
    evidence = EvidenceBuilder.from_transcript(transcript)
    requirement = RequirementItem(
        description="优先保留与“颁奖”相关的有效画面或发言",
        priority="must",
        acceptance_rule="成片有颁奖片段",
    )

    matches = LexicalEvidenceRetriever().search(requirement.description, evidence)
    assert matches[0][0].content == "现在进行一等奖颁奖仪式"
    candidate = CandidateClip(
        source_start=9,
        source_end=15,
        matched_requirement_ids=[requirement.id],
        citations=[
            EvidenceCitation(
                requirement_id=requirement.id,
                evidence_id=matches[0][0].id,
                quote="一等奖颁奖",
                retrieval_score=matches[0][1],
            )
        ],
        selection_reason="转录明确提到一等奖颁奖",
        confidence=0.9,
        suggested_duration=6,
    )

    validator = EvidenceValidator()
    assert validator.validate(candidate, [requirement], evidence, video_duration=60).valid
    invalid = candidate.model_copy(
        update={"citations": [candidate.citations[0].model_copy(update={"quote": "冠军领奖"})]}
    )
    invalid_result = validator.validate(invalid, [requirement], evidence, video_duration=60)
    assert not invalid_result.valid
    assert "quote_not_in_evidence:" + matches[0][0].id in invalid_result.errors


def test_evidence_builder_merges_adjacent_asr_and_keeps_stable_provenance():
    first_transcript = [
        TranscriptSegment(start=0, end=2, text="下面进行", speaker_id=0),
        TranscriptSegment(start=2.4, end=4, text="一等奖，颁奖。", speaker_id=0),
        TranscriptSegment(start=4.2, end=6, text="获奖感言", speaker_id=1),
    ]
    second_transcript = [
        TranscriptSegment(start=0, end=2, text="下面进行", speaker_id=0),
        TranscriptSegment(start=2.4, end=4, text="一等奖，颁奖。", speaker_id=0),
        TranscriptSegment(start=4.2, end=6, text="获奖感言", speaker_id=1),
    ]

    first = EvidenceBuilder.from_transcript(first_transcript)
    second = EvidenceBuilder.from_transcript(second_transcript)

    assert len(first) == 2
    assert first[0].content == "下面进行 一等奖，颁奖。"
    assert first[0].segment_ids == [first_transcript[0].id, first_transcript[1].id]
    assert first[0].metadata["segment_count"] == 2
    assert first[0].id == second[0].id
    assert first[0].content_hash == second[0].content_hash


def test_evidence_quote_normalisation_tolerates_punctuation_but_not_entity_changes():
    evidence = EvidenceBuilder.from_transcript(
        [TranscriptSegment(start=5, end=9, text="下面进行“一等奖”颁奖。")]
    )
    requirement = RequirementItem(
        description="必须包含一等奖颁奖",
        priority="must",
        acceptance_rule="候选引用一等奖原文",
    )
    candidate = CandidateClip(
        source_start=5,
        source_end=9,
        matched_requirement_ids=[requirement.id],
        citations=[EvidenceCitation(
            requirement_id=requirement.id,
            evidence_id=evidence[0].id,
            quote="下面进行 一等奖，颁奖",
        )],
        selection_reason="一等奖颁奖原文",
        confidence=0.9,
        suggested_duration=4,
    )
    validator = EvidenceValidator()
    assert validator.validate(candidate, [requirement], evidence, 20).valid

    changed_entity = candidate.model_copy(
        update={"citations": [candidate.citations[0].model_copy(update={"quote": "下面进行二等奖颁奖"})]}
    )
    result = validator.validate(changed_entity, [requirement], evidence, 20)
    assert not result.valid
    assert f"quote_not_in_evidence:{evidence[0].id}" in result.errors


def test_orchestrator_requires_requirement_approval_before_analysis(tmp_path: Path, monkeypatch):
    requirement = VideoRequirement(target_duration=180, video_type="general", style="formal")
    brief = RequirementBrief(raw_text="制作活动回顾")
    spec = RequirementSpec(brief_id=brief.id, target_duration=180, open_questions=[])
    execution = ExecutionBrief(
        requirement_spec_id=spec.id,
        requirement_spec_version=1,
        visible_instruction="制作活动回顾。",
    )
    compilation = RequirementCompilation(
        brief=brief,
        spec=spec,
        execution_brief=execution,
        legacy_requirement=requirement,
    )

    class FakeRequirementAgent:
        def compile(self, *args, **kwargs):
            return compilation

    orchestrator = VideoEditOrchestrator.__new__(VideoEditOrchestrator)
    orchestrator.agent1 = FakeRequirementAgent()
    orchestrator.preview_paths = {}
    orchestrator.task_dir = None
    orchestrator.store = None
    monkeypatch.setattr("src.orchestrator.OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(
        "src.orchestrator.FFmpegTool.get_video_info",
        lambda _: {"duration": 60.0, "has_audio": True},
    )

    created = orchestrator.create_requirement_draft("input.mp4", "制作活动回顾")

    assert created.spec.id == spec.id
    assert orchestrator.status.step == "requirement_draft"
    assert not (orchestrator.task_dir / "analysis.json").exists()
    try:
        orchestrator.analyze_confirmed_requirement("input.mp4")
        assert False, "未确认需求不应进入分析"
    except ValueError as error:
        assert "尚未确认" in str(error)

    confirmed = orchestrator.confirm_requirement_draft(orchestrator.status.requirement_gate.id, "teacher-1")
    assert confirmed.status == "confirmed"


def test_candidate_agent_can_only_use_tool_retrieved_evidence():
    requirement = RequirementItem(
        description="必须包含与“颁奖”相关的内容",
        priority="must",
        acceptance_rule="候选引用颁奖证据",
    )
    evidence = EvidenceBuilder.from_transcript(
        [TranscriptSegment(start=20, end=24, text="下面开始颁奖仪式")]
    )
    execution = ExecutionBrief(
        requirement_spec_id="spec-1",
        requirement_spec_version=1,
        visible_instruction="必须包含颁奖。",
        included_requirement_ids=[requirement.id],
        status="confirmed",
    )

    def fake_tool_calling_runner(**kwargs):
        result = kwargs["tool_handlers"]["search_evidence"](
            requirement_id=requirement.id,
            query="颁奖",
            top_k=3,
        )
        item = result["evidence"][0]
        return {
            "candidates": [{
                "source_start": item["source_start"],
                "source_end": item["source_end"],
                "matched_requirement_ids": [requirement.id],
                "citations": [{
                    "requirement_id": requirement.id,
                    "evidence_id": item["evidence_id"],
                    "quote": "开始颁奖仪式",
                    "relation": "direct",
                    "retrieval_score": item["retrieval_score"],
                }],
                "selection_reason": "原文明确出现颁奖仪式",
                "confidence": 0.9,
                "suggested_duration": 4,
                "risk_flags": [],
            }]
        }

    result = CandidateAgent(
        llm_runner=fake_tool_calling_runner,
        retriever=HybridEvidenceRetriever(),
    ).run(
        execution,
        [requirement],
        evidence,
        video_duration=60,
    )

    assert len(result.valid_candidates) == 1
    assert result.retrieval_trace[requirement.id] == [evidence[0].id]


def test_hybrid_retrieval_uses_dense_semantics_when_words_do_not_match():
    evidence = EvidenceBuilder.from_transcript([
        TranscriptSegment(start=0, end=3, text="运动员冲过终点"),
        TranscriptSegment(start=10, end=14, text="校长发表讲话"),
    ])

    def fake_embeddings(texts):
        vectors = []
        for text in texts:
            if "领导致辞" in text or "校长发表讲话" in text:
                vectors.append([0.0, 1.0])
            else:
                vectors.append([1.0, 0.0])
        return vectors

    matches = HybridEvidenceRetriever(embedding_fn=fake_embeddings).search("领导致辞", evidence, top_k=1)

    assert matches[0][0].content == "校长发表讲话"


def test_candidate_rejects_existing_evidence_not_returned_by_tool():
    requirement = RequirementItem(description="保留颁奖", priority="should")
    evidence = EvidenceBuilder.from_transcript(
        [TranscriptSegment(start=20, end=24, text="下面开始颁奖仪式")]
    )
    execution = ExecutionBrief(
        requirement_spec_id="spec-2",
        requirement_spec_version=1,
        visible_instruction="保留颁奖。",
        included_requirement_ids=[requirement.id],
        status="confirmed",
    )

    def runner_without_tool_call(**_):
        return {"candidates": [{
            "source_start": 20,
            "source_end": 24,
            "matched_requirement_ids": [requirement.id],
            "citations": [{
                "requirement_id": requirement.id,
                "evidence_id": evidence[0].id,
                "quote": evidence[0].content,
                "relation": "direct",
                "retrieval_score": 1.0,
            }],
            "selection_reason": "没有调用工具却猜中了证据 ID",
            "confidence": 0.9,
            "suggested_duration": 4,
            "risk_flags": [],
        }]}

    result = CandidateAgent(
        llm_runner=runner_without_tool_call,
        retriever=HybridEvidenceRetriever(),
    ).run(execution, [requirement], evidence, video_duration=60)

    assert not result.valid_candidates
    assert f"evidence_not_retrieved:{evidence[0].id}" in result.candidates[0].risk_flags


def test_script_plan_remaps_subtitles_to_output_timeline():
    analysis = ContentAnalysis(
        video_duration=120,
        transcript=[
            TranscriptSegment(start=10, end=14, text="第一段内容"),
            TranscriptSegment(start=60, end=66, text="第二段内容"),
        ],
        highlights=[
            HighlightClip(start=10, end=20, text="第一段", importance=0.9, category="highlight", reason="开场"),
            HighlightClip(start=60, end=70, text="第二段", importance=0.8, category="highlight", reason="高潮"),
        ],
    )
    requirement = VideoRequirement(target_duration=30, video_type="general", style="formal")

    script = ScriptAgent().run(analysis, requirement)

    assert script.estimated_duration == 20
    assert "00:00:00,000 --> 00:00:04,000" in script.srt_subtitles
    assert "00:00:10,000 --> 00:00:16,000" in script.srt_subtitles

    confirmed = ScriptAgent().apply_selection(script, analysis, [2], correct_subtitles=False)
    assert confirmed.estimated_duration == 10
    assert "00:00:00,000 --> 00:00:06,000" in confirmed.srt_subtitles


def test_script_selection_keeps_must_clip_even_when_regular_budget_would_drop_it():
    must = RequirementItem(
        description="必须包含颁奖",
        priority="must",
        acceptance_rule="成片包含颁奖候选",
    )
    analysis = ContentAnalysis(
        video_duration=100,
        highlights=[
            HighlightClip(
                start=0,
                end=25,
                text="普通高分",
                importance=0.99,
                category="highlight",
                reason="普通片段",
            ),
            HighlightClip(
                start=50,
                end=75,
                text="颁奖",
                importance=0.4,
                category="highlight",
                reason="必须片段",
                matched_requirement_ids=[must.id],
            ),
        ],
    )
    requirement = VideoRequirement(target_duration=30, video_type="general", style="formal")

    script = ScriptAgent().run(analysis, requirement, [must])

    assert any(operation.source_start == 50 for operation in script.operations)


def test_media_probe_reads_generated_video(tmp_path: Path):
    ffmpeg = FFmpegTool._find_ffmpeg()
    input_path = tmp_path / "input.mp4"
    command = [
        ffmpeg,
        "-f", "lavfi", "-i", "color=c=black:s=320x240:d=2:r=24",
        "-f", "lavfi", "-i", "sine=frequency=1000:duration=2",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", "-y", str(input_path),
    ]
    subprocess.run(command, check=True, capture_output=True, text=True)

    info = FFmpegTool.get_video_info(str(input_path))

    assert info is not None
    assert info["has_audio"] is True
    assert info["width"] == 320
    assert 1.9 <= info["duration"] <= 2.1


def test_executor_renders_valid_video_with_remapped_subtitles(tmp_path: Path):
    ffmpeg = FFmpegTool._find_ffmpeg()
    input_path = tmp_path / "input.mp4"
    output_path = tmp_path / "output.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-f", "lavfi", "-i", "color=c=black:s=320x240:d=3:r=24",
            "-f", "lavfi", "-i", "sine=frequency=1000:duration=3",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", "-y", str(input_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    script = EditScript(
        title="测试",
        estimated_duration=2.0,
        operations=[
            EditOperation(order=1, action="cut", source_start=0.0, source_end=1.0),
            EditOperation(order=2, action="cut", source_start=1.5, source_end=2.5),
        ],
        srt_subtitles="1\n00:00:00,000 --> 00:00:01,000\n测试字幕\n",
    )

    result = ExecutorAgent().run(script, str(input_path), str(output_path))

    assert result.success, result.errors
    assert output_path.exists()
    assert 1.8 <= result.output_duration <= 2.2


def test_prepare_writes_recoverable_task_artifacts(tmp_path: Path, monkeypatch):
    ffmpeg = FFmpegTool._find_ffmpeg()
    input_path = tmp_path / "input.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-f", "lavfi", "-i", "color=c=black:s=320x240:d=4:r=24",
            "-f", "lavfi", "-i", "sine=frequency=1000:duration=4",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", "-y", str(input_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    requirement = VideoRequirement(target_duration=30, video_type="general", style="formal")
    brief = RequirementBrief(raw_text="必须包含测试内容")
    requirement_item = RequirementItem(
        description="必须包含与“测试”相关的内容",
        priority="must",
        acceptance_rule="候选引用测试转录证据",
    )
    spec = RequirementSpec(
        brief_id=brief.id,
        target_duration=30,
        requirements=[requirement_item],
        open_questions=[],
    )
    execution = ExecutionBrief(
        requirement_spec_id=spec.id,
        requirement_spec_version=1,
        visible_instruction="必须包含测试内容。",
        included_requirement_ids=[requirement_item.id],
    )
    compilation = RequirementCompilation(
        brief=brief,
        spec=spec,
        execution_brief=execution,
        legacy_requirement=requirement,
    )
    transcript = [TranscriptSegment(start=0, end=3, text="测试")]
    evidence = EvidenceBuilder.from_transcript(transcript)
    analysis = ContentAnalysis(video_duration=4, transcript=transcript, evidence=evidence)
    candidate = CandidateClip(
        source_start=0,
        source_end=3,
        matched_requirement_ids=[requirement_item.id],
        citations=[EvidenceCitation(
            requirement_id=requirement_item.id,
            evidence_id=evidence[0].id,
            quote="测试",
            retrieval_score=1.0,
        )],
        selection_reason="引用测试转录证据",
        confidence=0.9,
        suggested_duration=3,
    )

    class FakeAgent:
        def __init__(self, value):
            self.value = value

        def run(self, *args):
            return self.value

    class FakeRequirementAgent:
        def compile(self, *args, **kwargs):
            return compilation

    class FakeCandidateResult:
        candidates = [candidate]
        valid_ids = [candidate.id]
        valid_candidates = [candidate]
        retrieval_trace = {requirement_item.id: [evidence[0].id]}

    orchestrator = VideoEditOrchestrator.__new__(VideoEditOrchestrator)
    orchestrator.status = PipelineStatus(step="init")
    orchestrator.task_dir = None
    orchestrator.store = None
    orchestrator.preview_paths = {}
    orchestrator.agent1 = FakeRequirementAgent()
    orchestrator.agent2 = FakeAgent(analysis)
    orchestrator.candidate_agent = FakeAgent(FakeCandidateResult())
    orchestrator.agent3 = ScriptAgent()
    orchestrator.agent4 = ExecutorAgent()
    monkeypatch.setattr("src.orchestrator.OUTPUT_DIR", tmp_path / "output")

    created = orchestrator.create_requirement_draft(str(input_path), "测试")
    orchestrator.confirm_requirement_draft(orchestrator.status.requirement_gate.id, "tester")
    prepared = orchestrator.analyze_confirmed_requirement(str(input_path))

    assert created.spec.id == spec.id
    assert len(prepared.operations) == 1
    assert (orchestrator.task_dir / "manifest.json").exists()
    assert (orchestrator.task_dir / "requirement_spec_v1.json").exists()
    assert (orchestrator.task_dir / "execution_brief_v1.json").exists()
    assert (orchestrator.task_dir / "evidence.json").exists()
    assert (orchestrator.task_dir / "candidate_clips.json").exists()
    assert (orchestrator.task_dir / "analysis.json").exists()
    assert (orchestrator.task_dir / "edit_plan.json").exists()

    result = orchestrator.confirm_and_render(prepared, [1], str(input_path))
    assert result.success, result.errors
    assert Path(result.output_path).name == "final_v2.mp4"
    assert Path(result.output_path).exists()
    assert (orchestrator.task_dir / "subtitles.srt").exists()
    assert (orchestrator.task_dir / "render.log").exists()
    assert (orchestrator.task_dir / "execution_result.json").exists()
    assert (orchestrator.task_dir / "verification_report.json").exists()
    assert (orchestrator.task_dir / "delivery_report.json").exists()
    assert orchestrator.status.task_snapshot.state == "awaiting_delivery_resolution"


def test_analysis_chunking_and_deduplication_are_time_based():
    agent = AnalysisAgent.__new__(AnalysisAgent)
    transcript = [TranscriptSegment(start=float(second), end=float(second + 10), text=str(second)) for second in range(0, 300, 10)]
    chunks = agent._split_transcript(transcript, max_duration=120, overlap_duration=15)
    assert len(chunks) == 3
    assert chunks[1][0].start == 110

    clips = [
        HighlightClip(start=0, end=20, text="A", importance=0.9, category="highlight", reason="A"),
        HighlightClip(start=5, end=15, text="B", importance=0.8, category="highlight", reason="B"),
        HighlightClip(start=40, end=50, text="C", importance=0.7, category="highlight", reason="C"),
    ]
    deduplicated = agent._deduplicate(clips)
    assert [clip.text for clip in deduplicated] == ["A", "C"]


def test_subtitle_correction_only_updates_returned_segments(monkeypatch):
    analysis = ContentAnalysis(
        video_duration=20,
        transcript=[
            TranscriptSegment(start=0, end=3, text="我门今天开始"),
            TranscriptSegment(start=10, end=13, text="不应被处理"),
        ],
        highlights=[
            HighlightClip(
                start=0,
                end=5,
                text="开始",
                importance=0.9,
                category="highlight",
                reason="测试",
            )
        ],
    )
    script = EditScript(
        estimated_duration=5,
        operations=[EditOperation(order=1, action="cut", source_start=0, source_end=5)],
    )
    monkeypatch.setattr(
        "src.agents.script_agent.call_llm",
        lambda **_: {"segments": [{"index": 0, "text": "我们今天开始。"}]},
    )

    confirmed = ScriptAgent().apply_selection(script, analysis, [1])

    assert "我们今天开始。" in confirmed.srt_subtitles
    assert "不应被处理" not in confirmed.srt_subtitles


def test_intro_and_outro_extend_duration_and_shift_subtitles():
    analysis = ContentAnalysis(
        video_duration=10,
        transcript=[TranscriptSegment(start=0, end=2, text="开场内容")],
        highlights=[HighlightClip(start=0, end=4, text="开场", importance=0.9, category="highlight", reason="测试")],
    )
    script = EditScript(
        title="活动回顾",
        estimated_duration=4,
        operations=[EditOperation(order=1, action="cut", source_start=0, source_end=4)],
    )

    confirmed = ScriptAgent().apply_selection(
        script,
        analysis,
        [1],
        intro_style="title",
        outro_style="fade_black",
        title_text="夏季活动回顾",
        correct_subtitles=False,
    )

    assert confirmed.estimated_duration == 8
    assert confirmed.title_text == "夏季活动回顾"
    assert "00:00:02,000 --> 00:00:04,000" in confirmed.srt_subtitles


def test_executor_supports_transition_bgm_and_subtitle_style(tmp_path: Path):
    ffmpeg = FFmpegTool._find_ffmpeg()
    input_path = tmp_path / "input.mp4"
    bgm_path = tmp_path / "bgm.m4a"
    output_path = tmp_path / "output.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-f", "lavfi", "-i", "testsrc2=s=320x240:d=5:r=24",
            "-f", "lavfi", "-i", "sine=frequency=1000:duration=5",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", "-y", str(input_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [ffmpeg, "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "aac", "-y", str(bgm_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    script = EditScript(
        title="功能测试",
        estimated_duration=3.5,
        operations=[
            EditOperation(order=1, action="cut", source_start=0, source_end=2),
            EditOperation(order=2, action="cut", source_start=2.5, source_end=4.5),
        ],
        srt_subtitles="1\n00:00:00,000 --> 00:00:01,000\n字幕样式测试\n",
        transition_duration=0.5,
        subtitle_style="highlight",
        bgm_path=str(bgm_path),
        bgm_volume=0.1,
    )

    result = ExecutorAgent().run(script, str(input_path), str(output_path))

    assert result.success, result.errors
    assert output_path.exists()
    info = FFmpegTool.get_video_info(str(output_path))
    assert info and info["has_audio"]
    assert 3.2 <= result.output_duration <= 3.8


def test_executor_renders_selected_intro_and_outro(tmp_path: Path):
    ffmpeg = FFmpegTool._find_ffmpeg()
    input_path = tmp_path / "input.mp4"
    output_path = tmp_path / "with_cards.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-f", "lavfi", "-i", "color=c=green:s=320x240:d=3:r=24",
            "-f", "lavfi", "-i", "sine=frequency=800:duration=3",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", "-y", str(input_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    script = EditScript(
        title="测试片头",
        estimated_duration=6,
        operations=[EditOperation(order=1, action="cut", source_start=0, source_end=2)],
        intro_style="title",
        outro_style="fade_black",
        title_text="我的活动",
    )

    result = ExecutorAgent().run(script, str(input_path), str(output_path))

    assert result.success, result.errors
    assert 5.7 <= result.output_duration <= 6.3


def test_preview_generation_creates_small_playable_video(tmp_path: Path):
    ffmpeg = FFmpegTool._find_ffmpeg()
    input_path = tmp_path / "input.mp4"
    preview_path = tmp_path / "preview.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-f", "lavfi", "-i", "color=c=blue:s=640x480:d=2:r=24",
            "-f", "lavfi", "-i", "sine=frequency=600:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", "-y", str(input_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert FFmpegTool.create_preview(str(input_path), 0, 1.5, str(preview_path))
    info = FFmpegTool.get_video_info(str(preview_path))
    assert info and info["has_audio"]
    assert info["height"] == 360
