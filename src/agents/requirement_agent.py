"""
===========================================================================
requirement_agent.py — Agent 1: 需求理解
===========================================================================
功能：把用户的自然语言请求转换为结构化的 VideoRequirement

技术原理（大白话版）：
  用户可能说得很模糊："帮我把运动会视频剪一下"
  Agent 1 的职责是把模糊需求变得精确：
  - "剪一下" → 多长？什么风格？聚焦哪些内容？
  - 通过精心设计的 System Prompt，让 GPT 来"补全"用户的意图

提示词设计：
  这个文件的核心是 SYSTEM_PROMPT。一个好的 Prompt：
  1. 定义角色（你是谁）
  2. 定义任务（你要做什么）
  3. 定义输出格式（必须返回什么结构的 JSON）
  4. 给例子（Few-shot，帮 AI 理解你的期望）
  5. 加约束（什么不能做）

Python 知识点：
  1. super().__init__("RequirementAgent") → 调用父类 BaseAgent 的 __init__
  2. from src.models.schemas import VideoRequirement → 使用我们定义的数据模型
  3. **requirement.model_dump() → 把 Pydantic 模型"解包"成字典传参
===========================================================================
"""

from src.agents.base import BaseAgent
from src.tools.llm import call_llm
import re
from typing import Optional

from src.domain_config import get_domain_config
from src.models.schemas import (
    ExecutionBrief,
    RequirementBrief,
    RequirementCompilation,
    RequirementItem,
    RequirementSlot,
    RequirementSpec,
    VideoRequirement,
)


class RequirementParsingError(RuntimeError):
    """LLM 未能产出合法需求；调用方必须显式进入人工草稿流程。"""


# ============================================================
# System Prompt —— Agent 1 的"大脑设定"
# ============================================================

SYSTEM_PROMPT = """你是一个专业的视频剪辑需求分析师。你的任务是把用户的模糊需求转化为精确的剪辑参数。

## 你的工作流程
1. 分析用户的话，提取关键信息（时长、类型、风格）
2. 补全用户没说清楚的部分（根据常识推断）
3. 返回结构化的 JSON

## 视频类型判断规则
- 提到"运动会""比赛""体育""田径""球赛" → video_type: "sports"
- 提到"竞赛""答题""演讲""辩论""答辩" → video_type: "competition"
- 提到"会议""讲座""培训""课程" → video_type: "meeting"
- 其他情况 → video_type: "general"

## 剪辑风格判断规则
- 提到"燃""精彩""高光""热血""激动" → style: "exciting"
- 提到"正式""严肃""庄重""官方" → style: "formal"
- 提到"温馨""感人""温情""回忆" → style: "warm"
- 提到"搞笑""有趣""轻松""好玩" → style: "funny"

## 重点关注词判断规则
根据视频类型自动推断：
- sports: ["冲刺", "欢呼", "加油", "冠军", "破纪录", "冲线"]
- competition: ["回答正确", "恭喜", "得分", "获胜", "冠军"]
- meeting: ["总结", "结论", "下一步", "关键", "重点"]
- general: []

## 时长规则
- 如果用户明确说"X分钟"，用 X*60 秒
- 如果用户说"短视频""精简"，默认 180 秒（3分钟）
- 如果用户说"长视频""完整版"，默认 600 秒（10分钟）
- 如果用户没说时长，默认 300 秒（5分钟）

## 输出格式（严格遵守）
{
  "target_duration": 180,
  "video_type": "sports",
  "style": "exciting",
  "focus_keywords": ["冲刺", "欢呼", "颁奖"],
  "need_subtitles": true,
  "need_bgm": false,
  "avoid_keywords": [],
  "output_format": "mp4"
}

## 注意事项
- focus_keywords 至少给 3 个，最多 8 个
- 如果用户没提到字幕，默认需要；配乐默认不需要，MVP 不会自动添加背景音乐
- 只返回 JSON，不要任何其他文字
"""


class RequirementAgent(BaseAgent):
    """
    Agent 1：需求理解智能体

    用法：
        agent = RequirementAgent()
        requirement = agent.run("帮我把运动会视频剪成3分钟精彩集锦")
        print(requirement.target_duration)  # 180
        print(requirement.style)            # "exciting"
    """

    def __init__(self):
        super().__init__("RequirementAgent")

    def run(self, user_input: str) -> VideoRequirement:
        """
        解析用户需求

        参数：
          user_input: 用户的原始输入（自然语言）

        返回值：
          VideoRequirement: 结构化的剪辑需求

        可能的异常：
          - LLM 返回非法 JSON/字段或 API 调用失败 → 抛出 RequirementParsingError

        这里禁止返回默认正式需求。降级为人工草稿由 ``compile`` 显式处理，
        从而让界面、审计记录和用户都能知道模型没有成功理解需求。
        """
        self.log(f"收到用户需求: {user_input[:100]}...")
        self._start_timer()

        try:
            raw_response = call_llm(
                system_prompt=SYSTEM_PROMPT,
                user_message=user_input,
                return_json=True,
                temperature=0.3,  # 需求解析偏保守，减少随机性
            )

            requirement = VideoRequirement(**raw_response)
            self.log(f"需求解析完成: {requirement.model_dump()}")
        except Exception as error:
            self.log(f"需求解析失败: {type(error).__name__}", level="error")
            self._end_timer()
            raise RequirementParsingError(
                "AI 需求解析失败，系统没有生成默认正式需求"
            ) from error
        self._end_timer()
        return requirement

    def compile(
        self,
        user_input: str,
        scenario: str = "school",
        submitted_by: Optional[str] = None,
        overrides: Optional[dict] = None,
    ) -> RequirementCompilation:
        """先建立不臆测的任务书骨架，素材转录后再由 AI 提出业务问题。

        快速工作流不在看见素材前调用 LLM。用户明确说出的值由代码抽取；
        没有说出的内容保留为待确认槽位，并在同一审核页展示。
        ``run`` 仅保留给旧接口和独立演示使用。
        """
        if scenario not in {"school", "enterprise"}:
            raise ValueError("scenario 只能是 school 或 enterprise")
        raw_text = user_input.strip() or "请根据素材整理一版可审核的剪辑方案"

        brief = RequirementBrief(
            scenario=scenario,
            raw_text=raw_text,
            submitted_by=submitted_by,
        )
        mode = "material_assisted"
        warnings: list[str] = []
        requirement = self._extract_explicit_requirement(raw_text)
        requirement = self._apply_overrides(requirement, overrides)
        items = self._build_requirement_items(raw_text, requirement)
        # 是否需要追问、具体问什么，由独立的澄清 Agent 在任务书草稿完成后判断。
        # 编译器不再通过固定模板替 AI 提问。
        questions: list[str] = []
        spec = RequirementSpec(
            brief_id=brief.id,
            purpose=self._infer_purpose(raw_text, scenario),
            audience=self._infer_audience(raw_text),
            video_type=requirement.video_type,
            style=requirement.style,
            need_subtitles=requirement.need_subtitles,
            need_bgm=requirement.need_bgm,
            target_duration=requirement.target_duration,
            duration_tolerance=max(10.0, min(30.0, requirement.target_duration * 0.1)),
            requirements=items,
            open_questions=questions,
        )
        visible_instruction = self._build_visible_instruction(spec)
        execution = ExecutionBrief(
            requirement_spec_id=spec.id,
            requirement_spec_version=spec.version,
            visible_instruction=visible_instruction,
            included_requirement_ids=[item.id for item in items],
        )
        slots = self._build_slots(
            raw_text,
            spec,
            scenario=scenario,
            overrides=overrides,
            manual_required=False,
        )
        self.log(f"已编译需求任务书 v{spec.version}，包含 {len(items)} 条要求和 {len(questions)} 个待确认问题")
        return RequirementCompilation(
            brief=brief,
            spec=spec,
            execution_brief=execution,
            legacy_requirement=requirement,
            mode=mode,
            warnings=warnings,
            slots=slots,
        )

    @staticmethod
    def _extract_explicit_requirement(raw_text: str) -> VideoRequirement:
        """只抽取原文明确出现的值；未知项使用中性机器值并标为待确认。"""
        minute_match = re.search(r"(\d+(?:\.\d+)?)\s*分钟", raw_text)
        second_match = re.search(r"(\d+(?:\.\d+)?)\s*秒", raw_text)
        if minute_match:
            duration = int(float(minute_match.group(1)) * 60)
        elif second_match:
            duration = int(float(second_match.group(1)))
        else:
            duration = 300
        duration = max(30, min(600, duration))

        if any(word in raw_text for word in ("运动会", "体育", "田径", "球赛")):
            video_type = "sports"
        elif any(word in raw_text for word in ("竞赛", "演讲", "辩论", "答辩")):
            video_type = "competition"
        elif any(word in raw_text for word in ("会议", "讲座", "培训", "访谈", "采访")):
            video_type = "meeting"
        else:
            video_type = "general"

        style = "general"
        for words, value in (
            (("燃", "热血", "快节奏"), "exciting"),
            (("正式", "庄重", "官方"), "formal"),
            (("温馨", "感人", "温情"), "warm"),
            (("搞笑", "轻松", "有趣"), "funny"),
        ):
            if any(word in raw_text for word in words):
                style = value
                break

        focus_keywords: list[str] = []
        for pattern in (
            r"(?:重点|主要)\s*(?:保留|突出|要)?\s*([^。；，,]{2,30})",
            r"必须\s*(?:保留|包含)?\s*([^。；，,]{2,30})",
        ):
            focus_keywords.extend(match.strip() for match in re.findall(pattern, raw_text))
        return VideoRequirement(
            target_duration=duration,
            video_type=video_type,
            style=style,
            focus_keywords=list(dict.fromkeys(focus_keywords))[:8],
            need_subtitles="不要字幕" not in raw_text and "无需字幕" not in raw_text,
            need_bgm=any(word in raw_text for word in ("背景音乐", "配乐", "BGM", "bgm")),
        )

    @staticmethod
    def _apply_overrides(
        requirement: VideoRequirement,
        overrides: Optional[dict],
    ) -> VideoRequirement:
        """只接受 VideoRequirement 已声明字段；空关键词列表也是用户的有效选择。"""
        if not overrides:
            return requirement
        allowed_fields = set(VideoRequirement.model_fields)
        explicit_values = {
            key: value
            for key, value in overrides.items()
            if key in allowed_fields and value is not None and value != ""
        }
        return VideoRequirement.model_validate(
            {**requirement.model_dump(), **explicit_values}
        )

    @staticmethod
    def _build_requirement_items(raw_text: str, requirement: VideoRequirement) -> list[RequirementItem]:
        """将可执行关键词转为业务要求；明确的必须/禁止项附带验收规则。"""
        items: list[RequirementItem] = []
        for keyword in requirement.focus_keywords[:8]:
            is_must = bool(re.search(rf"(?:必须|一定要|务必).{{0,16}}{re.escape(keyword)}|{re.escape(keyword)}.{{0,8}}(?:必须|一定要|务必)", raw_text))
            items.append(
                RequirementItem(
                    category="content",
                    description=f"优先保留与“{keyword}”相关的有效画面或发言",
                    priority="must" if is_must else "should",
                    status="needs_confirmation" if is_must else "draft",
                    acceptance_rule=(f"成片中至少有一个已确认片段覆盖“{keyword}”，并引用转录或人工标注证据" if is_must else None),
                )
            )
        for keyword in requirement.avoid_keywords[:8]:
            items.append(
                RequirementItem(
                    category="restriction",
                    description=f"不得包含与“{keyword}”相关的内容",
                    priority="prohibited",
                    status="needs_confirmation",
                    acceptance_rule=f"候选片段与最终时间线不得命中“{keyword}”；无法自动判断时标记人工复核",
                )
            )
        if not items:
            items.append(
                RequirementItem(
                    category="content",
                    description="从素材中筛选能代表活动主题和氛围的片段",
                    priority="should",
                    status="needs_confirmation",
                )
            )
        return items

    @staticmethod
    def _build_slots(
        raw_text: str,
        spec: RequirementSpec,
        *,
        scenario: str,
        overrides: Optional[dict],
        manual_required: bool,
    ) -> list[RequirementSlot]:
        """将任务书投影为带来源的槽位，后续澄清只补丁这些槽位。"""
        explicit = overrides or {}
        publish_match = re.search(r"(公众号|抖音|视频号|内网|汇报|宣传|官网)", raw_text)
        # UI 会始终提交 focus_keywords；空列表只代表用户尚未填写，不能视为已确认。
        focus_explicit = bool(explicit.get("focus_keywords"))
        duration_explicit = "target_duration" in explicit or bool(
            re.search(r"\d+(?:\.\d+)?\s*(?:分钟|秒)", raw_text)
        )
        style_explicit = "style" in explicit or any(
            word in raw_text
            for word in (
                "燃", "热血", "快节奏", "正式", "庄重", "官方",
                "温馨", "感人", "温情", "搞笑", "轻松", "有趣",
            )
        )
        purpose_explicit = spec.purpose != "待你确认"
        audience_explicit = spec.audience != "待你确认"
        domain = get_domain_config(scenario)
        return [
            RequirementSlot(
                key="purpose",
                label="视频用途",
                value=spec.purpose,
                status="confirmed" if purpose_explicit else "missing",
                source_type="user_input" if purpose_explicit else "llm",
                source_ref=spec.id,
            ),
            RequirementSlot(
                key="audience",
                label="目标受众",
                value=spec.audience,
                status="confirmed" if audience_explicit else "missing",
                source_type="user_input" if audience_explicit else "llm",
                source_ref="raw_text",
            ),
            RequirementSlot(
                key="target_duration",
                label="目标时长",
                value=spec.target_duration,
                status="confirmed" if duration_explicit else "inferred",
                source_type=(
                    "form" if "target_duration" in explicit
                    else "user_input" if duration_explicit
                    else "llm"
                ),
                source_ref=spec.id,
            ),
            RequirementSlot(
                key="style",
                label="整体风格",
                value=spec.style,
                status="confirmed" if style_explicit else "inferred",
                source_type="form" if style_explicit else "llm",
                source_ref=spec.id,
            ),
            RequirementSlot(
                key="need_bgm",
                label="背景音乐",
                value=spec.need_bgm,
                status=(
                    "confirmed"
                    if any(word in raw_text for word in (
                        "背景音乐", "配乐", "BGM", "bgm", "不要音乐", "无需音乐",
                    ))
                    else "missing"
                ),
                source_type="user_input",
                source_ref="raw_text",
            ),
            RequirementSlot(
                key="focus_keywords",
                label="重点内容",
                value=[
                    item.description for item in spec.requirements
                    if item.category == "content"
                ],
                status=("confirmed" if focus_explicit else "missing"),
                source_type="form" if focus_explicit else "llm",
                source_ref=spec.id,
                question=None,
            ),
            RequirementSlot(
                key="publish_channel",
                label="发布渠道",
                value=publish_match.group(1) if publish_match else None,
                status="confirmed" if publish_match else "missing",
                source_type="user_input",
                source_ref="raw_text",
                question=None,
            ),
            RequirementSlot(
                key="high_risk_review",
                label="高风险信息核对",
                value={
                    "must_check": domain["must_check"],
                    "privacy": domain["privacy"],
                },
                status="unknown",
                risk_level="high",
                source_type="domain_config",
                source_ref=scenario,
                question=None,
            ),
        ]

    @staticmethod
    def _infer_purpose(raw_text: str, scenario: str) -> str:
        if any(word in raw_text for word in ("访谈", "采访")):
            return "访谈精华摘要"
        if "汇报" in raw_text:
            return "活动成果汇报视频"
        if "回顾" in raw_text or "集锦" in raw_text:
            return "学校活动回顾视频" if scenario == "school" else "企业活动回顾视频"
        if any(word in raw_text for word in ("宣传", "发布", "公众号", "视频号")):
            return "活动对外宣传回顾视频"
        return "待你确认"

    @staticmethod
    def _infer_audience(raw_text: str) -> str:
        audience_patterns = (
            (("家长",), "师生与家长"),
            (("学生", "师生"), "师生"),
            (("客户", "合作伙伴"), "客户与合作伙伴"),
            (("员工", "内部"), "企业内部员工"),
            (("领导", "汇报"), "相关负责人"),
            (("公众", "对外"), "社会公众"),
        )
        for words, label in audience_patterns:
            if any(word in raw_text for word in words):
                return label
        return "待你确认"

    @staticmethod
    def _build_visible_instruction(spec: RequirementSpec) -> str:
        priority_names = {
            "must": "必须保留",
            "should": "建议保留",
            "optional": "可有可无",
            "prohibited": "禁止出现",
        }
        style_names = {
            "formal": "正式稳重",
            "exciting": "精彩有节奏",
            "warm": "温馨自然",
            "funny": "轻松活泼",
            "general": "由你在任务书中确认",
        }
        item_lines = [
            f"- **{priority_names[item.priority]}：** {item.description}"
            for item in spec.requirements
        ]
        subtitle = "生成中文字幕，并允许你在出片前修改" if spec.need_subtitles else "不生成中文字幕"
        bgm = "只使用你上传并确认有权使用的背景音乐" if spec.need_bgm else "不自动添加背景音乐"
        style = style_names.get(spec.style, "根据活动内容自然处理")
        return "\n".join(
            [
                "1. **理解素材：** 先把视频语音转成带时间位置的文字，定位与任务书有关的内容。",
                "2. **筛选候选：** 按照下面的内容要求寻找片段；不会仅凭一个关键词直接决定入选。",
                *item_lines,
                "3. **给出依据：** 每个候选片段都会展示原视频时间、字幕原文、对应要求和推荐理由，方便你核对。",
                "4. **标记风险：** 姓名、职务、奖项、产品名称和隐私信息不会被当作已确认事实，会提醒你人工检查。",
                "5. **等待审核：** AI 只生成候选片段和粗剪顺序；你确认保留、删除、字幕和顺序后，系统才生成成片。",
                "",
                "**本次输出约定：**",
                f"- 目标成片时长 {spec.target_duration:.0f} 秒，允许前后相差 {spec.duration_tolerance:.0f} 秒",
                f"- 画面整体感觉：{style}",
                f"- 字幕：{subtitle}",
                f"- 音乐：{bgm}",
                "- 文件格式：MP4",
            ]
        )


# ============================================================
# 测试代码
# ============================================================
if __name__ == "__main__":
    print("=" * 60)
    print("Agent 1 测试：需求理解")
    print("=" * 60)

    agent = RequirementAgent()

    # 测试用例
    test_inputs = [
        "帮我把运动会视频剪成3分钟精彩集锦，重点要冲刺和颁奖的画面",
        "把昨天的知识竞赛视频处理一下",
        "这个会议录像太长了，帮我剪个5分钟精华版",
    ]

    for user_input in test_inputs:
        print(f"\n用户输入: {user_input}")
        try:
            result = agent.run(user_input)
            print(f"  视频类型: {result.video_type}")
            print(f"  风格: {result.style}")
            print(f"  目标时长: {result.target_duration}s ({result.target_duration//60}分钟)")
            print(f"  关键词: {result.focus_keywords}")
            print(f"  字幕: {result.need_subtitles}, 配乐: {result.need_bgm}")
        except Exception as e:
            print(f"  错误: {e}")
