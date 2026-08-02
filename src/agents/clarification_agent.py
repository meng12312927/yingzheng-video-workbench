"""由 AI 分析任务书缺口，并生成少量、可解释的澄清问题。"""

from __future__ import annotations

import json
from typing import Callable, Iterable, Optional

from src.agents.base import BaseAgent
from src.models.schemas import ClarificationQuestion, RequirementSlot, RequirementSpec
from src.tools.llm import call_llm


SYSTEM_PROMPT = """你是活动视频剪辑的需求澄清分析师。

你的任务不是重新生成任务书，而是判断当前任务书中哪些不确定信息会明显影响：
1. 应该选择哪些素材；
2. 成片能否逐项验收；
3. 姓名、职务、奖项、产品名或隐私信息是否存在发布风险。

请只从 allowed_slots 中选择确实有必要询问的 slot_key，最多提出 3 个问题，也可以返回空数组。
不得询问用户已经明确提供或表单已经确认的信息；不得为了凑数量提问；不得索要密码、密钥等敏感信息。
问题必须面向不懂剪辑技术的老师或企业活动负责人，避免使用 slot、prompt、should、formal 等内部词。
每个问题都要说明为什么询问，以及不补充可能影响什么。所有问题都允许用户暂时忽略。
不得承诺当前系统不支持的能力。当前只能据此选入或排除片段、核对字幕文字、标记人工复核；
不支持自动识别人脸或模糊人脸。遇到此类隐私需求时，只能询问是否避免选入相关画面，或标记交给人工后期处理。

严格返回 JSON：
{
  "questions": [
    {
      "slot_key": "allowed_slots 中的 key",
      "question": "直接、具体、一次只问一件事",
      "reason": "根据当前需求指出具体缺口",
      "impact": "说明对选片、字幕、验收或发布的影响",
      "answer_hint": "可选的简短示例"
    }
  ]
}
"""


class RequirementClarificationAgent(BaseAgent):
    """LLM 只生成问题；问题数量、槽位范围和状态由代码校验。"""

    MAX_QUESTIONS = 3

    def __init__(self, llm_runner: Optional[Callable] = None):
        super().__init__("RequirementClarificationAgent")
        self.llm_runner = llm_runner or call_llm

    def run(
        self,
        *,
        raw_text: str,
        spec: RequirementSpec,
        slots: Iterable[RequirementSlot],
        scenario: str,
    ) -> list[ClarificationQuestion]:
        slots = list(slots)
        eligible = [
            slot for slot in slots
            if slot.status != "confirmed"
            and not (
                slot.source_type == "clarification"
                and slot.status == "unknown"
                and slot.question is None
            )
        ]
        if not eligible:
            return []
        allowed = {slot.key: slot for slot in eligible}
        request = {
            "original_request": raw_text,
            "scenario": scenario,
            "taskbook": {
                "purpose": spec.purpose,
                "audience": spec.audience,
                "target_duration": spec.target_duration,
                "style": spec.style,
                "need_subtitles": spec.need_subtitles,
                "need_bgm": spec.need_bgm,
                "requirements": [
                    {
                        "description": item.description,
                        "priority": item.priority,
                        "status": item.status,
                    }
                    for item in spec.requirements
                ],
            },
            "allowed_slots": [
                {
                    "key": slot.key,
                    "label": slot.label,
                    "current_value": slot.value,
                    "status": slot.status,
                    "risk_level": slot.risk_level,
                    "source_type": slot.source_type,
                }
                for slot in eligible
            ],
        }
        self._start_timer()
        response = self.llm_runner(
            system_prompt=SYSTEM_PROMPT,
            user_message=json.dumps(request, ensure_ascii=False),
            return_json=True,
            temperature=0.1,
            max_tokens=1400,
        )
        raw_questions = response.get("questions", []) if isinstance(response, dict) else []
        questions: list[ClarificationQuestion] = []
        seen_keys: set[str] = set()
        for raw in raw_questions:
            if not isinstance(raw, dict):
                continue
            slot_key = str(raw.get("slot_key", "")).strip()
            if slot_key not in allowed or slot_key in seen_keys:
                continue
            try:
                question = ClarificationQuestion.model_validate(raw)
            except Exception:
                continue
            questions.append(question)
            seen_keys.add(slot_key)
            if len(questions) >= self.MAX_QUESTIONS:
                break
        self._end_timer()
        self.log(f"根据任务书生成 {len(questions)} 个动态澄清问题")
        return questions

    @staticmethod
    def apply_questions(
        spec: RequirementSpec,
        slots: Iterable[RequirementSlot],
        questions: Iterable[ClarificationQuestion],
    ) -> tuple[RequirementSpec, list[RequirementSlot]]:
        """把经代码校验的问题投影到槽位，不允许 AI 直接修改任务书值。"""
        by_key = {question.slot_key: question for question in questions}
        updated_slots: list[RequirementSlot] = []
        for slot in slots:
            question = by_key.get(slot.key)
            if question is None:
                updated_slots.append(
                    slot.model_copy(update={
                        "question": None,
                        "question_reason": None,
                        "question_impact": None,
                        "answer_hint": None,
                        "question_source": None,
                    })
                )
                continue
            updated_slots.append(
                slot.model_copy(update={
                    "status": "conflict" if slot.status == "conflict" else "missing",
                    "question": question.question,
                    "question_reason": question.reason,
                    "question_impact": question.impact,
                    "answer_hint": question.answer_hint,
                    "question_source": f"clarification-agent:{spec.id}:v{spec.version}",
                })
            )
        revised_spec = spec.model_copy(
            update={"open_questions": [question.question for question in by_key.values()]}
        )
        return revised_spec, updated_slots
