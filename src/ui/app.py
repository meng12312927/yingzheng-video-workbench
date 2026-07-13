"""
===========================================================================
app.py — Gradio Web 界面
===========================================================================
功能：给系统套一个漂亮的 Web 界面，用户可以在浏览器里操作

技术原理（大白话版）：
  Gradio 是一个专门给 AI 应用做界面的 Python 库。
  你只需要写 Python 函数，Gradio 自动把它变成网页上的按钮和输入框。

用法：
  python src/ui/app.py
  然后浏览器打开 http://localhost:7860
===========================================================================
"""

import sys
from pathlib import Path

# 确保项目根目录在 Python 搜索路径中
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import gradio as gr
from src.orchestrator import VideoEditOrchestrator


# 全局编排器（只加载一次，所有请求共享）
_orchestrator = None


def get_orchestrator():
    """延迟初始化：第一次调用时才加载 Whiser 模型"""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = VideoEditOrchestrator()
    return _orchestrator


def process_video(video_path, user_input, progress=gr.Progress()):
    """
    处理视频的主函数

    这个函数会被 Gradio 自动绑定到"开始剪辑"按钮上。
    progress 参数是 Gradio 提供的进度条对象。
    """
    if video_path is None:
        return "请先上传视频文件", None, ""

    if not user_input or not user_input.strip():
        user_input = "帮我把这个视频剪成3分钟精彩集锦"

    progress(0.0, desc="启动中...")

    try:
        orch = get_orchestrator()

        # Step 1: 需求理解
        progress(0.1, desc="Agent 1/4: 正在理解你的需求...")
        requirement = orch.agent1.run(user_input)

        # Step 2: 内容分析
        progress(0.3, desc="Agent 2/4: 正在转录视频 (Whisper)...")
        analysis = orch.agent2.run(video_path, requirement)

        # Step 3: 脚本生成
        progress(0.6, desc="Agent 3/4: 正在生成剪辑脚本...")
        script = orch.agent3.run(analysis, requirement)

        # Step 4: 执行处理
        progress(0.8, desc="Agent 4/4: 正在处理视频 (FFmpeg)...")
        result = orch.agent4.run(script, video_path)

        progress(1.0, desc="完成！")

        # 构建返回信息
        summary = f"""
## 剪辑完成！

| 项目 | 详情 |
|------|------|
| **视频类型** | {requirement.video_type} |
| **剪辑风格** | {requirement.style} |
| **目标时长** | {requirement.target_duration//60}分{requirement.target_duration%60}秒 |
| **实际时长** | {result.output_duration:.0f}秒 |
| **高光片段** | {len(analysis.highlights)} 个 |
| **执行操作** | {result.operations_done} 成功 / {result.operations_failed} 失败 |
| **视频摘要** | {analysis.summary} |

### Top 高光片段
{chr(10).join([f'- [{h.importance:.0%}] {h.start:.0f}s: {h.reason}' for h in analysis.highlights[:5]])}

### 备注
{script.notes}
"""

        if result.errors:
            summary += f"\n\n### 警告\n{chr(10).join(['- ' + e for e in result.errors])}"

        return (
            summary,
            result.output_path if result.success else None,
            script.srt_subtitles if result.success else "",
        )

    except Exception as e:
        progress(1.0, desc="出错")
        return f"## 错误\n\n{str(e)}\n\n请检查：\n1. API Key 是否正确\n2. 视频文件是否完整\n3. FFmpeg 是否安装", None, ""


def create_ui():
    """
    创建 Gradio 界面
    """

    # 自定义 CSS（让界面好看一点）
    custom_css = """
    .gradio-container {
        max-width: 1000px !important;
        margin: auto !important;
    }
    .main-title {
        text-align: center;
        font-size: 2em;
        font-weight: bold;
        margin-bottom: 0.5em;
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
    }
    """

    with gr.Blocks(
        title="多Agent视频自动剪辑系统",
        css=custom_css,
        theme=gr.themes.Soft(),
    ) as demo:

        # 标题
        gr.Markdown(
            """
            <div class="main-title">🎬 多 Agent 协作视频自动剪辑系统</div>
            """
        )
        gr.Markdown(
            """
            上传视频，用自然语言描述你的需求，AI 自动完成剪辑。
            支持运动会、知识竞赛、会议讲座等场景。
            """
        )

        with gr.Row():
            with gr.Column(scale=1):
                # 视频上传
                video_input = gr.Video(
                    label="📤 上传视频",
                    sources=["upload"],
                )

                # 需求输入
                user_input = gr.Textbox(
                    label="✏️ 剪辑需求（自然语言描述）",
                    placeholder="例如：帮我把运动会视频剪成3分钟精彩集锦，重点要冲刺和颁奖的画面",
                    lines=3,
                )

                # 快捷需求按钮
                gr.Markdown("**快捷需求模板：**")
                with gr.Row():
                    gr.Examples(
                        examples=[
                            ["帮我把运动会视频剪成3分钟精彩集锦"],
                            ["把知识竞赛的精彩回答剪出来，5分钟以内"],
                            ["会议录像太长，剪一个3分钟的精华版"],
                        ],
                        inputs=user_input,
                    )

                # 开始按钮
                submit_btn = gr.Button(
                    "🚀 开始自动剪辑",
                    variant="primary",
                    size="lg",
                )

            with gr.Column(scale=1):
                # 结果摘要
                result_text = gr.Markdown(
                    label="📋 处理结果",
                    value="等待上传视频...",
                )

                # 成品视频
                output_video = gr.Video(
                    label="🎥 成品视频",
                    autoplay=False,
                )

                # 字幕下载
                subtitle_output = gr.Textbox(
                    label="📝 SRT 字幕",
                    info="可以复制保存为 .srt 文件",
                    lines=5,
                    visible=False,
                )

        # 底部说明
        gr.Markdown("""
        ---
        ### 📖 使用说明

        1. **上传视频**：支持 mp4, mov, avi, mkv 等常见格式
        2. **描述需求**：用大白话告诉 AI 你想要什么样的成品
        3. **等待处理**：系统会依次调用 4 个 Agent 协作完成剪辑
        4. **下载成品**：完成后可以直接预览和下载

        ### 🔧 技术栈

        `Python` · `OpenAI GPT-4o-mini` · `Faster-Whisper` · `FFmpeg` · `Gradio` · `Multi-Agent Architecture`
        """)

        # 绑定事件
        submit_btn.click(
            fn=process_video,
            inputs=[video_input, user_input],
            outputs=[result_text, output_video, subtitle_output],
        )

    return demo


# ============================================================
# 启动入口
# ============================================================
if __name__ == "__main__":
    print("=" * 60)
    print("  多 Agent 视频自动剪辑系统 - Web 界面")
    print("=" * 60)
    print()
    print("  浏览器打开 http://localhost:7860")
    print("  按 Ctrl+C 停止服务")
    print()
    print("  首次启动会加载 Whisper 模型 (~1.5GB)，请耐心等待...")
    print("=" * 60)

    demo = create_ui()
    demo.launch(
        server_name="127.0.0.1",
        server_port=7860,
        share=False,  # 改成 True 可以生成公网链接
        show_error=True,
    )
