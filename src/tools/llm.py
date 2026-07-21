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

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable, Dict, List, Union
from openai import OpenAI
from src.config import (
    EMBEDDING_API_KEY,
    EMBEDDING_BASE_URL,
    EMBEDDING_MODEL,
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_MODEL,
    LLM_TIMEOUT_SECONDS,
)

# OpenAI SDK 同时用于 OpenAI 与 DeepSeek 等 OpenAI 兼容服务。
_client = OpenAI(
    api_key=LLM_API_KEY,
    base_url=LLM_BASE_URL,
    timeout=LLM_TIMEOUT_SECONDS,
)
_embedding_client = None
_call_context: ContextVar[dict | None] = ContextVar("model_call_context", default=None)


@contextmanager
def model_call_context(
    task_dir: Path,
    *,
    stage: str,
    prompt_template_version: str,
    input_spec_id: str | None = None,
    input_spec_version: int | None = None,
):
    """让现有 Agent 无需持有存储对象，也能写入任务级模型调用日志。"""
    token = _call_context.set(
        {
            "task_dir": Path(task_dir),
            "stage": stage,
            "prompt_template_version": prompt_template_version,
            "input_spec_id": input_spec_id,
            "input_spec_version": input_spec_version,
        }
    )
    try:
        yield
    finally:
        _call_context.reset(token)


def _record_call(
    *,
    started_at: float,
    model: str,
    usage=None,
    tool_names: List[str] | None = None,
    error_code: str | None = None,
    fallback_used: bool = False,
) -> None:
    context = _call_context.get()
    if not context:
        return
    from src.models.schemas import ModelCallRecord

    record = ModelCallRecord(
        task_id=context["task_dir"].name,
        stage=context["stage"],
        provider="openai-compatible",
        model=model,
        prompt_template_version=context["prompt_template_version"],
        input_spec_id=context.get("input_spec_id"),
        input_spec_version=context.get("input_spec_version"),
        duration_ms=max(0, round((time.perf_counter() - started_at) * 1000)),
        input_tokens=getattr(usage, "prompt_tokens", None),
        output_tokens=getattr(usage, "completion_tokens", None),
        tool_names=list(dict.fromkeys(tool_names or [])),
        error_code=error_code,
        fallback_used=fallback_used,
    )
    destination = context["task_dir"] / "model_calls.jsonl"
    with destination.open("a", encoding="utf-8") as handle:
        handle.write(record.model_dump_json() + "\n")
        handle.flush()


def embed_texts(texts: List[str]) -> List[List[float]]:
    """调用独立 Embedding 服务；返回顺序与输入文本严格一致。"""
    global _embedding_client
    if not texts:
        return []
    started_at = time.perf_counter()
    if not EMBEDDING_API_KEY:
        _record_call(
            started_at=started_at,
            model=EMBEDDING_MODEL,
            error_code="embedding_not_configured",
            fallback_used=True,
        )
        raise RuntimeError("未配置 EMBEDDING_API_KEY")
    if _embedding_client is None:
        _embedding_client = OpenAI(
            api_key=EMBEDDING_API_KEY,
            base_url=EMBEDDING_BASE_URL,
            timeout=LLM_TIMEOUT_SECONDS,
        )
    try:
        response = _embedding_client.embeddings.create(model=EMBEDDING_MODEL, input=texts)
        ordered = sorted(response.data, key=lambda item: item.index)
        _record_call(
            started_at=started_at,
            model=EMBEDDING_MODEL,
            usage=getattr(response, "usage", None),
        )
        return [list(item.embedding) for item in ordered]
    except Exception as error:
        _record_call(
            started_at=started_at,
            model=EMBEDDING_MODEL,
            error_code=f"embedding_{type(error).__name__}",
            fallback_used=True,
        )
        raise


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

    started_at = time.perf_counter()
    last_response = None
    for attempt in range(max_retries):
        try:
            kwargs = {
                "model": LLM_MODEL,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }

            if return_json:
                kwargs["response_format"] = {"type": "json_object"}
                if "json" not in messages[0]["content"].lower():
                    messages[0]["content"] += "\n\n请始终以 JSON 格式返回。"

            response = _client.chat.completions.create(**kwargs)
            last_response = response
            content = response.choices[0].message.content

            if return_json:
                # 清理可能的 markdown 代码块标记
                content = content.strip()
                if content.startswith("```"):
                    content = content.split("\n", 1)[1]
                if content.endswith("```"):
                    content = content[:-3]
                result = json.loads(content.strip())
                _record_call(
                    started_at=started_at,
                    model=LLM_MODEL,
                    usage=getattr(response, "usage", None),
                )
                return result
            else:
                _record_call(
                    started_at=started_at,
                    model=LLM_MODEL,
                    usage=getattr(response, "usage", None),
                )
                return content.strip()

        except json.JSONDecodeError as e:
            print(f"[LLM] JSON 解析失败 (attempt {attempt + 1}): {e}")
            if attempt < max_retries - 1:
                time.sleep(1)
            else:
                print(f"[LLM] 重试 {max_retries} 次后仍然失败，返回空结果")
                _record_call(
                    started_at=started_at,
                    model=LLM_MODEL,
                    usage=getattr(last_response, "usage", None),
                    error_code="invalid_json",
                    fallback_used=True,
                )
                return {} if return_json else ""

        except Exception as e:
            print(f"[LLM] API 调用失败 (attempt {attempt + 1}): {e}")
            if attempt < max_retries - 1:
                time.sleep(2)
            else:
                _record_call(
                    started_at=started_at,
                    model=LLM_MODEL,
                    error_code=f"llm_{type(e).__name__}",
                    fallback_used=True,
                )
                raise RuntimeError(f"LLM 调用在 {max_retries} 次重试后仍然失败: {e}")


def call_llm_with_tools(
    system_prompt: str,
    user_message: str,
    tool_definitions: List[Dict[str, Any]],
    tool_handlers: Dict[str, Callable[..., Any]],
    temperature: float = 0.0,
    max_tokens: int = 4096,
    max_rounds: int = 4,
) -> dict:
    """执行受限的 OpenAI-compatible Function Calling 循环。

    工具 Schema 与处理函数均由调用方显式提供。模型只有通过这些工具取得
    的证据才可用于最终 JSON，不允许把全文资料作为自由上下文塞入 Prompt。
    """
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]
    started_at = time.perf_counter()
    called_tools: List[str] = []
    usage = None
    try:
        for _ in range(max_rounds):
            response = _client.chat.completions.create(
                model=LLM_MODEL,
                messages=messages,
                tools=tool_definitions,
                tool_choice="auto",
                temperature=temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )
            usage = getattr(response, "usage", None)
            message = response.choices[0].message
            tool_calls = message.tool_calls or []
            if not tool_calls:
                content = (message.content or "{}").strip()
                if content.startswith("```"):
                    content = content.split("\n", 1)[1]
                if content.endswith("```"):
                    content = content[:-3]
                result = json.loads(content.strip())
                _record_call(
                    started_at=started_at,
                    model=LLM_MODEL,
                    usage=usage,
                    tool_names=called_tools,
                )
                return result

            messages.append(message.model_dump(exclude_none=True))
            for tool_call in tool_calls:
                name = tool_call.function.name
                called_tools.append(name)
                if name not in tool_handlers:
                    result: Any = {"error": f"unsupported_tool:{name}"}
                else:
                    try:
                        arguments = json.loads(tool_call.function.arguments or "{}")
                        result = tool_handlers[name](**arguments)
                    except Exception as error:
                        result = {"error": f"tool_error:{type(error).__name__}:{error}"}
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )
        raise RuntimeError("工具调用超过最大轮数，未得到结构化候选结果")
    except Exception as error:
        _record_call(
            started_at=started_at,
            model=LLM_MODEL,
            usage=usage,
            tool_names=called_tools,
            error_code=f"tool_calling_{type(error).__name__}",
            fallback_used=True,
        )
        raise


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

    if not LLM_API_KEY:
        print("请先在 .env 或 .env.deepseek 文件中设置 LLM_API_KEY")
    else:
        print("测试 JSON 返回...")
        result = call_llm(
            system_prompt="你是一个 JSON 生成器，必须返回 JSON 格式。",
            user_message="返回一个包含 name 和 age 的示例 JSON",
        )
        print(f"返回类型: {type(result).__name__}")
        print(f"返回内容: {result}")
