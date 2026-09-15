from types import SimpleNamespace

import pytest

from src.agents.candidate_agent import CandidateAgent
from src.agents.script_agent import ScriptAgent
from src.models.schemas import (
    CandidateClip, ContentAnalysis, Evidence, EvidenceCitation, ExecutionBrief,
    HighlightClip, RequirementItem, RequirementSpec, TranscriptSegment, VideoRequirement,
)
from src.orchestrator import VideoEditOrchestrator
from src.services.evidence import BM25EvidenceRetriever, EvidenceBuilder
from src.services.plans import EditPlanService
from src.ui.app import _delivery_markdown, _editable_requirement_value, describe_candidate_boundary


def _highlight(candidate):
    return HighlightClip(
        start=candidate.source_start, end=candidate.source_end,
        source_asset_id=candidate.source_asset_id, text="素材内容", importance=candidate.confidence,
        category="highlight", reason=candidate.selection_reason, candidate_id=candidate.id,
        matched_requirement_ids=candidate.matched_requirement_ids,
    )


def test_required_fields_do_not_put_pending_marker_inside_input():
    assert _editable_requirement_value("待你确认") == ""
    assert _editable_requirement_value(" 公众号 ") == "公众号"


def test_delivery_method_human_has_chinese_label():
    report = SimpleNamespace(status="passed", results=[
        SimpleNamespace(status="passed", method="human", summary="已核对人物姓名")
    ])
    text = _delivery_markdown(report)
    assert "人工确认" in text
    assert "human" not in text


def test_candidate_generation_keeps_reserve_after_raw_sum_reaches_target():
    requirement = RequirementItem(description="保留活动内容", priority="should")
    brief = ExecutionBrief(requirement_spec_id="spec", requirement_spec_version=1,
                           visible_instruction="活动回顾", included_requirement_ids=[requirement.id])
    ranges = [(0, 21), (17, 38), (38, 59), (55, 77), (77, 98), (100, 120)]
    evidence = [Evidence(source_start=start, source_end=end, content=f"活动内容 {index}")
                for index, (start, end) in enumerate(ranges)]
    def no_model(**kwargs):
        raise AssertionError("补选不应调用 LLM")
    agent = CandidateAgent(llm_runner=no_model, retriever=BM25EvidenceRetriever())
    result = agent.run(brief, [requirement], evidence, 120,
                       target_duration=60, duration_tolerance=8, rank_with_model=False)
    assert len(result.valid_candidates) == 6
    analysis = ContentAnalysis(video_duration=120, highlights=[_highlight(item) for item in result.valid_candidates])
    script = ScriptAgent().run(analysis, VideoRequirement(target_duration=60, video_type="general", style="general"),
                               duration_tolerance=8, transition_duration=0.35)
    assert 60 <= script.estimated_duration <= 68
    for left, right in zip(script.operations, script.operations[1:]):
        assert left.source_end <= right.source_start
    assert script.estimated_duration == pytest.approx(
        sum(item.source_end - item.source_start for item in script.operations)
        - 0.35 * (len(script.operations) - 1)
    )


def test_duration_supplement_preserves_edits_and_does_not_restore_deleted_ranges():
    item = RequirementItem(description="活动内容", priority="should")
    spec = RequirementSpec(brief_id="brief", target_duration=100, duration_tolerance=10, requirements=[item])
    transcript = [TranscriptSegment(start=index * 20, end=(index + 1) * 20,
                                    text=f"活动内容第 {index} 段。") for index in range(8)]
    evidence = EvidenceBuilder.from_transcript(transcript)
    first = CandidateClip(source_start=0, source_end=20, matched_requirement_ids=[item.id],
                          citations=[EvidenceCitation(requirement_id=item.id, evidence_id=evidence[0].id,
                                                      quote=evidence[0].content)],
                          selection_reason="开场", confidence=0.8, suggested_duration=20)
    analysis = ContentAnalysis(video_duration=160, transcript=transcript, evidence=evidence,
                               candidate_clips=[first], highlights=[_highlight(first)])
    service = EditPlanService()
    script = ScriptAgent().run(analysis, VideoRequirement(target_duration=100, video_type="general", style="general"))
    plan = service.create(spec=spec, analysis=analysis, script=script)
    plan.timeline_segments[0].subtitle_text = "用户修改过的字幕"
    orch = VideoEditOrchestrator.__new__(VideoEditOrchestrator)
    orch.agent3 = ScriptAgent()
    orch.status = SimpleNamespace(analysis=analysis, edit_plan=plan, requirement_spec=spec,
        execution_brief=ExecutionBrief(requirement_spec_id=spec.id, requirement_spec_version=1,
                                      visible_instruction="活动回顾", included_requirement_ids=[item.id]))
    orch.store = SimpleNamespace(task_id="test", write_model=lambda *args: None,
                                 write_payload=lambda *args: None, append_audit=lambda *args: None)
    def revise(**kwargs):
        updated, _ = service.revise(task_id="test", plan=orch.status.edit_plan,
                                    analysis=orch.status.analysis, **kwargs)
        orch.status.edit_plan = updated
        return updated
    orch.revise_edit_plan = revise
    result = orch.supplement_plan_duration(
        excluded_ranges=[{"source_asset_id": None, "start": 20, "end": 40}], actor_id="user")
    assert result.estimated_duration >= 100
    assert result.timeline_segments[0].subtitle_text == "用户修改过的字幕"
    assert not any(max(segment.source_start, 20) < min(segment.source_end, 40)
                   for segment in result.timeline_segments)
    assert service.validate(result, spec, orch.status.analysis.candidate_clips, 160,
                            [entry.id for entry in evidence]).valid


def test_boundary_feedback_rejects_inverted_range():
    assert "结尾必须在开头之后" in describe_candidate_boundary(None, 1, 12, 10)
