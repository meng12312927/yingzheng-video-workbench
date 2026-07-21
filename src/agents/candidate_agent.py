"""受限证据检索与 Function Calling 驱动的候选片段生成。"""

from __future__ import annotations

from typing import Callable, Dict, Iterable, Optional

from src.agents.base import BaseAgent
from src.models.schemas import (
    CandidateClip,
    Evidence,
    EvidenceCitation,
    ExecutionBrief,
    RequirementItem,
)
from src.services.evidence import EvidenceValidator, HybridEvidenceRetriever
from src.tools.llm import call_llm_with_tools, embed_texts


TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "search_evidence",
            "description": "按指定需求检索可引用的转录证据。只能使用返回的 evidence_id 和原文。",
            "parameters": {
                "type": "object",
                "properties": {
                    "requirement_id": {"type": "string"},
                    "query": {"type": "string"},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 8},
                },
                "required": ["requirement_id", "query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "expand_evidence",
            "description": "读取一条已检索证据相邻的上下文，不能访问任意全文。",
            "parameters": {
                "type": "object",
                "properties": {
                    "evidence_id": {"type": "string"},
                    "context_count": {"type": "integer", "minimum": 0, "maximum": 2},
                },
                "required": ["evidence_id"],
                "additionalProperties": False,
            },
        },
    },
]


class CandidateGenerationResult:
    """保留所有候选与验证结果，方便 UI 展示无效引用而不自动选用。"""

    def __init__(
        self,
        candidates: list[CandidateClip],
        valid_ids: list[str],
        retrieval_trace: Dict[str, list[str]],
        missing_must_requirement_ids: Optional[list[str]] = None,
    ):
        self.candidates = candidates
        self.valid_ids = valid_ids
        self.retrieval_trace = retrieval_trace
        self.missing_must_requirement_ids = missing_must_requirement_ids or []

    @property
    def valid_candidates(self) -> list[CandidateClip]:
        valid = set(self.valid_ids)
        return [candidate for candidate in self.candidates if candidate.id in valid]


class CandidateAgent(BaseAgent):
    """模型只能通过小范围工具检索证据，再输出带引文的候选。"""

    def __init__(self, llm_runner: Optional[Callable] = None, retriever=None):
        super().__init__("CandidateAgent")
        self.llm_runner = llm_runner or call_llm_with_tools
        self.retriever = retriever or HybridEvidenceRetriever(embedding_fn=embed_texts)
        self.validator = EvidenceValidator()

    def run(
        self,
        execution_brief: ExecutionBrief,
        requirements: Iterable[RequirementItem],
        evidence: Iterable[Evidence],
        video_duration: float,
    ) -> CandidateGenerationResult:
        requirements = list(requirements)
        evidence = list(evidence)
        requirement_by_id = {item.id: item for item in requirements}
        prohibited_ids = {item.id for item in requirements if item.priority == "prohibited"}
        evidence_by_id = {item.id: item for item in evidence}
        evidence_order = {item.id: index for index, item in enumerate(evidence)}
        retrieved_ids: set[str] = set()
        retrieval_trace: Dict[str, list[str]] = {}

        def serialise(item: Evidence, score: float) -> dict:
            return {
                "evidence_id": item.id,
                "source_start": item.source_start,
                "source_end": item.source_end,
                "content": item.content,
                "retrieval_score": round(score, 4),
            }

        def search_evidence(requirement_id: str, query: str, top_k: int = 5) -> dict:
            if requirement_id not in requirement_by_id:
                return {"error": "unknown_requirement"}
            matches = self.retriever.search(query, evidence, top_k=max(1, min(int(top_k), 8)))
            retrieval_trace.setdefault(requirement_id, [])
            retrieval_trace[requirement_id].extend(
                item.id for item, _ in matches
                if item.id not in retrieval_trace[requirement_id]
            )
            retrieved_ids.update(item.id for item, _ in matches)
            return {"requirement_id": requirement_id, "evidence": [serialise(item, score) for item, score in matches]}

        def expand_evidence(evidence_id: str, context_count: int = 1) -> dict:
            if evidence_id not in retrieved_ids or evidence_id not in evidence_by_id:
                return {"error": "evidence_not_retrieved"}
            source_requirements = [
                requirement_id
                for requirement_id, ids in retrieval_trace.items()
                if not requirement_id.startswith("_") and evidence_id in ids
            ]
            index = evidence_order[evidence_id]
            radius = max(0, min(int(context_count), 2))
            context = evidence[max(0, index - radius): index + radius + 1]
            retrieved_ids.update(item.id for item in context)
            retrieval_trace.setdefault("_expanded", [])
            retrieval_trace["_expanded"].extend(
                item.id for item in context if item.id not in retrieval_trace["_expanded"]
            )
            for requirement_id in source_requirements:
                retrieval_trace.setdefault(requirement_id, [])
                retrieval_trace[requirement_id].extend(
                    item.id for item in context
                    if item.id not in retrieval_trace[requirement_id]
                )
            return {"evidence": [serialise(item, 0.0) for item in context]}

        system_prompt = (
            "你是活动视频候选分析器。必须先调用 search_evidence；只可引用工具返回的 evidence_id 和原文。"
            "最终仅返回 JSON：{\"candidates\":[{source_start,source_end,matched_requirement_ids,citations,"
            "selection_reason,confidence,suggested_duration,risk_flags}]}。citations 中必须有 requirement_id、"
            "evidence_id、quote、relation、retrieval_score。不能编造证据、时间或人名。"
        )
        request = {
            "visible_instruction": execution_brief.visible_instruction,
            "requirements": [
                {"id": item.id, "description": item.description, "priority": item.priority, "acceptance_rule": item.acceptance_rule}
                for item in requirements
            ],
            "video_duration": video_duration,
        }
        self._start_timer()
        try:
            response = self.llm_runner(
                system_prompt=system_prompt,
                user_message=str(request),
                tool_definitions=TOOL_DEFINITIONS,
                tool_handlers={"search_evidence": search_evidence, "expand_evidence": expand_evidence},
                temperature=0.0,
            )
            raw_candidates = response.get("candidates", []) if isinstance(response, dict) else []
        except Exception as error:
            self.log(f"工具调用候选分析失败，使用可验证检索候选: {error}", level="warning")
            raw_candidates = self._fallback_candidates(requirements, evidence)
            for raw in raw_candidates:
                for citation in raw.get("citations", []):
                    evidence_id = citation["evidence_id"]
                    requirement_id = citation["requirement_id"]
                    retrieved_ids.add(evidence_id)
                    retrieval_trace.setdefault(requirement_id, []).append(evidence_id)

        candidates: list[CandidateClip] = []
        valid_ids: list[str] = []
        for raw in raw_candidates:
            try:
                candidate = raw if isinstance(raw, CandidateClip) else CandidateClip.model_validate(raw)
            except Exception:
                continue
            validation = self.validator.validate(
                candidate,
                requirements,
                evidence,
                video_duration,
                allowed_evidence_ids=retrieved_ids,
            )
            scoped_errors = [
                f"evidence_not_retrieved_for_requirement:{citation.requirement_id}:{citation.evidence_id}"
                for citation in candidate.citations
                if citation.evidence_id not in retrieval_trace.get(citation.requirement_id, [])
            ]
            prohibited_matches = prohibited_ids.intersection(candidate.matched_requirement_ids)
            if prohibited_matches:
                candidate = candidate.model_copy(
                    update={
                        "risk_flags": [
                            *candidate.risk_flags,
                            *[f"prohibited_match:{item_id}" for item_id in sorted(prohibited_matches)],
                        ]
                    }
                )
            elif validation.valid and not scoped_errors:
                valid_ids.append(candidate.id)
            else:
                candidate = candidate.model_copy(update={
                    "risk_flags": [
                        *candidate.risk_flags,
                        *validation.errors,
                        *scoped_errors,
                    ]
                })
            candidates.append(candidate)
        self._end_timer()
        self.log(f"生成 {len(candidates)} 个候选，其中 {len(valid_ids)} 个引用校验通过")
        covered_ids = {
            requirement_id
            for candidate in candidates
            if candidate.id in set(valid_ids)
            for requirement_id in candidate.matched_requirement_ids
        }
        missing_must = [
            item.id for item in requirements
            if item.priority == "must" and item.id not in covered_ids
        ]
        return CandidateGenerationResult(candidates, valid_ids, retrieval_trace, missing_must)

    def _fallback_candidates(self, requirements: list[RequirementItem], evidence: list[Evidence]) -> list[dict]:
        """无模型或工具调用失败时仍只生成能被同一验证器接受的候选。"""
        candidates: list[dict] = []
        for requirement in requirements:
            if requirement.priority == "prohibited":
                continue
            matches = self.retriever.search(requirement.description, evidence, top_k=1)
            if not matches:
                continue
            item, score = matches[0]
            candidates.append(
                {
                    "source_start": item.source_start,
                    "source_end": item.source_end,
                    "matched_requirement_ids": [requirement.id],
                    "citations": [{
                        "requirement_id": requirement.id,
                        "evidence_id": item.id,
                        "quote": item.content,
                        "relation": "direct",
                        "retrieval_score": score,
                    }],
                    "selection_reason": f"检索到与该需求直接相关的转录证据：{item.content[:50]}",
                    "confidence": min(0.7, max(0.3, score)),
                    "suggested_duration": item.source_end - item.source_start,
                    "risk_flags": ["llm_unavailable"],
                }
            )
        return candidates
