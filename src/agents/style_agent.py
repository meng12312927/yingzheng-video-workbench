"""基于已确认任务书的受控 LLM 风格推荐。"""

from __future__ import annotations

from typing import Callable, Optional

from src.models.schemas import RequirementSpec, StyleProposal
from src.services.styles import StyleCatalog
from src.tools.llm import call_llm


class StyleRecommendationAgent:
    def __init__(
        self,
        catalog: Optional[StyleCatalog] = None,
        llm_runner: Optional[Callable] = None,
    ):
        self.catalog = catalog or StyleCatalog()
        self.llm_runner = llm_runner or call_llm

    def run(self, spec: RequirementSpec, scenario: str) -> list[StyleProposal]:
        if spec.status != "confirmed":
            raise ValueError("只有已确认任务书可以生成风格建议")
        bundles = self.catalog.available_bundles(scenario)
        if not bundles:
            return []
        system_prompt = (
            "你是活动视频视觉风格推荐器。只能从给定 bundle_id 中最多推荐 3 项，"
            "说明它与用途、受众和活动类型的业务匹配原因。返回 JSON："
            "{\"proposals\":[{\"bundle_id\":str,\"rationale\":str,\"confidence\":0..1}]}。"
        )
        request = {
            "task": {
                "purpose": spec.purpose,
                "audience": spec.audience,
                "video_type": spec.video_type,
                "style": spec.style,
                "target_duration": spec.target_duration,
            },
            "bundles": [
                {"bundle_id": bundle.id, "name": bundle.name, "scenario": bundle.scenario}
                for bundle in bundles
            ],
        }
        try:
            response = self.llm_runner(
                system_prompt=system_prompt,
                user_message=str(request),
                return_json=True,
                temperature=0.0,
            )
            proposals = self._validate_response(response, spec, {bundle.id for bundle in bundles})
            if proposals:
                return proposals
        except Exception:
            pass
        preferred = self._fallback_bundle(spec.style, scenario, bundles)
        return [
            StyleProposal(
                requirement_spec_id=spec.id,
                requirement_spec_version=spec.version,
                bundle_id=preferred.id,
                rationale=(
                    f"根据“{spec.purpose}”及受众“{spec.audience}”，"
                    f"建议使用已有的“{preferred.name}”组合；这是模型不可用时的可见保守建议。"
                ),
                confidence=0.5,
                source="deterministic_fallback",
            )
        ]

    @staticmethod
    def _validate_response(response, spec: RequirementSpec, allowed_ids: set[str]) -> list[StyleProposal]:
        raw_items = response.get("proposals", []) if isinstance(response, dict) else []
        proposals: list[StyleProposal] = []
        seen = set()
        for item in raw_items:
            bundle_id = item.get("bundle_id") if isinstance(item, dict) else None
            if bundle_id not in allowed_ids or bundle_id in seen:
                continue
            seen.add(bundle_id)
            try:
                proposals.append(
                    StyleProposal(
                        requirement_spec_id=spec.id,
                        requirement_spec_version=spec.version,
                        bundle_id=bundle_id,
                        rationale=item["rationale"],
                        confidence=item["confidence"],
                    )
                )
            except Exception:
                continue
            if len(proposals) == 3:
                break
        return proposals

    @staticmethod
    def _fallback_bundle(style: str, scenario: str, bundles):
        preferred_id = {
            "exciting": "bundle_school_highlight",
            "formal": "bundle_enterprise_report" if scenario == "enterprise" else "bundle_formal_clean",
            "warm": "bundle_formal_clean",
            "funny": "bundle_school_highlight",
        }.get(style, "bundle_formal_clean")
        return next((bundle for bundle in bundles if bundle.id == preferred_id), bundles[0])

