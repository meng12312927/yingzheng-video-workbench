"""在调用候选模型前分配时长，避免依赖候选自然累加。"""

from __future__ import annotations

import math
from typing import Iterable

from src.models.schemas import (
    DurationBudgetPlan,
    RequirementDurationBudget,
    RequirementItem,
)


class DurationBudgetPlanner:
    """用可解释的确定性规则给 must/should/optional 分配时长。"""

    IDEAL_SPEECH_CLIP_SECONDS = 22.0

    def build(
        self,
        requirements: Iterable[RequirementItem],
        *,
        target_duration: float,
        duration_tolerance: float,
        available_duration: float,
    ) -> DurationBudgetPlan:
        items = [
            item for item in requirements
            if item.category == "content" and item.priority != "prohibited"
        ]
        required_minimum = max(0.0, target_duration - duration_tolerance)
        usable_target = min(float(target_duration), max(0.0, available_duration))
        # 活动回顾默认给气氛、环境和过渡镜头留 10%；当前无视觉候选时会明确显示缺口。
        atmosphere_budget = min(usable_target * 0.1, 30.0) if len(items) > 1 else 0.0
        content_budget = max(0.0, usable_target - atmosphere_budget)
        weights = {"must": 2.0, "should": 1.0, "optional": 0.5}
        total_weight = sum(weights[item.priority] for item in items) or 1.0
        budgets = []
        for item in items:
            target = content_budget * weights[item.priority] / total_weight
            minimum = min(target, 12.0 if item.priority == "must" else 6.0)
            budgets.append(
                RequirementDurationBudget(
                    requirement_id=item.id,
                    priority=item.priority,
                    target_seconds=round(target, 2),
                    minimum_seconds=round(minimum, 2),
                    desired_candidate_count=max(
                        1,
                        min(8, math.ceil(target / self.IDEAL_SPEECH_CLIP_SECONDS)),
                    ),
                )
            )
        return DurationBudgetPlan(
            target_duration=target_duration,
            duration_tolerance=duration_tolerance,
            required_minimum=required_minimum,
            content_budget=round(content_budget, 2),
            atmosphere_budget=round(atmosphere_budget, 2),
            requirement_budgets=budgets,
            recommendations=(
                ["目标时长超过全部素材总时长，请降低目标时长或补充素材。"]
                if target_duration > available_duration else []
            ),
        )

    @staticmethod
    def complete(
        plan: DurationBudgetPlan,
        selected_durations: Iterable[float],
    ) -> DurationBudgetPlan:
        planned = sum(max(0.0, float(value)) for value in selected_durations)
        shortage = max(0.0, plan.required_minimum - planned)
        recommendations = list(plan.recommendations)
        if shortage > 0:
            recommendations.append(
                f"当前合格候选距离最低时长还差约 {shortage:.0f} 秒；可扩展完整表达、补选其他主题或从原素材人工补片。"
            )
        if plan.atmosphere_budget > 0:
            recommendations.append(
                f"建议从无对白画面补充约 {plan.atmosphere_budget:.0f} 秒开场、环境或过渡镜头。"
            )
        return plan.model_copy(update={
            "planned_candidate_duration": round(planned, 2),
            "shortage_seconds": round(shortage, 2),
            "status": "short" if shortage > 0 else "sufficient",
            "recommendations": list(dict.fromkeys(recommendations)),
        })
