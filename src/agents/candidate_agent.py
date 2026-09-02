"""受限证据检索与 Function Calling 驱动的候选片段生成。"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, Optional

from src.agents.base import BaseAgent
from src.models.schemas import (
    CandidateClip,
    DurationBudgetPlan,
    Evidence,
    EvidenceCitation,
    ExecutionBrief,
    RequirementItem,
    TranscriptSegment,
)
from src.services.evidence import EvidenceValidator, HybridEvidenceRetriever
from src.services.duration_planner import DurationBudgetPlanner
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


class SpeechBoundaryValidator:
    """保守识别明显从半句话开始或在因果/转折关系中途结束的候选。"""

    CONTINUATION_PREFIXES = (
        "并且", "而且", "以及", "但是", "所以", "因此", "然后", "同时",
        "另外", "其实", "也", "就", "才", "还", "这种", "这些", "这样",
        "从而", "或者", "甚至", "不过", "其中", "对此", "由此",
    )
    DANGLING_SUFFIXES = (
        "因为", "所以", "但是", "如果", "当", "从", "为了", "通过", "以及",
        "和", "与", "或者", "就是", "主要是", "包括", "比如", "例如",
    )
    QUESTION_MARKERS = (
        "？", "?", "请问", "为什么", "怎么看", "如何", "是否", "能否",
        "有没有", "什么", "哪些", "怎么样",
    )
    ANSWER_PREFIXES = (
        "我觉得", "我认为", "首先", "因为", "是的", "对", "可以", "目前",
        "我们", "这个", "其实",
    )

    def validate(
        self,
        candidate: CandidateClip,
        transcript: Iterable[TranscriptSegment],
        *,
        max_continuation_gap: float = 1.5,
    ) -> tuple[str, ...]:
        segments = sorted(
            (
                item for item in transcript
                if item.text.strip()
                and item.end > item.start
                and (
                    candidate.source_asset_id is None
                    or item.source_asset_id == candidate.source_asset_id
                )
            ),
            key=lambda item: (item.start, item.end),
        )
        if not segments:
            return ()
        included_indices = [
            index for index, item in enumerate(segments)
            if item.end > candidate.source_start + 0.01
            and item.start < candidate.source_end - 0.01
        ]
        if not included_indices:
            return ("candidate_without_transcript",)
        first_index = included_indices[0]
        last_index = included_indices[-1]
        first = segments[first_index]
        last = segments[last_index]
        errors: list[str] = []
        if candidate.source_start > first.start + 0.15:
            errors.append("candidate_starts_inside_utterance")
        if candidate.source_end < last.end - 0.15:
            errors.append("candidate_ends_inside_utterance")
        if first_index > 0:
            previous = segments[first_index - 1]
            same_turn = (
                first.speaker_id == previous.speaker_id
                and first.start - previous.end <= max_continuation_gap
            )
            if same_turn and (
                first.text.strip().startswith(self.CONTINUATION_PREFIXES)
                or previous.text.strip().endswith(self.DANGLING_SUFFIXES)
            ):
                errors.append("candidate_starts_mid_sentence")
            if (
                first.start - previous.end <= 2.5
                and self._looks_like_question(previous.text)
                and first.text.strip().startswith(self.ANSWER_PREFIXES)
            ):
                errors.append("candidate_answer_missing_question")
        if last_index + 1 < len(segments):
            following = segments[last_index + 1]
            same_turn = (
                following.speaker_id == last.speaker_id
                and following.start - last.end <= max_continuation_gap
            )
            if same_turn and (
                following.text.strip().startswith(self.CONTINUATION_PREFIXES)
                or last.text.strip().endswith(self.DANGLING_SUFFIXES)
            ):
                errors.append("candidate_ends_mid_sentence")
            if (
                following.start - last.end <= 2.5
                and self._looks_like_question(last.text)
            ):
                errors.append("candidate_question_missing_answer")
        return tuple(dict.fromkeys(errors))

    @classmethod
    def _looks_like_question(cls, text: str) -> bool:
        value = text.strip()
        return any(marker in value for marker in cls.QUESTION_MARKERS)


class CandidateGenerationResult:
    """保留所有候选与验证结果，方便 UI 展示无效引用而不自动选用。"""

    def __init__(
        self,
        candidates: list[CandidateClip],
        valid_ids: list[str],
        retrieval_trace: Dict[str, list[str]],
        missing_must_requirement_ids: Optional[list[str]] = None,
        degraded: bool = False,
        failure_reason: Optional[str] = None,
        failed_requirement_ids: Optional[list[str]] = None,
        duration_plan: Optional[DurationBudgetPlan] = None,
        retrieval_degradation_reason: Optional[str] = None,
    ):
        self.candidates = candidates
        self.valid_ids = valid_ids
        self.retrieval_trace = retrieval_trace
        self.missing_must_requirement_ids = missing_must_requirement_ids or []
        self.degraded = degraded
        self.failure_reason = failure_reason
        self.failed_requirement_ids = failed_requirement_ids or []
        self.duration_plan = duration_plan
        self.retrieval_degradation_reason = retrieval_degradation_reason

    @property
    def valid_candidates(self) -> list[CandidateClip]:
        valid = set(self.valid_ids)
        return [candidate for candidate in self.candidates if candidate.id in valid]


class CandidateAgent(BaseAgent):
    """按单项需求生成小型候选 JSON，再由代码补齐并验证证据字段。"""

    MAX_CANDIDATES = 18
    MAX_CANDIDATES_PER_REQUIREMENT = 8
    BATCH_ATTEMPTS = 2
    BATCH_MAX_TOKENS = 1000

    def __init__(self, llm_runner: Optional[Callable] = None, retriever=None):
        super().__init__("CandidateAgent")
        self.llm_runner = llm_runner or call_llm_with_tools
        self.retriever = retriever or HybridEvidenceRetriever(embedding_fn=embed_texts)
        self.validator = EvidenceValidator()
        self.boundary_validator = SpeechBoundaryValidator()
        self.duration_planner = DurationBudgetPlanner()

    def run(
        self,
        execution_brief: ExecutionBrief,
        requirements: Iterable[RequirementItem],
        evidence: Iterable[Evidence],
        video_duration: float | dict[str, float],
        target_duration: Optional[float] = None,
        duration_tolerance: Optional[float] = None,
        transcript: Optional[Iterable[TranscriptSegment]] = None,
    ) -> CandidateGenerationResult:
        requirements = list(requirements)
        evidence = list(evidence)
        transcript = list(transcript or [])
        if hasattr(self.retriever, "begin_run"):
            self.retriever.begin_run()
        selectable_requirements = [
            item for item in requirements
            if item.category == "content" and item.priority != "prohibited"
        ]
        prohibited_ids = {item.id for item in requirements if item.priority == "prohibited"}
        evidence_by_id = {item.id: item for item in evidence}
        evidence_order = {item.id: index for index, item in enumerate(evidence)}
        retrieval_trace: Dict[str, list[str]] = {}
        retrieval_scores: Dict[tuple[str, str], float] = {}

        def serialise(item: Evidence, score: float) -> dict:
            return {
                "evidence_id": item.id,
                "source_asset_id": item.source_asset_id,
                "source_start": item.source_start,
                "source_end": item.source_end,
                "content": item.content,
                "retrieval_score": round(score, 4),
            }

        system_prompt = (
            "你只处理一项活动视频需求。必须先调用 search_evidence；上下文不足时调用 expand_evidence。"
            "最终仅返回紧凑 JSON：{\"selections\":[{\"evidence_id\":\"...\","
            "\"source_start\":0,\"source_end\":10,\"reason\":\"简短理由\",\"confidence\":0.8}]}。"
            "选择数量不得超过请求中的 maximum_selections。不要返回引文、需求 ID、检索分数、字幕全文或其他字段，这些由程序补齐。"
            "只能使用工具返回的 evidence_id 和时间；片段必须覆盖完整的一句话或完整观点，"
            "不能从半句话开始或在人物表达中途结束。没有合格内容就返回空 selections。"
        )
        self._start_timer()
        candidates: list[CandidateClip] = []
        failed_requirement_ids: list[str] = []
        batch_errors: list[str] = []
        available_duration = (
            sum(video_duration.values())
            if isinstance(video_duration, dict)
            else float(video_duration)
        )
        duration_plan = self.duration_planner.build(
            selectable_requirements,
            target_duration=float(target_duration or min(available_duration, 120.0)),
            duration_tolerance=float(duration_tolerance or 0.0),
            available_duration=available_duration,
        )
        budget_by_requirement = {
            item.requirement_id: item
            for item in duration_plan.requirement_budgets
        }

        for requirement in selectable_requirements:
            requirement_budget = budget_by_requirement.get(requirement.id)
            desired_seconds = (
                requirement_budget.target_seconds if requirement_budget else 20.0
            )
            maximum_selections = (
                requirement_budget.desired_candidate_count
                if requirement_budget else 2
            )
            retrieval_trace.setdefault(requirement.id, [])

            def record_matches(matches) -> None:
                for item, score in matches:
                    if item.id not in retrieval_trace[requirement.id]:
                        retrieval_trace[requirement.id].append(item.id)
                    retrieval_scores[(requirement.id, item.id)] = max(
                        score, retrieval_scores.get((requirement.id, item.id), 0.0)
                    )

            def search_evidence(requirement_id: str, query: str, top_k: int = 5) -> dict:
                if requirement_id != requirement.id:
                    return {"error": "unknown_requirement"}
                matches = self.retriever.search(
                    query, evidence, top_k=max(1, min(int(top_k), 8))
                )
                record_matches(matches)
                return {
                    "requirement_id": requirement.id,
                    "evidence": [serialise(item, score) for item, score in matches],
                }

            def expand_evidence(evidence_id: str, context_count: int = 1) -> dict:
                if (
                    evidence_id not in retrieval_trace[requirement.id]
                    or evidence_id not in evidence_by_id
                ):
                    return {"error": "evidence_not_retrieved"}
                index = evidence_order[evidence_id]
                radius = max(0, min(int(context_count), 2))
                context = evidence[max(0, index - radius): index + radius + 1]
                record_matches((item, 0.0) for item in context)
                retrieval_trace.setdefault("_expanded", [])
                retrieval_trace["_expanded"].extend(
                    item.id for item in context
                    if item.id not in retrieval_trace["_expanded"]
                )
                return {"evidence": [serialise(item, 0.0) for item in context]}

            request = {
                # 保留列表形态，便于审计并兼容现有测试桩；每批永远只有一项。
                "requirements": [{
                    "id": requirement.id,
                    "description": requirement.description,
                    "priority": requirement.priority,
                    "acceptance_rule": requirement.acceptance_rule,
                }],
                "prohibited_content": [
                    item.description for item in requirements if item.priority == "prohibited"
                ],
                "video_duration": (
                    max(video_duration.values(), default=0.0)
                    if isinstance(video_duration, dict)
                    else video_duration
                ),
                "source_durations": (
                    video_duration if isinstance(video_duration, dict)
                    else {"legacy": video_duration}
                ),
                "desired_total_seconds_for_this_requirement": desired_seconds,
                "maximum_selections": min(
                    self.MAX_CANDIDATES_PER_REQUIREMENT, maximum_selections
                ),
            }
            response: Optional[dict] = None
            last_error: Optional[Exception] = None
            for attempt in range(self.BATCH_ATTEMPTS):
                try:
                    attempt_request = {
                        **request,
                        "maximum_selections": max(
                            1,
                            int(request["maximum_selections"]) // (attempt + 1),
                        ),
                    }
                    response = self.llm_runner(
                        system_prompt=system_prompt,
                        user_message=str(attempt_request),
                        tool_definitions=TOOL_DEFINITIONS,
                        tool_handlers={
                            "search_evidence": search_evidence,
                            "expand_evidence": expand_evidence,
                        },
                        temperature=0.0,
                        max_tokens=self.BATCH_MAX_TOKENS,
                        max_rounds=3,
                    )
                    if not isinstance(response, dict):
                        raise ValueError("候选批次没有返回 JSON 对象")
                    break
                except Exception as error:
                    last_error = error
                    self.log(
                        f"需求候选批次第 {attempt + 1} 次失败: {type(error).__name__}",
                        level="warning",
                    )
            if response is None:
                failed_requirement_ids.append(requirement.id)
                batch_errors.append(
                    f"{requirement.id}:{type(last_error).__name__ if last_error else 'unknown'}"
                )
                fallback = self._fallback_candidates([requirement], evidence)
                for raw in fallback:
                    citation = raw.get("citations", [{}])[0]
                    evidence_id = citation.get("evidence_id")
                    if evidence_id:
                        if evidence_id not in retrieval_trace[requirement.id]:
                            retrieval_trace[requirement.id].append(evidence_id)
                        retrieval_scores[(requirement.id, evidence_id)] = float(
                            citation.get("retrieval_score", 0.0)
                        )
                    candidate = self._materialise_selection(
                        raw,
                        requirement,
                        evidence_by_id,
                        retrieval_trace,
                        retrieval_scores,
                        transcript,
                        video_duration,
                        fallback=True,
                    )
                    if candidate:
                        candidates.append(candidate)
                continue

            raw_selections = response.get("selections")
            if raw_selections is None:
                raw_selections = response.get("candidates", [])
            if not isinstance(raw_selections, list):
                raw_selections = []
            for raw in raw_selections[:maximum_selections]:
                if not isinstance(raw, dict):
                    continue
                candidate = self._materialise_selection(
                    raw,
                    requirement,
                    evidence_by_id,
                    retrieval_trace,
                    retrieval_scores,
                    transcript,
                    video_duration,
                    fallback=False,
                )
                if candidate:
                    candidates.append(candidate)

        candidates = self._merge_duplicate_candidates(candidates)
        valid_ids: list[str] = []
        for index, candidate in enumerate(candidates):
            validation = self.validator.validate(
                candidate,
                requirements,
                evidence,
                video_duration,
                allowed_evidence_ids={
                    evidence_id
                    for requirement_id, ids in retrieval_trace.items()
                    if not requirement_id.startswith("_")
                    for evidence_id in ids
                },
            )
            boundary_errors = self.boundary_validator.validate(candidate, transcript) if transcript else ()
            scoped_errors = [
                f"evidence_not_retrieved_for_requirement:{citation.requirement_id}:{citation.evidence_id}"
                for citation in candidate.citations
                if citation.evidence_id not in retrieval_trace.get(citation.requirement_id, [])
            ]
            prohibited_matches = prohibited_ids.intersection(candidate.matched_requirement_ids)
            is_fallback = "candidate_generation_fallback" in candidate.risk_flags
            model_errors = [
                flag for flag in candidate.risk_flags if flag.startswith("model_")
            ]
            if prohibited_matches:
                updated = candidate.model_copy(
                    update={
                        "risk_flags": [
                            *candidate.risk_flags,
                            *[f"prohibited_match:{item_id}" for item_id in sorted(prohibited_matches)],
                        ]
                    }
                )
            elif validation.valid and not scoped_errors and not boundary_errors and not is_fallback and not model_errors:
                valid_ids.append(candidate.id)
                updated = candidate
            else:
                updated = candidate.model_copy(update={
                    "risk_flags": [
                        *candidate.risk_flags,
                        *validation.errors,
                        *scoped_errors,
                        *boundary_errors,
                        *(["candidate_ai_unavailable_not_exportable"] if is_fallback else []),
                    ]
                })
            candidates[index] = updated
        valid_ids = self._limit_valid_candidates(candidates, valid_ids, selectable_requirements)
        valid_id_set = set(valid_ids)
        duration_plan = self.duration_planner.complete(
            duration_plan,
            (
                candidate.source_end - candidate.source_start
                for candidate in candidates
                if candidate.id in valid_id_set
            ),
        )
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
            if item.category == "content"
            and item.priority == "must"
            and item.id not in covered_ids
        ]
        return CandidateGenerationResult(
            candidates,
            valid_ids,
            retrieval_trace,
            missing_must,
            degraded=bool(failed_requirement_ids) or bool(
                getattr(self.retriever, "degradation_reason", None)
            ),
            failure_reason=(
                "部分候选批次失败：" + ",".join(batch_errors)
                if batch_errors else getattr(self.retriever, "degradation_reason", None)
            ),
            failed_requirement_ids=failed_requirement_ids,
            duration_plan=duration_plan,
            retrieval_degradation_reason=getattr(
                self.retriever, "degradation_reason", None
            ),
        )

    def _materialise_selection(
        self,
        raw: Dict[str, Any],
        requirement: RequirementItem,
        evidence_by_id: Dict[str, Evidence],
        retrieval_trace: Dict[str, list[str]],
        retrieval_scores: Dict[tuple[str, str], float],
        transcript: list[TranscriptSegment],
        video_duration: float | dict[str, float],
        *,
        fallback: bool,
    ) -> Optional[CandidateClip]:
        """只信任模型的选择；引用原文、需求映射和分数全部由代码生成。"""
        legacy_citations = raw.get("citations") or []
        evidence_id = raw.get("evidence_id")
        if not evidence_id and legacy_citations and isinstance(legacy_citations[0], dict):
            evidence_id = legacy_citations[0].get("evidence_id")
        item = evidence_by_id.get(str(evidence_id or ""))
        if item is None:
            return None

        risk_flags = list(raw.get("risk_flags") or [])
        legacy_requirement_ids = raw.get("matched_requirement_ids")
        if legacy_requirement_ids and set(legacy_requirement_ids) != {requirement.id}:
            risk_flags.append("model_cross_requirement_output")
        if legacy_citations:
            citation_requirement_ids = {
                citation.get("requirement_id")
                for citation in legacy_citations
                if isinstance(citation, dict) and citation.get("requirement_id")
            }
            if citation_requirement_ids and citation_requirement_ids != {requirement.id}:
                risk_flags.append("model_cross_requirement_output")
        if item.id not in retrieval_trace.get(requirement.id, []):
            risk_flags.append(
                f"evidence_not_retrieved_for_requirement:{requirement.id}:{item.id}"
            )

        try:
            proposed_start = float(raw.get("source_start", item.source_start))
            proposed_end = float(raw.get("source_end", item.source_end))
        except (TypeError, ValueError):
            proposed_start, proposed_end = item.source_start, item.source_end
        source_duration = self._source_duration(video_duration, item.source_asset_id)
        if source_duration <= 0:
            return None
        source_transcript = [
            segment for segment in transcript
            if (
                item.source_asset_id is None
                or segment.source_asset_id == item.source_asset_id
            )
        ]
        # 模型不能把一个证据无限扩成整段视频；最多向证据两侧各延伸 20 秒。
        start = max(0.0, max(item.source_start - 20.0, min(proposed_start, item.source_start)))
        end = min(
            source_duration,
            min(item.source_end + 20.0, max(proposed_end, item.source_end)),
        )
        start, end = self._snap_to_transcript_boundaries(
            start, end, source_transcript, source_duration
        )
        if end <= start:
            return None
        try:
            confidence = max(0.0, min(1.0, float(raw.get("confidence", 0.7))))
        except (TypeError, ValueError):
            confidence = 0.7
        reason = str(raw.get("reason") or raw.get("selection_reason") or "符合当前任务书要求").strip()
        if fallback:
            risk_flags.append("candidate_generation_fallback")
        return CandidateClip(
            source_asset_id=item.source_asset_id,
            source_start=start,
            source_end=end,
            matched_requirement_ids=[requirement.id],
            citations=[EvidenceCitation(
                requirement_id=requirement.id,
                evidence_id=item.id,
                quote=item.content,
                relation="direct",
                retrieval_score=max(
                    0.0,
                    min(1.0, retrieval_scores.get((requirement.id, item.id), 0.0)),
                ),
            )],
            selection_reason=reason,
            confidence=confidence,
            suggested_duration=end - start,
            risk_flags=list(dict.fromkeys(risk_flags)),
        )

    def _snap_to_transcript_boundaries(
        self,
        start: float,
        end: float,
        transcript: list[TranscriptSegment],
        video_duration: float,
    ) -> tuple[float, float]:
        """将模型时间吸附到完整 ASR 段，并补齐明显的前后承接句。"""
        segments = sorted(
            (item for item in transcript if item.text.strip() and item.end > item.start),
            key=lambda item: (item.start, item.end),
        )
        included = [
            index for index, item in enumerate(segments)
            if item.end > start and item.start < end
        ]
        if not included:
            return max(0.0, start), min(video_duration, end)
        first_index, last_index = included[0], included[-1]
        if first_index > 0:
            first, previous = segments[first_index], segments[first_index - 1]
            if (
                first.start - previous.end <= 2.5
                and SpeechBoundaryValidator._looks_like_question(previous.text)
                and first.text.strip().startswith(SpeechBoundaryValidator.ANSWER_PREFIXES)
            ):
                first_index -= 1
        if last_index + 1 < len(segments):
            last, following = segments[last_index], segments[last_index + 1]
            if (
                following.start - last.end <= 2.5
                and SpeechBoundaryValidator._looks_like_question(last.text)
            ):
                last_index += 1
        for _ in range(4):
            changed = False
            if first_index > 0:
                first, previous = segments[first_index], segments[first_index - 1]
                if (
                    first.start - previous.end <= 1.5
                    and first.speaker_id == previous.speaker_id
                    and (
                        first.text.strip().startswith(SpeechBoundaryValidator.CONTINUATION_PREFIXES)
                        or previous.text.strip().endswith(SpeechBoundaryValidator.DANGLING_SUFFIXES)
                    )
                ):
                    first_index -= 1
                    changed = True
            if last_index + 1 < len(segments):
                last, following = segments[last_index], segments[last_index + 1]
                if (
                    following.start - last.end <= 1.5
                    and following.speaker_id == last.speaker_id
                    and (
                        following.text.strip().startswith(SpeechBoundaryValidator.CONTINUATION_PREFIXES)
                        or last.text.strip().endswith(SpeechBoundaryValidator.DANGLING_SUFFIXES)
                    )
                ):
                    last_index += 1
                    changed = True
            if not changed:
                break
        return (
            max(0.0, segments[first_index].start),
            min(video_duration, segments[last_index].end),
        )

    @staticmethod
    def _source_duration(
        video_duration: float | dict[str, float],
        source_asset_id: Optional[str],
    ) -> float:
        if isinstance(video_duration, dict):
            if source_asset_id is None and len(video_duration) == 1:
                return float(next(iter(video_duration.values())))
            return float(video_duration.get(source_asset_id or "", 0.0))
        return float(video_duration)

    @staticmethod
    def _merge_duplicate_candidates(candidates: list[CandidateClip]) -> list[CandidateClip]:
        """合并不同需求选中的同一素材范围，同时保留每项需求的独立引用。"""
        merged: list[CandidateClip] = []
        for candidate in sorted(
            candidates,
            key=lambda item: (
                item.source_asset_id or "",
                item.source_start,
                item.source_end,
            ),
        ):
            fallback = "candidate_generation_fallback" in candidate.risk_flags
            duplicate = None
            candidate_evidence = {item.evidence_id for item in candidate.citations}
            for existing in merged:
                if existing.source_asset_id != candidate.source_asset_id:
                    continue
                if fallback != ("candidate_generation_fallback" in existing.risk_flags):
                    continue
                overlap = max(
                    0.0,
                    min(existing.source_end, candidate.source_end)
                    - max(existing.source_start, candidate.source_start),
                )
                shorter = min(
                    existing.source_end - existing.source_start,
                    candidate.source_end - candidate.source_start,
                )
                same_evidence = bool(
                    candidate_evidence.intersection(
                        item.evidence_id for item in existing.citations
                    )
                )
                if same_evidence or (shorter > 0 and overlap / shorter >= 0.75):
                    duplicate = existing
                    break
            if duplicate is None:
                merged.append(candidate)
                continue
            index = merged.index(duplicate)
            citations = {
                (item.requirement_id, item.evidence_id): item
                for item in [*duplicate.citations, *candidate.citations]
            }
            reasons = list(dict.fromkeys([
                duplicate.selection_reason, candidate.selection_reason
            ]))
            start = min(duplicate.source_start, candidate.source_start)
            end = max(duplicate.source_end, candidate.source_end)
            merged[index] = duplicate.model_copy(update={
                "source_start": start,
                "source_end": end,
                "matched_requirement_ids": list(dict.fromkeys([
                    *duplicate.matched_requirement_ids,
                    *candidate.matched_requirement_ids,
                ])),
                "citations": list(citations.values()),
                "selection_reason": "；".join(reasons),
                "confidence": max(duplicate.confidence, candidate.confidence),
                "suggested_duration": end - start,
                "risk_flags": list(dict.fromkeys([
                    *duplicate.risk_flags, *candidate.risk_flags
                ])),
            })
        return merged

    def _limit_valid_candidates(
        self,
        candidates: list[CandidateClip],
        valid_ids: list[str],
        requirements: list[RequirementItem],
    ) -> list[str]:
        """超过总上限时先保证需求覆盖，再按置信度补齐。"""
        if len(valid_ids) <= self.MAX_CANDIDATES:
            return valid_ids
        valid = [item for item in candidates if item.id in set(valid_ids)]
        chosen: list[CandidateClip] = []
        for requirement in requirements:
            matches = [
                item for item in valid
                if requirement.id in item.matched_requirement_ids and item not in chosen
            ]
            if matches and len(chosen) < self.MAX_CANDIDATES:
                chosen.append(max(matches, key=lambda item: item.confidence))
        for item in sorted(valid, key=lambda value: (-value.confidence, value.source_start)):
            if item not in chosen and len(chosen) < self.MAX_CANDIDATES:
                chosen.append(item)
        return [item.id for item in chosen]

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
                    "risk_flags": ["candidate_generation_fallback"],
                }
            )
        return candidates
