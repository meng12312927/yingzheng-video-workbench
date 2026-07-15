"""
===========================================================================
run.py — 一键启动入口
===========================================================================

用法：
  命令行模式:
    python run.py data/my_video.mp4 "帮我把运动会视频剪成3分钟"

  Web 界面模式:
    python run.py --ui

  交互模式:
    python run.py
    （然后按提示输入）
===========================================================================
"""

import sys
from pathlib import Path

# 确保项目根目录在 Python 搜索路径中
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))


def check_environment():
    """检查运行环境是否就绪"""
    from src.tools.ffmpeg import FFmpegTool

    issues = []

    # 1. 检查 .env 文件
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        issues.append(
            "未找到 .env 文件！\n"
            "  请复制 .env.example 为 .env，然后填入你的 OpenAI API Key。\n"
            "  如果没有 API Key，去 https://platform.openai.com 注册。"
        )

    # 2. 检查 FFmpeg
    ffmpeg_path = FFmpegTool._find_ffmpeg()
    if ffmpeg_path == "ffmpeg":
        issues.append("未找到 FFmpeg！请先安装 FFmpeg（macOS：brew install ffmpeg）。")

    # 3. 检查 data 目录
    data_dir = PROJECT_ROOT / "data"
    if not list(data_dir.glob("*.mp4")) and not list(data_dir.glob("*.mov")):
        issues.append(
            f"data 目录下没有视频文件！\n"
            f"  请放一个测试视频到: {data_dir}"
        )

    if issues:
        print("=" * 60)
        print("  环境检查发现问题：")
        print("=" * 60)
        for i, issue in enumerate(issues):
            print(f"\n  [{i+1}] {issue}")
        print()
        return False

    return True


def run_cli():
    """命令行模式"""
    if not check_environment():
        return

    from src.orchestrator import VideoEditOrchestrator

    # 获取输入
    if len(sys.argv) >= 3:
        video_path = sys.argv[1]
        user_input = sys.argv[2]
    elif len(sys.argv) == 2:
        video_path = sys.argv[1]
        user_input = input("请输入剪辑需求: ")
    else:
        # 交互模式
        data_dir = PROJECT_ROOT / "data"
        videos = list(data_dir.glob("*.mp4")) + list(data_dir.glob("*.mov"))
        if not videos:
            print("data 目录下没有视频，请先放入视频文件！")
            return

        print("\n可用的视频文件：")
        for i, v in enumerate(videos):
            size_mb = v.stat().st_size / (1024 * 1024)
            print(f"  [{i+1}] {v.name} ({size_mb:.1f}MB)")

        choice = input(f"\n选择视频 (1-{len(videos)}): ").strip()
        try:
            video_path = str(videos[int(choice) - 1])
        except (ValueError, IndexError):
            print("无效选择")
            return

        user_input = input("请输入剪辑需求 (如: 剪成3分钟精彩集锦): ").strip()
        if not user_input:
            user_input = "帮我把这个视频剪成3分钟精彩集锦"

    print("\n正在生成剪辑方案...")
    print(f"  视频: {video_path}")
    print(f"  需求: {user_input}")

    orchestrator = VideoEditOrchestrator()
    try:
        script = orchestrator.prepare(video_path, user_input)
    except Exception as error:
        print(f"\n❌ 生成方案失败: {error}")
        return

    if not script.operations:
        print("\n❌ 未找到可导出的候选片段，请调整需求或检查音频质量。")
        return

    print("\n候选片段：")
    for operation in script.operations:
        print(
            f"  [{operation.order}] {operation.source_start:.1f}s - {operation.source_end:.1f}s"
            f"  {operation.note or ''}"
        )
    choice = input("保留片段编号（逗号分隔，直接回车=全部保留）: ").strip()
    try:
        selected_orders = (
            [int(value.strip()) for value in choice.split(",") if value.strip()]
            if choice
            else [operation.order for operation in script.operations]
        )
        result = orchestrator.confirm_and_render(script, selected_orders, video_path)
    except (ValueError, EOFError) as error:
        print(f"\n❌ 导出失败: {error}")
        return

    if result.success:
        print(f"\n✅ 剪辑完成！成品: {result.output_path}")
    else:
        print(f"\n❌ 剪辑失败: {result.errors}")


def run_ui():
    """启动 Web 界面"""
    print("启动 Web 界面...")

    try:
        from src.ui.app import create_ui
        demo = create_ui()
        from src.config import GRADIO_SERVER_PORT
        demo.launch(server_name="127.0.0.1", server_port=GRADIO_SERVER_PORT, share=False)
    except ImportError as error:
        print(f"缺少 UI 依赖: {error}")
        print("请先执行: .venv/bin/pip install -r requirements.txt")


if __name__ == "__main__":
    if "--ui" in sys.argv:
        run_ui()
    else:
        run_cli()
