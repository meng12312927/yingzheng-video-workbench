"""
===========================================================================
orchestrator.py — Agent 编排器
===========================================================================
功能：串联 4 个 Agent，管理整个视频剪辑流水线的执行

技术原理（大白话版）：
  如果把 4 个 Agent 比作工厂流水线的 4 个工位：
  - Orchestrator 就是"生产调度员"——它不亲自干活，
    但决定了"谁在什么时候做什么，做完交给谁"。

  编排模式 vs 对话模式：
  - 对话模式：Agent A 直接跟 Agent B 聊天 → 混乱、难调试
  - 编排模式：所有 Agent 只跟 Orchestrator 交互 → 清晰、可控
    这是多 Agent 系统设计的"最佳实践"。

Python 知识点：
  1. 鸭子类型（Duck Typing）——不关心你是哪个类，只关心你有没有 run() 方法
  2. PipelineStatus ——用数据模型追踪状态，而不是用一堆变量
  3. 异常处理的"兜底"策略——每一步都可能失败，都要有应对
===========================================================================
"""

import time
import logging
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.agents.requirement_agent import RequirementAgent
from src.agents.analysis_agent import AnalysisAgent
from src.agents.script_agent import ScriptAgent
from src.agents.executor_agent import ExecutorAgent
from src.config import OUTPUT_DIR, WHISPER_MODEL_SIZE
from src.tools.ffmpeg import FFmpegTool
from src.models.schemas import (
    VideoRequirement,
    ContentAnalysis,
    EditScript,
    ExecutionResult,
    PipelineStatus,
)

logger = logging.getLogger("Orchestrator")


class VideoEditOrchestrator:
    """
    视频剪辑编排器——整个系统的"总指挥"

    用法（最简单的端到端流程）：
        orchestrator = VideoEditOrchestrator()
        result = orchestrator.run(
            video_path="data/my_video.mp4",
            user_input="帮我把运动会视频剪成3分钟精彩集锦",
        )
        print(f"成品: {result.output_path}")
    """

    def __init__(self):
        """初始化编排器和 4 个 Agent"""
        logger.info("=" * 50)
        logger.info("初始化 VideoEditOrchestrator")
        logger.info("=" * 50)

        # 创建 4 个 Agent 实例
        # 注意：Agent 2 加载 Whisper 模型需要一些时间
        self.agent1 = RequirementAgent()
        self.agent2 = AnalysisAgent(whisper_model_size=WHISPER_MODEL_SIZE)
        self.agent3 = ScriptAgent()
        self.agent4 = ExecutorAgent()

        self.status = PipelineStatus(step="init")
        self.task_dir: Path | None = None
        self.preview_paths: dict[str, str] = {}
        logger.info("4 个 Agent 初始化完成，就绪")

    def run(
        self,
        video_path: str,
        user_input: str,
        output_path: str = "",
    ) -> ExecutionResult:
        """
        运行完整的视频剪辑流水线

        参数：
          video_path: 原视频路径
          user_input: 用户的自然语言需求
          output_path: 输出路径（可选）

        返回值：
          ExecutionResult: 最终执行结果

        流程：
          Agent 1 (需求理解) → Agent 2 (内容分析) →
          Agent 3 (脚本生成) → Agent 4 (执行处理)
        """
        total_start = time.time()

        print("\n" + "=" * 60)
        print("  多 Agent 视频自动剪辑系统")
        print("=" * 60)
        print(f"  视频: {video_path}")
        print(f"  需求: {user_input[:50]}...")
        print("=" * 60 + "\n")

        try:
            script = self.prepare(video_path, user_input)
            self._print_requirement(self.status.requirement)
            self._print_analysis(self.status.analysis)
            self._print_script(script)
            result = self.render(script, video_path, output_path)
            self._print_result(result, total_start)
        except Exception as e:
            logger.error(f"流水线失败: {e}")
            return self._error_result(str(e))

        # ============================================
        # 完成
        # ============================================
        self.status.step = "done"
        self.status.completed_at = datetime.now().isoformat()
        return result

    def prepare(
        self,
        video_path: str,
        user_input: str,
        overrides: Optional[dict] = None,
        generate_previews: bool = False,
    ) -> EditScript:
        """生成可供用户确认的剪辑计划，不执行视频渲染。"""
        self.status = PipelineStatus(step="created", started_at=datetime.now().isoformat())
        self.task_dir = OUTPUT_DIR / "tasks" / uuid.uuid4().hex
        self.task_dir.mkdir(parents=True, exist_ok=False)
        self.preview_paths = {}
        media_info = FFmpegTool.get_video_info(video_path)
        if not media_info or not media_info.get("has_audio"):
            self._write_manifest("failed", error="视频预检失败：需要可解码的视频和音频流")
            raise ValueError("视频预检失败：需要可解码的视频和音频流")
        self._write_json("media_info.json", media_info)
        self._write_manifest("preflight_ok", video_path=str(Path(video_path).resolve()))
        self.status.step = "requirement"
        requirement = self.agent1.run(user_input)
        if overrides:
            updates = {
                key: value
                for key, value in overrides.items()
                if value is not None and value != "" and value != []
            }
            requirement = VideoRequirement.model_validate({**requirement.model_dump(), **updates})
        self._write_json("requirement.json", requirement.model_dump())
        self._write_manifest("requirement_ready")
        self.status.step = "analysis"
        analysis = self.agent2.run(video_path, requirement)
        self._write_json("analysis.json", analysis.model_dump())
        self._write_manifest("analyzed")
        self.status.step = "script"
        script = self.agent3.run(analysis, requirement)
        self.status.requirement = requirement
        self.status.analysis = analysis
        self.status.script = script
        self.status.step = "awaiting_confirmation"
        self._write_json("edit_plan.json", script.model_dump())
        if generate_previews:
            self.preview_paths = self._create_previews(video_path, script)
        self._write_manifest("awaiting_confirmation")
        return script

    def render(self, script: EditScript, video_path: str, output_path: str = "") -> ExecutionResult:
        """仅渲染已经确认的剪辑计划。"""
        if not output_path and self.task_dir:
            output_path = str(self.task_dir / "final.mp4")
        if self.task_dir and script.srt_subtitles:
            (self.task_dir / "subtitles.srt").write_text(script.srt_subtitles, encoding="utf-8")
        result = self.agent4.run(script, video_path, output_path)
        self.status.result = result
        self.status.step = "done" if result.success else "error"
        self.status.completed_at = datetime.now().isoformat()
        if self.task_dir:
            self._write_json("execution_result.json", result.model_dump())
            (self.task_dir / "render.log").write_text(result.log, encoding="utf-8")
            self._write_manifest("succeeded" if result.success else "failed", output_path=result.output_path)
        return result

    def confirm_and_render(
        self,
        script: EditScript,
        selected_orders: list[int],
        video_path: str,
        output_path: str = "",
        transition_duration: float = 0.0,
        subtitle_style: str = "classic",
        bgm_path: Optional[str] = None,
        bgm_volume: float = 0.15,
        intro_style: str = "none",
        outro_style: str = "none",
        title_text: str = "",
    ) -> ExecutionResult:
        """应用用户的片段选择后再渲染，防止未确认计划直接导出。"""
        if self.status.analysis is None:
            raise ValueError("没有可确认的分析结果，请先生成剪辑方案")
        confirmed = self.agent3.apply_selection(
            script,
            self.status.analysis,
            selected_orders,
            transition_duration=transition_duration,
            subtitle_style=subtitle_style,
            bgm_path=bgm_path,
            bgm_volume=bgm_volume,
            intro_style=intro_style,
            outro_style=outro_style,
            title_text=title_text,
        )
        if not confirmed.operations:
            raise ValueError("请至少保留一个片段")
        self.status.script = confirmed
        if self.task_dir:
            self._write_json("confirmed_edit_plan.json", confirmed.model_dump())
            self._write_manifest("rendering")
        return self.render(confirmed, video_path, output_path)

    def _create_previews(self, video_path: str, script: EditScript) -> dict[str, str]:
        """生成候选片段预览；失败不影响主剪辑流程。"""
        if not self.task_dir:
            return {}
        preview_dir = self.task_dir / "previews"
        preview_dir.mkdir(exist_ok=True)
        previews: dict[str, str] = {}
        for operation in script.operations:
            if operation.action != "cut" or operation.source_start is None or operation.source_end is None:
                continue
            preview_path = preview_dir / f"clip_{operation.order:02d}.mp4"
            if FFmpegTool.create_preview(video_path, operation.source_start, operation.source_end, str(preview_path)):
                previews[str(operation.order)] = str(preview_path.resolve())
        return previews

    def _write_json(self, filename: str, payload: dict) -> None:
        if self.task_dir:
            destination = self.task_dir / filename
            temporary = destination.with_suffix(destination.suffix + ".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(destination)

    def _write_manifest(self, state: str, **extra: str) -> None:
        if self.task_dir:
            self._write_json(
                "manifest.json",
                {
                    "task_id": self.task_dir.name,
                    "state": state,
                    "updated_at": datetime.now().isoformat(),
                    **extra,
                },
            )

    def run_interactive(self, video_path: str, user_input: str):
        """
        交互式运行——每一步完成后暂停，让用户确认

        适合：用户想看中间结果、手动调整参数
        不适合：全自动场景
        """
        # 实现略（以后可以加）
        pass

    # ============================================================
    # 私有方法：格式化输出
    # ============================================================

    def _print_requirement(self, req: VideoRequirement):
        """打印需求理解结果"""
        print(f"\n{'='*40}")
        print("  [Agent 1] 需求理解完成")
        print(f"  类型: {req.video_type} | 风格: {req.style}")
        print(f"  目标时长: {req.target_duration//60}分{req.target_duration%60}秒")
        print(f"  关键词: {', '.join(req.focus_keywords)}")
        print(f"  字幕: {'是' if req.need_subtitles else '否'} | BGM: {'是' if req.need_bgm else '否'}")

    def _print_analysis(self, analysis: ContentAnalysis):
        """打印内容分析结果"""
        print(f"\n{'='*40}")
        print("  [Agent 2] 内容分析完成")
        print(f"  视频时长: {analysis.video_duration/60:.1f} 分钟")
        print(f"  转录片段: {len(analysis.transcript)} 个")
        print(f"  高光片段: {len(analysis.highlights)} 个")
        print(f"  摘要: {analysis.summary[:80]}...")
        if analysis.highlights:
            print("  Top 3 高光:")
            for i, h in enumerate(analysis.highlights[:3]):
                print(f"    {i+1}. [{h.importance:.2f}] {h.start:.0f}s-{h.end:.0f}s | {h.reason[:30]}")

    def _print_script(self, script: EditScript):
        """打印剪辑脚本摘要"""
        cuts = [op for op in script.operations if op.action == "cut"]
        transitions = [op for op in script.operations if op.action == "transition"]
        print(f"\n{'='*40}")
        print("  [Agent 3] 剪辑脚本生成完成")
        print(f"  标题: {script.title}")
        print(f"  操作: {len(script.operations)} 个 ({len(cuts)} 个裁剪, {len(transitions)} 个转场)")
        print(f"  预估时长: {script.estimated_duration:.0f} 秒")
        if script.notes:
            print(f"  备注: {script.notes[:80]}")

    def _print_result(self, result: ExecutionResult, total_start: float):
        """打印最终结果"""
        total_time = time.time() - total_start
        print(f"\n{'='*60}")
        if result.success:
            print("  剪辑完成！")
            print(f"  成品: {result.output_path}")
            print(f"  时长: {result.output_duration:.0f} 秒")
            print(f"  总耗时: {total_time:.0f} 秒 ({total_time/60:.1f} 分钟)")
            if result.errors:
                print(f"  警告: {len(result.errors)} 个非致命错误")
        else:
            print("  剪辑失败！")
            for err in result.errors:
                print(f"  错误: {err}")
        print(f"{'='*60}\n")

    def _error_result(self, message: str) -> ExecutionResult:
        """生成错误结果"""
        self.status.step = "error"
        self.status.error_message = message
        if self.task_dir:
            self._write_manifest("failed", error=message)
        return ExecutionResult(
            success=False,
            output_path="",
            output_duration=0,
            operations_done=0,
            operations_failed=0,
            errors=[message],
        )


# ============================================================
# 便捷函数：一行调用
# ============================================================

def auto_edit(video_path: str, user_input: str, output_path: str = "") -> ExecutionResult:
    """
    一行代码完成视频自动剪辑

    用法：
        from src.orchestrator import auto_edit
        result = auto_edit("my_video.mp4", "帮我剪成3分钟精华版")

    这是最简洁的调用方式，适合在其他脚本中集成。
    """
    orch = VideoEditOrchestrator()
    return orch.run(video_path, user_input, output_path)


# ============================================================
# 测试代码
# ============================================================
if __name__ == "__main__":
    print("=" * 60)
    print("Orchestrator 测试：完整流程")
    print("=" * 60)

    import sys

    # 检查命令行参数
    if len(sys.argv) < 2:
        print("用法: python orchestrator.py <视频路径> [需求描述]")
        print("示例: python orchestrator.py data/test.mp4 '剪成3分钟精华版'")
        sys.exit(0)

    video_path = sys.argv[1]
    user_input = sys.argv[2] if len(sys.argv) > 2 else "帮我把这个视频剪成3分钟精彩集锦"

    if not Path(video_path).exists():
        print(f"视频文件不存在: {video_path}")
        sys.exit(1)

    orch = VideoEditOrchestrator()
    result = orch.run(video_path, user_input)

    if result.success:
        print(f"\n成品视频: {result.output_path}")
    else:
        print(f"\n剪辑失败: {result.errors}")
