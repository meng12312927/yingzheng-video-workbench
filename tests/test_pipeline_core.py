import ast
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.agents.script_agent import ScriptAgent
from src.agents.executor_agent import ExecutorAgent
from src.agents.analysis_agent import AnalysisAgent
from src.agents.candidate_agent import CandidateAgent
from src.agents.candidate_agent import SpeechBoundaryValidator
from src.agents.clarification_agent import RequirementClarificationAgent
from src.agents.alignment_agent import RequirementAlignmentAgent
from src.agents.material_interview_agent import MaterialInterviewAgent
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
    SourceAsset,
    TaskMaterialSet,
    TimelineSegment,
)
from src.services.editor_adapters import JianyingHandoffAdapter, OTIOAdapter
from src.services.media_assets import MediaAssetService
from src.services.mobile_review import MobileReviewService
from src.services.infrastructure import LocalIdempotencyBackend
from src.services.organization_profiles import OrganizationProfileService
from src.services.task_store import TaskStore
from src.services.requirements import RequirementClarificationService
from src.services.reference_documents import ReferenceDocumentService
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
from src.services.duration_planner import DurationBudgetPlanner
from src.services.plans import EditPlanService
from src.services.timeline_ir import TimelineCompiler
from src.services.verification import VerificationEngine
from src.orchestrator import VideoEditOrchestrator
from src.tools.ffmpeg import FFmpegTool
from src.tools import llm as llm_tools


def test_multi_asset_analysis_keeps_source_local_time_and_partial_results(
    tmp_path: Path, monkeypatch
):
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    monkeypatch.setattr(
        "src.services.media_assets.FFmpegTool.get_video_info",
        lambda path: {
            "duration": 10.0 if str(path).endswith("first.mp4") else 20.0,
            "width": 1280,
            "height": 720,
            "codec": "h264",
            "has_audio": True,
        },
    )

    class FakeAnalysisAgent:
        def run(self, path, requirement, source_asset_id=None):
            if str(path).endswith("second.mp4"):
                raise RuntimeError("second failed")
            transcript = [
                TranscriptSegment(
                    source_asset_id=source_asset_id,
                    start=1,
                    end=3,
                    text="第一段素材的完整表达",
                )
            ]
            return ContentAnalysis(
                video_duration=10,
                source_durations={source_asset_id: 10},
                transcript=transcript,
                evidence=EvidenceBuilder.from_transcript(transcript),
                summary="第一段摘要",
            )

    service = MediaAssetService()
    material_set = service.register("task-1", [str(first), str(second)])
    completed, aggregate = service.analyze(
        material_set,
        VideoRequirement(target_duration=30, video_type="general", style="formal"),
        FakeAnalysisAgent(),
        TaskStore(tmp_path / "task-1"),
    )

    assert completed.status == "partial"
    assert [item.status for item in completed.results] == ["ready", "failed"]
    assert aggregate.video_duration == 30
    assert aggregate.transcript[0].start == 1
    assert aggregate.transcript[0].source_asset_id == completed.sources[0].id
    assert aggregate.evidence[0].source_asset_id == completed.sources[0].id
    assert (tmp_path / "task-1" / "sources" / completed.sources[0].id / "analysis.json").exists()
    assert not (tmp_path / "task-1" / "merged_source.mp4").exists()

    class RetryAgent:
        def run(self, path, requirement, source_asset_id=None):
            transcript = [
                TranscriptSegment(
                    source_asset_id=source_asset_id,
                    start=2,
                    end=5,
                    text="第二段素材重试成功",
                )
            ]
            return ContentAnalysis(
                video_duration=20,
                source_durations={source_asset_id: 20},
                transcript=transcript,
                evidence=EvidenceBuilder.from_transcript(transcript),
            )

    retried, retried_aggregate = service.retry_source(
        completed,
        source_asset_id=completed.sources[1].id,
        requirement=VideoRequirement(
            target_duration=30, video_type="general", style="formal"
        ),
        analysis_agent=RetryAgent(),
        store=TaskStore(tmp_path / "task-1"),
        retry_reason="网络恢复后重试",
    )
    assert retried.status == "ready"
    assert retried.results[0].attempt_count == 1
    assert retried.results[1].attempt_count == 2
    assert len(retried_aggregate.transcript) == 2


def test_same_local_time_from_different_assets_is_not_merged():
    transcript = [
        TranscriptSegment(source_asset_id="asset-a", start=0, end=5, text="甲素材"),
        TranscriptSegment(source_asset_id="asset-b", start=0, end=5, text="乙素材"),
    ]
    evidence = EvidenceBuilder.from_transcript(transcript)
    requirement = RequirementItem(description="保留内容")
    candidates = [
        CandidateClip(
            source_asset_id=item.source_asset_id,
            source_start=item.source_start,
            source_end=item.source_end,
            matched_requirement_ids=[requirement.id],
            citations=[EvidenceCitation(
                requirement_id=requirement.id,
                evidence_id=item.id,
                quote=item.content,
            )],
            selection_reason="相关内容",
            confidence=0.8,
            suggested_duration=5,
        )
        for item in evidence
    ]

    assert len(evidence) == 2
    assert len(CandidateAgent._merge_duplicate_candidates(candidates)) == 2


def test_material_interview_agent_only_accepts_retrieved_source_evidence():
    transcript = [
        TranscriptSegment(
            source_asset_id="asset-a",
            start=10,
            end=18,
            text="主持人介绍了活动背景和本次活动目标",
        )
    ]
    evidence = EvidenceBuilder.from_transcript(transcript)
    analysis = ContentAnalysis(
        video_duration=30,
        source_durations={"asset-a": 30},
        transcript=transcript,
        evidence=evidence,
        summary="一段活动介绍",
    )

    class Retriever:
        def search(self, query, items, top_k=8):
            return [(list(items)[0], 0.9)]

    def runner(**kwargs):
        result = kwargs["tool_handlers"]["search_material_evidence"](
            "活动介绍", 3
        )
        return {
            "questions": [
                {
                    "decision_type": "retain",
                    "question": "主持人介绍活动背景的这段内容，需要完整保留还是只保留核心目标？",
                    "reason": "素材中同时包含活动背景和活动目标。",
                    "impact": "会影响开场信息量和后续内容可用时长。",
                    "answer_hint": "例如：背景简短带过，完整保留活动目标",
                    "supporting_evidence_ids": [
                        result["evidence"][0]["evidence_id"], "invented"
                    ],
                }
            ]
        }

    agent = MaterialInterviewAgent(llm_runner=runner, retriever=Retriever())
    questions = agent.run(
        raw_text="剪一个活动回顾",
        spec=RequirementSpec(brief_id="brief-1", target_duration=60),
        analysis=analysis,
        scenario="school",
        source_labels={"asset-a": "开场.mp4"},
    )

    assert len(questions) == 1
    assert questions[0].supporting_evidence_ids == [evidence[0].id]
    assert questions[0].source_asset_ids == ["asset-a"]


def test_duration_budget_reserves_more_time_for_must_and_reports_shortage():
    must = RequirementItem(
        description="完整保留领导致辞",
        priority="must",
        acceptance_rule="至少一段完整致辞",
    )
    should = RequirementItem(description="保留活动氛围")
    planner = DurationBudgetPlanner()
    plan = planner.build(
        [must, should],
        target_duration=210,
        duration_tolerance=15,
        available_duration=600,
    )
    budgets = {item.requirement_id: item for item in plan.requirement_budgets}

    assert budgets[must.id].target_seconds > budgets[should.id].target_seconds
    assert budgets[must.id].desired_candidate_count > 2
    completed = planner.complete(plan, [30, 30, 30])
    assert completed.status == "short"
    assert completed.shortage_seconds == 105
    assert any("还差" in item for item in completed.recommendations)


def test_speech_boundary_expands_question_and_answer_as_one_unit():
    transcript = [
        TranscriptSegment(source_asset_id="asset-a", start=0, end=4, text="请问你怎么看这次活动？"),
        TranscriptSegment(source_asset_id="asset-a", start=4.2, end=10, text="我觉得活动让大家交流得更深入。"),
    ]
    agent = CandidateAgent(llm_runner=lambda **_: {})
    start, end = agent._snap_to_transcript_boundaries(4.2, 10, transcript, 20)

    assert start == 0
    assert end == 10


def test_approved_plan_compiles_to_jianying_handoff_and_otio(tmp_path: Path):
    source_path = tmp_path / "source.mp4"
    source_path.write_bytes(b"placeholder")
    source = SourceAsset(
        id="asset-a",
        order=1,
        filename="source.mp4",
        source_path=str(source_path),
        content_hash="hash-a",
        duration=30,
        fps=25,
        has_audio=True,
        analysis_state="ready",
    )
    delivery = DeliverySpec(
        requirement_spec_id="spec-a",
        requirement_spec_version=1,
        target_duration=10,
        duration_tolerance=2,
    )
    segment = TimelineSegment(
        order=1,
        candidate_id="candidate-a",
        source_asset_id="asset-a",
        source_start=5,
        source_end=15,
        output_start=0,
        output_end=10,
        matched_requirement_ids=["req-a"],
        evidence_ids=["evidence-a"],
    )
    script = EditScript(
        title="活动回顾",
        estimated_duration=10,
        operations=[EditOperation(
            order=1,
            source_asset_id="asset-a",
            action="cut",
            source_start=5,
            source_end=15,
        )],
    )
    plan = AuditableEditPlan(
        requirement_spec_id="spec-a",
        requirement_spec_version=1,
        delivery_spec=delivery,
        timeline_segments=[segment],
        candidate_ids=["candidate-a"],
        execution_script=script,
        estimated_duration=10,
        status="approved",
    )
    material_set = TaskMaterialSet(task_id="task-a", sources=[source])
    timeline = TimelineCompiler().compile(
        task_id="task-a", plan=plan, material_set=material_set
    )

    assert timeline.clips[0].source_asset_id == "asset-a"
    handoff = JianyingHandoffAdapter(render_clips=False).export(
        timeline, tmp_path / "jianying"
    )
    otio = OTIOAdapter().export(timeline, tmp_path / "otio")
    assert handoff.success
    assert (tmp_path / "jianying" / "clip_order.csv").exists()
    assert (tmp_path / "jianying" / "导入剪映说明.md").exists()
    assert otio.success
    assert Path(otio.output_path).suffix == ".otio"


def test_reference_document_creates_pending_proper_noun_review(tmp_path: Path):
    names = tmp_path / "人员名单.csv"
    names.write_text("姓名,职务\n李浩然,活动负责人\n", encoding="utf-8")
    service = ReferenceDocumentService()
    library = service.ingest(
        task_id="task-a",
        path=str(names),
        category="people",
    )
    transcript = [
        TranscriptSegment(
            source_asset_id="asset-a",
            start=2,
            end=6,
            text="下面请李昊然为大家介绍活动安排",
        )
    ]
    queue = service.build_entity_queue(
        task_id="task-a",
        analysis=ContentAnalysis(
            video_duration=10,
            source_durations={"asset-a": 10},
            transcript=transcript,
        ),
        library=library,
    )

    match = next(item for item in queue.items if item.suggested_text == "李浩然")
    assert match.status == "pending"
    assert match.observed_text == "李昊然"
    confirmed = service.resolve(
        queue,
        item_id=match.id,
        action="confirm",
        actor_id="reviewer",
    )
    resolved = next(item for item in confirmed.items if item.id == match.id)
    assert resolved.status == "confirmed"
    assert resolved.confirmed_value == "李浩然"


def test_mobile_review_token_is_version_bound_and_submission_is_idempotent(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks" / "task-mobile")
    delivery = DeliverySpec(
        requirement_spec_id="spec-mobile",
        requirement_spec_version=1,
        target_duration=10,
        duration_tolerance=2,
    )
    plan = AuditableEditPlan(
        requirement_spec_id="spec-mobile",
        requirement_spec_version=1,
        delivery_spec=delivery,
        execution_script=EditScript(title="审核", estimated_duration=0),
        estimated_duration=0,
    )
    store.save_plan_draft(plan)
    service = MobileReviewService()
    link = service.issue(
        store=store,
        plan=plan,
        base_url="http://127.0.0.1:7961",
        ttl_hours=1,
    )
    token = link.url.rsplit("/", 1)[-1]
    assert link.url.startswith("http://127.0.0.1:7961/review/")

    submission = service.submit(
        tasks_root=tmp_path / "tasks",
        token=token,
        decision="changes_requested",
        comment="请增加颁奖画面",
        reviewer_name="李主任",
        idempotency_key="same-submit",
    )
    duplicate = service.submit(
        tasks_root=tmp_path / "tasks",
        token=token,
        decision="changes_requested",
        comment="请增加颁奖画面",
        reviewer_name="李主任",
        idempotency_key="same-submit",
    )

    assert submission.id == duplicate.id
    with pytest.raises(ValueError, match="失效"):
        service.inspect(tmp_path / "tasks", token)
    revoked_link = service.issue(
        store=store,
        plan=plan,
        base_url="http://127.0.0.1:7961",
        ttl_hours=1,
    )
    service.revoke(store, revoked_link.token_record_id)
    with pytest.raises(ValueError, match="失效"):
        service.inspect(
            tmp_path / "tasks",
            revoked_link.url.rsplit("/", 1)[-1],
        )


def test_organization_profile_is_versioned_and_deleted_rules_leave_context(tmp_path: Path):
    service = OrganizationProfileService(tmp_path / "organizations")
    profile = service.create(
        name="示例学校",
        scenario="school",
        created_by="admin",
    )
    revised = service.add_item(
        profile.id,
        kind="proper_noun",
        label="校长姓名",
        value="李浩然",
        aliases=["李昊然"],
        source="学校办公室确认",
    )
    restricted = service.add_item(
        profile.id,
        kind="publishing_restriction",
        label="未成年人信息",
        value="不得展示未获授权学生的全名",
        source="学校发布规范 v2",
    )

    context = service.active_context(profile.id)
    assert context["proper_nouns"][0]["value"] == "李浩然"
    assert context["publishing_restrictions"][0]["source"] == "学校发布规范 v2"
    assert service.load(profile.id, version=revised.version).version == 2

    deleted = service.delete_item(profile.id, restricted.items[-1].id)
    assert deleted.version == 4
    assert service.active_context(profile.id)["publishing_restrictions"] == []


def test_local_idempotency_backend_persists_render_lock(tmp_path: Path):
    first = LocalIdempotencyBackend(tmp_path / "locks.json")
    second = LocalIdempotencyBackend(tmp_path / "locks.json")

    assert first.acquire("render:task:1", 60)
    assert not second.acquire("render:task:1", 60)
    first.release("render:task:1")
    assert second.acquire("render:task:1", 60)


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


def test_embedding_requests_are_split_into_batches_of_ten(monkeypatch):
    batch_sizes = []

    class FakeEmbeddings:
        def create(self, *, model, input):
            batch_sizes.append(len(input))
            return SimpleNamespace(
                data=[
                    SimpleNamespace(index=index, embedding=[float(value)])
                    for index, value in enumerate(input)
                ],
                usage=None,
            )

    monkeypatch.setattr(
        llm_tools,
        "_embedding_client",
        SimpleNamespace(embeddings=FakeEmbeddings()),
    )
    monkeypatch.setattr(llm_tools, "EMBEDDING_API_KEY", "configured-for-test")

    vectors = llm_tools.embed_texts(list(range(23)))

    assert batch_sizes == [10, 10, 3]
    assert [vector[0] for vector in vectors] == list(range(23))


def test_tool_calling_forces_final_json_and_repairs_truncated_output(monkeypatch):
    calls = []

    class FakeMessage(SimpleNamespace):
        def model_dump(self, exclude_none=True):
            return {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.function.name,
                            "arguments": call.function.arguments,
                        },
                    }
                    for call in (self.tool_calls or [])
                ],
            }

    tool_call = SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(name="search_evidence", arguments='{"query":"颁奖"}'),
    )
    responses = [
        SimpleNamespace(
            choices=[SimpleNamespace(
                message=FakeMessage(content=None, tool_calls=[tool_call]),
                finish_reason="tool_calls",
            )],
            usage=None,
        ),
        SimpleNamespace(
            choices=[SimpleNamespace(
                message=FakeMessage(content='{"candidates":[', tool_calls=[]),
                finish_reason="length",
            )],
            usage=None,
        ),
        SimpleNamespace(
            choices=[SimpleNamespace(
                message=FakeMessage(content='{"candidates":[]}', tool_calls=[]),
                finish_reason="stop",
            )],
            usage=None,
        ),
    ]

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return responses.pop(0)

    monkeypatch.setattr(
        llm_tools,
        "_client",
        SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions())),
    )

    result = llm_tools.call_llm_with_tools(
        system_prompt="返回 JSON",
        user_message="查找颁奖",
        tool_definitions=[{"type": "function", "function": {"name": "search_evidence"}}],
        tool_handlers={"search_evidence": lambda query: {"items": [query]}},
        max_rounds=2,
    )

    assert result == {"candidates": []}
    assert "tools" in calls[0]
    assert "tools" not in calls[1]
    assert "tools" not in calls[2]


def test_candidate_fallback_suggestions_are_not_exportable():
    requirement = RequirementItem(description="保留颁奖", priority="should")
    evidence = EvidenceBuilder.from_transcript([
        TranscriptSegment(start=0, end=4, text="现在开始颁奖仪式")
    ])
    execution = ExecutionBrief(
        requirement_spec_id="spec-fallback",
        requirement_spec_version=1,
        visible_instruction="保留颁奖。",
    )

    def failed_runner(**_):
        raise RuntimeError("simulated truncated JSON")

    result = CandidateAgent(
        llm_runner=failed_runner,
        retriever=HybridEvidenceRetriever(),
    ).run(execution, [requirement], evidence, video_duration=10)

    assert result.degraded
    assert result.candidates
    assert not result.valid_candidates
    assert "candidate_ai_unavailable_not_exportable" in result.candidates[0].risk_flags


def test_candidate_ending_before_continuation_is_rejected():
    transcript = [
        TranscriptSegment(start=0, end=2, text="我们采取了很多办法"),
        TranscriptSegment(start=2, end=4, text="所以最后顺利完成了活动"),
    ]
    candidate = CandidateClip(
        source_start=0,
        source_end=2,
        matched_requirement_ids=["req-1"],
        citations=[EvidenceCitation(
            requirement_id="req-1",
            evidence_id="evidence-1",
            quote="我们采取了很多办法",
        )],
        selection_reason="活动经验",
        confidence=0.8,
        suggested_duration=2,
    )

    errors = SpeechBoundaryValidator().validate(candidate, transcript)

    assert "candidate_ends_mid_sentence" in errors


def test_candidate_agent_expands_mid_sentence_candidate_to_complete_expression():
    requirement = RequirementItem(description="保留活动经验", priority="should")
    transcript = [
        TranscriptSegment(start=0, end=2, text="我们采取了很多办法"),
        TranscriptSegment(start=2, end=4, text="所以最后顺利完成了活动"),
    ]
    evidence = EvidenceBuilder.from_transcript(transcript, max_window_seconds=2)
    execution = ExecutionBrief(
        requirement_spec_id="spec-boundary",
        requirement_spec_version=1,
        visible_instruction="保留完整的活动经验。",
    )

    def runner(**kwargs):
        result = kwargs["tool_handlers"]["search_evidence"](
            requirement_id=requirement.id,
            query="办法",
            top_k=1,
        )
        item = result["evidence"][0]
        return {"candidates": [{
            "source_start": item["source_start"],
            "source_end": item["source_end"],
            "matched_requirement_ids": [requirement.id],
            "citations": [{
                "requirement_id": requirement.id,
                "evidence_id": item["evidence_id"],
                "quote": "我们采取了很多办法",
                "relation": "direct",
                "retrieval_score": item["retrieval_score"],
            }],
            "selection_reason": "说明活动经验",
            "confidence": 0.8,
            "suggested_duration": 2,
            "risk_flags": [],
        }]}

    result = CandidateAgent(
        llm_runner=runner,
        retriever=HybridEvidenceRetriever(),
    ).run(
        execution,
        [requirement],
        evidence,
        video_duration=10,
        transcript=transcript,
    )

    assert len(result.valid_candidates) == 1
    assert result.valid_candidates[0].source_start == 0
    assert result.valid_candidates[0].source_end == 4
    assert "candidate_ends_mid_sentence" not in result.valid_candidates[0].risk_flags


def test_candidate_agent_batches_requirements_and_builds_citations_in_code():
    first = RequirementItem(description="保留开幕介绍", priority="should")
    second = RequirementItem(description="保留颁奖结果", priority="should")
    transcript = [
        TranscriptSegment(start=0, end=5, text="主持人宣布活动正式开幕"),
        TranscriptSegment(start=20, end=25, text="一等奖获得者上台领奖"),
    ]
    evidence = EvidenceBuilder.from_transcript(transcript)
    execution = ExecutionBrief(
        requirement_spec_id="spec-batched",
        requirement_spec_version=1,
        visible_instruction="保留开幕与颁奖。",
    )
    calls = []

    def runner(**kwargs):
        request = ast.literal_eval(kwargs["user_message"])
        requirement_id = request["requirements"][0]["id"]
        calls.append({
            "requirement_count": len(request["requirements"]),
            "max_tokens": kwargs["max_tokens"],
            "requirement_id": requirement_id,
        })
        result = kwargs["tool_handlers"]["search_evidence"](
            requirement_id=requirement_id,
            query=request["requirements"][0]["description"],
            top_k=1,
        )
        item = result["evidence"][0]
        return {"selections": [{
            "evidence_id": item["evidence_id"],
            "source_start": item["source_start"],
            "source_end": item["source_end"],
            "reason": "与当前要求直接相关",
            "confidence": 0.9,
            # 即使模型额外返回伪造字段，代码也不能采用。
            "quote": "伪造引文",
            "retrieval_score": 0.01,
        }]}

    result = CandidateAgent(
        llm_runner=runner,
        retriever=HybridEvidenceRetriever(),
    ).run(
        execution,
        [first, second],
        evidence,
        video_duration=30,
        transcript=transcript,
    )

    assert len(calls) == 2
    assert all(call["requirement_count"] == 1 for call in calls)
    assert all(call["max_tokens"] == CandidateAgent.BATCH_MAX_TOKENS for call in calls)
    assert {call["requirement_id"] for call in calls} == {first.id, second.id}
    assert len(result.valid_candidates) == 2
    assert {citation.quote for item in result.valid_candidates for citation in item.citations} == {
        "主持人宣布活动正式开幕",
        "一等奖获得者上台领奖",
    }
    assert all(
        citation.retrieval_score == 1.0
        for item in result.valid_candidates
        for citation in item.citations
    )


def test_candidate_agent_isolates_failed_requirement_batch():
    failed = RequirementItem(description="保留不存在的领导讲话", priority="should")
    successful = RequirementItem(description="保留颁奖结果", priority="should")
    transcript = [
        TranscriptSegment(start=0, end=4, text="暖场画面"),
        TranscriptSegment(start=20, end=25, text="一等奖获得者上台领奖"),
    ]
    evidence = EvidenceBuilder.from_transcript(transcript)
    execution = ExecutionBrief(
        requirement_spec_id="spec-isolation",
        requirement_spec_version=1,
        visible_instruction="分别分析两个要求。",
    )
    attempts = {failed.id: 0, successful.id: 0}

    def runner(**kwargs):
        request = ast.literal_eval(kwargs["user_message"])
        requirement_id = request["requirements"][0]["id"]
        attempts[requirement_id] += 1
        if requirement_id == failed.id:
            raise RuntimeError("simulated batch truncation")
        result = kwargs["tool_handlers"]["search_evidence"](
            requirement_id=requirement_id,
            query="颁奖",
            top_k=1,
        )
        item = result["evidence"][0]
        return {"selections": [{
            "evidence_id": item["evidence_id"],
            "source_start": item["source_start"],
            "source_end": item["source_end"],
            "reason": "颁奖结果",
            "confidence": 0.9,
        }]}

    result = CandidateAgent(
        llm_runner=runner,
        retriever=HybridEvidenceRetriever(),
    ).run(
        execution,
        [failed, successful],
        evidence,
        video_duration=30,
        transcript=transcript,
    )

    assert attempts[failed.id] == CandidateAgent.BATCH_ATTEMPTS
    assert attempts[successful.id] == 1
    assert result.degraded
    assert result.failed_requirement_ids == [failed.id]
    assert any(
        successful.id in candidate.matched_requirement_ids
        for candidate in result.valid_candidates
    )
    assert all(
        "candidate_generation_fallback" not in candidate.risk_flags
        for candidate in result.valid_candidates
    )


def test_plan_validation_blocks_duration_far_below_taskbook():
    spec = RequirementSpec(
        brief_id="brief-duration-gate",
        target_duration=210,
        duration_tolerance=21,
        status="confirmed",
    )
    delivery = DeliverySpec(
        requirement_spec_id=spec.id,
        requirement_spec_version=spec.version,
        target_duration=210,
        duration_tolerance=21,
        need_subtitles=False,
    )
    script = EditScript(estimated_duration=59, operations=[])
    plan = AuditableEditPlan(
        requirement_spec_id=spec.id,
        requirement_spec_version=spec.version,
        delivery_spec=delivery,
        execution_script=script,
        estimated_duration=59,
    )

    validation = EditPlanService().validate(
        plan,
        spec,
        candidates=[],
        video_duration=330,
    )

    assert not validation.valid
    assert "plan_duration_too_short" in validation.errors


def test_empty_focus_form_value_remains_an_unresolved_slot_for_ai_analysis(monkeypatch):
    agent = RequirementAgent()
    parsed = VideoRequirement(
        target_duration=180,
        video_type="general",
        style="formal",
        focus_keywords=[],
        need_subtitles=True,
    )
    monkeypatch.setattr(agent, "run", lambda _: parsed)

    compilation = agent.compile(
        "制作一条学校活动回顾视频",
        scenario="school",
        overrides={"focus_keywords": []},
    )

    focus_slot = next(slot for slot in compilation.slots if slot.key == "focus_keywords")
    assert focus_slot.status == "missing"
    assert focus_slot.question is None


def test_clarification_agent_generates_explainable_questions_only_for_allowed_slots():
    spec = RequirementSpec(
        brief_id="brief-ai-questions",
        target_duration=180,
        requirements=[RequirementItem(description="保留活动主题", priority="should")],
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
            key="focus_keywords",
            label="重点内容",
            status="missing",
        ),
        RequirementSlot(
            key="high_risk_review",
            label="高风险信息核对",
            status="unknown",
            risk_level="high",
        ),
    ]

    def fake_llm(**kwargs):
        request = __import__("json").loads(kwargs["user_message"])
        assert {item["key"] for item in request["allowed_slots"]} == {
            "focus_keywords", "high_risk_review"
        }
        return {"questions": [
            {
                "slot_key": "target_duration",
                "question": "已经确认的时长还要改吗？",
                "reason": "不应通过校验",
                "impact": "不应展示",
            },
            {
                "slot_key": "focus_keywords",
                "question": "这次活动中，有没有必须出现的人物或环节？",
                "reason": "当前只说明要保留活动主题，没有指出必须出现的内容。",
                "impact": "会影响候选片段筛选和最终逐项验收。",
                "answer_hint": "例如：校长致辞、一等奖颁奖",
            },
            {
                "slot_key": "unknown_slot",
                "question": "无效问题是什么？",
                "reason": "不应通过校验",
                "impact": "不应展示",
            },
        ]}

    agent = RequirementClarificationAgent(llm_runner=fake_llm)
    questions = agent.run(
        raw_text="制作一条三分钟学校活动回顾",
        spec=spec,
        slots=slots,
        scenario="school",
    )
    revised_spec, revised_slots = agent.apply_questions(spec, slots, questions)

    assert [item.slot_key for item in questions] == ["focus_keywords"]
    assert revised_spec.open_questions == [questions[0].question]
    focus_slot = next(item for item in revised_slots if item.key == "focus_keywords")
    assert focus_slot.question_reason == questions[0].reason
    assert focus_slot.question_impact == questions[0].impact
    assert focus_slot.question_source.startswith("clarification-agent:")


def test_alignment_agent_grounds_suggestion_in_allowed_material_evidence():
    spec = RequirementSpec(
        brief_id="brief-alignment",
        target_duration=120,
        requirements=[RequirementItem(description="制作活动回顾", priority="should")],
    )
    transcript = [
        TranscriptSegment(start=0, end=4, text="参加主题党日活动让我印象很深"),
        TranscriptSegment(start=4, end=8, text="希望以后增加更多实践环节"),
    ]
    analysis = ContentAnalysis(
        video_duration=180,
        transcript=transcript,
        evidence=EvidenceBuilder.from_transcript(transcript),
        summary="主题党日活动参与感受访谈",
    )
    allowed_id = analysis.evidence[0].id

    def fake_llm(**kwargs):
        request = __import__("json").loads(kwargs["user_message"])
        assert request["material_evidence"][0]["evidence_id"] == allowed_id
        return {
            "alignment_status": "too_vague",
            "confidence": 0.91,
            "material_summary": "素材是一段主题党日活动参与感受访谈",
            "detected_topics": ["参与感受", "改进建议"],
            "rationale": "原任务只写活动回顾，没有指出访谈主题。",
            "suggested_requirement_text": "剪成两分钟主题党日访谈摘要，保留完整表达。",
            "suggested_purpose": "主题党日活动访谈摘要",
            "suggested_video_type": "meeting",
            "suggested_style": "formal",
            "suggested_target_duration": 120,
            "suggested_focus_items": ["参与活动的感受", "对活动的改进建议"],
            "supporting_evidence_ids": [allowed_id, "invented-evidence"],
        }

    proposal = RequirementAlignmentAgent(llm_runner=fake_llm).run(
        raw_text="帮我剪一个活动回顾",
        spec=spec,
        analysis=analysis,
        scenario="school",
    )

    assert proposal.requires_user_decision
    assert proposal.supporting_evidence_ids == [allowed_id]
    assert proposal.suggested_focus_items == ["参与活动的感受", "对活动的改进建议"]


def test_alignment_decision_revises_taskbook_and_reuses_cached_transcript(
    tmp_path: Path,
    monkeypatch,
):
    brief = RequirementBrief(raw_text="帮我剪一个活动回顾", scenario="school")
    initial_item = RequirementItem(description="制作活动回顾", priority="should")
    spec = RequirementSpec(
        brief_id=brief.id,
        target_duration=120,
        requirements=[initial_item],
    )
    execution = ExecutionBrief(
        requirement_spec_id=spec.id,
        requirement_spec_version=1,
        visible_instruction="制作活动回顾。",
        included_requirement_ids=[initial_item.id],
    )
    legacy = VideoRequirement(
        target_duration=120,
        video_type="general",
        style="formal",
        focus_keywords=[],
    )
    slots = RequirementAgent._build_slots(
        brief.raw_text,
        spec,
        scenario="school",
        overrides={"target_duration": 120},
        manual_required=False,
    )
    compilation = RequirementCompilation(
        brief=brief,
        spec=spec,
        execution_brief=execution,
        legacy_requirement=legacy,
        slots=slots,
    )
    transcript = [
        TranscriptSegment(start=0, end=4, text="参加主题党日活动让我印象很深"),
        TranscriptSegment(start=4, end=8, text="希望以后增加更多实践环节"),
    ]
    material_analysis = ContentAnalysis(
        video_duration=180,
        transcript=transcript,
        evidence=EvidenceBuilder.from_transcript(transcript),
        summary="主题党日活动参与感受访谈",
    )

    class FakeRequirementAgent:
        def compile(self, *args, **kwargs):
            return compilation

    class FakeAnalysisAgent:
        def __init__(self):
            self.calls = 0

        def run(self, *args, **kwargs):
            self.calls += 1
            return material_analysis

    class EmptyCandidateResult:
        candidates = []
        valid_ids = []
        valid_candidates = []
        retrieval_trace = {}
        missing_must_requirement_ids = []
        degraded = False
        failure_reason = None

    class FakeCandidateAgent:
        def run(self, *args, **kwargs):
            return EmptyCandidateResult()

    def alignment_llm(**_):
        return {
            "alignment_status": "too_vague",
            "confidence": 0.9,
            "material_summary": "素材是一段主题党日活动参与感受访谈",
            "detected_topics": ["参与感受", "改进建议"],
            "rationale": "初始要求没有说明访谈主题。",
            "suggested_requirement_text": "剪成两分钟主题党日访谈摘要，保留完整表达。",
            "suggested_purpose": "主题党日活动访谈摘要",
            "suggested_video_type": "meeting",
            "suggested_style": "formal",
            "suggested_target_duration": 120,
            "suggested_focus_items": ["参与活动的感受", "对活动的改进建议"],
            "supporting_evidence_ids": [material_analysis.evidence[0].id],
        }

    analysis_agent = FakeAnalysisAgent()
    orchestrator = VideoEditOrchestrator.__new__(VideoEditOrchestrator)
    orchestrator.agent1 = FakeRequirementAgent()
    orchestrator.clarification_agent = RequirementClarificationAgent(
        llm_runner=lambda **_: {"questions": []}
    )
    orchestrator.alignment_agent = RequirementAlignmentAgent(llm_runner=alignment_llm)
    orchestrator.agent2 = analysis_agent
    orchestrator.candidate_agent = FakeCandidateAgent()
    orchestrator.style_agent = None
    orchestrator.agent3 = ScriptAgent()
    orchestrator.agent4 = ExecutorAgent()
    orchestrator.plan_service = EditPlanService()
    orchestrator.clarification_service = RequirementClarificationService()
    orchestrator.status = PipelineStatus(step="init")
    orchestrator.task_dir = None
    orchestrator.store = None
    orchestrator.state_machine = None
    orchestrator.preview_paths = {}
    orchestrator.video_path = None
    orchestrator._render_lock = __import__("threading").Lock()
    monkeypatch.setattr("src.orchestrator.OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(
        "src.orchestrator.FFmpegTool.get_video_info",
        lambda _: {"duration": 180, "has_audio": True, "has_video": True},
    )

    created = orchestrator.create_requirement_draft("input.mp4", brief.raw_text)

    assert created.alignment_proposal.requires_user_decision
    assert analysis_agent.calls == 1
    assert (orchestrator.task_dir / "material_analysis.json").exists()
    assert orchestrator.status.task_snapshot.state == "requirement_draft"

    with pytest.raises(ValueError, match="请先处理素材与需求对齐建议"):
        orchestrator.confirm_requirement_draft(
            orchestrator.status.requirement_gate.id,
            actor_id="tester",
        )

    revised = orchestrator.resolve_material_alignment("adopt", actor_id="tester")

    assert revised.spec.version == 2
    assert revised.spec.purpose == "主题党日活动访谈摘要"
    assert [item.description for item in revised.spec.requirements] == [
        "参与活动的感受",
        "对活动的改进建议",
        "人物发言或问答必须保留完整，不在表达中途截断",
    ]
    assert (orchestrator.task_dir / "material_alignment_decision.json").exists()

    orchestrator.confirm_requirement_draft(orchestrator.status.requirement_gate.id, "tester")
    orchestrator.analyze_confirmed_requirement("input.mp4")

    assert analysis_agent.calls == 1


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


def test_ignored_clarifications_do_not_become_fake_requirements():
    spec = RequirementSpec(
        brief_id="brief-ignore",
        target_duration=180,
        requirements=[RequirementItem(description="保留活动主题内容", priority="should")],
        open_questions=["重点是什么？", "哪些实体需要核对？"],
    )
    execution = ExecutionBrief(
        requirement_spec_id=spec.id,
        requirement_spec_version=1,
        visible_instruction="制作活动回顾。",
    )
    slots = [
        RequirementSlot(
            key="focus_keywords",
            label="重点内容",
            status="missing",
            question="重点是什么？",
        ),
        RequirementSlot(
            key="high_risk_review",
            label="高风险核对",
            status="unknown",
            question="哪些实体需要核对？",
        ),
    ]

    revised, _, revised_slots, turn = RequirementClarificationService().apply(
        task_id="task-ignore",
        spec=spec,
        execution=execution,
        slots=slots,
        answers={
            "focus_keywords": "暂不确定",
            "high_risk_review": "暂不确定",
        },
    )

    descriptions = [item.description for item in revised.requirements]
    assert not any("暂不确定" in description for description in descriptions)
    assert any("必须人工复核" in description for description in descriptions)
    assert all(slot.status == "unknown" for slot in revised_slots)
    assert turn.answers == {
        "focus_keywords": "暂时忽略",
        "high_risk_review": "暂时忽略",
    }


def test_compliance_requirement_is_not_used_as_clip_search_target():
    content = RequirementItem(description="保留主题发言", category="content", priority="should")
    compliance = RequirementItem(
        description="姓名和职务必须人工复核",
        category="compliance",
        priority="should",
        status="needs_confirmation",
    )
    evidence = EvidenceBuilder.from_transcript([
        TranscriptSegment(start=0, end=4, text="今天介绍活动主题")
    ])
    execution = ExecutionBrief(
        requirement_spec_id="spec-compliance",
        requirement_spec_version=1,
        visible_instruction="制作活动回顾。",
    )
    seen_requirements = []

    def runner(**kwargs):
        request = ast.literal_eval(kwargs["user_message"])
        seen_requirements.extend(item["id"] for item in request["requirements"])
        return {"candidates": []}

    CandidateAgent(
        llm_runner=runner,
        retriever=HybridEvidenceRetriever(),
    ).run(execution, [content, compliance], evidence, video_duration=10)

    assert seen_requirements == [content.id]


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
    assert compilation.spec.open_questions == []
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


def test_hybrid_retrieval_disables_dense_channel_after_first_service_failure():
    evidence = EvidenceBuilder.from_transcript([
        TranscriptSegment(start=0, end=3, text="运动会开幕式"),
    ])
    calls = 0

    def failed_embeddings(_):
        nonlocal calls
        calls += 1
        raise RuntimeError("embedding service unavailable")

    retriever = HybridEvidenceRetriever(embedding_fn=failed_embeddings)
    assert retriever.search("开幕式", evidence, top_k=1)
    assert retriever.search("运动会", evidence, top_k=1)
    assert calls == 1
    assert retriever.dense is None


def test_hybrid_retrieval_rejects_non_finite_embedding_values():
    evidence = EvidenceBuilder.from_transcript([
        TranscriptSegment(start=0, end=3, text="运动会开幕式"),
    ])

    retriever = HybridEvidenceRetriever(
        embedding_fn=lambda texts: [[float("nan"), 1.0] for _ in texts]
    )

    matches = retriever.search("开幕式", evidence, top_k=1)

    assert matches
    assert matches[0][0].content == "运动会开幕式"
    assert retriever.dense is None


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
        target_duration=3,
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

        def run(self, *args, **kwargs):
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
    assert orchestrator.status.task_snapshot.state == "succeeded"


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
