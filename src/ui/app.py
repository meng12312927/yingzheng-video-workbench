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

import gradio as gr  # noqa: E402
from src.orchestrator import VideoEditOrchestrator  # noqa: E402
from src.models.schemas import EditScript  # noqa: E402
from src.config import GRADIO_SERVER_PORT  # noqa: E402


# 全局编排器（只加载一次，所有请求共享）
_orchestrator = None


def get_orchestrator():
    """延迟初始化：第一次调用时才加载 Whiser 模型"""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = VideoEditOrchestrator()
    return _orchestrator


def prepare_video(video_path, user_input, target_duration, style, keywords, need_subtitles, progress=gr.Progress()):
    """生成剪辑方案，等待用户确认片段后才渲染。"""
    if video_path is None:
        return "请先上传视频文件", gr.update(), None, gr.update(), None, None, gr.update()

    if not user_input or not user_input.strip():
        user_input = "帮我把这个视频剪成3分钟精彩集锦"

    progress(0.0, desc="启动中...")

    try:
        orch = get_orchestrator()

        progress(0.1, desc="正在理解需求并预检视频...")
        style_map = {"正式": "formal", "燃向": "exciting", "温馨": "warm", "轻松": "funny"}
        overrides = {
            "target_duration": int(target_duration),
            "style": style_map.get(style),
            "focus_keywords": [word.strip() for word in keywords.split(",") if word.strip()],
            "need_subtitles": need_subtitles,
        }
        script = orch.prepare(video_path, user_input, overrides=overrides, generate_previews=True)
        analysis = orch.status.analysis
        requirement = orch.status.requirement
        progress(1.0, desc="请确认要导出的片段")

        # 构建返回信息
        summary = f"""
## 剪辑方案已生成

| 项目 | 详情 |
|------|------|
| **视频类型** | {requirement.video_type} |
| **剪辑风格** | {requirement.style} |
| **目标时长** | {requirement.target_duration//60}分{requirement.target_duration%60}秒 |
| **预计时长** | {script.estimated_duration:.0f}秒 |
| **高光片段** | {len(analysis.highlights)} 个 |
| **候选片段** | {len(script.operations)} 个 |
| **视频摘要** | {analysis.summary} |

请勾选要保留的片段，再点击“确认并导出”。

### 备注
{script.notes}
"""

        return (
            summary,
            gr.update(
                choices=[
                    (f"#{op.order}  {op.source_start:.1f}s–{op.source_end:.1f}s  {op.note or ''}", str(op.order))
                    for op in script.operations
                ],
                value=[str(op.order) for op in script.operations],
                visible=True,
            ),
            {"video_path": video_path, "script": script.model_dump(), "previews": orch.preview_paths},
            gr.update(
                choices=[
                    (f"#{op.order}  {op.source_start:.1f}s–{op.source_end:.1f}s  {op.note or ''}", str(op.order))
                    for op in script.operations
                    if str(op.order) in orch.preview_paths
                ],
                value=None,
                visible=bool(orch.preview_paths),
            ),
            None,
            None,
            gr.update(value="", visible=False),
        )

    except Exception as e:
        progress(1.0, desc="出错")
        return f"## 错误\n\n{str(e)}", gr.update(), None, gr.update(), None, None, gr.update()


def preview_candidate(plan_state, order):
    """在导出前播放一个候选片段。"""
    if not plan_state or not order:
        return None
    return plan_state.get("previews", {}).get(str(order))


def render_confirmed(
    plan_state,
    selected_orders,
    bgm_path,
    bgm_volume,
    transition_duration,
    subtitle_style,
    intro_style,
    outro_style,
    title_text,
    progress=gr.Progress(),
):
    """只渲染用户确认后的剪辑计划。"""
    if not plan_state:
        return "请先生成剪辑方案", None, gr.update()
    if not selected_orders:
        return "请至少保留一个片段", None, gr.update()
    try:
        progress(0.1, desc="正在应用你的片段选择...")
        orch = get_orchestrator()
        script = EditScript.model_validate(plan_state["script"])
        result = orch.confirm_and_render(
            script,
            [int(order) for order in selected_orders],
            plan_state["video_path"],
            transition_duration=float(transition_duration),
            subtitle_style=subtitle_style,
            bgm_path=bgm_path,
            bgm_volume=float(bgm_volume),
            intro_style=intro_style,
            outro_style=outro_style,
            title_text=title_text,
        )
        progress(1.0, desc="完成！")
        if not result.success:
            return f"## 导出失败\n\n{chr(10).join('- ' + error for error in result.errors)}", None, gr.update()
        return (
            f"## 剪辑完成\n\n实际时长：{result.output_duration:.1f} 秒。",
            result.output_path,
            gr.update(value=orch.status.script.srt_subtitles, visible=True),
        )
    except Exception as error:
        return f"## 导出失败\n\n{error}", None, gr.update()


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
                with gr.Row():
                    target_duration = gr.Slider(
                        minimum=30,
                        maximum=600,
                        value=300,
                        step=30,
                        label="目标时长（秒）",
                    )
                    style = gr.Dropdown(
                        choices=["自动判断", "正式", "燃向", "温馨", "轻松"],
                        value="自动判断",
                        label="剪辑风格",
                    )
                keywords = gr.Textbox(
                    label="重点关键词（可选，逗号分隔）",
                    placeholder="例如：冲刺，颁奖，领导讲话",
                )
                need_subtitles = gr.Checkbox(value=True, label="烧录中文字幕")
                bgm_input = gr.Audio(
                    label="背景音乐（可选，上传后将循环混入原声）",
                    sources=["upload"],
                    type="filepath",
                )
                bgm_volume = gr.Slider(
                    minimum=0.0,
                    maximum=0.5,
                    value=0.15,
                    step=0.05,
                    label="背景音乐音量",
                )
                transition_duration = gr.Slider(
                    minimum=0.0,
                    maximum=1.0,
                    value=0.35,
                    step=0.05,
                    label="片段淡转场时长（0=硬切）",
                )
                subtitle_style = gr.Radio(
                    choices=[("经典白字", "classic"), ("简洁白字", "clean"), ("醒目黄字", "highlight")],
                    value="classic",
                    label="字幕样式",
                )
                title_text = gr.Textbox(
                    label="片头标题（可选）",
                    placeholder="例如：2026 年夏季运动会精彩回顾",
                )
                with gr.Row():
                    intro_style = gr.Dropdown(
                        choices=[("不添加", "none"), ("标题片头（2 秒）", "title"), ("淡入黑场片头（2 秒）", "fade_black")],
                        value="none",
                        label="片头",
                    )
                    outro_style = gr.Dropdown(
                        choices=[("不添加", "none"), ("感谢观看片尾（2 秒）", "title"), ("淡出黑场片尾（2 秒）", "fade_black")],
                        value="none",
                        label="片尾",
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

                # 生成方案和确认导出
                submit_btn = gr.Button(
                    "🪄 生成剪辑方案",
                    variant="primary",
                    size="lg",
                )
                selected_cuts = gr.CheckboxGroup(label="确认保留的片段", visible=False)
                export_btn = gr.Button("🎬 确认并导出", variant="secondary")
                plan_state = gr.State(value=None)

            with gr.Column(scale=1):
                # 结果摘要
                result_text = gr.Markdown(
                    label="📋 处理结果",
                    value="等待上传视频...",
                )

                preview_selector = gr.Dropdown(label="预览候选片段", visible=False)
                preview_video = gr.Video(label="候选片段预览", autoplay=False)

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
            fn=prepare_video,
            inputs=[video_input, user_input, target_duration, style, keywords, need_subtitles],
            outputs=[
                result_text,
                selected_cuts,
                plan_state,
                preview_selector,
                preview_video,
                output_video,
                subtitle_output,
            ],
            api_name=False,
        )
        preview_selector.change(
            fn=preview_candidate,
            inputs=[plan_state, preview_selector],
            outputs=[preview_video],
            api_name=False,
        )
        export_btn.click(
            fn=render_confirmed,
            inputs=[
                plan_state,
                selected_cuts,
                bgm_input,
                bgm_volume,
                transition_duration,
                subtitle_style,
                intro_style,
                outro_style,
                title_text,
            ],
            outputs=[result_text, output_video, subtitle_output],
            api_name=False,
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
    print("  浏览器打开终端启动后显示的本地地址")
    print("  按 Ctrl+C 停止服务")
    print()
    print("  首次启动会加载 Whisper 模型 (~1.5GB)，请耐心等待...")
    print("=" * 60)

    demo = create_ui()
    demo.launch(
        server_name="127.0.0.1",
        server_port=GRADIO_SERVER_PORT,
        share=False,  # 改成 True 可以生成公网链接
        show_error=True,
    )
