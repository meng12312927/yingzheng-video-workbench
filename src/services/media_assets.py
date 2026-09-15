"""多素材登记、独立转录与任务级只读聚合视图。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable, Iterable

from src.models.schemas import (
    ContentAnalysis,
    SourceAnalysisResult,
    SourceAsset,
    TaskMaterialSet,
    VideoRequirement,
    utc_now,
)
from src.services.task_store import TaskStore
from src.tools.ffmpeg import FFmpegTool


class MediaAssetService:
    """原素材不拼接；所有事实先落在 ``source_asset_id + local_time`` 上。"""

    def __init__(
        self,
        cache_root: Path | None = None,
        cache_namespace: str = "default",
    ):
        self.cache_root = Path(cache_root) if cache_root else None
        self.cache_namespace = cache_namespace

    def _cached_analysis(self, source: SourceAsset) -> ContentAnalysis | None:
        if not self.cache_root:
            return None
        cache_dir = self.cache_root / self.cache_namespace / source.id
        try:
            return TaskStore(cache_dir).read_model("analysis.json", ContentAnalysis)
        except (FileNotFoundError, ValueError, OSError):
            return None

    def _save_cached_analysis(self, source: SourceAsset, analysis: ContentAnalysis) -> None:
        if not self.cache_root:
            return
        cache_dir = self.cache_root / self.cache_namespace / source.id
        TaskStore(cache_dir).write_model("analysis.json", analysis)

    def register(
        self,
        task_id: str,
        source_paths: Iterable[str],
    ) -> TaskMaterialSet:
        assets: list[SourceAsset] = []
        for order, raw_path in enumerate(source_paths, start=1):
            path = Path(raw_path).expanduser().resolve()
            info = FFmpegTool.get_video_info(str(path))
            if not info:
                raise ValueError(f"第 {order} 段素材无法读取，请重新上传")
            duration = float(info.get("duration", 0.0))
            if duration <= 0:
                raise ValueError(f"第 {order} 段素材时长无效，请重新上传")
            digest = self._sha256(path)
            assets.append(
                SourceAsset(
                    id=f"asset_{order:03d}_{digest[:12]}",
                    order=order,
                    filename=path.name,
                    source_path=str(path),
                    content_hash=digest,
                    duration=duration,
                    width=int(info.get("width", 0) or 0),
                    height=int(info.get("height", 0) or 0),
                    fps=self._parse_fps(info.get("fps")),
                    codec=str(info.get("codec", "") or ""),
                    size_mb=(
                        float(info["size_mb"])
                        if info.get("size_mb") is not None
                        else None
                    ),
                    has_audio=bool(info.get("has_audio")),
                    analysis_state=(
                        "preflight_ok" if info.get("has_audio") else "no_audio"
                    ),
                )
            )
        if not assets:
            raise ValueError("请至少上传一段视频素材")
        return TaskMaterialSet(task_id=task_id, sources=assets, status="registered")

    def analyze(
        self,
        material_set: TaskMaterialSet,
        requirement: VideoRequirement,
        analysis_agent,
        store: TaskStore,
        *,
        run_in_context: Callable[[SourceAsset], ContentAnalysis] | None = None,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> tuple[TaskMaterialSet, ContentAnalysis]:
        """逐素材分析并持久化；单个素材失败只产生局部失败记录。"""
        results: list[SourceAnalysisResult] = []
        analyzing = material_set.model_copy(
            update={"status": "analyzing", "updated_at": utc_now()}
        )
        store.write_model("material_set.json", analyzing)
        source_count = len(analyzing.sources)
        for source_index, source in enumerate(analyzing.sources, start=1):
            if progress_callback:
                progress_callback(
                    (source_index - 1) / max(1, source_count),
                    f"正在处理第 {source_index}/{source_count} 段：{source.filename}",
                )
            source_store = TaskStore(store.task_dir / "sources" / source.id)
            source_store.write_model("source_manifest.json", source)
            if not source.has_audio:
                skipped = source.model_copy(update={"analysis_state": "no_audio"})
                result = SourceAnalysisResult(
                    source=skipped,
                    status="no_audio",
                    warnings=["该素材没有音频流，当前版本不生成语音转录；素材仍可用于人工补片。"],
                    attempt_count=0,
                    completed_at=utc_now(),
                )
                results.append(result)
                source_store.write_model("analysis_result.json", result)
                continue
            transcribing = source.model_copy(update={"analysis_state": "transcribing"})
            source_store.write_model("source_manifest.json", transcribing)
            try:
                analysis = self._cached_analysis(source)
                cache_hit = analysis is not None
                if analysis is not None:
                    pass
                elif run_in_context is not None:
                    analysis = run_in_context(source)
                else:
                    analysis = analysis_agent.run(
                        source.source_path,
                        requirement,
                        source_asset_id=source.id,
                    )
                if not cache_hit:
                    self._save_cached_analysis(source, analysis)
                ready = source.model_copy(update={"analysis_state": "ready"})
                result = SourceAnalysisResult(
                    source=ready,
                    analysis=analysis,
                    status="ready",
                    attempt_count=0 if cache_hit else 1,
                    warnings=["已复用相同素材的转录缓存"] if cache_hit else [],
                    completed_at=utc_now(),
                )
                source_store.write_model("source_manifest.json", ready)
                source_store.write_model("analysis.json", analysis)
                source_store.write_payload(
                    "transcript.json",
                    {"segments": [item.model_dump(mode="json") for item in analysis.transcript]},
                )
                source_store.write_payload(
                    "evidence.json",
                    {"evidence": [item.model_dump(mode="json") for item in analysis.evidence]},
                )
            except Exception as error:
                failed = source.model_copy(
                    update={
                        "analysis_state": "failed",
                        "error_code": type(error).__name__,
                        "error_message": str(error)[:500],
                    }
                )
                result = SourceAnalysisResult(
                    source=failed,
                    status="failed",
                    error_code=type(error).__name__,
                    error_message=str(error)[:500],
                    attempt_count=1,
                    completed_at=utc_now(),
                )
                source_store.write_model("source_manifest.json", failed)
            results.append(result)
            source_store.write_model("analysis_result.json", result)
            if progress_callback:
                progress_callback(
                    source_index / max(1, source_count),
                    f"已完成第 {source_index}/{source_count} 段：{source.filename}",
                )

        ready_count = sum(item.status == "ready" for item in results)
        failed_count = sum(item.status == "failed" for item in results)
        if ready_count and failed_count:
            aggregate_status = "partial"
        elif ready_count:
            aggregate_status = "ready"
        else:
            aggregate_status = "failed"
        completed = analyzing.model_copy(
            update={
                "sources": [item.source for item in results],
                "results": results,
                "status": aggregate_status,
                "updated_at": utc_now(),
            }
        )
        aggregate = self.aggregate(completed)
        store.write_model("material_set.json", completed)
        store.write_model("material_analysis.json", aggregate)
        store.write_payload(
            "transcript.json",
            {"segments": [item.model_dump(mode="json") for item in aggregate.transcript]},
        )
        store.write_payload(
            "evidence.json",
            {"evidence": [item.model_dump(mode="json") for item in aggregate.evidence]},
        )
        return completed, aggregate

    def retry_source(
        self,
        material_set: TaskMaterialSet,
        *,
        source_asset_id: str,
        requirement: VideoRequirement,
        analysis_agent,
        store: TaskStore,
        retry_reason: str,
        run_in_context: Callable[[SourceAsset], ContentAnalysis] | None = None,
    ) -> tuple[TaskMaterialSet, ContentAnalysis]:
        """只重新分析指定素材，已成功的其他素材结果原样复用。"""
        source = next(
            (item for item in material_set.sources if item.id == source_asset_id), None
        )
        if source is None:
            raise ValueError("找不到要重试的来源素材")
        if not source.has_audio:
            raise ValueError("该素材没有音频流，不能重试语音转录")
        previous = next(
            (item for item in material_set.results if item.source.id == source_asset_id),
            None,
        )
        attempt = (previous.attempt_count if previous else 0) + 1
        source_store = TaskStore(store.task_dir / "sources" / source.id)
        transcribing = source.model_copy(
            update={"analysis_state": "transcribing", "error_code": None, "error_message": None}
        )
        source_store.write_model("source_manifest.json", transcribing)
        try:
            analysis = (
                run_in_context(source)
                if run_in_context is not None
                else analysis_agent.run(
                    source.source_path,
                    requirement,
                    source_asset_id=source.id,
                )
            )
            updated_source = source.model_copy(update={
                "analysis_state": "ready",
                "error_code": None,
                "error_message": None,
            })
            replacement = SourceAnalysisResult(
                source=updated_source,
                analysis=analysis,
                status="ready",
                attempt_count=attempt,
                last_retry_reason=retry_reason,
                completed_at=utc_now(),
            )
            source_store.write_model("analysis.json", analysis)
            source_store.write_payload(
                "transcript.json",
                {"segments": [item.model_dump(mode="json") for item in analysis.transcript]},
            )
            source_store.write_payload(
                "evidence.json",
                {"evidence": [item.model_dump(mode="json") for item in analysis.evidence]},
            )
        except Exception as error:
            updated_source = source.model_copy(update={
                "analysis_state": "failed",
                "error_code": type(error).__name__,
                "error_message": str(error)[:500],
            })
            replacement = SourceAnalysisResult(
                source=updated_source,
                status="failed",
                error_code=type(error).__name__,
                error_message=str(error)[:500],
                attempt_count=attempt,
                last_retry_reason=retry_reason,
                completed_at=utc_now(),
            )
        results = [
            replacement if item.source.id == source_asset_id else item
            for item in material_set.results
        ]
        if not any(item.source.id == source_asset_id for item in material_set.results):
            results.append(replacement)
        ready_count = sum(item.status == "ready" for item in results)
        failed_count = sum(item.status == "failed" for item in results)
        status = (
            "partial" if ready_count and failed_count
            else "ready" if ready_count
            else "failed"
        )
        completed = material_set.model_copy(update={
            "sources": [
                updated_source if item.id == source_asset_id else item
                for item in material_set.sources
            ],
            "results": results,
            "status": status,
            "updated_at": utc_now(),
        })
        aggregate = self.aggregate(completed)
        source_store.write_model("source_manifest.json", updated_source)
        source_store.write_model("analysis_result.json", replacement)
        store.write_model("material_set.json", completed)
        store.write_model("material_analysis.json", aggregate)
        store.write_payload(
            "transcript.json",
            {"segments": [item.model_dump(mode="json") for item in aggregate.transcript]},
        )
        store.write_payload(
            "evidence.json",
            {"evidence": [item.model_dump(mode="json") for item in aggregate.evidence]},
        )
        return completed, aggregate

    @staticmethod
    def aggregate(material_set: TaskMaterialSet) -> ContentAnalysis:
        """生成只供检索/展示的任务级视图，不改变任何来源的本地时间。"""
        analyses = [item.analysis for item in material_set.results if item.analysis]
        summaries = [
            f"{item.source.filename}：{item.analysis.summary}"
            for item in material_set.results
            if item.analysis and item.analysis.summary
        ]
        keyword_timeline: dict[str, list[float]] = {}
        for result in material_set.results:
            if not result.analysis:
                continue
            for keyword, times in result.analysis.keyword_timeline.items():
                keyword_timeline[f"{result.source.id}:{keyword}"] = list(times)
        return ContentAnalysis(
            video_duration=sum(source.duration for source in material_set.sources),
            source_durations={source.id: source.duration for source in material_set.sources},
            transcript=[segment for analysis in analyses for segment in analysis.transcript],
            evidence=[item for analysis in analyses for item in analysis.evidence],
            highlights=[],
            summary="；".join(summaries)[:2000],
            keyword_timeline=keyword_timeline,
        )

    @staticmethod
    def _sha256(path: Path) -> str:
        if not path.is_file():
            # 正常预检不会走到这里；测试桩或迁移任务可能只保留历史路径。
            return hashlib.sha256(str(path).encode("utf-8")).hexdigest()
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _parse_fps(value) -> float | None:
        if value in (None, ""):
            return None
        try:
            fps = float(value)
        except (TypeError, ValueError):
            return None
        return fps if fps > 0 else None
