"""
===========================================================================
executor_agent.py — Agent 4: 执行处理
===========================================================================
功能：读取剪辑脚本，调用 FFmpeg 完成实际的视频处理

技术原理（大白话版）：
  Agent 4 是"动手干活的那个"。它不调用 LLM（不需要思考），
  纯粹按照 Agent 3 给的"施工图纸"一步步执行 FFmpeg 命令。

流程：
  1. 解析 EditScript 中的每个 EditOperation
  2. 对每个 "cut" 操作 → 调用 FFmpeg 裁剪片段
  3. 对所有裁剪后的片段 → 按顺序拼接
  4. 烧录字幕 → 输出成品

Python 知识点：
  1. tempfile 模块——创建临时文件（用完自动删除）
  2. shutil 模块——高级文件操作
  3. Path 的 mkdir(parents=True) ——自动创建父目录
===========================================================================
"""

import tempfile
import shutil
from pathlib import Path
from datetime import datetime

from src.agents.base import BaseAgent
from src.tools.ffmpeg import FFmpegTool
from src.models.schemas import EditScript, EditOperation, ExecutionResult


class ExecutorAgent(BaseAgent):
    """
    Agent 4：视频执行智能体

    用法：
        agent = ExecutorAgent()
        result = agent.run(edit_script, "input.mp4", "output.mp4")
        if result.success:
            print(f"成品: {result.output_path}")
    """

    def __init__(self):
        super().__init__("ExecutorAgent")

    def run(
        self,
        script: EditScript,
        video_path: str,
        output_path: str = "",
    ) -> ExecutionResult:
        """
        执行剪辑脚本

        参数：
          script: Agent 3 生成的剪辑脚本
          video_path: 原视频路径
          output_path: 输出路径（留空则自动生成）

        返回值：
          ExecutionResult: 执行结果
        """
        self.log(f"开始执行剪辑，共 {len(script.operations)} 个操作")
        self._start_timer()

        errors = []
        done = 0
        failed = 0
        log_lines = []

        # 1. 准备输出路径
        source_info = FFmpegTool.get_video_info(video_path)
        if not source_info:
            return self._fail(["无法读取输入视频，请确认文件可被 FFmpeg 解码"], log_lines)
        if not output_path:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = str(
                Path("output") / f"edited_{timestamp}.mp4"
            )
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        # 2. 创建临时工作目录
        temp_dir = Path(tempfile.mkdtemp(prefix="video_edit_"))
        self.log(f"临时目录: {temp_dir}")
        log_lines.append(f"临时目录: {temp_dir}")

        try:
            # ============================================
            # 第一步：执行所有 cut 操作（裁剪片段）
            # ============================================
            cut_ops = [op for op in script.operations if op.action == "cut"]
            self.log(f"Step 1: 裁剪 {len(cut_ops)} 个片段...")
            log_lines.append(f"裁剪操作数: {len(cut_ops)}")

            segment_paths = []
            for op in sorted(cut_ops, key=lambda x: x.order):
                if op.source_start is None or op.source_end is None:
                    self.log(f"跳过操作 #{op.order}: 缺少时间信息", level="warning")
                    failed += 1
                    continue
                if not (0 <= op.source_start < op.source_end <= source_info["duration"]):
                    errors.append(f"裁剪片段 #{op.order} 时间范围超出原视频")
                    failed += 1
                    continue

                seg_path = str(temp_dir / f"seg_{op.order:03d}.mp4")
                success = FFmpegTool.cut_segment(
                    video_path,
                    op.source_start,
                    op.source_end,
                    seg_path,
                )

                if success:
                    segment_paths.append(seg_path)
                    done += 1
                else:
                    errors.append(f"裁剪片段 #{op.order} 失败")
                    failed += 1

            if not segment_paths:
                errors.append("没有成功裁剪任何片段！")
                return self._fail(errors, log_lines)

            # ============================================
            # 第二步：拼接所有片段
            # ============================================
            self.log(f"Step 2: 拼接 {len(segment_paths)} 个片段...")
            log_lines.append(f"拼接片段数: {len(segment_paths)}")

            merged_path = str(temp_dir / "merged.mp4")
            success = FFmpegTool.concat_segments(
                segment_paths, merged_path, transition_duration=script.transition_duration
            )

            if not success:
                errors.append("拼接失败！")
                return self._fail(errors, log_lines)

            done += 1

            # ============================================
            # 第三步：按用户选择添加片头和片尾
            # ============================================
            decorated_source = merged_path
            card_paths = []
            card_options = (
                ("片头", script.intro_style, script.title_text or script.title or "精彩回顾"),
                ("片尾", script.outro_style, "感谢观看"),
            )
            for label, style, text in card_options:
                if style == "none":
                    continue
                card_path = str(temp_dir / ("intro.mp4" if label == "片头" else "outro.mp4"))
                success = FFmpegTool.create_title_card(
                    text,
                    duration=2.0,
                    output_path=card_path,
                    width=int(source_info["width"]),
                    height=int(source_info["height"]),
                    fade=style == "fade_black",
                )
                if not success:
                    errors.append(f"{label}生成失败")
                    return self._fail(errors, log_lines)
                card_paths.append((label, card_path))
                done += 1

            if card_paths:
                ordered_paths = [path for label, path in card_paths if label == "片头"]
                ordered_paths += [merged_path]
                ordered_paths += [path for label, path in card_paths if label == "片尾"]
                decorated_path = str(temp_dir / "with_intro_outro.mp4")
                if not FFmpegTool.concat_segments(ordered_paths, decorated_path):
                    errors.append("片头片尾拼接失败")
                    return self._fail(errors, log_lines)
                decorated_source = decorated_path
                done += 1

            # ============================================
            # 第四步：混入用户提供的背景音乐
            # ============================================
            render_source = decorated_source
            if script.bgm_path:
                self.log("Step 3: 混入背景音乐...")
                log_lines.append(f"背景音乐: {Path(script.bgm_path).name}")
                mixed_path = str(temp_dir / "with_bgm.mp4")
                success = FFmpegTool.mix_background_music(
                    decorated_source, script.bgm_path, mixed_path, script.bgm_volume
                )
                if success:
                    render_source = mixed_path
                    done += 1
                else:
                    errors.append("背景音乐混入失败，已保留原始人声版本")
                    failed += 1

            # ============================================
            # 第五步：烧录字幕
            # ============================================
            if script.srt_subtitles:
                self.log("Step 3: 烧录字幕...")
                log_lines.append("烧录字幕")

                # 把 SRT 内容写入临时文件
                srt_path = temp_dir / "subtitles.srt"
                srt_path.write_text(script.srt_subtitles, encoding="utf-8")

                success = FFmpegTool.burn_subtitles(
                    render_source,
                    str(srt_path),
                    output_path,
                    style_name=script.subtitle_style,
                )

                if success:
                    done += 1
                else:
                    # 字幕烧录失败，直接用合并后的视频作为输出
                    self.log("字幕烧录失败，输出无字幕版本", level="warning")
                    errors.append("字幕烧录失败，已输出无字幕版本")
                    failed += 1
                    shutil.copy2(render_source, output_path)
            else:
                # 没有字幕，直接复制
                shutil.copy2(render_source, output_path)

            # ============================================
            # 第四步：验证输出
            # ============================================
            valid, validation_error, actual_duration = FFmpegTool.validate_output(
                output_path, script.estimated_duration
            )
            if not valid:
                errors.append(validation_error)
                return self._fail(errors, log_lines)
            output_file = Path(output_path)
            output_size_mb = output_file.stat().st_size / (1024 * 1024)
            self.log(f"输出文件: {output_path} ({output_size_mb:.1f}MB)")
            log_lines.append(f"输出文件大小: {output_size_mb:.1f}MB")

            self._end_timer()

            result = ExecutionResult(
                success=True,
                output_path=str(output_file.resolve()),
                output_duration=actual_duration,
                operations_done=done,
                operations_failed=failed,
                errors=errors,
                log="\n".join(log_lines),
            )

            self.log(f"执行完成！成品: {output_path}")
            self.log(f"实际时长: {actual_duration:.0f}s, 成功: {done}, 失败: {failed}")
            return result

        except Exception as e:
            errors.append(f"执行异常: {str(e)}")
            self.log(f"执行异常: {e}", level="error")
            return self._fail(errors, log_lines)

        finally:
            # 清理临时文件
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
                self.log("已清理临时目录")
            except Exception:
                pass

    def _fail(self, errors: list[str], log_lines: list[str]) -> ExecutionResult:
        """返回一个失败结果"""
        return ExecutionResult(
            success=False,
            output_path="",
            output_duration=0,
            operations_done=0,
            operations_failed=0,
            errors=errors,
            log="\n".join(log_lines),
        )


# ============================================================
# 测试代码
# ============================================================
if __name__ == "__main__":
    print("=" * 60)
    print("Agent 4 测试：执行处理")
    print("=" * 60)

    from pathlib import Path

    test_video = Path("data/test.mp4")
    if test_video.exists():
        # 创建一个简单的测试脚本
        from src.models.schemas import EditScript, EditOperation

        script = EditScript(
            title="测试",
            estimated_duration=10.0,
            operations=[
                EditOperation(order=1, action="cut", source_start=0, source_end=5, note="前5秒"),
                EditOperation(order=2, action="cut", source_start=10, source_end=15, note="第10-15秒"),
            ],
        )

        agent = ExecutorAgent()
        result = agent.run(script, str(test_video))
        print(f"\n成功: {result.success}")
        print(f"输出: {result.output_path}")
        print(f"时长: {result.output_duration:.1f}s")
        print(f"成功/失败: {result.operations_done}/{result.operations_failed}")
    else:
        print(f"请先放一个测试视频到: {test_video}")
