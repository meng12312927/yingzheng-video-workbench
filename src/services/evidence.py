"""活动视频的证据构建、受限检索与确定性引用校验。"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

import numpy as np

from src.models.schemas import CandidateClip, Evidence, RequirementItem, TranscriptSegment


def _normalise(text: str) -> str:
    """忽略 Unicode 形式、空白和标点差异，但保留所有实体字符。"""
    normalised = unicodedata.normalize("NFKC", text).casefold()
    return "".join(
        character
        for character in normalised
        if not character.isspace()
        and not unicodedata.category(character).startswith(("P", "Z"))
    )


class EvidenceBuilder:
    """把相邻 ASR 片段合并为有稳定 ID、原文与来源段的证据窗口。"""

    @classmethod
    def from_transcript(
        cls,
        transcript: Iterable[TranscriptSegment],
        *,
        max_gap_seconds: float = 1.5,
        max_window_seconds: float = 20.0,
        max_characters: int = 240,
        respect_speaker_boundaries: bool = True,
    ) -> list[Evidence]:
        if max_gap_seconds < 0 or max_window_seconds <= 0 or max_characters <= 0:
            raise ValueError("证据窗口参数必须为正值（gap 可为 0）")
        valid_segments = sorted(
            (
                segment for segment in transcript
                if segment.text.strip() and segment.end > segment.start >= 0
            ),
            key=lambda segment: (
                segment.source_asset_id or "",
                segment.start,
                segment.end,
                segment.id,
            ),
        )
        windows: list[list[TranscriptSegment]] = []
        current: list[TranscriptSegment] = []
        for segment in valid_segments:
            if not current:
                current = [segment]
                continue
            window_start = current[0].start
            window_end = max(item.end for item in current)
            joined_length = len(" ".join([*(item.text.strip() for item in current), segment.text.strip()]))
            same_speaker = segment.speaker_id == current[-1].speaker_id
            same_source = segment.source_asset_id == current[-1].source_asset_id
            can_merge = (
                same_source
                and
                segment.start - window_end <= max_gap_seconds
                and max(window_end, segment.end) - window_start <= max_window_seconds
                and joined_length <= max_characters
                and (same_speaker or not respect_speaker_boundaries)
            )
            if can_merge:
                current.append(segment)
            else:
                windows.append(current)
                current = [segment]
        if current:
            windows.append(current)

        evidence: list[Evidence] = []
        for window in windows:
            content = " ".join(segment.text.strip() for segment in window)
            source_start = window[0].start
            source_end = max(segment.end for segment in window)
            source_asset_id = window[0].source_asset_id
            fingerprint = (
                f"{source_asset_id or 'legacy'}|{source_start:.3f}|{source_end:.3f}|{content}"
            ).encode("utf-8")
            digest = hashlib.sha256(fingerprint).hexdigest()
            speaker_ids = {segment.speaker_id for segment in window}
            evidence.append(
                Evidence(
                    id=f"evidence_{digest[:16]}",
                    source_asset_id=source_asset_id,
                    source_start=source_start,
                    source_end=source_end,
                    content=content,
                    content_hash=digest,
                    segment_ids=[segment.id for segment in window],
                    metadata={
                        "speaker_id": next(iter(speaker_ids)) if len(speaker_ids) == 1 else None,
                        "segment_count": len(window),
                        "window_strategy": "adjacent_asr_v1",
                        "source_asset_id": source_asset_id,
                    },
                )
            )
        return evidence


def _tokenize(text: str) -> list[str]:
    """面向中文转录的轻量分词：英文单词 + 中文单字/二元组。"""
    normalised = text.lower()
    tokens = re.findall(r"[a-z0-9]{2,}", normalised)
    for span in re.findall(r"[\u4e00-\u9fff]+", normalised):
        tokens.extend(span)
        tokens.extend(span[index:index + 2] for index in range(len(span) - 1))
    return tokens


class BM25EvidenceRetriever:
    """不依赖外部分词服务的本地 BM25 召回。"""

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b

    def search(self, query: str, evidence: Iterable[Evidence], top_k: int = 8) -> list[tuple[Evidence, float]]:
        items = list(evidence)
        query_terms = set(_tokenize(query))
        if not items or not query_terms:
            return []
        documents = [_tokenize(item.content) for item in items]
        document_frequency = Counter(
            term for document in documents for term in set(document)
        )
        average_length = sum(len(document) for document in documents) / max(1, len(documents))
        raw_scores: list[tuple[Evidence, float]] = []
        for item, document in zip(items, documents):
            frequencies = Counter(document)
            score = 0.0
            for term in query_terms:
                frequency = frequencies.get(term, 0)
                if not frequency:
                    continue
                frequency_in_documents = document_frequency[term]
                inverse_document_frequency = math.log(
                    1 + (len(documents) - frequency_in_documents + 0.5) / (frequency_in_documents + 0.5)
                )
                denominator = frequency + self.k1 * (
                    1 - self.b + self.b * len(document) / max(1.0, average_length)
                )
                score += inverse_document_frequency * frequency * (self.k1 + 1) / denominator
            if score > 0:
                raw_scores.append((item, score))
        if not raw_scores:
            return []
        maximum = max(score for _, score in raw_scores)
        normalised = [(item, score / maximum) for item, score in raw_scores]
        return sorted(normalised, key=lambda pair: (-pair[1], pair[0].source_start))[:top_k]


class DenseEvidenceRetriever:
    """Embedding + 本地 FAISS/NumPy 余弦相似度检索。"""

    def __init__(self, embedding_fn: Callable[[list[str]], list[list[float]]]):
        self.embedding_fn = embedding_fn
        self._cache_key: tuple[str, ...] = ()
        self._matrix: Optional[np.ndarray] = None

    def search(self, query: str, evidence: Iterable[Evidence], top_k: int = 8) -> list[tuple[Evidence, float]]:
        items = list(evidence)
        if not items:
            return []
        cache_key = tuple(item.content_hash or item.id for item in items)
        if cache_key != self._cache_key or self._matrix is None:
            vectors = np.asarray(self.embedding_fn([item.content for item in items]), dtype="float64")
            if vectors.ndim != 2 or len(vectors) != len(items):
                raise ValueError("Embedding 返回数量或维度不正确")
            self._matrix = self._normalise(vectors)
            self._cache_key = cache_key
        query_vector = np.asarray(self.embedding_fn([query]), dtype="float64")
        if query_vector.ndim != 2 or query_vector.shape[0] != 1:
            raise ValueError("查询 Embedding 维度不正确")
        query_vector = self._normalise(query_vector)
        count = min(max(1, int(top_k)), len(items))
        try:
            import faiss

            index = faiss.IndexFlatIP(self._matrix.shape[1])
            index.add(self._matrix)
            scores, indices = index.search(query_vector, count)
            ranked = zip(indices[0].tolist(), scores[0].tolist())
        except ImportError:
            similarities = (self._matrix @ query_vector[0]).tolist()
            ranked = sorted(enumerate(similarities), key=lambda pair: -pair[1])[:count]
        return [
            (items[index], max(0.0, min(1.0, (float(score) + 1.0) / 2.0)))
            for index, score in ranked
            if index >= 0
        ]

    @staticmethod
    def _normalise(matrix: np.ndarray) -> np.ndarray:
        # 先用 float64 求范数，避免异常大的 provider 数值在 float32 平方时溢出；
        # 非有限向量直接交给上层关闭 dense 通道，不能让 NaN 污染 RRF 排序。
        values = np.asarray(matrix, dtype="float64")
        if not np.isfinite(values).all():
            raise ValueError("Embedding 包含非有限数值")
        norms = np.linalg.norm(values, axis=1, keepdims=True)
        if not np.isfinite(norms).all() or np.any(norms <= 1e-12):
            raise ValueError("Embedding 向量范数无效")
        return (values / norms).astype("float32")


class HybridEvidenceRetriever:
    """BM25 与向量召回通过 Reciprocal Rank Fusion 合并。"""

    def __init__(
        self,
        embedding_fn: Optional[Callable[[list[str]], list[list[float]]]] = None,
        rrf_k: int = 60,
    ):
        self.bm25 = BM25EvidenceRetriever()
        self.embedding_fn = embedding_fn
        self.dense = DenseEvidenceRetriever(embedding_fn) if embedding_fn else None
        self.rrf_k = rrf_k
        self.degradation_reason: Optional[str] = None

    def begin_run(self) -> None:
        """新任务重新尝试向量通道；同一任务失败后仍只走一次降级。"""
        self.degradation_reason = None
        if self.embedding_fn is not None and self.dense is None:
            self.dense = DenseEvidenceRetriever(self.embedding_fn)

    def search(self, query: str, evidence: Iterable[Evidence], top_k: int = 8) -> list[tuple[Evidence, float]]:
        items = list(evidence)
        limit = max(top_k * 2, 8)
        rankings = [self.bm25.search(query, items, limit)]
        if self.dense:
            try:
                rankings.append(self.dense.search(query, items, limit))
            except Exception as error:
                # 当前进程内首次失败后停用向量通道，避免每个需求都重复请求
                # 一个已确认不可用的服务；BM25 仍可继续完成证据召回。
                self.dense = None
                self.degradation_reason = (
                    f"dense_retrieval_disabled:{type(error).__name__}:{str(error)[:200]}"
                )
        non_empty = [ranking for ranking in rankings if ranking]
        if not non_empty:
            return []
        by_id = {item.id: item for item in items}
        fused: dict[str, float] = {}
        for ranking in non_empty:
            for rank, (item, _) in enumerate(ranking, start=1):
                fused[item.id] = fused.get(item.id, 0.0) + 1.0 / (self.rrf_k + rank)
        theoretical_max = len(non_empty) / (self.rrf_k + 1)
        results = [
            (by_id[evidence_id], min(1.0, score / theoretical_max))
            for evidence_id, score in fused.items()
        ]
        return sorted(results, key=lambda pair: (-pair[1], pair[0].source_start))[:top_k]


class LexicalEvidenceRetriever(BM25EvidenceRetriever):
    """兼容旧导入名；实际实现已升级为 BM25。"""


@dataclass(frozen=True)
class EvidenceValidationResult:
    valid: bool
    errors: tuple[str, ...]


class EvidenceValidator:
    """验证证据出处与边界，不把语义相关性伪装成确定性事实。"""

    def validate(
        self,
        candidate: CandidateClip,
        requirements: Iterable[RequirementItem],
        evidence: Iterable[Evidence],
        video_duration: float | dict[str, float],
        allowed_evidence_ids: Optional[Iterable[str]] = None,
    ) -> EvidenceValidationResult:
        requirement_ids = {item.id for item in requirements}
        evidence_by_id = {item.id: item for item in evidence}
        allowed_ids = set(allowed_evidence_ids) if allowed_evidence_ids is not None else None
        errors: list[str] = []

        if isinstance(video_duration, dict):
            candidate_duration = video_duration.get(candidate.source_asset_id or "")
            if candidate.source_asset_id is None and len(video_duration) == 1:
                candidate_duration = next(iter(video_duration.values()))
            if candidate.source_asset_id and candidate_duration is None:
                errors.append(f"unknown_source_asset:{candidate.source_asset_id}")
            candidate_duration = candidate_duration or 0.0
        else:
            candidate_duration = video_duration
        if candidate.source_end > candidate_duration:
            errors.append("candidate_outside_media")
        if not candidate.citations:
            errors.append("missing_citation")
        if not candidate.matched_requirement_ids:
            errors.append("missing_requirement")
        for requirement_id in candidate.matched_requirement_ids:
            if requirement_id not in requirement_ids:
                errors.append(f"unknown_requirement:{requirement_id}")
        for citation in candidate.citations:
            item = evidence_by_id.get(citation.evidence_id)
            if allowed_ids is not None and citation.evidence_id not in allowed_ids:
                errors.append(f"evidence_not_retrieved:{citation.evidence_id}")
            if citation.requirement_id not in requirement_ids:
                errors.append(f"unknown_requirement:{citation.requirement_id}")
            if citation.requirement_id not in candidate.matched_requirement_ids:
                errors.append(f"unmatched_citation_requirement:{citation.requirement_id}")
            if item is None:
                errors.append(f"unknown_evidence:{citation.evidence_id}")
                continue
            if (
                candidate.source_asset_id is not None
                and item.source_asset_id != candidate.source_asset_id
            ):
                errors.append(f"evidence_from_other_source:{citation.evidence_id}")
            if _normalise(citation.quote) not in _normalise(item.content):
                errors.append(f"quote_not_in_evidence:{citation.evidence_id}")
            if not (candidate.source_start <= item.source_start and item.source_end <= candidate.source_end):
                errors.append(f"evidence_outside_candidate:{citation.evidence_id}")
        return EvidenceValidationResult(valid=not errors, errors=tuple(dict.fromkeys(errors)))
