"""可审核剪辑计划的构建、修订和确定性校验。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from src.models.schemas import (
    AuditableEditPlan,
    CandidateClip,
    CandidateDecision,
    ContentAnalysis,
    DeliverySpec,
    EditOperation,
    EditScript,
    RequirementSpec,
    TimelineSegment,
    TranscriptSegment,
    new_id,
)


@dataclass(frozen=True)
class PlanValidationResult:
    valid: bool
    errors: tuple[str, ...]
    missing_must_requirement_ids: tuple[str, ...] = ()


class EditPlanService:
    """时间线是审核事实，EditScript 从时间线确定性生成。"""

    def create(
        self,
        *,
        spec: RequirementSpec,
        analysis: ContentAnalysis,
        script: EditScript,
        delivery_spec: Optional[DeliverySpec] = None,
    ) -> AuditableEditPlan:
        delivery = delivery_spec or DeliverySpec(
            requirement_spec_id=spec.id,
            requirement_spec_version=spec.version,
            target_duration=spec.target_duration,
            duration_tolerance=spec.duration_tolerance,
            need_subtitles=spec.need_subtitles,
            subtitle_style=script.subtitle_style,
            transition_duration=script.transition_duration,
            intro_style=script.intro_style,
            outro_style=script.outro_style,
            title_text=script.title_text,
            bgm_path=script.bgm_path,
            bgm_volume=script.bgm_volume,
        )
        candidates = list(analysis.candidate_clips)
        selected_candidates: list[CandidateClip] = []
        for operation in script.operations:
            if operation.action != "cut" or operation.source_start is None or operation.source_end is None:
                continue
            matched = next(
                (
                    candidate for candidate in candidates
                    if abs(candidate.source_start - operation.source_start) < 0.01
                    and abs(candidate.source_end - operation.source_end) < 0.01
                ),
                None,
            )
            if matched:
                selected_candidates.append(matched)
        segments = self._segments_from_candidates(selected_candidates, delivery)
        execution = self._build_script(segments, analysis.transcript, delivery, title=script.title)
        return AuditableEditPlan(
            requirement_spec_id=spec.id,
            requirement_spec_version=spec.version,
            delivery_spec=delivery,
            timeline_segments=segments,
            candidate_ids=[segment.candidate_id for segment in segments],
            execution_script=execution,
            estimated_duration=execution.estimated_duration,
        )

    def revise(
        self,
        *,
        task_id: str,
        plan: AuditableEditPlan,
        analysis: ContentAnalysis,
        selected_candidate_ids: Optional[list[str]] = None,
        segment_updates: Optional[dict[str, dict]] = None,
        manual_segments: Optional[list[dict]] = None,
        delivery_updates: Optional[dict] = None,
        actor_id: Optional[str] = None,
    ) -> tuple[AuditableEditPlan, list[CandidateDecision]]:
        if plan.status not in {"draft", "approved"}:
            raise ValueError("只有当前草稿或已批准计划可以创建修订版")
        selected = set(
            plan.candidate_ids if selected_candidate_ids is None else selected_candidate_ids
        )
        updates = segment_updates or {}
        decisions: list[CandidateDecision] = []
        segments: list[TimelineSegment] = []
        for segment in plan.timeline_segments:
            if segment.candidate_id not in selected:
                decisions.append(
                    CandidateDecision(
                        task_id=task_id,
                        plan_id=plan.id,
                        plan_version=plan.version + 1,
                        candidate_id=segment.candidate_id,
                        action="delete",
                        before=segment.model_dump(mode="json"),
                        actor_id=actor_id,
                    )
                )
                continue
            patch = updates.get(segment.candidate_id, {})
            submitted = {
                key: value for key, value in patch.items()
                if key in {
                    "source_start", "source_end", "order", "title_text", "subtitle_text",
                    "transition_style", "transition_duration",
                }
            }
            allowed = {
                key: value for key, value in submitted.items()
                if getattr(segment, key) != value
            }
            revised = segment.model_copy(update=allowed)
            if revised.source_start < 0 or revised.source_end > analysis.video_duration:
                raise ValueError("修订片段超出素材范围")
            if allowed:
                keys = set(allowed)
                if keys == {"order"}:
                    action = "reorder"
                elif keys == {"subtitle_text"}:
                    action = "subtitle_edit"
                elif keys == {"title_text"}:
                    action = "title_edit"
                elif keys.issubset({"source_start", "source_end"}):
                    action = "trim"
                else:
                    action = "replace"
                decisions.append(
                    CandidateDecision(
                        task_id=task_id,
                        plan_id=plan.id,
                        plan_version=plan.version + 1,
                        candidate_id=segment.candidate_id,
                        action=action,
                        before=segment.model_dump(mode="json"),
                        after=revised.model_dump(mode="json"),
                        actor_id=actor_id,
                    )
                )
            elif selected_candidate_ids is not None:
                decisions.append(
                    CandidateDecision(
                        task_id=task_id,
                        plan_id=plan.id,
                        plan_version=plan.version + 1,
                        candidate_id=segment.candidate_id,
                        action="keep",
                        before=segment.model_dump(mode="json"),
                        after=segment.model_dump(mode="json"),
                        actor_id=actor_id,
                    )
                )
            segments.append(revised)
        for raw in manual_segments or []:
            candidate_id = str(raw.get("candidate_id") or new_id("manual_candidate"))
            source_start = float(raw["source_start"])
            source_end = float(raw["source_end"])
            if not 0 <= source_start < source_end <= analysis.video_duration:
                raise ValueError("人工补片超出素材范围")
            segment = TimelineSegment(
                order=int(raw.get("order", len(segments) + 1)),
                candidate_id=candidate_id,
                source_start=source_start,
                source_end=source_end,
                output_start=0,
                output_end=source_end - source_start,
                matched_requirement_ids=list(raw.get("matched_requirement_ids", [])),
                evidence_ids=list(raw.get("evidence_ids", [])),
                title_text=raw.get("title_text"),
                subtitle_text=raw.get("subtitle_text"),
            )
            segments.append(segment)
            decisions.append(
                CandidateDecision(
                    task_id=task_id,
                    plan_id=plan.id,
                    plan_version=plan.version + 1,
                    candidate_id=candidate_id,
                    action="manual_add",
                    after=segment.model_dump(mode="json"),
                    actor_id=actor_id,
                )
            )
        segments.sort(key=lambda item: (item.order, item.source_start))
        delivery = plan.delivery_spec.model_copy(update=delivery_updates or {})
        retimed = self._retime(segments, delivery)
        script = self._build_script(
            retimed,
            analysis.transcript,
            delivery,
            title=plan.execution_script.title,
        )
        revised_plan = plan.model_copy(
            update={
                "version": plan.version + 1,
                "delivery_spec": delivery.model_copy(update={"version": delivery.version + 1}),
                "timeline_segments": retimed,
                "candidate_ids": [segment.candidate_id for segment in retimed],
                "execution_script": script,
                "estimated_duration": script.estimated_duration,
                "approval_exceptions": [],
                "status": "draft",
                "approved_at": None,
            }
        )
        return revised_plan, decisions

    def validate(
        self,
        plan: AuditableEditPlan,
        spec: RequirementSpec,
        candidates: Iterable[CandidateClip],
        video_duration: float,
        known_evidence_ids: Optional[Iterable[str]] = None,
        user_annotation_evidence_ids: Optional[Iterable[str]] = None,
    ) -> PlanValidationResult:
        candidate_by_id = {candidate.id: candidate for candidate in candidates}
        requirement_ids = {item.id for item in spec.requirements}
        evidence_ids = set(known_evidence_ids) if known_evidence_ids is not None else None
        user_evidence_ids = (
            set(user_annotation_evidence_ids)
            if user_annotation_evidence_ids is not None else None
        )
        errors: list[str] = []
        covered = {
            requirement_id
            for segment in plan.timeline_segments
            for requirement_id in segment.matched_requirement_ids
        }
        must_ids = {item.id for item in spec.requirements if item.priority == "must"}
        prohibited_ids = {item.id for item in spec.requirements if item.priority == "prohibited"}
        missing = tuple(sorted(must_ids - covered))
        if missing:
            errors.extend(f"missing_must:{item_id}" for item_id in missing)
        for segment in plan.timeline_segments:
            if not 0 <= segment.source_start < segment.source_end <= video_duration:
                errors.append(f"segment_outside_media:{segment.id}")
            if not segment.matched_requirement_ids:
                errors.append(f"segment_missing_requirement:{segment.id}")
            if not segment.evidence_ids:
                errors.append(f"segment_missing_evidence:{segment.id}")
            if prohibited_ids.intersection(segment.matched_requirement_ids):
                errors.append(f"prohibited_in_plan:{segment.id}")
            unknown_requirements = set(segment.matched_requirement_ids) - requirement_ids
            if unknown_requirements:
                errors.append(f"unknown_requirement:{segment.id}")
            if evidence_ids is not None and not set(segment.evidence_ids).issubset(evidence_ids):
                errors.append(f"unknown_evidence:{segment.id}")
            if not segment.candidate_id.startswith("manual_candidate_"):
                candidate = candidate_by_id.get(segment.candidate_id)
                if candidate is None:
                    errors.append(f"unknown_candidate:{segment.candidate_id}")
                elif not (
                    candidate.source_start - 5 <= segment.source_start
                    and segment.source_end <= candidate.source_end + 5
                ):
                    errors.append(f"segment_exceeds_candidate_context:{segment.id}")
            elif user_evidence_ids is not None and not set(segment.evidence_ids).intersection(user_evidence_ids):
                errors.append(f"manual_segment_missing_user_annotation:{segment.id}")
        operation_ranges = [
            (operation.source_start, operation.source_end)
            for operation in plan.execution_script.operations
            if operation.action == "cut"
        ]
        timeline_ranges = [
            (segment.source_start, segment.source_end) for segment in plan.timeline_segments
        ]
        if operation_ranges != timeline_ranges:
            errors.append("execution_script_mismatch")
        if abs(plan.estimated_duration - plan.execution_script.estimated_duration) > 0.01:
            errors.append("estimated_duration_mismatch")
        lower_duration = max(
            0.0,
            plan.delivery_spec.target_duration - plan.delivery_spec.duration_tolerance,
        )
        upper_duration = (
            plan.delivery_spec.target_duration + plan.delivery_spec.duration_tolerance
        )
        if plan.estimated_duration < lower_duration:
            errors.append("plan_duration_too_short")
        elif plan.estimated_duration > upper_duration:
            errors.append("plan_duration_too_long")
        return PlanValidationResult(not errors, tuple(dict.fromkeys(errors)), missing)

    def _segments_from_candidates(
        self,
        candidates: Iterable[CandidateClip],
        delivery: DeliverySpec,
    ) -> list[TimelineSegment]:
        segments = [
            TimelineSegment(
                order=index,
                candidate_id=candidate.id,
                source_start=candidate.source_start,
                source_end=candidate.source_end,
                output_start=0,
                output_end=candidate.source_end - candidate.source_start,
                matched_requirement_ids=candidate.matched_requirement_ids,
                evidence_ids=[citation.evidence_id for citation in candidate.citations],
                transition_style="fade" if delivery.transition_duration else "cut",
                transition_duration=delivery.transition_duration,
            )
            for index, candidate in enumerate(candidates, start=1)
        ]
        return self._retime(segments, delivery)

    @staticmethod
    def _retime(
        segments: Iterable[TimelineSegment],
        delivery: DeliverySpec,
    ) -> list[TimelineSegment]:
        offset = 2.0 if delivery.intro_style != "none" else 0.0
        retimed = []
        for index, segment in enumerate(segments, start=1):
            duration = segment.source_end - segment.source_start
            start = offset
            end = start + duration
            retimed.append(
                segment.model_copy(
                    update={
                        "order": index,
                        "output_start": start,
                        "output_end": end,
                        "transition_style": "fade" if delivery.transition_duration else "cut",
                        "transition_duration": delivery.transition_duration,
                    }
                )
            )
            offset = end - delivery.transition_duration
        return retimed

    def _build_script(
        self,
        segments: list[TimelineSegment],
        transcript: list[TranscriptSegment],
        delivery: DeliverySpec,
        *,
        title: str,
    ) -> EditScript:
        operations = [
            EditOperation(
                order=segment.order,
                action="cut",
                source_start=segment.source_start,
                source_end=segment.source_end,
                note=segment.title_text or "已审核候选片段",
            )
            for segment in segments
        ]
        media_duration = sum(segment.source_end - segment.source_start for segment in segments)
        transition_overlap = delivery.transition_duration * max(0, len(segments) - 1)
        card_duration = 2.0 * sum(
            style != "none" for style in (delivery.intro_style, delivery.outro_style)
        )
        estimated = max(0.0, media_duration - transition_overlap + card_duration)
        subtitles = self._build_srt(transcript, segments) if delivery.need_subtitles else ""
        return EditScript(
            title=title or "活动回顾",
            estimated_duration=estimated,
            operations=operations,
            srt_subtitles=subtitles,
            subtitle_style=delivery.subtitle_style,
            transition_duration=delivery.transition_duration,
            bgm_path=delivery.bgm_path,
            bgm_volume=delivery.bgm_volume,
            intro_style=delivery.intro_style,
            outro_style=delivery.outro_style,
            title_text=delivery.title_text or title,
            notes="由版本化审核时间线确定性生成。",
        )

    @staticmethod
    def _build_srt(
        transcript: list[TranscriptSegment],
        segments: list[TimelineSegment],
    ) -> str:
        blocks: list[str] = []
        subtitle_index = 1
        for segment in segments:
            if segment.subtitle_text:
                blocks.extend([
                    str(subtitle_index),
                    f"{EditPlanService._srt_time(segment.output_start)} --> {EditPlanService._srt_time(segment.output_end)}",
                    segment.subtitle_text,
                    "",
                ])
                subtitle_index += 1
                continue
            for source in transcript:
                overlap_start = max(source.start, segment.source_start)
                overlap_end = min(source.end, segment.source_end)
                if overlap_end <= overlap_start or not source.text.strip():
                    continue
                output_start = segment.output_start + overlap_start - segment.source_start
                output_end = segment.output_start + overlap_end - segment.source_start
                blocks.extend([
                    str(subtitle_index),
                    f"{EditPlanService._srt_time(output_start)} --> {EditPlanService._srt_time(output_end)}",
                    source.text.strip(),
                    "",
                ])
                subtitle_index += 1
        return "\n".join(blocks)

    @staticmethod
    def _srt_time(seconds: float) -> str:
        milliseconds = max(0, round(seconds * 1000))
        hours, remainder = divmod(milliseconds, 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        secs, millis = divmod(remainder, 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"
