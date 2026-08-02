from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from src.agents.candidate_agent import CandidateAgent
from src.evaluation import OfflineEvaluationRunner
from src.models.schemas import (
    ContentAnalysis,
    EditOperation,
    EditScript,
    Evidence,
    EvidenceCitation,
    ExecutionBrief,
    ExecutionResult,
    RequirementBrief,
    RequirementItem,
    RequirementSlot,
    RequirementSpec,
    PipelineStatus,
    VideoRequirement,
    CandidateClip,
)
from src.orchestrator import VideoEditOrchestrator
from src.services.plans import EditPlanService
from src.services.requirements import RequirementClarificationService
from src.services.state_machine import TaskStateMachine
from src.services.task_store import TaskStore
from src.services.verification import VerificationEngine
from src.tools import llm


def _apply(machine: TaskStateMachine, event: str, key: str):
    snapshot = machine.load()
    return machine.apply(
        event,
        expected_version=snapshot.version,
        idempotency_key=key,
    )[0]


def _review_task(tmp_path: Path, monkeypatch):
    root = tmp_path / "output"
    task_dir = root / "tasks" / "recoverable-task"
    task_dir.mkdir(parents=True)
    monkeypatch.setattr("src.orchestrator.OUTPUT_DIR", root)
    store = TaskStore(task_dir)
    machine = TaskStateMachine(task_dir)
    machine.initialise()
    _apply(machine, "preflight_passed", "preflight")

    brief = RequirementBrief(raw_text="必须包含颁奖", scenario="school")
    requirement = RequirementItem(
        id="req-award",
        description="必须包含颁奖",
        priority="must",
        status="confirmed",
        acceptance_rule="至少一个批准片段覆盖颁奖",
    )
    spec = RequirementSpec(
        id="spec-recover",
        brief_id=brief.id,
        target_duration=10,
        duration_tolerance=5,
        requirements=[requirement],
        open_questions=[],
    )
    execution = ExecutionBrief(
        requirement_spec_id=spec.id,
        requirement_spec_version=1,
        visible_instruction="保留颁奖并引用证据",
        included_requirement_ids=[requirement.id],
    )
    slots = [
        RequirementSlot(
            key="target_duration",
            label="目标时长",
            value=10,
            status="confirmed",
            source_type="form",
        )
    ]
    gate = store.save_requirement_draft(brief, spec, execution, slots=slots)
    store.write_model(
        "legacy_requirement_v1.json",
        VideoRequirement(
            target_duration=30,
            video_type="general",
            style="formal",
            focus_keywords=["颁奖"],
        ),
    )
    _apply(machine, "requirement_drafted", "requirement-drafted")
    confirmed_spec, _, _ = store.confirm_requirement(gate.id, "tester")
    _apply(machine, "requirement_confirmed", "requirement-confirmed")
    _apply(machine, "style_skipped", "style-skipped")
    _apply(machine, "analysis_started", "analysis-started")

    evidence = Evidence(
        id="evidence-award",
        source_start=2,
        source_end=7,
        content="现在颁发一等奖",
        content_hash="award",
    )
    candidate = CandidateClip(
        id="candidate-award",
        source_start=2,
        source_end=7,
        matched_requirement_ids=[requirement.id],
        citations=[
            EvidenceCitation(
                requirement_id=requirement.id,
                evidence_id=evidence.id,
                quote="颁发一等奖",
            )
        ],
        selection_reason="直接命中颁奖要求",
        confidence=0.9,
        suggested_duration=5,
    )
    analysis = ContentAnalysis(
        video_duration=20,
        evidence=[evidence],
        candidate_clips=[candidate],
    )
    store.write_model("analysis.json", analysis)
    script = EditScript(
        title="颁奖回顾",
        estimated_duration=5,
        operations=[
            EditOperation(order=1, action="cut", source_start=2, source_end=7)
        ],
    )
    plan_service = EditPlanService()
    plan = plan_service.create(spec=confirmed_spec, analysis=analysis, script=script)
    store.save_plan_draft(plan)
    _apply(machine, "review_ready", "review-ready")
    store.write_payload(
        "manifest.json",
        {"task_id": task_dir.name, "state": "awaiting_review", "video_path": "/tmp/source.mp4"},
    )
    runtime = SimpleNamespace(
        agent1=None,
        agent2=None,
        candidate_agent=None,
        style_agent=None,
        agent3=None,
        agent4=None,
        plan_service=plan_service,
        clarification_service=RequirementClarificationService(),
        verification_engine=VerificationEngine(),
        render_backend=None,
    )
    return task_dir, runtime


def test_evaluation_dataset_and_report_cover_mvp2_metrics(tmp_path: Path):
    runner = OfflineEvaluationRunner()
    dataset = runner.load_dataset(Path("eval/mvp2_dataset.json"))
    report = runner.run_to_file(
        Path("eval/mvp2_dataset.json"),
        tmp_path / "evaluation_report.json",
    )
    assert len(dataset.tasks) == 6
    assert report["school_task_count"] == report["enterprise_task_count"] == 3
    assert set(report["variants"]) == {"bm25", "rrf", "rrf_rerank"}
    assert set(report["ablations"]) == {"raw_requirement_bm25", "no_evidence_retrieval"}
    assert "embedding_unavailable_bm25_fallback" in report["degradation"]
    for metrics in report["variants"].values():
        assert {"must_recall_at_k", "mrr", "ndcg_at_k", "latency_p50_ms", "latency_p95_ms", "failure_count"} <= set(metrics)
    assert 0 <= report["raw_citation_validity_rate"] <= 1
    assert 0 <= report["hallucination_rate"] <= 1
    assert 0 <= report["human_modification_rate"] <= 1


def test_model_observability_records_metadata_without_prompts(tmp_path: Path, monkeypatch):
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))],
        usage=SimpleNamespace(prompt_tokens=12, completion_tokens=4),
    )
    monkeypatch.setattr(llm._client.chat.completions, "create", lambda **_: response)
    secret = "private-person-name"
    with llm.model_call_context(
        tmp_path,
        stage="test-stage",
        prompt_template_version="test-v1",
        input_spec_id="spec-1",
        input_spec_version=1,
    ):
        assert llm.call_llm(secret, secret) == {"ok": True}
    log = (tmp_path / "model_calls.jsonl").read_text(encoding="utf-8")
    assert '"stage":"test-stage"' in log
    assert '"input_tokens":12' in log
    assert secret not in log


def test_requirement_editor_creates_new_version_and_supersedes_old_gate(tmp_path: Path):
    task_dir = tmp_path / "requirement-editor"
    store = TaskStore(task_dir)
    machine = TaskStateMachine(task_dir)
    machine.initialise()
    _apply(machine, "preflight_passed", "preflight")
    brief = RequirementBrief(raw_text="做活动回顾")
    spec = RequirementSpec(
        brief_id=brief.id,
        target_duration=60,
        requirements=[RequirementItem(description="保留开场")],
        open_questions=[],
    )
    execution = ExecutionBrief(
        requirement_spec_id=spec.id,
        requirement_spec_version=1,
        visible_instruction="保留开场",
    )
    slots = [
        RequirementSlot(
            key="purpose",
            label="用途",
            value=spec.purpose,
            status="inferred",
        )
    ]
    old_gate = store.save_requirement_draft(brief, spec, execution, slots=slots)
    store.write_model(
        "legacy_requirement_v1.json",
        VideoRequirement(target_duration=60, video_type="general", style="formal"),
    )
    snapshot = _apply(machine, "requirement_drafted", "requirement-drafted")
    orchestrator = VideoEditOrchestrator.__new__(VideoEditOrchestrator)
    orchestrator.task_dir = task_dir
    orchestrator.store = store
    orchestrator.state_machine = machine
    orchestrator.video_path = "/tmp/video.mp4"
    orchestrator.preview_paths = {}
    orchestrator.status = PipelineStatus(
        step=snapshot.state,
        task_snapshot=snapshot,
        requirement=store.read_model("legacy_requirement_v1.json", VideoRequirement),
        requirement_spec=spec,
        execution_brief=execution,
        requirement_gate=old_gate,
    )
    revised = orchestrator.revise_requirement_draft(
        updates={"purpose": "公众号活动回顾", "target_duration": 90},
        requirements=[
            {
                "description": "必须包含开幕式",
                "priority": "must",
                "acceptance_rule": "至少一个批准片段覆盖开幕式",
            }
        ],
        actor_id="teacher",
    )
    assert revised.version == 2
    assert revised.purpose == "公众号活动回顾"
    assert revised.target_duration == 90
    assert store.load_gates()[0].status == "superseded"
    assert orchestrator.status.requirement_gate.target_version == 2
    audit = (task_dir / "audit_events.jsonl").read_text(encoding="utf-8")
    assert "requirement_revised" in audit


def test_open_task_recovers_review_state_and_unapproved_plan_cannot_render(
    tmp_path: Path, monkeypatch
):
    task_dir, runtime = _review_task(tmp_path, monkeypatch)
    recovered = VideoEditOrchestrator.open_task(task_dir.name, runtime)
    assert recovered.status.task_snapshot.state == "awaiting_review"
    assert recovered.status.requirement_spec.id == "spec-recover"
    assert recovered.status.edit_plan.candidate_ids == ["candidate-award"]
    with pytest.raises(ValueError, match="未批准计划"):
        recovered.render(
            recovered.status.edit_plan.execution_script,
            recovered.video_path,
        )


def test_short_plan_requires_explicit_preapproval_and_is_not_rejected_after_render(
    tmp_path: Path, monkeypatch
):
    task_dir, runtime = _review_task(tmp_path, monkeypatch)
    recovered = VideoEditOrchestrator.open_task(task_dir.name, runtime)
    short_spec = recovered.status.requirement_spec.model_copy(update={
        "target_duration": 12,
        "duration_tolerance": 2,
        "need_subtitles": False,
    })
    short_delivery = recovered.status.edit_plan.delivery_spec.model_copy(update={
        "target_duration": 12,
        "duration_tolerance": 2,
        "need_subtitles": False,
    })
    short_plan = recovered.status.edit_plan.model_copy(update={
        "delivery_spec": short_delivery,
    })
    recovered.status.requirement_spec = short_spec
    recovered.status.edit_plan = short_plan
    recovered.store.write_model("requirement_spec_v1.json", short_spec)
    recovered.store.write_model("edit_plan_v1.json", short_plan)

    with pytest.raises(ValueError, match="至少需要 10.0 秒"):
        recovered.approve_current_plan("tester")

    approved = recovered.approve_current_plan(
        "tester", allow_duration_exception=True
    )

    assert approved.status == "approved"
    assert approved.approval_exceptions[0].code == "plan_duration_too_short"
    assert approved.approval_exceptions[0].planned_duration == 5
    audit = (task_dir / "audit_events.jsonl").read_text(encoding="utf-8")
    assert "edit_plan_approved_with_exception" in audit

    output = tmp_path / "short-approved.mp4"
    output.write_bytes(b"video")
    monkeypatch.setattr(
        "src.services.verification.FFmpegTool.get_video_info",
        lambda _: {"duration": 5.0, "has_video": True, "has_audio": True},
    )
    report = VerificationEngine().verify(
        task_id=task_dir.name,
        execution_result=ExecutionResult(
            success=True,
            output_path=str(output),
            output_duration=5,
            operations_done=1,
            operations_failed=0,
        ),
        spec=short_spec,
        plan=approved,
        candidates=recovered.status.analysis.candidate_clips,
    )

    assert report.status == "passed"
    assert any(
        item.code == "duration_below_target_preapproved"
        and item.method == "human"
        for item in report.results
    )


def test_delivery_exception_can_return_to_review(tmp_path: Path):
    machine = TaskStateMachine(tmp_path / "delivery-revision")
    machine.initialise()
    for event in (
        "preflight_passed",
        "requirement_drafted",
        "requirement_confirmed",
        "style_skipped",
        "analysis_started",
        "review_ready",
        "plan_approved",
        "render_started",
        "render_completed",
        "delivery_resolution_required",
    ):
        _apply(machine, event, event)
    assert machine.load().state == "awaiting_delivery_resolution"
    _apply(machine, "delivery_revision_requested", "return-to-review")
    assert machine.load().state == "awaiting_review"


def test_unexpected_render_backend_error_becomes_recoverable_failed_state(
    tmp_path: Path, monkeypatch
):
    task_dir, runtime = _review_task(tmp_path, monkeypatch)
    recovered = VideoEditOrchestrator.open_task(task_dir.name, runtime)
    recovered.approve_current_plan("tester")

    class BrokenBackend:
        def render(self, *_):
            raise RuntimeError("encoder crashed")

    recovered.render_backend = BrokenBackend()
    result = recovered.render_approved_plan(
        recovered.status.edit_plan,
        recovered.video_path,
    )
    assert not result.success
    assert recovered.status.task_snapshot.state == "failed"
    assert (task_dir / "execution_result.json").exists()
    assert "render_failed" in (task_dir / "audit_events.jsonl").read_text(encoding="utf-8")


def test_concurrent_render_submission_calls_backend_only_once(
    tmp_path: Path, monkeypatch
):
    task_dir, runtime = _review_task(tmp_path, monkeypatch)
    recovered = VideoEditOrchestrator.open_task(task_dir.name, runtime)
    recovered.approve_current_plan("tester")
    started = threading.Event()
    release = threading.Event()

    class BlockingBackend:
        calls = 0

        def render(self, _script, _video_path, output_path):
            self.calls += 1
            started.set()
            release.wait(timeout=5)
            return ExecutionResult(
                success=False,
                output_path=output_path,
                output_duration=0,
                operations_done=0,
                operations_failed=1,
                errors=["simulated failure"],
            )

    backend = BlockingBackend()
    recovered.render_backend = backend
    results = []
    worker = threading.Thread(
        target=lambda: results.append(
            recovered.render_approved_plan(
                recovered.status.edit_plan,
                recovered.video_path,
            )
        )
    )
    worker.start()
    assert started.wait(timeout=2)
    with pytest.raises(ValueError, match="正在渲染"):
        recovered.render_approved_plan(
            recovered.status.edit_plan,
            recovered.video_path,
        )
    release.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert backend.calls == 1
    assert len(results) == 1


def test_concurrent_duplicate_approval_writes_one_business_approval(
    tmp_path: Path, monkeypatch
):
    task_dir, runtime = _review_task(tmp_path, monkeypatch)
    recovered = VideoEditOrchestrator.open_task(task_dir.name, runtime)
    barrier = threading.Barrier(2)
    errors = []

    def approve():
        try:
            barrier.wait(timeout=2)
            recovered.approve_current_plan("tester")
        except Exception as error:  # pragma: no cover - 断言会暴露线程异常
            errors.append(error)

    workers = [threading.Thread(target=approve) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5)
    assert not errors
    assert recovered.status.task_snapshot.state == "plan_approved"
    audit_lines = (task_dir / "audit_events.jsonl").read_text(encoding="utf-8").splitlines()
    assert sum('"action":"edit_plan_approved"' in line for line in audit_lines) == 1


def test_plan_revision_only_audits_actual_text_change(tmp_path: Path, monkeypatch):
    task_dir, runtime = _review_task(tmp_path, monkeypatch)
    recovered = VideoEditOrchestrator.open_task(task_dir.name, runtime)
    segment = recovered.status.edit_plan.timeline_segments[0]
    unchanged, decisions = recovered.plan_service.revise(
        task_id=task_dir.name,
        plan=recovered.status.edit_plan,
        analysis=recovered.status.analysis,
        selected_candidate_ids=[segment.candidate_id],
        segment_updates={
            segment.candidate_id: {
                "order": segment.order,
                "source_start": segment.source_start,
                "source_end": segment.source_end,
                "subtitle_text": segment.subtitle_text,
                "title_text": segment.title_text,
            }
        },
    )
    assert [decision.action for decision in decisions] == ["keep"]
    _, decisions = recovered.plan_service.revise(
        task_id=task_dir.name,
        plan=unchanged,
        analysis=recovered.status.analysis,
        selected_candidate_ids=[segment.candidate_id],
        segment_updates={segment.candidate_id: {"subtitle_text": "一等奖颁奖"}},
        actor_id="tester",
    )
    assert [decision.action for decision in decisions] == ["subtitle_edit"]


def test_manual_clip_creates_user_annotation_and_can_be_approved(
    tmp_path: Path, monkeypatch
):
    task_dir, runtime = _review_task(tmp_path, monkeypatch)
    recovered = VideoEditOrchestrator.open_task(task_dir.name, runtime)
    existing = recovered.status.edit_plan.timeline_segments[0]
    revised = recovered.revise_edit_plan(
        selected_candidate_ids=[existing.candidate_id],
        manual_segments=[
            {
                "source_start": 8,
                "source_end": 12,
                "order": 2,
                "matched_requirement_ids": ["req-award"],
                "annotation": "操作者确认这是补充领奖合影",
                "subtitle_text": "一等奖领奖合影",
            }
        ],
        actor_id="tester",
    )
    manual = next(
        segment for segment in revised.timeline_segments
        if segment.candidate_id.startswith("manual_candidate_")
    )
    assert manual.evidence_ids[0].startswith("user_evidence_")
    evidence = next(
        item for item in recovered.status.analysis.evidence
        if item.id == manual.evidence_ids[0]
    )
    assert evidence.type == "user_annotation"
    assert evidence.content == "操作者确认这是补充领奖合影"
    approved = recovered.approve_current_plan("tester")
    assert approved.status == "approved"
    audit = (task_dir / "audit_events.jsonl").read_text(encoding="utf-8")
    assert "user_annotation_created" in audit
    assert "candidate_manual_add" in audit


def test_candidate_citation_is_scoped_to_the_requirement_that_retrieved_it():
    first = RequirementItem(description="保留开幕式")
    second = RequirementItem(description="保留颁奖")
    evidence = [
        Evidence(
            id="evidence-opening",
            source_start=1,
            source_end=4,
            content="活动正式开幕",
            content_hash="opening",
        )
    ]
    execution = ExecutionBrief(
        requirement_spec_id="spec",
        requirement_spec_version=1,
        visible_instruction="保留开幕与颁奖",
        included_requirement_ids=[first.id, second.id],
    )

    def malicious_runner(**kwargs):
        kwargs["tool_handlers"]["search_evidence"](first.id, "开幕式", 5)
        return {
            "candidates": [
                {
                    "source_start": 1,
                    "source_end": 4,
                    "matched_requirement_ids": [second.id],
                    "citations": [
                        {
                            "requirement_id": second.id,
                            "evidence_id": "evidence-opening",
                            "quote": "正式开幕",
                        }
                    ],
                    "selection_reason": "错误地跨需求复用证据",
                    "confidence": 0.8,
                    "suggested_duration": 3,
                }
            ]
        }

    result = CandidateAgent(llm_runner=malicious_runner).run(
        execution,
        [first, second],
        evidence,
        video_duration=10,
    )
    assert not result.valid_ids
    assert any(
        flag.startswith("evidence_not_retrieved_for_requirement")
        for flag in result.candidates[0].risk_flags
    )
