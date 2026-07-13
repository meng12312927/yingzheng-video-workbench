"""
===========================================================================
ffmpeg.py — FFmpeg 视频处理工具
===========================================================================
功能：视频裁剪、拼接、字幕烧录、格式转换等底层操作

技术原理（大白话版）：
  FFmpeg 是世界上最强大的视频处理工具，几乎所有视频软件底层都用它。
  我们通过 Python 的 subprocess（子进程）来调用 FFmpeg 命令行。
  相当于 Python 是"指挥官"，FFmpeg 是"执行士兵"。

Python 知识点：
  1. subprocess.run() — 在 Python 里执行外部命令
     类似你在命令行敲 "ffmpeg -i video.mp4 ..."，但是用代码自动敲
  2. Path 对象 — pathlib 提供的跨平台路径处理
     Path("data") / "video.mp4" → Windows: data\video.mp4, Mac: data/video.mp4
  3. @staticmethod — 静态方法，不需要创建对象就能调用
     FFmpegTool.cut_segment(...) 而不是 FFmpegTool().cut_segment(...)

常用 FFmpeg 参数速查：
  -i          输入文件
  -ss         开始时间（seek start）
  -t          持续时长
  -to         结束时间
  -c:v        视频编码器
  -c:a        音频编码器
  -vf         视频滤镜（video filter），字幕烧录用这个
  -y          覆盖输出文件不询问
  -loglevel  日志级别（error=只显示错误）
===========================================================================
"""

import subprocess
import shutil
import json
import re
from pathlib import Path
from typing import Optional


class FFmpegTool:
    """
    FFmpeg 视频处理工具集

    所有方法都是静态方法，可以直接 FFmpegTool.cut_segment(...) 调用
    """

    # ============================================================
    # 内部工具方法
    # ============================================================

    @staticmethod
    def _find_ffmpeg() -> str:
        """
        找到 FFmpeg 的安装路径

        为什么需要这个函数？
        Windows 上 FFmpeg 安装后可能需要重启才能被系统找到。
        这个函数会尝试多种方式定位 FFmpeg，确保能调用成功。
        """
        # 方式1：用 shutil.which 在系统 PATH 里找
        ffmpeg_path = shutil.which("ffmpeg")
        if ffmpeg_path:
            return ffmpeg_path

        # 方式2：搜索常见安装位置
        common_paths = [
            Path("C:/Program Files/FFmpeg/bin/ffmpeg.exe"),
            Path("C:/ffmpeg/bin/ffmpeg.exe"),
            Path.home() / "ffmpeg" / "bin" / "ffmpeg.exe",
        ]
        # 如果通过 winget 安装的 Gyan.FFmpeg，路径在用户目录下
        winget_path = Path("C:/Users") / Path.home().name
        for p in winget_path.glob("**/ffmpeg.exe"):
            common_paths.append(p)

        for p in common_paths:
            if p.exists():
                return str(p)

        # 方式3：都找不到就返回 "ffmpeg"，
        # 让 subprocess 报错，用户能看到明确的错误信息
        return "ffmpeg"

    @staticmethod
    def _run(cmd: list[str], description: str = "") -> bool:
        """
        执行 FFmpeg 命令（内部方法，外部不用关心）

        参数：
          cmd: 命令参数列表，如 ["ffmpeg", "-i", "input.mp4", ...]
          description: 对这一步的描述，用于打印日志

        返回值：True=成功, False=失败
        """
        if description:
            print(f"[FFmpeg] {description}...")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600,  # 10 分钟超时（视频处理可能很久）
            )
            if result.returncode == 0:
                if description:
                    print(f"[FFmpeg] {description} — 完成")
                return True
            else:
                print(f"[FFmpeg] 错误: {result.stderr[-500:]}")  # 只打印最后 500 字符
                return False
        except FileNotFoundError:
            print(f"[FFmpeg] 找不到 FFmpeg！请先安装 FFmpeg")
            print(f"         下载地址: https://ffmpeg.org/download.html")
            return False
        except subprocess.TimeoutExpired:
            print(f"[FFmpeg] 超时！视频处理超过了 10 分钟")
            return False

    # ============================================================
    # 核心功能
    # ============================================================

    @staticmethod
    def get_video_info(video_path: str) -> Optional[dict]:
        """
        获取视频的基本信息

        返回值示例：
        {
            "duration": 125.5,    # 时长（秒）
            "width": 1920,        # 宽度
            "height": 1080,       # 高度
            "fps": 30.0,          # 帧率
            "codec": "h264",      # 视频编码
            "size_mb": 45.2,      # 文件大小（MB）
        }
        """
        ffmpeg = FFmpegTool._find_ffmpeg()
        cmd = [
            ffmpeg, "-i", str(video_path),
            "-f", "null", "-",  # 不实际转码，只读取信息
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            # FFmpeg 把视频信息输出到 stderr（一个历史遗留的设计）
            info_text = result.stderr

            # 用正则表达式从输出中提取信息
            info = {}

            # 提取时长: Duration: 00:02:05.50
            duration_match = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", info_text)
            if duration_match:
                h, m, s = duration_match.groups()
                info["duration"] = int(h) * 3600 + int(m) * 60 + float(s)

            # 提取分辨率: 1920x1080
            res_match = re.search(r"(\d{2,4})x(\d{2,4})", info_text)
            if res_match:
                info["width"] = int(res_match.group(1))
                info["height"] = int(res_match.group(2))

            # 提取帧率
            fps_match = re.search(r"(\d+\.?\d*) fps", info_text)
            if fps_match:
                info["fps"] = float(fps_match.group(1))

            # 提取编码器
            codec_match = re.search(r"Video: (\w+)", info_text)
            if codec_match:
                info["codec"] = codec_match.group(1)

            # 文件大小
            file_path = Path(video_path)
            if file_path.exists():
                info["size_mb"] = round(file_path.stat().st_size / (1024 * 1024), 2)

            return info if info else None

        except Exception as e:
            print(f"[FFmpeg] 获取视频信息失败: {e}")
            return None

    @staticmethod
    def cut_segment(
        video_path: str,
        start_time: float,
        end_time: float,
        output_path: str,
        re_encode: bool = True,
    ) -> bool:
        """
        从视频中裁剪出一个片段

        参数：
          video_path: 原视频路径
          start_time: 开始时间（秒），如 10.5 表示从第 10.5 秒开始
          end_time: 结束时间（秒），如 30.0 表示到第 30 秒结束
          output_path: 输出文件路径
          re_encode: True=重新编码（慢但精确）, False=快速切割（可能不精确）

        FFmpeg 命令拆解：
          ffmpeg -ss 10.5 -i input.mp4 -to 30.0 -c:v h264_nvenc -c:a copy output.mp4
                │       │           │           │                     │
                │       │           │           │                     └─ 输出
                │       │           │           └─ 编码器（h264_nvenc=GPU硬件编码）
                │       │           └─ 结束时间（也可以用 -t 20.5 表示持续时长）
                │       └─ 输入文件
                └─ 开始位置（放 -i 前面可以快速跳转）
        """
        duration = end_time - start_time
        ffmpeg = FFmpegTool._find_ffmpeg()

        cmd = [
            ffmpeg,
            "-ss", str(start_time),     # 跳转到开始时间
            "-i", str(video_path),       # 输入文件
            "-t", str(duration),         # 持续时长
        ]

        if re_encode:
            # 用 GPU 硬件编码（快！）
            cmd += [
                "-c:v", "h264_nvenc",    # NVIDIA GPU 编码
                "-preset", "p1",         # 最快预设
                "-c:a", "aac",           # 音频用 AAC
            ]
        else:
            # 不重新编码，直接复制（超快，但切割位置可能偏移几帧）
            cmd += ["-c", "copy"]

        cmd += ["-y", str(output_path)]  # -y = 覆盖已存在的文件

        return FFmpegTool._run(cmd, f"裁剪 {start_time:.1f}s - {end_time:.1f}s")

    @staticmethod
    def concat_segments(
        segment_paths: list[str],
        output_path: str,
        add_transition: bool = False,
    ) -> bool:
        """
        把多个视频片段拼接成一个完整视频

        参数：
          segment_paths: 片段路径列表（按顺序排列）
          output_path: 输出文件路径
          add_transition: 是否在片段间加淡入淡出（实验性）

        拼接有两种方式：
          方式A：用 concat demuxer（需要先写一个文件列表）
          方式B：用 concat protocol（只适用于相同格式的视频）
          我们用方式A，最通用。

        Python 知识点：
          with open(...) as f: 是 Python 的"上下文管理器"
          它保证文件用完后自动关闭，即使中间出错了也会关
        """
        if not segment_paths:
            print("[FFmpeg] 没有要拼接的片段")
            return False

        ffmpeg = FFmpegTool._find_ffmpeg()

        # Step 1: 创建一个临时文本文件，列出所有要拼接的片段
        concat_list_path = Path(output_path).parent / "_concat_list.txt"
        with open(concat_list_path, "w", encoding="utf-8") as f:
            for seg_path in segment_paths:
                # concat 文件格式：
                # file 'path/to/segment1.mp4'
                # file 'path/to/segment2.mp4'
                # 注意：路径中的反斜杠要替换成正斜杠
                safe_path = str(Path(seg_path).resolve()).replace("\\", "/")
                f.write(f"file '{safe_path}'\n")

        # Step 2: 用 FFmpeg concat demuxer 拼接
        cmd = [
            ffmpeg,
            "-f", "concat",              # concat 格式
            "-safe", "0",                # 允许绝对路径
            "-i", str(concat_list_path), # 片段列表文件
            "-c:v", "h264_nvenc",        # GPU 编码
            "-preset", "p1",
            "-c:a", "aac",
            "-y", str(output_path),
        ]

        success = FFmpegTool._run(cmd, f"拼接 {len(segment_paths)} 个片段")

        # Step 3: 清理临时文件
        if concat_list_path.exists():
            concat_list_path.unlink()

        return success

    @staticmethod
    def burn_subtitles(
        video_path: str,
        subtitle_path: str,
        output_path: str,
        font_size: int = 18,
        font_color: str = "white",
    ) -> bool:
        """
        把字幕文件烧录到视频上（硬字幕，永远显示）

        参数：
          video_path: 原视频
          subtitle_path: SRT 字幕文件路径
          output_path: 输出视频
          font_size: 字体大小
          font_color: 字体颜色

        FFmpeg 字幕滤镜说明：
          subtitles=subtitle.srt:force_style='FontSize=18'
          这个滤镜会读取 SRT 文件，按时间轴把文字画在视频上

        注意：Windows 上需要确保字幕文件路径中的反斜杠正确处理
        """
        ffmpeg = FFmpegTool._find_ffmpeg()

        # Windows 上 FFmpeg 字幕滤镜的路径处理：
        # 反斜杠 \ 需要转义，正斜杠 / 可以直接用
        safe_sub_path = str(Path(subtitle_path).resolve()).replace("\\", "\\\\").replace(":", "\\:")

        # 构建字幕样式
        style = f"FontSize={font_size},FontColor={font_color},Outline=1,OutlineColor=black"

        # 字幕滤镜
        subtitle_filter = f"subtitles='{safe_sub_path}':force_style='{style}'"

        cmd = [
            ffmpeg,
            "-i", str(video_path),
            "-vf", subtitle_filter,     # video filter = 视频滤镜
            "-c:v", "h264_nvenc",
            "-preset", "p1",
            "-c:a", "copy",             # 音频直接复制，不重编码
            "-y", str(output_path),
        ]

        return FFmpegTool._run(cmd, "烧录字幕")

    @staticmethod
    def extract_audio(video_path: str, output_path: str, format: str = "wav") -> bool:
        """
        从视频中提取音频

        参数：
          video_path: 视频文件
          output_path: 输出音频路径
          format: 音频格式（wav 是无损的，适合给 Whisper 用）
        """
        ffmpeg = FFmpegTool._find_ffmpeg()

        cmd = [
            ffmpeg,
            "-i", str(video_path),
            "-vn",                      # 不要视频流
            "-acodec", "pcm_s16le" if format == "wav" else "aac",
            "-ar", "16000",             # 采样率 16kHz（Whisper 推荐）
            "-ac", "1",                 # 单声道（Whisper 不需要立体声）
            "-y", str(output_path),
        ]

        return FFmpegTool._run(cmd, "提取音频")

    @staticmethod
    def create_silence(duration: float, output_path: str, width: int = 1920, height: int = 1080) -> bool:
        """
        生成一段黑色静默视频（用于片头/片尾/转场）
        """
        ffmpeg = FFmpegTool._find_ffmpeg()

        cmd = [
            ffmpeg,
            "-f", "lavfi",
            "-i", f"color=c=black:s={width}x{height}:d={duration}:r=30",
            "-f", "lavfi",
            "-i", f"anullsrc=r=44100:cl=mono",
            "-shortest",
            "-c:v", "h264_nvenc",
            "-preset", "p1",
            "-y", str(output_path),
        ]

        return FFmpegTool._run(cmd, f"生成 {duration}s 黑场")


# ============================================================
# 直接运行时的测试代码
# ============================================================
if __name__ == "__main__":
    print("=" * 60)
    print("FFmpeg 工具测试")
    print("=" * 60)

    tool = FFmpegTool()

    # 测试：找一个视频文件
    test_dir = Path("C:/Users/22307/video-agent-pipeline/data")
    videos = list(test_dir.glob("*.mp4")) + list(test_dir.glob("*.mov")) + list(test_dir.glob("*.avi"))

    if videos:
        test_video = videos[0]
        print(f"测试视频: {test_video}")

        # 1. 获取视频信息
        info = tool.get_video_info(str(test_video))
        if info:
            print(f"  时长: {info.get('duration', '?')}s")
            print(f"  分辨率: {info.get('width', '?')}x{info.get('height', '?')}")
            print(f"  大小: {info.get('size_mb', '?')}MB")

        # 2. 裁剪前 5 秒
        tool.cut_segment(
            str(test_video),
            0, 5,
            str(Path("C:/Users/22307/video-agent-pipeline/output/test_cut.mp4")),
        )
    else:
        print("请放一个测试视频到 data/ 文件夹，例如:")
        print("  C:\\Users\\22307\\video-agent-pipeline\\data\\test.mp4")
