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
from datetime import datetime
from pathlib import Path

from src.agents.requirement_agent import RequirementAgent
from src.agents.analysis_agent import AnalysisAgent
from src.agents.script_agent import ScriptAgent
from src.agents.executor_agent import ExecutorAgent
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
        self.agent2 = AnalysisAgent(whisper_model_size="large-v3")
        self.agent3 = ScriptAgent()
        self.agent4 = ExecutorAgent()

        self.status = PipelineStatus(step="init")
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
        self.status.started_at = datetime.now().isoformat()

        print("\n" + "=" * 60)
        print("  多 Agent 视频自动剪辑系统")
        print("=" * 60)
        print(f"  视频: {video_path}")
        print(f"  需求: {user_input[:50]}...")
        print("=" * 60 + "\n")

        # ============================================
        # Step 1: Agent 1 — 需求理解
        # ============================================
        self.status.step = "requirement"
        logger.info(">>> Step 1/4: Agent 1 - 需求理解")

        try:
            requirement = self.agent1.run(user_input)
            self.status.requirement = requirement
            self._print_requirement(requirement)
        except Exception as e:
            logger.error(f"Agent 1 失败: {e}")
            return self._error_result(f"需求理解失败: {e}")

        # ============================================
        # Step 2: Agent 2 — 内容分析
        # ============================================
        self.status.step = "analysis"
        logger.info(">>> Step 2/4: Agent 2 - 内容分析")

        try:
            analysis = self.agent2.run(video_path, requirement)
            self.status.analysis = analysis
            self._print_analysis(analysis)
        except Exception as e:
            logger.error(f"Agent 2 失败: {e}")
            return self._error_result(f"内容分析失败: {e}")

        # ============================================
        # Step 3: Agent 3 — 剪辑脚本生成
        # ============================================
        self.status.step = "script"
        logger.info(">>> Step 3/4: Agent 3 - 剪辑脚本生成")

        try:
            script = self.agent3.run(analysis, requirement)
            self.status.script = script
            self._print_script(script)
        except Exception as e:
            logger.error(f"Agent 3 失败: {e}")
            return self._error_result(f"脚本生成失败: {e}")

        # ============================================
        # Step 4: Agent 4 — 执行处理
        # ============================================
        self.status.step = "execution"
        logger.info(">>> Step 4/4: Agent 4 - 执行处理")

        try:
            result = self.agent4.run(script, video_path, output_path)
            self.status.result = result
            self._print_result(result, total_start)
        except Exception as e:
            logger.error(f"Agent 4 失败: {e}")
            return self._error_result(f"执行处理失败: {e}")

        # ============================================
        # 完成
        # ============================================
        self.status.step = "done"
        self.status.completed_at = datetime.now().isoformat()
        return result

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
        print(f"  [Agent 1] 需求理解完成")
        print(f"  类型: {req.video_type} | 风格: {req.style}")
        print(f"  目标时长: {req.target_duration//60}分{req.target_duration%60}秒")
        print(f"  关键词: {', '.join(req.focus_keywords)}")
        print(f"  字幕: {'是' if req.need_subtitles else '否'} | BGM: {'是' if req.need_bgm else '否'}")

    def _print_analysis(self, analysis: ContentAnalysis):
        """打印内容分析结果"""
        print(f"\n{'='*40}")
        print(f"  [Agent 2] 内容分析完成")
        print(f"  视频时长: {analysis.video_duration/60:.1f} 分钟")
        print(f"  转录片段: {len(analysis.transcript)} 个")
        print(f"  高光片段: {len(analysis.highlights)} 个")
        print(f"  摘要: {analysis.summary[:80]}...")
        if analysis.highlights:
            print(f"  Top 3 高光:")
            for i, h in enumerate(analysis.highlights[:3]):
                print(f"    {i+1}. [{h.importance:.2f}] {h.start:.0f}s-{h.end:.0f}s | {h.reason[:30]}")

    def _print_script(self, script: EditScript):
        """打印剪辑脚本摘要"""
        cuts = [op for op in script.operations if op.action == "cut"]
        transitions = [op for op in script.operations if op.action == "transition"]
        print(f"\n{'='*40}")
        print(f"  [Agent 3] 剪辑脚本生成完成")
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
            print(f"  剪辑完成！")
            print(f"  成品: {result.output_path}")
            print(f"  时长: {result.output_duration:.0f} 秒")
            print(f"  总耗时: {total_time:.0f} 秒 ({total_time/60:.1f} 分钟)")
            if result.errors:
                print(f"  警告: {len(result.errors)} 个非致命错误")
        else:
            print(f"  剪辑失败！")
            for err in result.errors:
                print(f"  错误: {err}")
        print(f"{'='*60}\n")

    def _error_result(self, message: str) -> ExecutionResult:
        """生成错误结果"""
        self.status.step = "error"
        self.status.error_message = message
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
