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

面试常考：Prompt Engineering
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
from src.models.schemas import VideoRequirement


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
  "need_bgm": true,
  "avoid_keywords": [],
  "output_format": "mp4"
}

## 注意事项
- focus_keywords 至少给 3 个，最多 8 个
- 如果用户没提到字幕/配乐，默认都需要
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
          - LLM 返回的 JSON 格式不对 → 返回默认需求
          - API 调用失败 → 抛出异常
        """
        self.log(f"收到用户需求: {user_input[:100]}...")
        self._start_timer()

        try:
            # 调用大模型解析需求
            raw_response = call_llm(
                system_prompt=SYSTEM_PROMPT,
                user_message=user_input,
                return_json=True,
                temperature=0.3,  # 需求解析偏保守，减少随机性
            )

            # 用 Pydantic 校验 + 补全默认值
            requirement = VideoRequirement(**raw_response)
            self.log(f"需求解析完成: {requirement.model_dump()}")
            self._end_timer()
            return requirement

        except Exception as e:
            self.log(f"需求解析失败: {e}", level="error")
            self.log("使用默认需求参数", level="warning")

            # 降级方案：返回默认需求
            default_req = VideoRequirement(
                target_duration=300,
                video_type="general",
                style="exciting",
                focus_keywords=[],
            )
            self._end_timer()
            return default_req


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
