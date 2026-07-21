"""MVP2 学校/企业场景差异；验证前保持单文件配置驱动。"""

from __future__ import annotations

from typing import Any, Literal


Scenario = Literal["school", "enterprise"]


DOMAIN_CONFIG: dict[Scenario, dict[str, Any]] = {
    "school": {
        "display_name": "学校活动",
        "audience": "学校师生、家长与活动负责人",
        "keywords": ["班级", "年级", "班主任", "校长", "同学", "奖项"],
        "must_check": ["师生姓名", "奖项名称", "领导职务"],
        "privacy": ["未成年人信息", "未成年人肖像"],
    },
    "enterprise": {
        "display_name": "企业活动",
        "audience": "企业员工、客户与活动负责人",
        "keywords": ["产品", "客户", "合作伙伴", "发言人", "CEO", "发布会"],
        "must_check": ["发言人职务", "产品名称", "合作伙伴"],
        "privacy": ["未公开商业信息", "对外发布限制"],
    },
}


def get_domain_config(scenario: str) -> dict[str, Any]:
    if scenario not in DOMAIN_CONFIG:
        raise ValueError("scenario 只能是 school 或 enterprise")
    return DOMAIN_CONFIG[scenario]  # type: ignore[index]

