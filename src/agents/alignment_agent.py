"""比较素材事实与初步任务书，生成用户可选择的需求优化建议。"""

from __future__ import annotations

import json
from typing import Callable, Optional

from src.agents.base import BaseAgent
from src.models.schemas import ContentAnalysis, MaterialAlignmentProposal, RequirementSpec
from src.tools.llm import call_llm


SYSTEM_PROMPT = """你是活动视频的素材—需求对齐分析师。

请比较用户原始需求、初步任务书和带证据 ID 的素材转录摘录，判断：
- aligned：需求已经具体，并且与素材内容基本一致；
- too_vague：需求过于宽泛，素材中已经出现可以写得更具体的主题；
- mismatch：用户描述的活动类型或重点与素材主要内容明显不一致。

你只能提出建议，不能宣布已经修改需求，也不能把转录中的姓名、职务、奖项等高风险实体当作已确认事实。
建议必须能够从提供的素材摘录得到支持，不得编造未出现的人物、环节或活动。使用老师和企业负责人能理解的中文。
建议的重点内容最多 6 条，必须适合后续选片和逐项验收；应明确要求人物表达保持完整，不截断问题和回答。
建议时长必须在 30–600 秒之间，并结合素材时长和信息密度，不要机械沿用原值。

严格返回 JSON：
{
  "alignment_status": "aligned|too_vague|mismatch",
  "confidence": 0.0,
  "material_summary": "素材主要内容",
  "detected_topics": ["主题1"],
  "rationale": "为什么认为一致、过宽或不一致",
  "suggested_requirement_text": "用户可直接采用的一段完整需求",
  "suggested_purpose": "更具体的成片用途",
  "suggested_video_type": "general|sports|competition|meeting",
  "suggested_style": "formal|exciting|warm|funny|general",
  "suggested_target_duration": 120,
  "suggested_focus_items": ["可验收的重点内容"],
  "supporting_evidence_ids": ["只能填写提供过的证据 ID"]
}
"""


class RequirementAlignmentAgent(BaseAgent):
    """LLM 生成对齐建议，Harness 负责证据范围与字段校验。"""

    MAX_EVIDENCE_WINDOWS = 12

    def __init__(self, llm_runner: Optional[Callable] = None):
        super().__init__("RequirementAlignmentAgent")
        self.llm_runner = llm_runner or call_llm

    @classmethod
    def _sample_evidence(cls, analysis: ContentAnalysis) -> list:
        evidence = [item for item in analysis.evidence if item.type == "transcript"]
        if len(evidence) <= cls.MAX_EVIDENCE_WINDOWS:
            return evidence
        last = len(evidence) - 1
        indices = {
            round(index * last / (cls.MAX_EVIDENCE_WINDOWS - 1))
            for index in range(cls.MAX_EVIDENCE_WINDOWS)
        }
        return [evidence[index] for index in sorted(indices)]

    def run(
        self,
        *,
        raw_text: str,
        spec: RequirementSpec,
        analysis: ContentAnalysis,
        scenario: str,
    ) -> MaterialAlignmentProposal:
        sampled = self._sample_evidence(analysis)
        allowed_ids = {item.id for item in sampled}
        request = {
            "original_request": raw_text,
            "scenario": scenario,
            "material_duration_seconds": analysis.video_duration,
            "existing_material_summary": analysis.summary,
            "initial_taskbook": {
                "purpose": spec.purpose,
                "audience": spec.audience,
                "video_type": spec.video_type,
                "style": spec.style,
                "target_duration": spec.target_duration,
                "requirements": [item.description for item in spec.requirements],
            },
            "material_evidence": [
                {
                    "evidence_id": item.id,
                    "start": item.source_start,
                    "end": item.source_end,
                    "transcript": item.content[:500],
                }
                for item in sampled
            ],
        }
        self._start_timer()
        response = self.llm_runner(
            system_prompt=SYSTEM_PROMPT,
            user_message=json.dumps(request, ensure_ascii=False),
            return_json=True,
            temperature=0.1,
            max_tokens=1800,
        )
        if not isinstance(response, dict):
            raise ValueError("素材对齐模型没有返回 JSON 对象")
        suggestion_text = str(response.get("suggested_requirement_text", "")).strip()
        duration = response.get("suggested_target_duration")
        if suggestion_text and not any(unit in suggestion_text for unit in ("分钟", "秒")):
            suggestion_text = f"目标成片约 {float(duration):g} 秒。" + suggestion_text
        if suggestion_text and "完整" not in suggestion_text and "截断" not in suggestion_text:
            suggestion_text += "每段人物发言或问答必须保留完整，不在表达中途截断。"
        proposal = MaterialAlignmentProposal.model_validate({
            **response,
            "suggested_requirement_text": suggestion_text,
            "requirement_spec_id": spec.id,
            "requirement_spec_version": spec.version,
            "supporting_evidence_ids": [
                evidence_id
                for evidence_id in response.get("supporting_evidence_ids", [])
                if evidence_id in allowed_ids
            ][:12],
        })
        if proposal.requires_user_decision and not proposal.supporting_evidence_ids:
            raise ValueError("素材对齐建议缺少可核对的转录证据")
        self._end_timer()
        self.log(
            f"素材与需求对齐结果: {proposal.alignment_status}，"
            f"建议 {len(proposal.suggested_focus_items)} 个重点"
        )
        return proposal
