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
    import shutil

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
    if not shutil.which("ffmpeg"):
        issues.append("未找到 FFmpeg！请先安装 FFmpeg。")

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

    print(f"\n启动剪辑流水线...")
    print(f"  视频: {video_path}")
    print(f"  需求: {user_input}")

    orchestrator = VideoEditOrchestrator()
    result = orchestrator.run(video_path, user_input)

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
        demo.launch(share=False)  # share=True 可生成公网链接
    except ImportError:
        print("UI 模块尚未完成！将在后续课程中实现。")
        print("现在请使用命令行模式: python run.py <视频路径> <需求>")


if __name__ == "__main__":
    if "--ui" in sys.argv:
        run_ui()
    else:
        run_cli()
