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
  2. Path 对象 — pathlib 提供的路径处理
     Path("data") / "video.mp4" → data/video.mp4
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
from src.config import VIDEO_ENCODER


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

        这个函数会优先从 PATH 和 Homebrew 的常用位置定位 FFmpeg。
        """
        # 方式1：用 shutil.which 在系统 PATH 里找
        ffmpeg_path = shutil.which("ffmpeg")
        if ffmpeg_path:
            return ffmpeg_path

        # 方式2：使用 imageio_ffmpeg 自带的 FFmpeg（跨平台，不需要额外安装）
        try:
            import imageio_ffmpeg
            bundled = imageio_ffmpeg.get_ffmpeg_exe()
            if Path(bundled).exists():
                return bundled
        except (ImportError, Exception):
            pass

        # 方式3：搜索常见安装位置
        common_paths = [
            Path("/opt/homebrew/bin/ffmpeg"),  # Apple Silicon Homebrew
            Path("/usr/local/bin/ffmpeg"),     # Intel Mac Homebrew
        ]
        for p in common_paths:
            if p.exists():
                return str(p)

        # 方式4：都找不到就返回 "ffmpeg"，
        # 让 subprocess 报错，用户能看到明确的错误信息
        return "ffmpeg"

    @staticmethod
    def _video_encode_args() -> list[str]:
        """返回适合 macOS 的视频编码参数。"""
        if VIDEO_ENCODER == "h264_videotoolbox":
            return ["-c:v", VIDEO_ENCODER, "-q:v", "65"]
        if VIDEO_ENCODER == "libx264":
            return ["-c:v", VIDEO_ENCODER, "-preset", "medium", "-crf", "20"]
        return ["-c:v", VIDEO_ENCODER]

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
            print("[FFmpeg] 找不到 FFmpeg！请先安装 FFmpeg")
            print("         下载地址: https://ffmpeg.org/download.html")
            return False
        except subprocess.TimeoutExpired:
            print("[FFmpeg] 超时！视频处理超过了 10 分钟")
            return False

    @staticmethod
    def _run_with_encoder_fallback(cmd: list[str], description: str) -> bool:
        """VideoToolbox 不可用时自动以 libx264 重试，保证 macOS 上可交付。"""
        if FFmpegTool._run(cmd, description):
            return True
        if VIDEO_ENCODER != "h264_videotoolbox":
            return False

        fallback: list[str] = []
        index = 0
        while index < len(cmd):
            if cmd[index:index + 2] == ["-c:v", "h264_videotoolbox"]:
                fallback.extend(["-c:v", "libx264", "-preset", "medium", "-crf", "20"])
                index += 2
            elif cmd[index:index + 2] == ["-q:v", "65"]:
                index += 2
            else:
                fallback.append(cmd[index])
                index += 1
        return FFmpegTool._run(fallback, f"{description}（VideoToolbox 不可用，改用 libx264）")

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
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            bundled_ffmpeg = Path(FFmpegTool._find_ffmpeg())
            sibling_probe = bundled_ffmpeg.with_name("ffprobe")
            if sibling_probe.exists():
                ffprobe = str(sibling_probe)
        if not ffprobe:
            return FFmpegTool._get_video_info_with_ffmpeg(video_path)
        cmd = [
            ffprobe,
            "-v", "error",
            "-show_format",
            "-show_streams",
            "-of", "json",
            str(video_path),
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                return None
            probe = json.loads(result.stdout)
            video_stream = next((s for s in probe.get("streams", []) if s.get("codec_type") == "video"), None)
            audio_stream = next((s for s in probe.get("streams", []) if s.get("codec_type") == "audio"), None)
            if not video_stream:
                return None
            info = {
                "duration": float(probe.get("format", {}).get("duration", 0)),
                "width": int(video_stream.get("width", 0)),
                "height": int(video_stream.get("height", 0)),
                "codec": video_stream.get("codec_name", ""),
                "has_audio": audio_stream is not None,
            }

            # 文件大小
            file_path = Path(video_path)
            if file_path.exists():
                info["size_mb"] = round(file_path.stat().st_size / (1024 * 1024), 2)

            return info if info else None

        except Exception as e:
            print(f"[FFmpeg] 获取视频信息失败: {e}")
            return None

    @staticmethod
    def _get_video_info_with_ffmpeg(video_path: str) -> Optional[dict]:
        """没有独立 ffprobe 时，用随 Faster-Whisper 安装的 ffmpeg 做兼容预检。"""
        try:
            result = subprocess.run(
                [FFmpegTool._find_ffmpeg(), "-hide_banner", "-i", str(video_path), "-f", "null", "-"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            text = result.stderr
            duration_match = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", text)
            video_match = re.search(r"Video: ([^,]+).*?(\d{2,5})x(\d{2,5})", text)
            if not duration_match or not video_match:
                return None
            hours, minutes, seconds = duration_match.groups()
            return {
                "duration": int(hours) * 3600 + int(minutes) * 60 + float(seconds),
                "width": int(video_match.group(2)),
                "height": int(video_match.group(3)),
                "codec": video_match.group(1).strip(),
                "has_audio": "Audio:" in text,
            }
        except Exception as error:
            print(f"[FFmpeg] 兼容预检失败: {error}")
            return None

    @staticmethod
    def validate_output(video_path: str, expected_duration: float) -> tuple[bool, str, float]:
        """校验导出文件存在、含视频流且时长与计划基本一致。"""
        path = Path(video_path)
        info = FFmpegTool.get_video_info(video_path)
        if not path.exists() or path.stat().st_size == 0 or not info:
            return False, "输出文件不存在、为空或无法解析", 0.0
        actual_duration = float(info.get("duration", 0))
        tolerance = max(2.0, expected_duration * 0.02)
        if abs(actual_duration - expected_duration) > tolerance:
            return False, f"输出时长 {actual_duration:.1f}s 与计划 {expected_duration:.1f}s 不符", actual_duration
        return True, "", actual_duration

    @staticmethod
    def concatenate_source_videos(video_paths: list[str], output_path: str) -> bool:
        """统一多段原素材的规格，并按传入顺序合成为一条可检索时间线。"""
        if len(video_paths) < 2:
            raise ValueError("合并原素材至少需要两个视频")

        infos = [FFmpegTool.get_video_info(path) for path in video_paths]
        if any(info is None for info in infos):
            print("[FFmpeg] 有原素材无法读取")
            return False

        first_info = infos[0] or {}
        width = max(2, int(first_info.get("width", 1280)) // 2 * 2)
        height = max(2, int(first_info.get("height", 720)) // 2 * 2)
        # 防止 4K 素材让 MVP 合并阶段占用过多资源，同时保留第一段素材的横竖方向。
        scale = min(1.0, 1920 / width, 1920 / height)
        width = max(2, int(width * scale) // 2 * 2)
        height = max(2, int(height * scale) // 2 * 2)

        destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        cmd = [FFmpegTool._find_ffmpeg(), "-hide_banner", "-loglevel", "error"]
        for path in video_paths:
            cmd.extend(["-i", str(path)])

        filters: list[str] = []
        concat_inputs: list[str] = []
        for index, info in enumerate(infos):
            assert info is not None
            duration = max(0.01, float(info.get("duration", 0)))
            filters.append(
                f"[{index}:v:0]"
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
                "setsar=1,fps=30,format=yuv420p,setpts=PTS-STARTPTS"
                f"[source_v{index}]"
            )
            if info.get("has_audio"):
                filters.append(
                    f"[{index}:a:0]aresample=48000,"
                    "aformat=sample_fmts=fltp:channel_layouts=stereo,"
                    f"atrim=duration={duration:.6f},asetpts=PTS-STARTPTS[source_a{index}]"
                )
            else:
                filters.append(
                    "anullsrc=channel_layout=stereo:sample_rate=48000,"
                    f"atrim=duration={duration:.6f},asetpts=PTS-STARTPTS[source_a{index}]"
                )
            concat_inputs.extend([f"[source_v{index}]", f"[source_a{index}]"])

        filters.append(
            "".join(concat_inputs)
            + f"concat=n={len(video_paths)}:v=1:a=1[merged_v][merged_a]"
        )
        cmd.extend([
            "-filter_complex", ";".join(filters),
            "-map", "[merged_v]",
            "-map", "[merged_a]",
            *FFmpegTool._video_encode_args(),
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",
            "-y", str(destination),
        ])
        return FFmpegTool._run_with_encoder_fallback(
            cmd,
            f"按上传顺序合并 {len(video_paths)} 段原素材",
        )

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
          ffmpeg -ss 10.5 -i input.mp4 -t 20.5 -c:v h264_videotoolbox -c:a aac output.mp4
                │       │           │           │                     │
                │       │           │           │                     └─ 输出
                │       │           │           └─ 编码器（macOS 默认 h264_videotoolbox）
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
            cmd += FFmpegTool._video_encode_args() + ["-c:a", "aac"]
        else:
            # 不重新编码，直接复制（超快，但切割位置可能偏移几帧）
            cmd += ["-c", "copy"]

        cmd += ["-y", str(output_path)]  # -y = 覆盖已存在的文件

        return FFmpegTool._run_with_encoder_fallback(cmd, f"裁剪 {start_time:.1f}s - {end_time:.1f}s")

    @staticmethod
    def concat_segments(
        segment_paths: list[str],
        output_path: str,
        transition_duration: float = 0.0,
    ) -> bool:
        """
        把多个视频片段拼接成一个完整视频

        参数：
          segment_paths: 片段路径列表（按顺序排列）
          output_path: 输出文件路径
          transition_duration: 相邻片段的淡转场时长；0 表示硬切

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

        if transition_duration > 0 and len(segment_paths) > 1:
            return FFmpegTool._concat_with_fades(segment_paths, output_path, transition_duration)

        # Step 1: 创建一个临时文本文件，列出所有要拼接的片段
        concat_list_path = Path(output_path).parent / "_concat_list.txt"
        with open(concat_list_path, "w", encoding="utf-8") as f:
            for seg_path in segment_paths:
                # concat 文件格式：
                # file 'path/to/segment1.mp4'
                # file 'path/to/segment2.mp4'
                safe_path = str(Path(seg_path).resolve())
                f.write(f"file '{safe_path}'\n")

        # Step 2: 用 FFmpeg concat demuxer 拼接
        cmd = [
            ffmpeg,
            "-f", "concat",              # concat 格式
            "-safe", "0",                # 允许绝对路径
            "-i", str(concat_list_path), # 片段列表文件
            *FFmpegTool._video_encode_args(),
            "-c:a", "aac",
            "-y", str(output_path),
        ]

        success = FFmpegTool._run_with_encoder_fallback(cmd, f"拼接 {len(segment_paths)} 个片段")

        # Step 3: 清理临时文件
        if concat_list_path.exists():
            concat_list_path.unlink()

        return success

    @staticmethod
    def _concat_with_fades(segment_paths: list[str], output_path: str, duration: float) -> bool:
        """以 xfade/acrossfade 拼接，减少硬切造成的突兀感。"""
        segment_infos = [FFmpegTool.get_video_info(path) for path in segment_paths]
        if any(info is None for info in segment_infos):
            return False
        if any(float(info["duration"]) <= duration for info in segment_infos):
            print("[FFmpeg] 片段太短，无法应用淡转场")
            return False

        ffmpeg = FFmpegTool._find_ffmpeg()
        cmd = [ffmpeg]
        for path in segment_paths:
            cmd += ["-i", str(path)]

        filters: list[str] = []
        video_label = "0:v"
        audio_label = "0:a"
        timeline_duration = float(segment_infos[0]["duration"])
        for index, info in enumerate(segment_infos[1:], start=1):
            output_video = f"v{index}"
            output_audio = f"a{index}"
            offset = max(0.0, timeline_duration - duration)
            filters.append(
                f"[{video_label}][{index}:v]xfade=transition=fade:duration={duration}:offset={offset}[{output_video}]"
            )
            filters.append(f"[{audio_label}][{index}:a]acrossfade=d={duration}[{output_audio}]")
            video_label = output_video
            audio_label = output_audio
            timeline_duration += float(info["duration"]) - duration

        cmd += [
            "-filter_complex", ";".join(filters),
            "-map", f"[{video_label}]",
            "-map", f"[{audio_label}]",
            *FFmpegTool._video_encode_args(),
            "-c:a", "aac",
            "-y", str(output_path),
        ]
        return FFmpegTool._run_with_encoder_fallback(cmd, f"淡转场拼接 {len(segment_paths)} 个片段")

    @staticmethod
    def mix_background_music(video_path: str, music_path: str, output_path: str, volume: float = 0.15) -> bool:
        """循环用户提供的 BGM，并以较低音量混入原始人声。"""
        if not Path(music_path).exists():
            print("[FFmpeg] 背景音乐文件不存在")
            return False
        ffmpeg = FFmpegTool._find_ffmpeg()
        cmd = [
            ffmpeg,
            "-i", str(video_path),
            "-stream_loop", "-1", "-i", str(music_path),
            "-filter_complex", f"[1:a]volume={volume}[bgm];[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=2[mix]",
            "-map", "0:v:0",
            "-map", "[mix]",
            *FFmpegTool._video_encode_args(),
            "-c:a", "aac",
            "-shortest",
            "-y", str(output_path),
        ]
        return FFmpegTool._run_with_encoder_fallback(cmd, "混入背景音乐")

    @staticmethod
    def create_preview(video_path: str, start_time: float, end_time: float, output_path: str) -> bool:
        """生成适合 UI 播放的低分辨率候选片段预览。"""
        ffmpeg = FFmpegTool._find_ffmpeg()
        cmd = [
            ffmpeg, "-ss", str(start_time), "-i", str(video_path), "-t", str(end_time - start_time),
            "-vf", "scale=-2:360", "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
            "-c:a", "aac", "-y", str(output_path),
        ]
        return FFmpegTool._run(cmd, f"生成预览 {start_time:.1f}s - {end_time:.1f}s")

    @staticmethod
    def create_title_card(
        text: str,
        duration: float,
        output_path: str,
        width: int,
        height: int,
        fade: bool = False,
    ) -> bool:
        """生成带静音音轨的黑底文字片头或片尾，确保可与主视频拼接。"""
        ffmpeg = FFmpegTool._find_ffmpeg()
        safe_text = text.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
        font_size = max(26, min(64, width // 18))
        filter_parts = [
            f"drawtext=fontcolor=white:fontsize={font_size}:text='{safe_text}':x=(w-text_w)/2:y=(h-text_h)/2"
        ]
        if fade:
            fade_duration = min(0.45, duration / 3)
            filter_parts.append(f"fade=t=in:st=0:d={fade_duration}")
            filter_parts.append(f"fade=t=out:st={max(0, duration - fade_duration)}:d={fade_duration}")
        cmd = [
            ffmpeg,
            "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:d={duration}:r=30",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-vf", ",".join(filter_parts),
            "-shortest",
            *FFmpegTool._video_encode_args(),
            "-c:a", "aac",
            "-y", str(output_path),
        ]
        return FFmpegTool._run_with_encoder_fallback(cmd, "生成片头/片尾")

    @staticmethod
    def burn_subtitles(
        video_path: str,
        subtitle_path: str,
        output_path: str,
        font_size: int = 18,
        font_color: str = "white",
        style_name: str = "classic",
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

        macOS 路径通常可直接使用；冒号需要为 FFmpeg filter 转义。
        """
        ffmpeg = FFmpegTool._find_ffmpeg()

        safe_sub_path = str(Path(subtitle_path).resolve()).replace(":", "\\:")

        # libass 样式。使用通用字体名，让 macOS 按中文字体回退。
        styles = {
            "classic": f"FontSize={font_size},FontColor={font_color},Outline=2,OutlineColor=black,Shadow=1,Alignment=2,MarginV=32",
            "clean": "FontSize=20,FontColor=&H00FFFFFF,Outline=1,OutlineColor=&H66000000,Shadow=0,Alignment=2,MarginV=36",
            "highlight": "FontSize=24,FontColor=&H0000FFFF,Outline=2,OutlineColor=&H00000000,Shadow=1,Bold=1,Alignment=2,MarginV=42",
        }
        style = styles.get(style_name, styles["classic"])

        # 字幕滤镜
        subtitle_filter = f"subtitles='{safe_sub_path}':force_style='{style}'"

        cmd = [
            ffmpeg,
            "-i", str(video_path),
            "-vf", subtitle_filter,     # video filter = 视频滤镜
            *FFmpegTool._video_encode_args(),
            "-c:a", "copy",             # 音频直接复制，不重编码
            "-y", str(output_path),
        ]

        return FFmpegTool._run_with_encoder_fallback(cmd, "烧录字幕")

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
            "-i", "anullsrc=r=44100:cl=mono",
            "-shortest",
            *FFmpegTool._video_encode_args(),
            "-y", str(output_path),
        ]

        return FFmpegTool._run_with_encoder_fallback(cmd, f"生成 {duration}s 黑场")


# ============================================================
# 直接运行时的测试代码
# ============================================================
if __name__ == "__main__":
    print("=" * 60)
    print("FFmpeg 工具测试")
    print("=" * 60)

    tool = FFmpegTool()

    # 测试：找一个视频文件
    test_dir = Path("data")
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
            str(Path("output/test_cut.mp4")),
        )
    else:
        print("请放一个测试视频到 data/ 文件夹，例如:")
        print("  data/test.mp4")
