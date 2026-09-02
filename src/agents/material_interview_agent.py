"""基于多素材证据生成业务化、可忽略的剪辑访谈问题。"""

from __future__ import annotations

import json
from typing import Callable, Optional

from src.agents.base import BaseAgent
from src.models.schemas import (
    ContentAnalysis,
    MaterialInterviewQuestion,
    RequirementSpec,
)
from src.services.evidence import HybridEvidenceRetriever
from src.tools.llm import call_llm_with_tools, embed_texts


MATERIAL_INTERVIEW_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_material_evidence",
            "description": "按主题检索各段原素材中的中文转录证据，返回来源素材和本地时间。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 8},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    }
]


SYSTEM_PROMPT = """你是活动视频的素材访谈 Agent。你要先检索素材证据，再提出少量真正影响剪辑结果的业务问题。

问题应帮助用户决定：哪些主题或表达值得保留、哪些内容不要、不同内容如何排序、哪里应该重点展开、哪些高风险文字需要人工确认。
每题只能问一个决定。不要让用户填写开始时间、结束时间、帧号、候选 ID、素材 ID或任何技术参数；可以用素材文件名、讲话内容或活动环节帮助用户辨认。
不要提出能从任务书直接回答的问题，不要重复问题，不要为了凑数量提问。每个问题都允许用户暂时忽略。
你只能引用工具实际返回的 evidence_id。不能把转录中的姓名、职务、奖项或产品名当作已确认事实。

最终只返回紧凑 JSON：
{"questions":[{"decision_type":"retain|exclude|order|emphasis|risk","question":"...","reason":"...","impact":"...","answer_hint":"...","supporting_evidence_ids":["..."]}]}
"""


class MaterialInterviewAgent(BaseAgent):
    """模型负责提出素材相关问题，Harness 负责限制证据范围与落库。"""

    MAX_QUESTIONS = 5

    def __init__(self, llm_runner: Optional[Callable] = None, retriever=None):
        super().__init__("MaterialInterviewAgent")
        self.llm_runner = llm_runner or call_llm_with_tools
        self.retriever = retriever or HybridEvidenceRetriever(embedding_fn=embed_texts)

    def run(
        self,
        *,
        raw_text: str,
        spec: RequirementSpec,
        analysis: ContentAnalysis,
        scenario: str,
        source_labels: Optional[dict[str, str]] = None,
        max_questions: int = 5,
    ) -> list[MaterialInterviewQuestion]:
        evidence = [item for item in analysis.evidence if item.type == "transcript"]
        if not evidence or max_questions <= 0:
            return []
        labels = source_labels or {}
        retrieved_ids: set[str] = set()
        evidence_by_id = {item.id: item for item in evidence}

        def search_material_evidence(query: str, top_k: int = 6) -> dict:
            matches = self.retriever.search(
                str(query), evidence, top_k=max(1, min(int(top_k), 8))
            )
            retrieved_ids.update(item.id for item, _ in matches)
            return {
                "evidence": [
                    {
                        "evidence_id": item.id,
                        "source_asset_id": item.source_asset_id,
                        "source_name": labels.get(
                            item.source_asset_id or "", item.source_asset_id or "原素材"
                        ),
                        "source_start": item.source_start,
                        "source_end": item.source_end,
                        "transcript": item.content[:500],
                        "retrieval_score": round(score, 4),
                    }
                    for item, score in matches
                ]
            }

        request = {
            "original_request": raw_text,
            "scenario": scenario,
            "material_summary": analysis.summary,
            "source_files": [
                {
                    "source_asset_id": source_id,
                    "name": labels.get(source_id, source_id),
                    "duration_seconds": duration,
                }
                for source_id, duration in analysis.source_durations.items()
            ],
            "confirmed_taskbook": {
                "purpose": spec.purpose,
                "audience": spec.audience,
                "target_duration": spec.target_duration,
                "requirements": [item.description for item in spec.requirements],
            },
            "maximum_questions": min(self.MAX_QUESTIONS, max_questions),
        }
        self._start_timer()
        response = self.llm_runner(
            system_prompt=SYSTEM_PROMPT,
            user_message=json.dumps(request, ensure_ascii=False),
            tool_definitions=MATERIAL_INTERVIEW_TOOLS,
            tool_handlers={"search_material_evidence": search_material_evidence},
            temperature=0.1,
            max_tokens=1600,
            max_rounds=4,
        )
        raw_questions = response.get("questions", []) if isinstance(response, dict) else []
        questions: list[MaterialInterviewQuestion] = []
        seen: set[str] = set()
        for raw in raw_questions:
            if not isinstance(raw, dict):
                continue
            ids = [
                str(item) for item in raw.get("supporting_evidence_ids", [])
                if str(item) in retrieved_ids and str(item) in evidence_by_id
            ][:8]
            if not ids:
                continue
            question_text = str(raw.get("question", "")).strip()
            normalised = "".join(question_text.split())
            if not normalised or normalised in seen:
                continue
            source_ids = list(dict.fromkeys(
                evidence_by_id[item].source_asset_id
                for item in ids
                if evidence_by_id[item].source_asset_id
            ))
            try:
                question = MaterialInterviewQuestion.model_validate({
                    **raw,
                    "question": question_text,
                    "supporting_evidence_ids": ids,
                    "source_asset_ids": source_ids,
                })
            except Exception:
                continue
            questions.append(question)
            seen.add(normalised)
            if len(questions) >= min(self.MAX_QUESTIONS, max_questions):
                break
        self._end_timer()
        self.log(f"基于素材证据生成 {len(questions)} 个剪辑业务问题")
        return questions
