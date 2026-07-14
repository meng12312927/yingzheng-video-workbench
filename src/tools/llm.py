"""
===========================================================================
llm.py — 大模型调用工具
===========================================================================
功能：封装对 GPT 的调用，是所有 Agent 的"大脑"

技术原理（大白话版）：
  通过 OpenAI API 向 GPT 发送消息并获取回复。
  核心概念：
  - System Prompt（系统提示）= 设定 AI 的角色
  - User Message（用户消息）= 你要 AI 执行的具体任务
  - JSON Mode = 强制 AI 返回 JSON 格式，方便程序解析
===========================================================================
"""

import json
import time
from typing import Union
from openai import OpenAI
from src.config import OPENAI_API_KEY, OPENAI_MODEL

# 全局客户端（单例，整个程序共用一个连接）
_client = OpenAI(api_key=OPENAI_API_KEY)


def call_llm(
    system_prompt: str,
    user_message: str,
    return_json: bool = True,
    temperature: float = 0.3,
    max_tokens: int = 4096,
    max_retries: int = 3,
) -> Union[dict, str]:
    """
    调用大模型（最常用的函数）

    参数：
      system_prompt: 系统提示，定义 AI 的角色
      user_message: 用户消息，要 AI 做什么
      return_json: True=返回 JSON 字典，False=返回文本
      temperature: 0.0-2.0，越小越保守，越大越随机
      max_tokens: 最大输出长度
      max_retries: 失败自动重试次数

    返回值：
      dict（如果 return_json=True）或 str（如果 return_json=False）
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    for attempt in range(max_retries):
        try:
            kwargs = {
                "model": OPENAI_MODEL,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }

            if return_json:
                kwargs["response_format"] = {"type": "json_object"}
                if "json" not in messages[0]["content"].lower():
                    messages[0]["content"] += "\n\n请始终以 JSON 格式返回。"

            response = _client.chat.completions.create(**kwargs)
            content = response.choices[0].message.content

            if return_json:
                # 清理可能的 markdown 代码块标记
                content = content.strip()
                if content.startswith("```"):
                    content = content.split("\n", 1)[1]
                if content.endswith("```"):
                    content = content[:-3]
                return json.loads(content.strip())
            else:
                return content.strip()

        except json.JSONDecodeError as e:
            print(f"[LLM] JSON 解析失败 (attempt {attempt + 1}): {e}")
            if attempt < max_retries - 1:
                time.sleep(1)
            else:
                print(f"[LLM] 重试 {max_retries} 次后仍然失败，返回空结果")
                return {} if return_json else ""

        except Exception as e:
            print(f"[LLM] API 调用失败 (attempt {attempt + 1}): {e}")
            if attempt < max_retries - 1:
                time.sleep(2)
            else:
                raise RuntimeError(f"LLM 调用在 {max_retries} 次重试后仍然失败: {e}")


def call_llm_simple(prompt: str, temperature: float = 0.3) -> str:
    """
    简化的调用——不需要分别写 system/user prompt
    """
    return call_llm(
        system_prompt="你是一个有帮助的 AI 助手。",
        user_message=prompt,
        return_json=False,
        temperature=temperature,
    )


# ============================================================
# 测试代码
# ============================================================
if __name__ == "__main__":
    print("=" * 60)
    print("LLM 工具测试")
    print("=" * 60)

    if not OPENAI_API_KEY:
        print("请先在 .env 文件中设置 OPENAI_API_KEY")
        print("  1. 复制 .env.example 为 .env")
        print("  2. 编辑 .env，填入你的 API Key")
    else:
        print("测试 JSON 返回...")
        result = call_llm(
            system_prompt="你是一个 JSON 生成器，必须返回 JSON 格式。",
            user_message="返回一个包含 name 和 age 的示例 JSON",
        )
        print(f"返回类型: {type(result).__name__}")
        print(f"返回内容: {result}")
