"""需求槽位的确定性澄清、冲突保护与版本生成。"""

from __future__ import annotations

import re
from typing import Iterable, Optional

from src.models.schemas import (
    ClarificationTurn,
    ExecutionBrief,
    RequirementItem,
    RequirementSlot,
    RequirementSpec,
    utc_now,
)


class RequirementClarificationService:
    """模型可以抽取答案，但只有本服务能合并槽位和创建新版本。"""

    UNKNOWN_ANSWERS = {"不知道", "不确定", "暂不确定", "待确认", "unknown"}

    def apply(
        self,
        *,
        task_id: str,
        spec: RequirementSpec,
        execution: ExecutionBrief,
        slots: Iterable[RequirementSlot],
        answers: dict[str, str],
        actor_id: Optional[str] = None,
        allow_confirmed_override: bool = False,
    ) -> tuple[RequirementSpec, ExecutionBrief, list[RequirementSlot], ClarificationTurn]:
        if spec.status not in {"draft", "confirmed"}:
            raise ValueError("只有当前草稿或已确认任务书可以澄清")
        slot_list = list(slots)
        by_key = {slot.key: slot for slot in slot_list}
        unknown_keys = set(answers) - set(by_key)
        if unknown_keys:
            raise ValueError("未知需求槽位：" + "、".join(sorted(unknown_keys)))

        changed_ids: list[str] = []
        effective_answers: dict[str, str] = {}
        recorded_answers: dict[str, str] = {}
        updated_slots: list[RequirementSlot] = []
        for slot in slot_list:
            raw_answer = answers.get(slot.key)
            if raw_answer is None or not str(raw_answer).strip():
                updated_slots.append(slot)
                continue
            answer = str(raw_answer).strip()
            if answer in self.UNKNOWN_ANSWERS:
                updated_slots.append(
                    slot.model_copy(
                        update={
                            "value": None,
                            "status": "unknown",
                            "source_type": "clarification",
                            "source_ref": f"requirement_v{spec.version + 1}",
                            "question": None,
                            "question_reason": None,
                            "question_impact": None,
                            "answer_hint": None,
                            "question_source": None,
                            "updated_at": utc_now(),
                        }
                    )
                )
                changed_ids.append(slot.id)
                recorded_answers[slot.key] = "暂时忽略"
                continue
            if (
                slot.status == "confirmed"
                and slot.value is not None
                and self._normalise_value(slot.value) != self._normalise_value(answer)
                and not allow_confirmed_override
            ):
                updated_slots.append(
                    slot.model_copy(
                        update={
                            "status": "conflict",
                            "question": f"“{slot.label}”已有确认值“{slot.value}”，是否明确替换为“{answer}”？",
                            "question_reason": "本轮答案与此前已经确认的内容不一致。",
                            "question_impact": "直接覆盖可能让后续选片和验收使用错误版本。",
                            "answer_hint": "确认要替换时，请再次填写新的最终值。",
                            "question_source": "deterministic-conflict-check",
                            "updated_at": utc_now(),
                        }
                    )
                )
                changed_ids.append(slot.id)
                continue
            parsed_value = self._parse_value(slot.key, answer)
            updated_slots.append(
                slot.model_copy(
                    update={
                        "value": parsed_value,
                        "status": "confirmed",
                        "source_type": "clarification",
                        "source_ref": f"requirement_v{spec.version + 1}",
                        "question": None,
                        "question_reason": None,
                        "question_impact": None,
                        "answer_hint": None,
                        "question_source": None,
                        "updated_at": utc_now(),
                    }
                )
            )
            changed_ids.append(slot.id)
            effective_answers[slot.key] = answer
            recorded_answers[slot.key] = answer

        new_requirements = list(spec.requirements)
        focus = effective_answers.get("focus_keywords")
        if focus:
            existing_descriptions = {item.description for item in new_requirements}
            for keyword in self._split_values(focus):
                description = f"优先保留与“{keyword}”相关的有效画面或发言"
                if description not in existing_descriptions:
                    new_requirements.append(
                        RequirementItem(
                            category="content",
                            description=description,
                            priority="should",
                            status="confirmed",
                        )
                    )
        high_risk = effective_answers.get("high_risk_review")
        if high_risk and not any(
            item.category == "compliance" and high_risk in item.description
            for item in new_requirements
        ):
            new_requirements.append(
                RequirementItem(
                    category="compliance",
                    description=f"用户确认的高风险核对说明：{high_risk}",
                    priority="should",
                    status="confirmed",
                )
            )
        elif any(
            slot.key == "high_risk_review" and slot.status == "unknown"
            for slot in updated_slots
        ) and not any(
            item.category == "compliance" and "高风险实体尚未确认" in item.description
            for item in new_requirements
        ):
            new_requirements.append(
                RequirementItem(
                    category="compliance",
                    description="高风险实体尚未确认；成片中的姓名、职务、奖项、产品名和隐私信息必须人工复核",
                    priority="should",
                    status="needs_confirmation",
                )
            )

        updates: dict = {
            "version": spec.version + 1,
            "status": "draft",
            "confirmed_at": None,
            "requirements": new_requirements,
            "open_questions": [
                slot.question
                for slot in updated_slots
                if slot.status in {"missing", "conflict"} and slot.question
            ][:5],
            "created_at": utc_now(),
        }
        for key in ("purpose", "audience", "style"):
            if key in effective_answers:
                updates[key] = effective_answers[key]
        if "target_duration" in effective_answers:
            updates["target_duration"] = self._parse_duration(effective_answers["target_duration"])
        revised_spec = spec.model_copy(update=updates)
        answer_lines = [f"- {by_key[key].label}：{value}" for key, value in effective_answers.items()]
        revised_execution = execution.model_copy(
            update={
                "version": execution.version + 1,
                "requirement_spec_version": revised_spec.version,
                "visible_instruction": "\n".join(
                    [execution.visible_instruction, "用户澄清：", *answer_lines]
                ),
                "included_requirement_ids": [item.id for item in revised_spec.requirements],
                "status": "draft",
                "confirmed_at": None,
            }
        )
        turn = ClarificationTurn(
            task_id=task_id,
            requirement_spec_id=spec.id,
            from_version=spec.version,
            to_version=revised_spec.version,
            answers=recorded_answers,
            changed_slot_ids=changed_ids,
            actor_id=actor_id,
        )
        return revised_spec, revised_execution, updated_slots, turn

    @staticmethod
    def _normalise_value(value) -> str:
        if isinstance(value, list):
            return ",".join(str(item).strip() for item in value)
        return str(value).strip()

    @classmethod
    def _parse_value(cls, key: str, answer: str):
        if key == "target_duration":
            return cls._parse_duration(answer)
        if key == "focus_keywords":
            return cls._split_values(answer)
        return answer

    @staticmethod
    def _parse_duration(answer: str) -> float:
        minute_match = re.search(r"(\d+(?:\.\d+)?)\s*分钟", answer)
        second_match = re.search(r"(\d+(?:\.\d+)?)\s*秒", answer)
        if minute_match:
            value = float(minute_match.group(1)) * 60
        elif second_match:
            value = float(second_match.group(1))
        else:
            value = float(answer)
        if not 1 <= value <= 3600:
            raise ValueError("目标时长必须在 1–3600 秒之间")
        return value

    @staticmethod
    def _split_values(answer: str) -> list[str]:
        return [
            value.strip()
            for value in re.split(r"[,，、;；\n]", answer)
            if value.strip()
        ][:8]
