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

import html
import sys
import threading
from pathlib import Path

# 确保项目根目录在 Python 搜索路径中
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import gradio as gr  # noqa: E402
from src.orchestrator import VideoEditOrchestrator  # noqa: E402
from src.models.schemas import PipelineStatus  # noqa: E402
from src.config import GRADIO_SERVER_PORT  # noqa: E402


# 运行时组件只加载一次；每个任务拥有独立 Orchestrator 状态和任务目录。
_runtime = None
_task_orchestrators = {}


def get_runtime():
    """延迟加载 Whisper 等昂贵组件。"""
    global _runtime
    if _runtime is None:
        _runtime = VideoEditOrchestrator()
    return _runtime


def _new_task_orchestrator():
    runtime = get_runtime()
    orchestrator = VideoEditOrchestrator.__new__(VideoEditOrchestrator)
    orchestrator.agent1 = runtime.agent1
    orchestrator.agent2 = runtime.agent2
    orchestrator.candidate_agent = runtime.candidate_agent
    orchestrator.style_agent = runtime.style_agent
    orchestrator.agent3 = runtime.agent3
    orchestrator.agent4 = runtime.agent4
    orchestrator.plan_service = runtime.plan_service
    orchestrator.clarification_service = runtime.clarification_service
    orchestrator.verification_engine = runtime.verification_engine
    orchestrator.render_backend = runtime.render_backend
    orchestrator.status = PipelineStatus(step="init")
    orchestrator.task_dir = None
    orchestrator.store = None
    orchestrator.state_machine = None
    orchestrator.preview_paths = {}
    orchestrator.video_path = None
    orchestrator._render_lock = threading.Lock()
    return orchestrator


def _get_task_orchestrator(state):
    task_id = state.get("task_id") if state else None
    if not task_id:
        raise ValueError("任务状态不存在")
    if task_id not in _task_orchestrators:
        _task_orchestrators[task_id] = VideoEditOrchestrator.open_task(task_id, get_runtime())
    return _task_orchestrators[task_id]


def _requirement_rows(spec):
    return [
        [item.id, item.priority, item.description, item.acceptance_rule or ""]
        for item in spec.requirements
    ]


def _timeline_rows(plan):
    return [
        [
            True,
            segment.order,
            segment.candidate_id,
            round(segment.source_start, 3),
            round(segment.source_end, 3),
            segment.subtitle_text or "",
            segment.title_text or "",
        ]
        for segment in plan.timeline_segments
    ]


def _truthy(value):
    return value is True or str(value).strip().lower() in {"true", "1", "yes", "是", "保留"}


def _delivery_markdown(report):
    if report is None:
        return ""
    rows = "\n".join(
        f"| `{item.status}` | `{item.method}` | {html.escape(item.summary)} |"
        for item in report.results
    )
    exception = ""
    if report.status == "approved_with_exceptions":
        exception = f"\n\n例外批准人：{html.escape(report.approved_by or '')}；原因：{html.escape(report.exception_reason or '')}"
    return f"""
## 逐项交付报告

状态：`{report.status}`；需求 v{report.requirement_spec_version}；方案 v{report.edit_plan_version}

| 结果 | 方法 | 说明 |
| --- | --- | --- |
{rows}
{exception}
"""


def _review_markdown(orch):
    spec = orch.status.requirement_spec
    analysis = orch.status.analysis
    plan = orch.status.edit_plan
    if not spec or not analysis or not plan:
        return "## 方案尚未生成"
    requirements = "\n".join(
        f"- `{item.priority}` `{item.id}` {html.escape(item.description)}"
        for item in spec.requirements
    ) or "- 无"
    candidate_rows = []
    requirement_by_id = {item.id: item.description for item in spec.requirements}
    for candidate in analysis.candidate_clips:
        citations = "<br>".join(
            f"`{citation.evidence_id}` {html.escape(citation.quote)}"
            for citation in candidate.citations
        ) or "无（不会进入时间线）"
        matched = "<br>".join(
            html.escape(requirement_by_id.get(item_id, item_id))
            for item_id in candidate.matched_requirement_ids
        ) or "无"
        risk = "、".join(candidate.risk_flags) or "无"
        candidate_rows.append(
            f"| `{candidate.id}` | {candidate.source_start:.1f}–{candidate.source_end:.1f}s | "
            f"{matched} | {citations} | {html.escape(candidate.selection_reason)} | {html.escape(risk)} |"
        )
    style_lines = "\n".join(
        f"- `{proposal.bundle_id}`：{html.escape(proposal.rationale)}"
        for proposal in orch.status.style_proposals
    ) or "- 无推荐，继续使用基础样式"
    covered_requirements = {
        requirement_id
        for candidate in analysis.candidate_clips
        for requirement_id in candidate.matched_requirement_ids
    }
    missing_must = [
        item for item in spec.requirements
        if item.priority == "must" and item.id not in covered_requirements
    ]
    coverage_notice = (
        "> **必须项缺口：** "
        + "；".join(f"`{item.id}` {html.escape(item.description)}" for item in missing_must)
        + "。批准前请修改选择或使用人工补片。"
        if missing_must else "> 必须项候选覆盖检查已通过。"
    )
    timeline_rows = "\n".join(
        f"| {segment.order} | `{segment.candidate_id}` | {segment.source_start:.1f}–{segment.source_end:.1f}s | "
        f"{segment.output_start:.1f}–{segment.output_end:.1f}s | "
        f"{html.escape(segment.subtitle_text or '跟随转录')} |"
        for segment in plan.timeline_segments
    )
    return f"""
## 合并方案审核页

任务书 v{spec.version}：{html.escape(spec.purpose)}；受众：{html.escape(spec.audience)}；目标 {spec.target_duration:.0f}±{spec.duration_tolerance:.0f} 秒。

### 需求
{requirements}

{coverage_notice}

### 可选风格建议
{style_lines}

### 证据与候选
| 候选 | 源时间 | 匹配需求 | 可校验原文 | 入选理由 | 风险 |
| --- | --- | --- | --- | --- | --- |
{chr(10).join(candidate_rows)}

### 粗剪时间线 v{plan.version}
| 顺序 | 候选 | 源时间 | 成片时间 | 字幕 |
| --- | --- | --- | --- | --- |
{timeline_rows}

预计成片时长：{plan.estimated_duration:.1f} 秒。可在左侧表格保留/删除、排序、修剪和修改字幕；导出按钮会先生成新版本、校验并批准，再开始渲染。
"""


def create_requirement_review(
    video_path, user_input, scenario, target_duration, style, keywords, need_subtitles,
    progress=gr.Progress(),
):
    """只生成需求任务书和执行说明，不启动 Whisper 或候选分析。"""
    if video_path is None:
        return "请先上传视频文件", None, gr.update(), gr.update(), gr.update(), gr.update(), "", "", gr.update()

    if not user_input or not user_input.strip():
        user_input = "帮我把这个视频剪成3分钟精彩集锦"

    progress(0.0, desc="启动中...")

    try:
        orch = _new_task_orchestrator()

        progress(0.1, desc="正在理解需求并预检视频...")
        style_map = {"正式": "formal", "燃向": "exciting", "温馨": "warm", "轻松": "funny"}
        overrides = {
            "target_duration": int(target_duration),
            "style": style_map.get(style),
            "focus_keywords": [word.strip() for word in keywords.split(",") if word.strip()],
            "need_subtitles": need_subtitles,
        }
        scenario_value = "school" if scenario == "学校活动" else "enterprise"
        compilation = orch.create_requirement_draft(
            video_path,
            user_input,
            scenario=scenario_value,
            overrides=overrides,
            submitted_by="local-user",
        )
        task_id = orch.task_dir.name
        _task_orchestrators[task_id] = orch
        spec = compilation.spec
        progress(1.0, desc="请审核需求任务书")
        warning_block = ""
        if compilation.warnings:
            warning_lines = "\n".join(f"- {html.escape(warning)}" for warning in compilation.warnings)
            warning_block = f"""
> **需要人工核对：AI 本次没有成功解析需求。**
>
{warning_lines}

请检查左侧目标时长、风格和重点内容；如需修改，请调整后重新生成任务书。
"""
        requirement_lines = "\n".join(
            f"- `{item.priority}` {html.escape(item.description)}"
            + (f"；验收：{html.escape(item.acceptance_rule)}" if item.acceptance_rule else "")
            for item in spec.requirements
        )
        questions = "\n".join(f"- {html.escape(question)}" for question in spec.open_questions) or "- 无"
        summary = f"""
## 需求任务书 v{spec.version}

{warning_block}

| 项目 | 详情 |
|------|------|
| **用途** | {html.escape(spec.purpose)} |
| **受众** | {html.escape(spec.audience)} |
| **目标时长** | {spec.target_duration:.0f} 秒（±{spec.duration_tolerance:.0f} 秒） |
| **风格** | {html.escape(spec.style)} |

### 内容要求
{requirement_lines}

### 待确认问题
{questions}

### 用户可见的 AI 执行说明
{html.escape(compilation.execution_brief.visible_instruction)}

审核后点击“确认任务书并分析素材”。在确认前不会启动转录或候选分析。
"""
        return (
            summary,
            {"task_id": task_id, "video_path": video_path, "gate_id": orch.status.requirement_gate.id},
            gr.update(visible=True),
            gr.update(visible=bool(spec.open_questions), value=""),
            gr.update(visible=bool(spec.open_questions), value=False),
            gr.update(value=_requirement_rows(spec), visible=True),
            spec.purpose,
            spec.audience,
            gr.update(visible=True),
        )

    except Exception as e:
        progress(1.0, desc="出错")
        return f"## 错误\n\n{html.escape(str(e))}", None, gr.update(), gr.update(), gr.update(), gr.update(), "", "", gr.update()


def revise_requirement_review(
    workflow_state, purpose, audience, target_duration, style, need_subtitles,
    requirement_rows,
):
    """将审核页修改保存成任务书新版本，旧 Gate 自动失效。"""
    if not workflow_state:
        return "请先生成需求任务书", workflow_state, gr.update(), purpose, audience, gr.update()
    try:
        orch = _get_task_orchestrator(workflow_state)
        existing = {item.id: item for item in orch.status.requirement_spec.requirements}
        items = []
        for row in requirement_rows or []:
            if not row or len(row) < 3 or not str(row[2]).strip():
                continue
            item_id = str(row[0]).strip()
            old = existing.get(item_id)
            items.append({
                "id": item_id if old else None,
                "category": old.category if old else "content",
                "priority": str(row[1]).strip(),
                "description": str(row[2]).strip(),
                "acceptance_rule": str(row[3]).strip() if len(row) > 3 and row[3] else None,
                "status": "confirmed",
            })
        style_map = {"正式": "formal", "燃向": "exciting", "温馨": "warm", "轻松": "funny"}
        revised = orch.revise_requirement_draft(
            updates={
                "purpose": str(purpose).strip(),
                "audience": str(audience).strip(),
                "target_duration": float(target_duration),
                "style": style_map.get(style, orch.status.requirement_spec.style),
                "need_subtitles": bool(need_subtitles),
            },
            requirements=items,
            actor_id="local-user",
        )
        workflow_state = {
            **workflow_state,
            "gate_id": orch.status.requirement_gate.id,
        }
        questions = "\n".join(f"- {html.escape(item)}" for item in revised.open_questions) or "- 无"
        return (
            f"## 需求任务书 v{revised.version} 已保存\n\n旧版本 Gate 已失效。\n\n### 待确认问题\n{questions}\n\n### 用户可见 AI 执行说明\n{html.escape(orch.status.execution_brief.visible_instruction)}",
            workflow_state,
            gr.update(visible=bool(revised.open_questions)),
            revised.purpose,
            revised.audience,
            gr.update(value=_requirement_rows(revised), visible=True),
        )
    except Exception as error:
        return f"## 任务书修改失败\n\n{html.escape(str(error))}", workflow_state, gr.update(), purpose, audience, gr.update()


def confirm_and_analyze(
    workflow_state,
    clarification_answers,
    allow_confirmed_override=False,
    progress=gr.Progress(),
):
    """确认当前任务书版本后，执行转录、受限证据检索和候选生成。"""
    if not workflow_state:
        return "请先创建需求任务书", gr.update(), None, gr.update(), None, gr.update(), gr.update()
    try:
        orch = _get_task_orchestrator(workflow_state)
        progress(0.05, desc="正在确认任务书...")
        orch.confirm_requirement_draft(
            workflow_state["gate_id"],
            actor_id="local-user",
            clarification_answers=clarification_answers or "",
            allow_confirmed_override=bool(allow_confirmed_override),
        )
        progress(0.15, desc="正在转录并检索证据...")
        script = orch.analyze_confirmed_requirement(workflow_state["video_path"], generate_previews=True)
        analysis = orch.status.analysis
        if not script.operations:
            return (
                "## 未生成可用候选\n\n没有候选通过证据引用校验。请补充更明确的必须内容或检查转录质量。",
                gr.update(value=[], visible=False),
                workflow_state,
                gr.update(choices=[], visible=False),
                None,
                gr.update(visible=False),
                gr.update(choices=[], visible=False),
            )
        progress(1.0, desc="请审核有证据的候选片段")
        labels = [
            (f"#{segment.order} {segment.source_start:.1f}s–{segment.source_end:.1f}s", str(segment.order))
            for segment in orch.status.edit_plan.timeline_segments
        ]
        state = {
            **workflow_state,
            "plan_id": orch.status.edit_plan.id,
            "plan_version": orch.status.edit_plan.version,
            "previews": orch.preview_paths,
        }
        style_choices = [
            (f"{proposal.bundle_id}｜{proposal.rationale}", proposal.bundle_id)
            for proposal in orch.status.style_proposals
        ]
        return (
            _review_markdown(orch),
            gr.update(value=_timeline_rows(orch.status.edit_plan), visible=True),
            state,
            gr.update(
                choices=[label for label in labels if label[1] in orch.preview_paths],
                value=None,
                visible=bool(orch.preview_paths),
            ),
            None,
            gr.update(visible=True),
            gr.update(choices=style_choices, value=None, visible=bool(style_choices)),
        )
    except Exception as error:
        progress(1.0, desc="出错")
        return f"## 分析失败\n\n{html.escape(str(error))}", gr.update(), workflow_state, gr.update(), None, gr.update(visible=False), gr.update()


def preview_candidate(plan_state, order):
    """在导出前播放一个候选片段。"""
    if not plan_state or not order:
        return None
    return plan_state.get("previews", {}).get(str(order))


def render_confirmed(
    plan_state,
    timeline_rows,
    manual_segment_rows,
    style_bundle_id,
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
        return "请先生成剪辑方案", None, gr.update(), gr.update(), gr.update()
    try:
        progress(0.1, desc="正在应用你的片段选择...")
        orch = _get_task_orchestrator(plan_state)
        selected_ids = []
        updates = {}
        for row in timeline_rows or []:
            if len(row) < 5 or not _truthy(row[0]):
                continue
            candidate_id = str(row[2]).strip()
            selected_ids.append(candidate_id)
            updates[candidate_id] = {
                "order": int(float(row[1])),
                "source_start": float(row[3]),
                "source_end": float(row[4]),
                "subtitle_text": str(row[5]).strip() or None if len(row) > 5 else None,
                "title_text": str(row[6]).strip() or None if len(row) > 6 else None,
            }
        delivery_updates = {
            "transition_duration": float(transition_duration),
            "subtitle_style": subtitle_style,
            "bgm_path": bgm_path,
            "bgm_volume": float(bgm_volume),
            "intro_style": intro_style,
            "outro_style": outro_style,
            "title_text": title_text,
            "style_bundle_id": style_bundle_id or None,
        }
        if style_bundle_id:
            delivery_updates.update(orch.style_agent.catalog.bundle_config(style_bundle_id))
        manual_segments = []
        for row in manual_segment_rows or []:
            if len(row) < 2 or row[0] in {None, ""} or row[1] in {None, ""}:
                continue
            manual_segments.append({
                "source_start": float(row[0]),
                "source_end": float(row[1]),
                "order": int(float(row[2])) if len(row) > 2 and row[2] not in {None, ""} else len(selected_ids) + len(manual_segments) + 1,
                "matched_requirement_ids": [
                    item.strip() for item in str(row[3] or "").replace("，", ",").split(",") if item.strip()
                ] if len(row) > 3 else [],
                "annotation": str(row[4]).strip() if len(row) > 4 and row[4] else "",
                "subtitle_text": str(row[5]).strip() or None if len(row) > 5 else None,
                "title_text": str(row[6]).strip() or None if len(row) > 6 else None,
            })
        if not selected_ids and not manual_segments:
            raise ValueError("请至少保留或人工添加一个片段")
        revised = orch.revise_edit_plan(
            selected_candidate_ids=selected_ids,
            segment_updates=updates,
            manual_segments=manual_segments,
            delivery_updates=delivery_updates,
            actor_id="local-user",
        )
        approved = orch.approve_current_plan("local-user")
        result = orch.render_approved_plan(approved, plan_state["video_path"])
        progress(1.0, desc="完成！")
        if not result.success:
            return f"## 导出失败\n\n{chr(10).join('- ' + error for error in result.errors)}", None, gr.update(), gr.update(), gr.update()
        needs_resolution = bool(
            orch.status.delivery_report and orch.status.delivery_report.status == "needs_resolution"
        )
        return (
            f"## 渲染完成\n\n实际时长：{result.output_duration:.1f} 秒。\n\n{_delivery_markdown(orch.status.delivery_report)}",
            result.output_path,
            gr.update(value=revised.execution_script.srt_subtitles, visible=True),
            gr.update(visible=needs_resolution, value=""),
            gr.update(visible=needs_resolution),
        )
    except Exception as error:
        return f"## 导出失败\n\n{html.escape(str(error))}", None, gr.update(), gr.update(), gr.update()


def resolve_delivery_exception(plan_state, reason):
    if not plan_state:
        return "请先完成渲染", gr.update(), gr.update()
    try:
        orch = _get_task_orchestrator(plan_state)
        report = orch.resolve_delivery(actor_id="local-user", exception_reason=reason or "")
        return _delivery_markdown(report), gr.update(visible=False), gr.update(visible=False)
    except Exception as error:
        return f"## 交付异常处理失败\n\n{html.escape(str(error))}", gr.update(), gr.update()


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
        title="映证｜活动视频需求与审核工作台",
        css=custom_css,
        theme=gr.themes.Soft(),
    ) as demo:

        # 标题
        gr.Markdown(
            """
            <div class="main-title">🎬 映证</div>
            """
        )
        gr.Markdown(
            """
            活动视频需求与审核工作台：先说清要什么，再审核有依据的候选片段。
            支持学校和企业活动视频的本地粗剪。
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
                scenario = gr.Radio(
                    choices=["学校活动", "企业活动"],
                    value="学校活动",
                    label="应用场景",
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

                # 需求确认 → 候选审核 → 导出
                submit_btn = gr.Button(
                    "🧾 生成需求任务书",
                    variant="primary",
                    size="lg",
                )
                clarification_input = gr.Textbox(
                    label="待确认问题的补充说明",
                    placeholder="例如：必须包含校长致辞和颁奖；发布到公众号；学生姓名需要人工核对。",
                    lines=3,
                    visible=False,
                )
                clarification_override = gr.Checkbox(
                    label="本次回答明确替换已确认值（仅在界面提示冲突时勾选）",
                    value=False,
                    visible=False,
                )
                purpose_editor = gr.Textbox(
                    label="任务书用途（生成后可修改）",
                    placeholder="例如：学校运动会公众号回顾",
                )
                audience_editor = gr.Textbox(
                    label="任务书受众（生成后可修改）",
                    placeholder="例如：师生与家长",
                )
                requirement_editor = gr.Dataframe(
                    headers=["需求ID", "优先级", "要求描述", "验收规则"],
                    datatype=["str", "str", "str", "str"],
                    type="array",
                    row_count=(1, "dynamic"),
                    col_count=(4, "fixed"),
                    label="可编辑内容要求（must/prohibited 必须填写验收规则）",
                    interactive=True,
                    visible=False,
                    wrap=True,
                )
                revise_requirement_btn = gr.Button(
                    "💾 保存任务书修改（生成新版本）",
                    visible=False,
                )
                confirm_requirement_btn = gr.Button(
                    "✅ 确认任务书并分析素材",
                    variant="secondary",
                    visible=False,
                )
                style_proposal_selector = gr.Dropdown(
                    label="AI 风格建议（可选，不选择则使用下方手动设置）",
                    visible=False,
                )
                timeline_editor = gr.Dataframe(
                    headers=["保留", "顺序", "候选ID", "源开始", "源结束", "字幕修正", "片段标题"],
                    datatype=["bool", "number", "str", "number", "number", "str", "str"],
                    type="array",
                    row_count=(1, "dynamic"),
                    col_count=(7, "fixed"),
                    label="可审核时间线",
                    interactive=True,
                    visible=False,
                    wrap=True,
                )
                manual_segment_editor = gr.Dataframe(
                    headers=["源开始", "源结束", "顺序", "需求ID（逗号分隔）", "人工标注说明", "字幕", "片段标题"],
                    datatype=["number", "number", "number", "str", "str", "str", "str"],
                    type="array",
                    row_count=(1, "dynamic"),
                    col_count=(7, "fixed"),
                    label="人工补片（可选；用于 AI 未召回但操作者已确认的时间段）",
                    interactive=True,
                    wrap=True,
                )
                export_btn = gr.Button("🎬 确认并导出", variant="secondary", visible=False)
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
                exception_reason = gr.Textbox(
                    label="交付例外原因",
                    placeholder="仅在逐项验收有 warning/failed/manual_review 时填写；也可修改左侧时间线后重新导出。",
                    lines=3,
                    visible=False,
                )
                approve_exception_btn = gr.Button(
                    "接受例外并批准交付",
                    variant="stop",
                    visible=False,
                )

        # 底部说明
        gr.Markdown("""
        ---
        ### 📖 使用说明

        1. **上传视频**：支持 mp4, mov, avi, mkv 等常见格式
        2. **描述需求**：用大白话说明成片用途和重点内容
        3. **确认任务书**：检查内容要求与 AI 执行说明，确认后才开始分析
        4. **审核候选**：核对原文证据、入选理由和预览，选择保留内容
        5. **确认导出**：批准当前方案并下载带字幕的初稿

        ### 🔧 技术栈

        `Python` · `OpenAI GPT-4o-mini` · `Faster-Whisper` · `FFmpeg` · `Gradio`
        """)

        # 绑定事件
        submit_btn.click(
            fn=create_requirement_review,
            inputs=[video_input, user_input, scenario, target_duration, style, keywords, need_subtitles],
            outputs=[
                result_text,
                plan_state,
                confirm_requirement_btn,
                clarification_input,
                clarification_override,
                requirement_editor,
                purpose_editor,
                audience_editor,
                revise_requirement_btn,
            ],
            api_name=False,
        )
        revise_requirement_btn.click(
            fn=revise_requirement_review,
            inputs=[
                plan_state,
                purpose_editor,
                audience_editor,
                target_duration,
                style,
                need_subtitles,
                requirement_editor,
            ],
            outputs=[
                result_text,
                plan_state,
                clarification_input,
                purpose_editor,
                audience_editor,
                requirement_editor,
            ],
            api_name=False,
        )
        confirm_requirement_btn.click(
            fn=confirm_and_analyze,
            inputs=[plan_state, clarification_input, clarification_override],
            outputs=[
                result_text,
                timeline_editor,
                plan_state,
                preview_selector,
                preview_video,
                export_btn,
                style_proposal_selector,
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
                timeline_editor,
                manual_segment_editor,
                style_proposal_selector,
                bgm_input,
                bgm_volume,
                transition_duration,
                subtitle_style,
                intro_style,
                outro_style,
                title_text,
            ],
            outputs=[
                result_text,
                output_video,
                subtitle_output,
                exception_reason,
                approve_exception_btn,
            ],
            api_name=False,
        )
        approve_exception_btn.click(
            fn=resolve_delivery_exception,
            inputs=[plan_state, exception_reason],
            outputs=[result_text, exception_reason, approve_exception_btn],
            api_name=False,
        )

    return demo


# ============================================================
# 启动入口
# ============================================================
if __name__ == "__main__":
    print("=" * 60)
    print("  映证｜活动视频需求与审核工作台")
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
