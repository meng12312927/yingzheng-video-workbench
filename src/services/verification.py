"""MVP2 逐项确定性验收与交付异常处理。"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

from src.models.schemas import (
    AuditableEditPlan,
    CandidateClip,
    DeliveryReport,
    ExecutionResult,
    RequirementSpec,
    VerificationResult,
)
from src.tools.ffmpeg import FFmpegTool


class VerificationEngine:
    def verify(
        self,
        *,
        task_id: str,
        execution_result: ExecutionResult,
        spec: RequirementSpec,
        plan: AuditableEditPlan,
        candidates: Iterable[CandidateClip],
    ) -> DeliveryReport:
        candidate_by_id = {candidate.id: candidate for candidate in candidates}
        results: list[VerificationResult] = []
        output = Path(execution_result.output_path)
        if not execution_result.success:
            results.append(self._system("failed", "渲染执行失败。", "render_failed"))
        if not output.exists() or output.stat().st_size <= 0:
            results.append(self._system("failed", "输出文件不存在或为空。", "output_missing"))
            media_info = None
        else:
            media_info = FFmpegTool.get_video_info(str(output))
            if not media_info:
                results.append(self._system("failed", "输出文件无法解析。", "output_unreadable"))
            elif not media_info.get("has_video", True):
                results.append(self._system("failed", "输出文件缺少视频流。", "video_stream_missing"))
            else:
                results.append(self._system("passed", "输出文件存在、非空且可解析。", "output_valid"))
                if not media_info.get("has_audio"):
                    results.append(self._system("failed", "输出文件缺少音频流。", "audio_stream_missing"))

        if plan.status != "approved":
            results.append(self._system("failed", "渲染计划未绑定有效批准版本。", "plan_not_approved"))
        else:
            results.append(self._system("passed", f"使用已批准计划 v{plan.version}。", "plan_approved"))
        if plan.requirement_spec_id != spec.id or plan.requirement_spec_version != spec.version:
            results.append(self._system("failed", "计划与当前需求版本不匹配。", "spec_version_mismatch"))

        actual_duration = float(media_info.get("duration", 0.0)) if media_info else execution_result.output_duration
        lower = max(0.0, spec.target_duration - spec.duration_tolerance)
        upper = spec.target_duration + spec.duration_tolerance
        duration_status = "passed" if lower <= actual_duration <= upper else "failed"
        results.append(
            self._system(
                duration_status,
                f"成片时长 {actual_duration:.1f} 秒；任务书要求 {lower:.1f}–{upper:.1f} 秒。",
                "duration_in_range" if duration_status == "passed" else "duration_out_of_range",
            )
        )

        timeline_candidate_ids = {segment.candidate_id for segment in plan.timeline_segments}
        unknown = {
            candidate_id for candidate_id in timeline_candidate_ids
            if not candidate_id.startswith("manual_candidate_") and candidate_id not in candidate_by_id
        }
        if unknown:
            results.append(self._system("failed", "计划包含未知候选。", "unknown_plan_candidate"))
        if spec.need_subtitles and not plan.execution_script.srt_subtitles.strip():
            results.append(self._system("failed", "任务书要求字幕，但计划没有字幕。", "subtitle_missing"))
        elif spec.need_subtitles:
            results.append(self._system("passed", "计划包含已重映射字幕。", "subtitle_present"))

        covered = {
            requirement_id
            for segment in plan.timeline_segments
            for requirement_id in segment.matched_requirement_ids
        }
        evidence_by_requirement: dict[str, list[str]] = {}
        for segment in plan.timeline_segments:
            for requirement_id in segment.matched_requirement_ids:
                evidence_by_requirement.setdefault(requirement_id, []).extend(segment.evidence_ids)
        for item in spec.requirements:
            evidence_ids = list(dict.fromkeys(evidence_by_requirement.get(item.id, [])))
            if item.priority == "must":
                status = "passed" if item.id in covered else "failed"
                summary = (
                    f"必须项已由批准片段覆盖：{item.description}"
                    if status == "passed"
                    else f"必须项没有进入成片：{item.description}"
                )
            elif item.priority == "prohibited":
                status = "failed" if item.id in covered else "passed"
                summary = (
                    f"禁止项进入了成片：{item.description}"
                    if status == "failed"
                    else f"未发现禁止项进入成片：{item.description}"
                )
            elif item.status == "needs_confirmation" or item.category in {"compliance", "entity"}:
                status = "manual_review"
                summary = f"该要求需要人工确认：{item.description}"
            else:
                status = "passed" if item.id in covered else "warning"
                summary = (
                    f"要求已有候选覆盖：{item.description}"
                    if status == "passed"
                    else f"非必须要求未被当前成片覆盖：{item.description}"
                )
            results.append(
                VerificationResult(
                    requirement_id=item.id,
                    status=status,
                    method="deterministic" if status != "manual_review" else "human",
                    summary=summary,
                    evidence_ids=evidence_ids,
                    code=f"requirement_{item.priority}_{status}",
                )
            )

        needs_resolution = any(
            result.status in {"failed", "warning", "manual_review"} for result in results
        )
        return DeliveryReport(
            task_id=task_id,
            output_path=str(output),
            requirement_spec_id=spec.id,
            requirement_spec_version=spec.version,
            edit_plan_id=plan.id,
            edit_plan_version=plan.version,
            results=results,
            status="needs_resolution" if needs_resolution else "passed",
        )

    @staticmethod
    def approve_exceptions(
        report: DeliveryReport,
        *,
        actor_id: str,
        reason: str,
    ) -> DeliveryReport:
        if report.status != "needs_resolution":
            raise ValueError("只有存在异常的交付报告需要例外批准")
        if not reason.strip():
            raise ValueError("接受交付例外时必须填写原因")
        return report.model_copy(
            update={
                "status": "approved_with_exceptions",
                "approved_by": actor_id,
                "exception_reason": reason.strip(),
            }
        )

    @staticmethod
    def _system(status: str, summary: str, code: str) -> VerificationResult:
        return VerificationResult(
            requirement_id="SYSTEM",
            status=status,
            method="deterministic",
            summary=summary,
            code=code,
        )

