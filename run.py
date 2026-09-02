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

    print("\n正在生成需求任务书...")
    print(f"  视频: {video_path}")
    print(f"  需求: {user_input}")

    orchestrator = VideoEditOrchestrator()
    try:
        compilation = orchestrator.create_requirement_draft(video_path, user_input)
    except Exception as error:
        print(f"\n❌ 生成任务书失败: {error}")
        return

    spec = compilation.spec
    if compilation.warnings:
        print("\n⚠️  本次需求解析需要人工核对：")
        for warning in compilation.warnings:
            print(f"  - {warning}")
    print(f"\n需求任务书 v{spec.version}：")
    print(f"  用途: {spec.purpose}")
    print(f"  受众: {spec.audience}")
    print(f"  目标时长: {spec.target_duration:.0f}s ±{spec.duration_tolerance:.0f}s")
    for item in spec.requirements:
        print(f"  - [{item.priority}] {item.description}")
    print("\nAI 执行说明：")
    print(compilation.execution_brief.visible_instruction)
    clarification = ""
    if spec.open_questions:
        print("\n待确认问题：")
        for question in spec.open_questions:
            print(f"  - {question}")
        clarification = input("请统一补充说明: ").strip()
        if not clarification:
            print("\n❌ 未回答待确认问题，任务已停在需求审核阶段。")
            return
    approved = input("确认当前任务书并开始分析？(y/N): ").strip().lower()
    if approved not in {"y", "yes"}:
        print("\n任务已停在需求审核阶段，未启动素材分析。")
        return
    try:
        orchestrator.confirm_requirement_draft(
            orchestrator.status.requirement_gate.id,
            actor_id="cli-user",
            clarification_answers=clarification,
        )
        script = orchestrator.analyze_confirmed_requirement(video_path)
    except Exception as error:
        print(f"\n❌ 证据分析失败: {error}")
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
        report = orchestrator.status.delivery_report
        if report:
            print(f"\n逐项交付报告（{report.status}）：")
            for item in report.results:
                print(f"  - [{item.status}/{item.method}] {item.summary}")
            if report.status == "needs_resolution":
                decision = input(
                    "验收存在异常。输入例外原因并批准交付，或直接回车保留任务等待修订: "
                ).strip()
                if decision:
                    try:
                        approved_report = orchestrator.resolve_delivery(
                            actor_id="cli-user",
                            exception_reason=decision,
                        )
                        print(f"✅ 已记录例外并批准交付: {approved_report.id}")
                    except ValueError as error:
                        print(f"❌ 例外批准失败: {error}")
                else:
                    print("任务保留在 awaiting_delivery_resolution，可在电脑审核页继续修订。")
    else:
        print(f"\n❌ 剪辑失败: {result.errors}")


def run_ui():
    """启动 Web 界面"""
    print("启动 Web 界面...")

    try:
        from src.ui.app import create_ui
        demo = create_ui()
        from src.config import GRADIO_SERVER_NAME, GRADIO_SERVER_PORT
        demo.launch(
            server_name=GRADIO_SERVER_NAME,
            server_port=GRADIO_SERVER_PORT,
            share=False,
        )
    except ImportError as error:
        print(f"缺少 UI 依赖: {error}")
        print("请先执行: .venv/bin/pip install -r requirements.txt")


def run_mobile_review():
    """启动手机轻量审核服务；局域网使用时把 base URL 配成电脑 IP。"""
    import uvicorn

    from src.config import MOBILE_REVIEW_PORT

    print(f"启动移动审核页: http://0.0.0.0:{MOBILE_REVIEW_PORT}")
    uvicorn.run(
        "src.mobile_review_api:app",
        host="0.0.0.0",
        port=MOBILE_REVIEW_PORT,
        reload=False,
    )


if __name__ == "__main__":
    if "--mobile-review" in sys.argv:
        run_mobile_review()
    elif "--ui" in sys.argv:
        run_ui()
    else:
        run_cli()
