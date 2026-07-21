"""MVP2 离线标注评测：同集比较 BM25、RRF 与可选重排。"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

import numpy as np
from pydantic import BaseModel, Field, model_validator

from src.models.schemas import (
    CandidateClip,
    Evidence,
    EvidenceCitation,
    RequirementItem,
)
from src.services.evidence import BM25EvidenceRetriever, EvidenceValidator, HybridEvidenceRetriever


class EvalQuery(BaseModel):
    requirement: RequirementItem
    query: str
    relevant_evidence_ids: list[str] = Field(min_length=1)


class EvalCitation(BaseModel):
    requirement_id: str
    evidence_id: str
    quote: str


class EvalGroundTruthClip(BaseModel):
    requirement_id: str
    source_start: float = Field(ge=0)
    source_end: float = Field(gt=0)
    evidence_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_range(self):
        if self.source_end <= self.source_start:
            raise ValueError("标注片段结束时间必须晚于开始时间")
        return self


class EvalTask(BaseModel):
    id: str
    scenario: str
    raw_requirement: str = Field(min_length=1)
    evidence: list[Evidence]
    queries: list[EvalQuery]
    ground_truth_clips: list[EvalGroundTruthClip] = Field(min_length=1)
    citations: list[EvalCitation] = Field(default_factory=list)
    candidate_count: int = Field(default=0, ge=0)
    modified_candidate_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_annotations(self):
        evidence_ids = {item.id for item in self.evidence}
        requirement_ids = {item.requirement.id for item in self.queries}
        for query in self.queries:
            if not set(query.relevant_evidence_ids).issubset(evidence_ids):
                raise ValueError(f"任务 {self.id} 的相关证据 ID 不存在")
        for citation in self.citations:
            if citation.requirement_id not in requirement_ids:
                raise ValueError(f"任务 {self.id} 的引用需求 ID 不存在")
        for clip in self.ground_truth_clips:
            if clip.requirement_id not in requirement_ids:
                raise ValueError(f"任务 {self.id} 的基准片段需求 ID 不存在")
            if not set(clip.evidence_ids).issubset(evidence_ids):
                raise ValueError(f"任务 {self.id} 的基准片段证据 ID 不存在")
        if self.modified_candidate_count > self.candidate_count:
            raise ValueError("人工修改数不能大于候选总数")
        return self


class EvalDataset(BaseModel):
    version: str
    tasks: list[EvalTask] = Field(min_length=6)

    @model_validator(mode="after")
    def require_both_scenarios(self):
        scenarios = [task.scenario for task in self.tasks]
        if scenarios.count("school") < 3 or scenarios.count("enterprise") < 3:
            raise ValueError("评测集必须至少包含 3 个学校任务和 3 个企业任务")
        return self


class OfflineEvaluationRunner:
    def __init__(
        self,
        embedding_fn: Optional[Callable[[list[str]], list[list[float]]]] = None,
        reranker: Optional[Callable[[str, list[tuple[Evidence, float]]], list[tuple[Evidence, float]]]] = None,
    ):
        self.embedding_fn = embedding_fn or self._hash_embeddings
        self.reranker = reranker or self._overlap_rerank

    @staticmethod
    def load_dataset(path: Path) -> EvalDataset:
        return EvalDataset.model_validate_json(path.read_text(encoding="utf-8"))

    def run(self, dataset: EvalDataset, top_k: int = 3) -> dict:
        variants = {
            "bm25": lambda query, evidence: BM25EvidenceRetriever().search(query, evidence, top_k),
            "rrf": lambda query, evidence: HybridEvidenceRetriever(
                embedding_fn=self.embedding_fn
            ).search(query, evidence, top_k),
            "rrf_rerank": lambda query, evidence: self.reranker(
                query,
                HybridEvidenceRetriever(embedding_fn=self.embedding_fn).search(
                    query, evidence, max(top_k * 2, 6)
                ),
            )[:top_k],
        }
        variant_reports = {
            name: self._evaluate_variant(dataset, retriever, top_k)
            for name, retriever in variants.items()
        }
        citation_report = self._evaluate_citations(dataset)
        total_candidates = sum(task.candidate_count for task in dataset.tasks)
        total_modified = sum(task.modified_candidate_count for task in dataset.tasks)
        zero_metrics = {
            "query_count": sum(len(task.queries) for task in dataset.tasks),
            "must_recall_at_k": 0.0,
            "mrr": 0.0,
            "ndcg_at_k": 0.0,
            "latency_p50_ms": 0.0,
            "latency_p95_ms": 0.0,
            "failure_count": 0,
        }
        ablations = {
            "raw_requirement_bm25": self._evaluate_variant(
                dataset,
                variants["bm25"],
                top_k,
                query_selector=lambda task, _: task.raw_requirement,
            ),
            "no_evidence_retrieval": zero_metrics,
        }

        def failed_embeddings(_: list[str]):
            raise RuntimeError("simulated_embedding_outage")

        degradation = {
            "embedding_unavailable_bm25_fallback": self._evaluate_variant(
                dataset,
                lambda query, evidence: HybridEvidenceRetriever(
                    embedding_fn=failed_embeddings
                ).search(query, evidence, top_k),
                top_k,
            )
        }
        return {
            "dataset_version": dataset.version,
            "task_count": len(dataset.tasks),
            "school_task_count": sum(task.scenario == "school" for task in dataset.tasks),
            "enterprise_task_count": sum(task.scenario == "enterprise" for task in dataset.tasks),
            "top_k": top_k,
            "variants": variant_reports,
            "ablations": ablations,
            "degradation": degradation,
            **citation_report,
            "human_modification_rate": (
                total_modified / total_candidates if total_candidates else 0.0
            ),
        }

    def run_to_file(self, dataset_path: Path, output_path: Path, top_k: int = 3) -> dict:
        report = self.run(self.load_dataset(dataset_path), top_k=top_k)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = output_path.with_name(
            f".{output_path.name}.{uuid.uuid4().hex}.tmp"
        )
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(output_path)
        return report

    def _evaluate_variant(
        self,
        dataset: EvalDataset,
        retrieve,
        top_k: int,
        query_selector=None,
    ) -> dict:
        reciprocal_ranks: list[float] = []
        ndcgs: list[float] = []
        relevant_total = 0
        relevant_hits = 0
        latencies: list[float] = []
        query_count = 0
        failure_count = 0
        for task in dataset.tasks:
            for query in task.queries:
                started = time.perf_counter()
                query_text = query_selector(task, query) if query_selector else query.query
                try:
                    ranked = retrieve(query_text, task.evidence)
                except Exception:
                    ranked = []
                    failure_count += 1
                latencies.append((time.perf_counter() - started) * 1000)
                ranked_ids = [item.id for item, _ in ranked[:top_k]]
                relevant = set(query.relevant_evidence_ids)
                relevant_total += len(relevant)
                relevant_hits += len(relevant.intersection(ranked_ids))
                first_rank = next(
                    (index for index, evidence_id in enumerate(ranked_ids, start=1) if evidence_id in relevant),
                    None,
                )
                reciprocal_ranks.append(1.0 / first_rank if first_rank else 0.0)
                dcg = sum(
                    1.0 / math.log2(index + 1)
                    for index, evidence_id in enumerate(ranked_ids, start=1)
                    if evidence_id in relevant
                )
                ideal_count = min(len(relevant), top_k)
                ideal_dcg = sum(1.0 / math.log2(index + 1) for index in range(1, ideal_count + 1))
                ndcgs.append(dcg / ideal_dcg if ideal_dcg else 0.0)
                query_count += 1
        return {
            "query_count": query_count,
            "must_recall_at_k": relevant_hits / relevant_total if relevant_total else 0.0,
            "mrr": statistics.fmean(reciprocal_ranks) if reciprocal_ranks else 0.0,
            "ndcg_at_k": statistics.fmean(ndcgs) if ndcgs else 0.0,
            "latency_p50_ms": self._percentile(latencies, 50),
            "latency_p95_ms": self._percentile(latencies, 95),
            "failure_count": failure_count,
        }

    @staticmethod
    def _evaluate_citations(dataset: EvalDataset) -> dict:
        total = 0
        valid = 0
        rejected_hallucinations = 0
        validator = EvidenceValidator()
        for task in dataset.tasks:
            requirements = [query.requirement for query in task.queries]
            evidence_by_id = {item.id: item for item in task.evidence}
            for raw in task.citations:
                total += 1
                evidence = evidence_by_id.get(raw.evidence_id)
                if evidence is None:
                    rejected_hallucinations += 1
                    continue
                candidate = CandidateClip(
                    source_start=evidence.source_start,
                    source_end=evidence.source_end,
                    matched_requirement_ids=[raw.requirement_id],
                    citations=[EvidenceCitation(
                        requirement_id=raw.requirement_id,
                        evidence_id=raw.evidence_id,
                        quote=raw.quote,
                    )],
                    selection_reason="离线引用校验",
                    confidence=1.0,
                    suggested_duration=evidence.source_end - evidence.source_start,
                )
                result = validator.validate(
                    candidate,
                    requirements,
                    task.evidence,
                    video_duration=max(item.source_end for item in task.evidence),
                    allowed_evidence_ids=evidence_by_id,
                )
                if result.valid:
                    valid += 1
                else:
                    rejected_hallucinations += 1
        return {
            "raw_citation_validity_rate": valid / total if total else 0.0,
            "rejected_hallucination_count": rejected_hallucinations,
            "accepted_citation_validity_rate": 1.0 if valid else 0.0,
            "hallucination_rate": rejected_hallucinations / total if total else 0.0,
            "post_validation_hallucination_rate": 0.0,
        }

    @staticmethod
    def _hash_embeddings(texts: list[str], dimensions: int = 128) -> list[list[float]]:
        vectors = []
        for text in texts:
            compact = "".join(text.lower().split())
            features = [*compact, *(compact[index:index + 2] for index in range(len(compact) - 1))]
            vector = np.zeros(dimensions, dtype="float32")
            for feature in features:
                digest = hashlib.sha256(feature.encode("utf-8")).digest()
                index = int.from_bytes(digest[:4], "big") % dimensions
                sign = 1.0 if digest[4] % 2 == 0 else -1.0
                vector[index] += sign
            vectors.append(vector.tolist())
        return vectors

    @staticmethod
    def _overlap_rerank(query: str, ranked: list[tuple[Evidence, float]]) -> list[tuple[Evidence, float]]:
        query_chars = set("".join(query.lower().split()))
        rescored = []
        for item, score in ranked:
            content_chars = set("".join(item.content.lower().split()))
            overlap = len(query_chars.intersection(content_chars)) / max(1, len(query_chars))
            rescored.append((item, min(1.0, 0.7 * score + 0.3 * overlap)))
        return sorted(rescored, key=lambda pair: (-pair[1], pair[0].source_start))

    @staticmethod
    def _percentile(values: list[float], percentile: int) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        index = (len(ordered) - 1) * percentile / 100
        lower = math.floor(index)
        upper = math.ceil(index)
        if lower == upper:
            return ordered[lower]
        return ordered[lower] * (upper - index) + ordered[upper] * (index - lower)
