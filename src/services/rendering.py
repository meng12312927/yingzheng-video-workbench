"""可替换渲染协议及当前 FFmpeg 后端。"""

from __future__ import annotations

from typing import Protocol

from src.agents.executor_agent import ExecutorAgent
from src.models.schemas import EditScript, ExecutionResult


class RenderBackend(Protocol):
    def render(self, script: EditScript, video_path: str, output_path: str) -> ExecutionResult:
        ...


class FFmpegRenderBackend:
    """保持现有 ExecutorAgent 行为，但让业务层不再直接依赖 Agent。"""

    def __init__(self, executor: ExecutorAgent | None = None):
        self.executor = executor or ExecutorAgent()

    def render(self, script: EditScript, video_path: str, output_path: str) -> ExecutionResult:
        return self.executor.run(script, video_path, output_path)

