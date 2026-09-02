"""剪映交接包与 OpenTimelineIO 导出适配器。"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Protocol

from src.models.schemas import CanonicalTimeline, EditorExportResult
from src.tools.ffmpeg import FFmpegTool


class EditorAdapter(Protocol):
    name: str

    def export(
        self,
        timeline: CanonicalTimeline,
        output_dir: Path,
    ) -> EditorExportResult:
        ...


class JianyingHandoffAdapter:
    """不写私有草稿：输出可直接拖入剪映的顺序片段和审核资料。"""

    name = "jianying_handoff"

    def __init__(self, *, render_clips: bool = True):
        self.render_clips = render_clips

    def export(
        self,
        timeline: CanonicalTimeline,
        output_dir: Path,
    ) -> EditorExportResult:
        destination = Path(output_dir).resolve()
        destination.mkdir(parents=True, exist_ok=True)
        clips_dir = destination / "clips"
        if self.render_clips:
            clips_dir.mkdir(exist_ok=True)
        sources = {item.source_asset_id: item for item in timeline.sources}
        artifacts: list[str] = []
        errors: list[str] = []
        rows = []
        for clip in sorted(timeline.clips, key=lambda item: item.order):
            source = sources[clip.source_asset_id]
            clip_name = f"{clip.order:03d}_{Path(source.filename).stem}.mp4"
            relative_clip = f"clips/{clip_name}"
            if self.render_clips:
                clip_path = clips_dir / clip_name
                if FFmpegTool.cut_segment(
                    source.source_path,
                    clip.source_in,
                    clip.source_out,
                    str(clip_path),
                ):
                    artifacts.append(str(clip_path))
                else:
                    errors.append(f"第 {clip.order} 个交接片段生成失败")
            rows.append({
                "顺序": clip.order,
                "片段文件": relative_clip,
                "原素材": source.filename,
                "原素材开始秒": f"{clip.source_in:.3f}",
                "原素材结束秒": f"{clip.source_out:.3f}",
                "预计成片开始秒": f"{clip.timeline_in:.3f}",
                "预计成片结束秒": f"{clip.timeline_out:.3f}",
                "转场": "淡化" if clip.transition_style == "fade" else "直接切换",
            })
        timeline_path = destination / "timeline.json"
        timeline_path.write_text(
            timeline.model_dump_json(indent=2), encoding="utf-8"
        )
        artifacts.append(str(timeline_path))
        csv_path = destination / "clip_order.csv"
        with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else ["顺序"])
            writer.writeheader()
            writer.writerows(rows)
        artifacts.append(str(csv_path))
        if timeline.subtitles_srt:
            subtitle_path = destination / "subtitles.srt"
            subtitle_path.write_text(timeline.subtitles_srt, encoding="utf-8")
            artifacts.append(str(subtitle_path))
        readme_path = destination / "导入剪映说明.md"
        readme_path.write_text(
            "# 剪映交接包\n\n"
            "1. 新建剪映项目，将 `clips` 文件夹中的视频全部导入。\n"
            "2. 按文件名前的三位序号依次拖入主轨。\n"
            "3. 如需字幕，导入 `subtitles.srt`。\n"
            "4. `clip_order.csv` 和 `timeline.json` 用于核对来源、时间和审核记录。\n\n"
            "本交接包不修改剪映私有草稿，也不承诺跨版本复现剪映专属特效。\n",
            encoding="utf-8",
        )
        artifacts.append(str(readme_path))
        return EditorExportResult(
            adapter=self.name,
            timeline_id=timeline.id,
            timeline_version=timeline.version,
            success=not errors,
            output_path=str(destination),
            artifacts=artifacts,
            errors=errors,
            warnings=(
                ["本次只生成了时间线清单，没有预裁剪片段。"]
                if not self.render_clips else []
            ),
        )


class OTIOAdapter:
    """输出标准 OTIO JSON，供支持 OpenTimelineIO 的工具继续转换。"""

    name = "otio"

    def export(
        self,
        timeline: CanonicalTimeline,
        output_dir: Path,
    ) -> EditorExportResult:
        destination = Path(output_dir).resolve()
        destination.mkdir(parents=True, exist_ok=True)
        sources = {item.source_asset_id: item for item in timeline.sources}
        children = []
        for clip in sorted(timeline.clips, key=lambda item: item.order):
            source = sources[clip.source_asset_id]
            rate = source.fps
            children.append({
                "OTIO_SCHEMA": "Clip.2",
                "name": f"{clip.order:03d}_{source.filename}",
                "metadata": {
                    "映证": {
                        "candidate_id": clip.candidate_id,
                        "requirement_ids": clip.matched_requirement_ids,
                        "evidence_ids": clip.evidence_ids,
                    }
                },
                "source_range": self._time_range(
                    clip.source_in, clip.source_out - clip.source_in, rate
                ),
                "media_reference": {
                    "OTIO_SCHEMA": "ExternalReference.1",
                    "name": source.filename,
                    "target_url": Path(source.source_path).resolve().as_uri(),
                    "available_range": self._time_range(0, source.duration, rate),
                    "metadata": {},
                },
                "effects": [],
                "markers": [],
                "enabled": True,
            })
        payload = {
            "OTIO_SCHEMA": "Timeline.1",
            "name": timeline.title or "映证时间线",
            "global_start_time": None,
            "metadata": {
                "映证": {
                    "timeline_id": timeline.id,
                    "timeline_version": timeline.version,
                    "approved_plan_id": timeline.approved_plan_id,
                }
            },
            "tracks": {
                "OTIO_SCHEMA": "Stack.1",
                "name": "tracks",
                "metadata": {},
                "effects": [],
                "markers": [],
                "enabled": True,
                "children": [{
                    "OTIO_SCHEMA": "Track.1",
                    "name": "V1",
                    "kind": "Video",
                    "metadata": {},
                    "effects": [],
                    "markers": [],
                    "enabled": True,
                    "children": children,
                }],
            },
        }
        output_path = destination / f"timeline_v{timeline.version}.otio"
        output_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return EditorExportResult(
            adapter=self.name,
            timeline_id=timeline.id,
            timeline_version=timeline.version,
            success=True,
            output_path=str(output_path),
            artifacts=[str(output_path)],
        )

    @staticmethod
    def _time_range(start_seconds: float, duration_seconds: float, rate: float) -> dict:
        return {
            "OTIO_SCHEMA": "TimeRange.1",
            "start_time": {
                "OTIO_SCHEMA": "RationalTime.1",
                "value": round(start_seconds * rate),
                "rate": rate,
            },
            "duration": {
                "OTIO_SCHEMA": "RationalTime.1",
                "value": round(duration_seconds * rate),
                "rate": rate,
            },
        }
