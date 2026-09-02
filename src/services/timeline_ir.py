"""从批准计划确定性编译编辑器无关时间线。"""

from __future__ import annotations

from src.models.schemas import (
    AuditableEditPlan,
    CanonicalTimeline,
    CanonicalTimelineClip,
    CanonicalTimelineSource,
    TaskMaterialSet,
)


class TimelineCompiler:
    """Adapter 不能读取业务草稿，只能消费此处生成的批准时间线。"""

    def compile(
        self,
        *,
        task_id: str,
        plan: AuditableEditPlan,
        material_set: TaskMaterialSet,
    ) -> CanonicalTimeline:
        if plan.status != "approved":
            raise ValueError("只有已批准计划可以编译通用时间线")
        sources = {
            source.id: CanonicalTimelineSource(
                source_asset_id=source.id,
                filename=source.filename,
                source_path=source.source_path,
                content_hash=source.content_hash,
                duration=source.duration,
                fps=source.fps or 30.0,
            )
            for source in material_set.sources
        }
        clips = []
        for segment in sorted(plan.timeline_segments, key=lambda item: item.order):
            source_id = segment.source_asset_id
            if source_id is None and len(sources) == 1:
                source_id = next(iter(sources))
            if not source_id or source_id not in sources:
                raise ValueError(f"时间线片段 {segment.order} 找不到来源素材")
            source = sources[source_id]
            if segment.source_end > source.duration:
                raise ValueError(f"时间线片段 {segment.order} 超出来源素材范围")
            clips.append(
                CanonicalTimelineClip(
                    order=segment.order,
                    source_asset_id=source_id,
                    source_in=segment.source_start,
                    source_out=segment.source_end,
                    timeline_in=segment.output_start,
                    timeline_out=segment.output_end,
                    candidate_id=segment.candidate_id,
                    matched_requirement_ids=segment.matched_requirement_ids,
                    evidence_ids=segment.evidence_ids,
                    subtitle_text=segment.subtitle_text,
                    title_text=segment.title_text,
                    transition_style=segment.transition_style,
                    transition_duration=segment.transition_duration,
                )
            )
        return CanonicalTimeline(
            version=plan.version,
            task_id=task_id,
            requirement_spec_id=plan.requirement_spec_id,
            requirement_spec_version=plan.requirement_spec_version,
            approved_plan_id=plan.id,
            approved_plan_version=plan.version,
            title=plan.execution_script.title,
            sources=list(sources.values()),
            clips=clips,
            subtitles_srt=plan.execution_script.srt_subtitles,
            estimated_duration=plan.estimated_duration,
            metadata={
                "subtitle_style": plan.delivery_spec.subtitle_style,
                "transition_duration": plan.delivery_spec.transition_duration,
                "intro_style": plan.delivery_spec.intro_style,
                "outro_style": plan.delivery_spec.outro_style,
                "title_text": plan.delivery_spec.title_text,
            },
        )
