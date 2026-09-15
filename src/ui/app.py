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
import json
import math
import re
import shutil
import sys
import threading
from pathlib import Path

# 确保项目根目录在 Python 搜索路径中
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import gradio as gr  # noqa: E402
from src.orchestrator import VideoEditOrchestrator  # noqa: E402
from src.models.schemas import PipelineStatus  # noqa: E402
from src.config import (  # noqa: E402
    GRADIO_SERVER_NAME,
    GRADIO_SERVER_PORT,
    MOBILE_REVIEW_BASE_URL,
)


# 运行时组件只加载一次；每个任务拥有独立 Orchestrator 状态和任务目录。
_runtime = None
_task_orchestrators = {}
MAX_CANDIDATE_CARDS = 60


PRIORITY_LABELS = {
    "must": "必须保留",
    "should": "建议保留",
    "optional": "可有可无",
    "prohibited": "禁止出现",
}
PRIORITY_VALUES = {
    "必须": "must",
    "必须保留": "must",
    "建议": "should",
    "建议保留": "should",
    "可选": "optional",
    "可有可无": "optional",
    "禁止": "prohibited",
    "禁止出现": "prohibited",
    "must": "must",
    "should": "should",
    "optional": "optional",
    "prohibited": "prohibited",
}
STYLE_LABELS = {
    "general": "待你确认（可在下方直接选择）",
    "formal": "正式稳重",
    "exciting": "精彩有节奏",
    "warm": "温馨自然",
    "funny": "轻松活泼",
}


def _format_seconds(value):
    """把秒数转换为用户更容易核对的 mm:ss.s。"""
    seconds = max(0.0, float(value))
    minutes = int(seconds // 60)
    remainder = seconds - minutes * 60
    return f"{minutes:02d}:{remainder:04.1f}"


def _source_name(orch, source_asset_id):
    material_set = getattr(orch.status, "material_set", None)
    if material_set:
        match = next(
            (item for item in material_set.sources if item.id == source_asset_id), None
        )
        if match:
            return match.filename
        if source_asset_id is None and len(material_set.sources) == 1:
            return material_set.sources[0].filename
    return "原素材"


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
    orchestrator.clarification_agent = runtime.clarification_agent
    orchestrator.alignment_agent = runtime.alignment_agent
    orchestrator.material_interview_agent = runtime.material_interview_agent
    orchestrator.agent2 = runtime.agent2
    orchestrator.candidate_agent = runtime.candidate_agent
    orchestrator.style_agent = runtime.style_agent
    orchestrator.agent3 = runtime.agent3
    orchestrator.agent4 = runtime.agent4
    orchestrator.plan_service = runtime.plan_service
    orchestrator.media_asset_service = runtime.media_asset_service
    orchestrator.timeline_compiler = runtime.timeline_compiler
    orchestrator.reference_document_service = runtime.reference_document_service
    orchestrator.mobile_review_service = runtime.mobile_review_service
    orchestrator.clarification_service = runtime.clarification_service
    orchestrator.verification_engine = runtime.verification_engine
    orchestrator.render_backend = runtime.render_backend
    orchestrator.metadata_repository = runtime.metadata_repository
    orchestrator.idempotency_backend = None
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
        [index, PRIORITY_LABELS.get(item.priority, item.priority), item.description, item.acceptance_rule or ""]
        for index, item in enumerate(spec.requirements, start=1)
    ]


def _human_duration(seconds):
    """把任务书中的秒数显示成普通用户容易理解的时长。"""
    seconds = float(seconds)
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds / 60:.0f} 分钟"
    if seconds >= 60:
        minutes = int(seconds // 60)
        remainder = round(seconds - minutes * 60)
        return f"{minutes} 分 {remainder} 秒"
    return f"{seconds:.0f} 秒"


def _editable_requirement_value(value):
    """待确认标记只用于数据状态，不能变成需要用户手动删除的输入值。"""
    text = str(value or "").strip()
    return "" if text in {"待你确认", "待确认", "未知"} else text


def _requirement_review_markdown(spec, execution, warnings=None, saved=False):
    """生成统一的用户审核视图，不暴露内部枚举、ID 或提示词字段。"""
    warning_block = ""
    if warnings:
        warning_lines = "\n".join(f"- {html.escape(warning)}" for warning in warnings)
        warning_block = f"""
> **有些内容需要你留意**

{warning_lines}
"""

    requirement_sections = []
    for index, item in enumerate(spec.requirements, start=1):
        priority = PRIORITY_LABELS.get(item.priority, "需要确认")
        acceptance = (
            f"\n\n**完成标准：** {html.escape(item.acceptance_rule)}"
            if item.acceptance_rule else ""
        )
        requirement_sections.append(
            f"#### {index}. {priority}\n\n{html.escape(item.description)}{acceptance}"
        )
    requirements = "\n\n".join(requirement_sections) or "暂未识别出具体内容要求，请在下方补充。"

    question_notice = (
        f"还有 **{len(spec.open_questions)} 个可选问题**。你可以在下方逐题补充，"
        "也可以选择“暂时忽略”，不会阻止继续分析。"
        if spec.open_questions
        else "目前没有必须补充的问题，可以直接确认并开始分析素材。"
    )
    saved_notice = "\n\n> ✅ 你的修改已保存为新版本，请再次确认。" if saved else ""
    style_label = STYLE_LABELS.get(spec.style, "由系统根据活动内容判断")
    subtitle_label = "需要，生成后仍可修改" if spec.need_subtitles else "不需要"
    bgm_label = "可使用你提供且确认有权使用的音乐" if spec.need_bgm else "本次不自动添加"

    return f"""
## 剪辑任务书 v{spec.version}

请确认下面的目标和内容取舍是否符合你的意思。系统已完成素材的独立转录与初步理解；确认后才会正式选择候选片段。{saved_notice}

{warning_block}

### 这支视频要做成什么

| 需要确认的项目 | 当前方案 |
| --- | --- |
| 成片用途 | {html.escape(spec.purpose)} |
| 主要观看者 | {html.escape(spec.audience)} |
| 成片长度 | 约 {_human_duration(spec.target_duration)}，允许前后相差 {_human_duration(spec.duration_tolerance)} |
| 整体感觉 | {html.escape(style_label)} |
| 中文字幕 | {subtitle_label} |
| 背景音乐 | {bgm_label} |

### 哪些内容要保留或避开

{requirements}

### 还可以补充

{question_notice}

### AI 接下来会怎样处理

{execution.visible_instruction}

用途、观看者、风格和音乐都可以在下方直接修改。确认无误后，点击“确认这些需求并生成方案”。
"""


def _material_set_markdown(orch):
    """用文件名和业务状态展示独立素材处理结果，不暴露内部素材 ID。"""
    material_set = orch.status.material_set
    if material_set is None:
        return ""
    status_labels = {
        "ready": "已完成转录与理解",
        "no_audio": "没有音轨，可用于人工补片",
        "failed": "处理失败，可单独重试",
        "transcribing": "正在转录",
        "preflight_ok": "等待转录",
        "pending": "等待处理",
        "excluded": "已排除",
    }
    result_by_source = {item.source.id: item for item in material_set.results}
    rows = []
    for source in material_set.sources:
        result = result_by_source.get(source.id)
        state = result.status if result else source.analysis_state
        summary = (
            result.analysis.summary.strip()
            if result and result.analysis and result.analysis.summary.strip()
            else "—"
        )
        rows.append(
            f"| {source.order} | {html.escape(source.filename)} | "
            f"{status_labels.get(state, '等待处理')} | {_human_duration(source.duration)} | "
            f"{html.escape(summary[:120])} |"
        )
    return (
        "### 本次素材处理结果\n\n"
        "每段素材保留自己的时间线；只有批准剪辑方案后才组合选中片段。\n\n"
        "| 顺序 | 来源文件 | 状态 | 时长 | 内容概览 |\n"
        "| --- | --- | --- | --- | --- |\n"
        + "\n".join(rows)
    )


def _material_alignment_markdown(proposal):
    """以业务语言展示素材事实与任务书差异，不暴露内部状态值或证据 ID。"""
    if proposal is None:
        return ""
    status_title = {
        "too_vague": "AI 发现当前要求还可以更具体",
        "mismatch": "AI 发现素材内容与当前要求可能不一致",
        "aligned": "素材内容与当前任务书基本一致",
    }.get(proposal.alignment_status, "素材与需求对齐建议")
    topics = "、".join(html.escape(item) for item in proposal.detected_topics) or "暂未提取"
    focus_lines = "\n".join(
        f"- {html.escape(item)}" for item in proposal.suggested_focus_items
    )
    return f"""
## {status_title}

**素材主要内容：** {html.escape(proposal.material_summary)}

**识别到的主题：** {topics}

**为什么建议调整：** {html.escape(proposal.rationale)}

### AI 建议的任务书方向

- 建议用途：{html.escape(proposal.suggested_purpose)}
- 建议时长：约 {_human_duration(proposal.suggested_target_duration)}
- 建议重点：
{focus_lines}

AI 不会自动替换你的要求。你可以直接采用、修改下方建议后采用，或者保持原任务书。
"""


def resolve_alignment_review(workflow_state, edited_text, action):
    """处理素材对齐建议，并把结果投影回同一个任务书审核页面。"""
    empty = [gr.update() for _ in range(9)]
    if not workflow_state:
        return "请先生成任务书", workflow_state, *empty
    try:
        orch = _get_task_orchestrator(workflow_state)
        compilation = orch.resolve_material_alignment(
            action,
            actor_id="local-user",
            edited_requirement_text=edited_text,
        )
        spec = compilation.spec
        state = {**workflow_state, "gate_id": orch.status.requirement_gate.id}
        style_label = {
            "formal": "正式",
            "exciting": "燃向",
            "warm": "温馨",
            "funny": "轻松",
            "general": "自动判断",
        }.get(spec.style, "自动判断")
        return (
            _requirement_review_markdown(
                spec,
                orch.status.execution_brief,
                warnings=compilation.warnings,
                saved=True,
            ),
            state,
            gr.update(value=_requirement_rows(spec), visible=True),
            gr.update(value=_editable_requirement_value(spec.purpose), visible=True),
            gr.update(value=_editable_requirement_value(spec.audience), visible=True),
            gr.update(value=spec.target_duration),
            gr.update(value=style_label),
            gr.update(value=spec.need_subtitles),
            gr.update(visible=True),
            gr.update(visible=True),
            gr.update(visible=False),
        )
    except Exception as error:
        return (
            f"## 素材建议处理失败\n\n{html.escape(str(error))}",
            workflow_state,
            *empty,
        )


def adopt_alignment(workflow_state, edited_text):
    return resolve_alignment_review(workflow_state, edited_text, "adopt")


def edit_and_adopt_alignment(workflow_state, edited_text):
    return resolve_alignment_review(workflow_state, edited_text, "edit_and_adopt")


def keep_original_alignment(workflow_state, edited_text):
    return resolve_alignment_review(workflow_state, edited_text, "keep_original")


def _timeline_rows(plan):
    return [
        [
            True,
            segment.order,
            f"片段 {index}",
            round(segment.source_start, 3),
            round(segment.source_end, 3),
            segment.subtitle_text or "",
            segment.title_text or "",
        ]
        for index, segment in enumerate(plan.timeline_segments, start=1)
    ]


def _candidate_label_map(plan):
    return {
        f"片段 {index}": segment.candidate_id
        for index, segment in enumerate(plan.timeline_segments, start=1)
    }


def load_candidate_cards(plan_state):
    """把时间线和对应预览投影成固定数量的用户可读片段卡片。"""
    hidden_groups = [gr.update(visible=False) for _ in range(MAX_CANDIDATE_CARDS)]
    hidden_headers = [gr.update(value="") for _ in range(MAX_CANDIDATE_CARDS)]
    hidden_previews = [gr.update(value=None, visible=False) for _ in range(MAX_CANDIDATE_CARDS)]
    hidden_details = [gr.update(value="") for _ in range(MAX_CANDIDATE_CARDS)]
    hidden_decisions = [gr.update(value="drop") for _ in range(MAX_CANDIDATE_CARDS)]
    hidden_numbers = [gr.update(value=0) for _ in range(MAX_CANDIDATE_CARDS)]
    hidden_text = [gr.update(value="") for _ in range(MAX_CANDIDATE_CARDS)]
    hidden_origins = [0.0 for _ in range(MAX_CANDIDATE_CARDS)]
    if not plan_state:
        return (
            *hidden_groups,
            *hidden_headers,
            *hidden_previews,
            *hidden_details,
            *hidden_decisions,
            *hidden_numbers,
            *hidden_numbers,
            *hidden_numbers,
            *hidden_text,
            *hidden_text,
            *hidden_origins,
            *hidden_text,
        )
    preview_paths = {}
    try:
        orch = _get_task_orchestrator(plan_state)
        plan = orch.status.edit_plan
        segments = list(plan.timeline_segments) if plan else []
        preview_paths = plan_state.get("previews", {}) or orch.preview_paths
        candidate_by_id = {
            item.id: item for item in (orch.status.analysis.candidate_clips if orch.status.analysis else [])
        }
        requirement_by_id = {
            item.id: item.description
            for item in (orch.status.requirement_spec.requirements if orch.status.requirement_spec else [])
        }
    except Exception:
        segments = []
        candidate_by_id = {}
        requirement_by_id = {}
    group_updates = []
    header_updates = []
    preview_updates = []
    detail_updates = []
    decision_updates = []
    order_updates = []
    start_updates = []
    end_updates = []
    subtitle_updates = []
    title_updates = []
    origin_updates = []
    boundary_updates = []
    for index in range(MAX_CANDIDATE_CARDS):
        if index < len(segments):
            segment = segments[index]
            duration = segment.source_end - segment.source_start
            source_name = _source_name(orch, segment.source_asset_id)
            group_updates.append(gr.update(visible=True))
            header_updates.append(
                gr.update(
                    value=(
                        f"### 片段 {index + 1}\n"
                        f"来源：**{html.escape(source_name)}** · "
                        f"{_format_seconds(segment.source_start)}–"
                        f"{_format_seconds(segment.source_end)}，约 {duration:.1f} 秒"
                    )
                )
            )
            preview_path = preview_paths.get(str(segment.order))
            preview_updates.append(
                gr.update(value=preview_path, visible=bool(preview_path))
            )
            candidate = candidate_by_id.get(segment.candidate_id)
            if candidate:
                matched = "；".join(
                    requirement_by_id.get(item, "任务书中的相关内容")
                    for item in candidate.matched_requirement_ids
                )
                quotes = "\n\n".join(
                    f"> “{html.escape(item.quote)}”" for item in candidate.citations
                ) or "暂无转录原文"
                risk = (
                    "这段由检索补入以接近目标时长，建议播放后确认。"
                    if "code_retrieved_review_required" in candidate.risk_flags
                    else "来源、时间和引用已经过自动核对。"
                )
                detail_updates.append(gr.update(value=(
                    f"**为什么推荐：** {html.escape(candidate.selection_reason)}\n\n"
                    f"**对应需求：** {html.escape(matched)}\n\n"
                    f"**可核对原文：**\n\n{quotes}\n\n"
                    f"**检查提示：** {risk}"
                )))
            else:
                detail_updates.append(gr.update(value="暂无更多说明"))
            decision_updates.append(gr.update(value="keep"))
            order_updates.append(gr.update(value=segment.order))
            duration_limit = (
                orch.status.analysis.source_durations.get(segment.source_asset_id or "", orch.status.analysis.video_duration)
                if orch.status.analysis else segment.source_end
            )
            context_start = max(0.0, (candidate.source_start if candidate else segment.source_start) - 5.0)
            context_end = min(duration_limit, (candidate.source_end if candidate else segment.source_end) + 5.0)
            start_updates.append(gr.update(value=round(segment.source_start, 2), minimum=context_start, maximum=context_end))
            end_updates.append(gr.update(value=round(segment.source_end, 2), minimum=context_start, maximum=context_end))
            origin_updates.append(context_start)
            boundary_updates.append(gr.update(value=describe_candidate_boundary(
                plan_state, index + 1, segment.source_start, segment.source_end, context_start,
            )))
            subtitle_updates.append(gr.update(value=segment.subtitle_text or ""))
            title_updates.append(gr.update(value=segment.title_text or ""))
        else:
            group_updates.append(hidden_groups[index])
            header_updates.append(hidden_headers[index])
            preview_updates.append(hidden_previews[index])
            detail_updates.append(hidden_details[index])
            decision_updates.append(hidden_decisions[index])
            order_updates.append(hidden_numbers[index])
            start_updates.append(hidden_numbers[index])
            end_updates.append(hidden_numbers[index])
            subtitle_updates.append(hidden_text[index])
            title_updates.append(hidden_text[index])
            origin_updates.append(0.0)
            boundary_updates.append(hidden_text[index])
    return (
        *group_updates,
        *header_updates,
        *preview_updates,
        *detail_updates,
        *decision_updates,
        *order_updates,
        *start_updates,
        *end_updates,
        *subtitle_updates,
        *title_updates,
        *origin_updates,
        *boundary_updates,
    )


def load_candidate_preview(plan_state, order):
    """只在用户需要观看时生成这一段，避免进入审核页前长时间等待。"""
    if not plan_state:
        raise gr.Error("请先生成剪辑方案")
    try:
        orch = _get_task_orchestrator(plan_state)
        path = orch.create_preview_for_order(int(order), context_seconds=5.0)
        return gr.update(value=path, visible=True)
    except Exception as error:
        raise gr.Error(str(error)) from error


def describe_candidate_boundary(plan_state, order, start, end, origin=0.0):
    """显示拖动位置附近的原话，用户不用猜时间码对应什么内容。"""
    del origin
    if float(end) <= float(start):
        return "⚠️ 结尾必须在开头之后，请调整选段范围。"
    try:
        orch = _get_task_orchestrator(plan_state)
        segment = orch.status.edit_plan.timeline_segments[int(order) - 1]
        transcript = [item for item in orch.status.analysis.transcript if item.source_asset_id == segment.source_asset_id]
        def nearby(position):
            if not transcript:
                return "以预览画面和声音为准"
            item = min(transcript, key=lambda item: min(abs(item.start - position), abs(item.end - position)))
            return html.escape(item.text)
        return (
            f"选中 **{float(end) - float(start):.1f} 秒**。拖动后预览会定位到相应画面。\n\n"
            f"**开头附近：** {nearby(float(start))}\n\n"
            f"**结尾附近：** {nearby(float(end))}"
        )
    except (AttributeError, ValueError, IndexError, KeyError):
        return f"选中 {float(end) - float(start):.1f} 秒；先加载预览，再拖动调整。"


def candidate_cards_to_rows(*values):
    """把片段卡片的直观输入转回已有计划服务使用的数组结构。"""
    fields_per_card = 6
    expected = MAX_CANDIDATE_CARDS * fields_per_card
    if len(values) != expected:
        raise ValueError("候选片段编辑数据不完整，请刷新页面后重试")
    rows = []
    for index in range(MAX_CANDIDATE_CARDS):
        offset = index * fields_per_card
        decision, order, start, end, subtitle, title = values[offset:offset + fields_per_card]
        rows.append([
            decision == "keep",
            order,
            f"片段 {index + 1}",
            start,
            end,
            subtitle,
            title,
        ])
    return rows


def _requirement_ids_from_numbers(raw_value, spec):
    """把 UI 中的任务书序号转换为内部需求 ID，ID 不暴露给用户。"""
    values = [
        item.strip()
        for item in str(raw_value or "").replace("，", ",").replace("、", ",").split(",")
        if item.strip()
    ]
    if not values:
        must_items = [item for item in spec.requirements if item.priority == "must"]
        if len(must_items) == 1:
            return [must_items[0].id]
        if len(spec.requirements) == 1:
            return [spec.requirements[0].id]
        raise ValueError("人工补片需要填写它对应的任务书要求序号，例如 1 或 1,2")
    result = []
    for value in values:
        try:
            index = int(float(value)) - 1
        except ValueError as error:
            raise ValueError(f"无法识别任务书要求序号：{value}") from error
        if not 0 <= index < len(spec.requirements):
            raise ValueError(f"任务书中没有第 {index + 1} 条要求")
        requirement_id = spec.requirements[index].id
        if requirement_id not in result:
            result.append(requirement_id)
    return result


def _is_blank_cell(value):
    """识别 Gradio Dataframe 空行中的 None、空字符串和 NaN。"""
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return isinstance(value, str) and not value.strip()


def _manual_segment_payloads(rows, spec, selected_count):
    """仅转换用户实际填写的人工补片；默认空白行不参与生成。"""
    segments = []
    for raw_row in rows or []:
        row = list(raw_row or [])
        if not row or all(_is_blank_cell(value) for value in row):
            continue
        has_source = (
            len(row) >= 9
            and isinstance(row[0], str)
            and row[0].startswith("asset_")
        )
        offset = 2 if has_source else 0
        # Gradio 的 number 列会把默认空白单元格提交为 0；整行 0/0/0
        # 且其余字段为空时只是占位行，不代表用户添加了 0 秒片段。
        numeric_placeholders = row[offset:offset + 3] + [0] * max(0, 3 - len(row[offset:]))
        remaining_cells = row[offset + 3:]
        try:
            is_default_placeholder = (
                all(float(value) == 0 for value in numeric_placeholders)
                and all(_is_blank_cell(value) for value in remaining_cells)
            )
        except (TypeError, ValueError):
            is_default_placeholder = False
        if is_default_placeholder:
            continue
        start = row[offset] if len(row) > offset else None
        end = row[offset + 1] if len(row) > offset + 1 else None
        if _is_blank_cell(start) or _is_blank_cell(end):
            raise ValueError("人工补片未填写完整：请同时填写开始和结束时间，或者清空这一行")
        order = row[offset + 2] if len(row) > offset + 2 else None
        requirement_numbers = row[offset + 3] if len(row) > offset + 3 else ""
        segments.append({
            "source_asset_id": row[0] if has_source else None,
            "source_start": float(start),
            "source_end": float(end),
            "order": (
                int(float(order))
                if not _is_blank_cell(order)
                else selected_count + len(segments) + 1
            ),
            "matched_requirement_ids": _requirement_ids_from_numbers(requirement_numbers, spec),
            "annotation": (
                str(row[offset + 4]).strip()
                if len(row) > offset + 4 and not _is_blank_cell(row[offset + 4]) else ""
            ),
            "subtitle_text": (
                str(row[offset + 5]).strip()
                if len(row) > offset + 5 and not _is_blank_cell(row[offset + 5]) else None
            ),
            "title_text": (
                str(row[offset + 6]).strip()
                if len(row) > offset + 6 and not _is_blank_cell(row[offset + 6]) else None
            ),
        })
    return segments


def _uploaded_video_paths(value):
    """兼容 Gradio 单文件/多文件返回值，并保留用户上传顺序。"""
    if value is None:
        return []
    values = value if isinstance(value, (list, tuple)) else [value]
    paths = []
    for item in values:
        path = getattr(item, "path", item)
        if path:
            paths.append(str(path))
    return paths


def _manual_requirement_choices(spec):
    return [
        (f"要求 {index}｜{item.description}", str(index))
        for index, item in enumerate(spec.requirements, start=1)
        if item.priority != "prohibited"
    ]


def _manual_range_markdown(start, end):
    start_value = max(0.0, float(start or 0))
    end_value = max(0.0, float(end or 0))
    duration = max(0.0, end_value - start_value)
    if end_value <= start_value:
        return "⚠️ 结束位置需要在开始位置之后，请继续拖动。"
    return (
        f"当前选中 **{_format_seconds(start_value)} — {_format_seconds(end_value)}**，"
        f"片段时长 **{duration:.1f} 秒**。"
    )


def _manual_segments_markdown(rows):
    if not rows:
        return "尚未补入片段。拖动上方时间线并点击“加入时间线”即可。"
    lines = ["### 已补入的片段"]
    for index, row in enumerate(rows, start=1):
        has_source = len(row) >= 9 and str(row[0]).startswith("asset_")
        offset = 2 if has_source else 0
        source_text = f"｜来源 {html.escape(str(row[1]))}" if has_source else ""
        requirement_text = str(row[offset + 3]).replace(",", "、") if len(row) > offset + 3 else ""
        note = str(row[offset + 4]).strip() if len(row) > offset + 4 and row[offset + 4] else "未填写说明"
        lines.append(
            f"{index}. **{_format_seconds(row[offset])} — {_format_seconds(row[offset + 1])}**"
            f"（{float(row[offset + 1]) - float(row[offset]):.1f} 秒）{source_text}"
            f"｜对应要求 {requirement_text}｜{html.escape(note)}"
        )
    return "\n\n".join(lines)


def append_manual_segment(
    rows, start, end, requirement_numbers, annotation, subtitle, title,
    plan_state=None, source_asset_id=None,
):
    """把用户拖动选中的区间加入补片列表，不要求手工填写秒数。"""
    start_value = float(start or 0)
    end_value = float(end or 0)
    if end_value <= start_value:
        raise gr.Error("结束位置必须晚于开始位置，请重新拖动时间线")
    selected_requirements = [str(value) for value in (requirement_numbers or [])]
    if not selected_requirements:
        raise gr.Error("请至少勾选一条这段内容对应的任务书要求")
    updated = [list(row) for row in (rows or [])]
    values = [
        start_value, end_value, None, ",".join(selected_requirements),
        str(annotation or "").strip(), str(subtitle or "").strip(),
        str(title or "").strip(),
    ]
    if source_asset_id:
        source_name = str(source_asset_id)
        try:
            orch = _get_task_orchestrator(plan_state)
            source_name = _source_name(orch, source_asset_id)
        except Exception:
            pass
        values = [source_asset_id, source_name, *values]
    updated.append(values)
    return updated, _manual_segments_markdown(updated)


def select_manual_source(plan_state, source_asset_id):
    """切换人工补片所预览的独立原素材，并同步滑块范围。"""
    if not plan_state or not source_asset_id:
        return gr.update(), gr.update(), gr.update()
    orch = _get_task_orchestrator(plan_state)
    source = next(
        item for item in orch.status.material_set.sources
        if item.id == source_asset_id
    )
    default_end = min(15.0, source.duration)
    return (
        gr.update(value=source.source_path, visible=True),
        gr.update(minimum=0, maximum=source.duration, value=0, visible=True),
        gr.update(minimum=0, maximum=source.duration, value=default_end, visible=True),
    )


def undo_manual_segment(rows):
    updated = [list(row) for row in (rows or [])]
    if updated:
        updated.pop()
    return updated, _manual_segments_markdown(updated)


def _truthy(value):
    return value is True or str(value).strip().lower() in {"true", "1", "yes", "是", "保留"}


def _pending_clarification_slots(orch):
    """返回 AI 判断需要澄清的槽位，以及可向用户解释的提问依据。"""
    spec = orch.status.requirement_spec
    if not spec or not orch.store:
        return []
    slots = orch.store.read_requirement_slots(spec.version)
    pending = []
    for slot in slots:
        if slot.status not in {"missing", "unknown", "conflict"} or not slot.question:
            continue
        explanation = []
        if slot.question_reason:
            explanation.append(f"为什么问：{slot.question_reason}")
        if slot.question_impact:
            explanation.append(f"可能影响：{slot.question_impact}")
        display = slot.question
        if explanation:
            display += "｜" + "｜".join(explanation)
        pending.append((slot.key, display, slot.answer_hint))
    return pending[:5]


def load_clarification_questions(workflow_state):
    """把最多五个待确认槽位投影成五组独立的前端问题。"""
    hidden_groups = [gr.update(visible=False) for _ in range(5)]
    hidden_modes = [gr.update(visible=False, value="ignore") for _ in range(5)]
    hidden_answers = [gr.update(visible=False, value="") for _ in range(5)]
    hidden_statuses = [gr.update(value="尚未确认") for _ in range(5)]
    if not workflow_state:
        return (
            gr.update(visible=False, value=False),
            *hidden_groups, *hidden_modes, *hidden_answers,
            {}, *hidden_statuses,
        )
    try:
        orch = _get_task_orchestrator(workflow_state)
        questions = _pending_clarification_slots(orch)
    except Exception:
        questions = []
    mode_updates = []
    answer_updates = []
    group_updates = []
    for index in range(5):
        if index < len(questions):
            _, question, answer_hint = questions[index]
            group_updates.append(gr.update(visible=True))
            mode_updates.append(
                gr.update(
                    label=f"问题 {index + 1}｜{question}",
                    visible=True,
                    value="ignore",
                )
            )
            answer_updates.append(
                gr.update(
                    label=(
                        f"选择“补充说明”时填写（{answer_hint}）"
                        if answer_hint else "选择“补充说明”时填写"
                    ),
                    visible=True,
                    value="",
                )
            )
        else:
            group_updates.append(hidden_groups[index])
            mode_updates.append(hidden_modes[index])
            answer_updates.append(hidden_answers[index])
    has_conflict = False
    if questions:
        slots = orch.store.read_requirement_slots(orch.status.requirement_spec.version)
        has_conflict = any(slot.status == "conflict" for slot in slots)
    return (
        gr.update(visible=has_conflict, value=False),
        *group_updates,
        *mode_updates,
        *answer_updates,
        {},
        *[
            gr.update(value="请选择“暂时忽略”或“补充说明”，然后确认本题。")
            if index < len(questions) else hidden_statuses[index]
            for index in range(5)
        ],
    )


def confirm_clarification_choice(workflow_state, confirmations, question_index, mode, answer):
    """单独确认一条 AI 提问，避免选择后没有明确提交动作。"""
    if not workflow_state:
        raise gr.Error("请先生成任务书")
    orch = _get_task_orchestrator(workflow_state)
    pending = _pending_clarification_slots(orch)
    index = int(question_index)
    if not 0 <= index < len(pending):
        raise gr.Error("这个问题已经失效，请重新查看任务书")
    answer_text = str(answer or "").strip()
    if mode == "supplement" and not answer_text:
        raise gr.Error("选择“补充说明”后，请先填写内容")
    slot_key = pending[index][0]
    updated = dict(confirmations or {})
    updated[str(index)] = {
        "slot_key": slot_key,
        "mode": mode,
        "answer": answer_text if mode == "supplement" else "",
    }
    message = (
        "✅ 已确认：采用你的补充说明。"
        if mode == "supplement"
        else "✅ 已确认：本题暂时忽略，不会阻止继续。"
    )
    return updated, message


def reset_clarification_choice(confirmations, question_index):
    updated = dict(confirmations or {})
    updated.pop(str(int(question_index)), None)
    return updated, "选择已改变，请再次点击“确认本题”。"


def _validate_clarification_confirmations(orch, confirmations, modes, answers):
    pending = _pending_clarification_slots(orch)
    saved = confirmations or {}
    for index, (slot_key, _, _) in enumerate(pending):
        mode = modes[index] if index < len(modes) else "ignore"
        answer = str(answers[index] or "").strip() if index < len(answers) else ""
        expected_answer = answer if mode == "supplement" else ""
        confirmation = saved.get(str(index), {})
        if (
            confirmation.get("slot_key") != slot_key
            or confirmation.get("mode") != mode
            or confirmation.get("answer") != expected_answer
        ):
            raise ValueError(f"请先点击问题 {index + 1} 下方的“确认本题”")


def _clarification_answer_map(orch, modes, answers):
    pending = _pending_clarification_slots(orch)
    result = {}
    for index, (slot_key, _, _) in enumerate(pending):
        mode = modes[index] if index < len(modes) else "ignore"
        answer = str(answers[index] or "").strip() if index < len(answers) else ""
        if mode == "supplement":
            if not answer:
                raise ValueError(f"问题 {index + 1} 选择了补充说明，请填写内容或改为暂时忽略")
            result[slot_key] = answer
        else:
            result[slot_key] = "暂不确定"
    return result


def begin_requirement_generation():
    return (
        "⏳ 正在检查并转录素材。完成后会直接跳到可编辑的需求确认区，请不要重复点击。",
        gr.update(value="⏳ 正在读取素材...", interactive=False),
    )


def finish_requirement_generation():
    return (
        "✅ 素材已理解。请在第 2 步统一确认用途、观看者、内容取舍、风格与音乐。",
        gr.update(value="重新读取素材", interactive=True),
    )


def begin_material_analysis():
    return (
        "⏳ 已保存你的答案；正在复用转录、批量检索并生成一版接近目标时长的方案。",
        gr.update(value="⏳ 正在生成方案...", interactive=False),
    )


def finish_material_analysis():
    return (
        "✅ 方案已生成。请在第 3 步播放候选并确认，证据详情默认折叠。",
        gr.update(value="重新生成方案", interactive=True),
    )


def reveal_requirement_stage():
    """素材理解完成后才展开任务书，避免初始页堆满空区域。"""
    return tuple(gr.update(visible=True) for _ in range(6))


def reveal_candidate_stage():
    """候选生成完成后才展示审核操作。"""
    return tuple(gr.update(visible=True) for _ in range(4))


def reveal_delivery_stage():
    """完成渲染后才展示成片和编辑器交接区。"""
    return tuple(gr.update(visible=True) for _ in range(3))


def _delivery_markdown(report):
    if report is None:
        return ""
    status_labels = {
        "passed": "通过",
        "warning": "需要注意",
        "failed": "未通过",
        "manual_review": "需要人工复核",
        "needs_resolution": "存在待处理项",
        "approved_with_exceptions": "已说明原因并批准",
    }
    method_labels = {
        "deterministic": "自动检查",
        "semantic": "语义检查",
        "manual": "人工确认",
        "human": "人工确认",
    }
    rows = "\n".join(
        f"| {status_labels.get(item.status, item.status)} | "
        f"{method_labels.get(item.method, item.method)} | {html.escape(item.summary)} |"
        for item in report.results
    )
    exception = ""
    if report.status == "approved_with_exceptions":
        exception = f"\n\n例外批准人：{html.escape(report.approved_by or '')}；原因：{html.escape(report.exception_reason or '')}"
    return f"""
## 逐项交付报告

状态：**{status_labels.get(report.status, report.status)}**

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
        f"{index}. **{PRIORITY_LABELS.get(item.priority, item.priority)}**：{html.escape(item.description)}"
        for index, item in enumerate(spec.requirements, start=1)
    ) or "- 无"
    requirement_by_id = {item.id: item.description for item in spec.requirements}
    generation = {}
    try:
        generation = json.loads(
            (orch.task_dir / "candidate_generation.json").read_text(encoding="utf-8")
        )
    except (AttributeError, OSError, ValueError, TypeError):
        generation = {}
    failed_descriptions = [
        requirement_by_id[item_id]
        for item_id in generation.get("failed_requirement_ids", [])
        if item_id in requirement_by_id
    ]
    batch_notice = (
        "> **⚠️ AI 排序本次没有完整返回：** "
        + "；".join(html.escape(item) for item in failed_descriptions)
        + "。系统已用来源、时间和原文都可验证的检索结果补足候选；"
        "这些片段不是冒充 AI 结论，请播放后决定是否保留。"
        if failed_descriptions else ""
    )
    candidate_number_by_id = {}
    for index, candidate in enumerate(analysis.candidate_clips, start=1):
        candidate_number_by_id[candidate.id] = index
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
        + "；".join(html.escape(item.description) for item in missing_must)
        + "。批准前请修改选择或使用人工补片。"
        if missing_must else "> ✅ 必须项候选覆盖检查已通过。"
    )
    lower_duration = max(0.0, spec.target_duration - spec.duration_tolerance)
    upper_duration = spec.target_duration + spec.duration_tolerance
    if plan.estimated_duration < lower_duration:
        if missing_must:
            duration_notice = (
                f"> **⚠️ 当前候选时长不足：** 预计 {plan.estimated_duration:.1f} 秒，"
                f"任务书至少需要 {lower_duration:.1f} 秒，而且仍有必须内容未覆盖。"
                "请在下方从原素材补入对应片段。"
            )
        else:
            duration_notice = (
                f"> **⚠️ 当前候选时长不足：** 预计 {plan.estimated_duration:.1f} 秒，"
                f"任务书至少需要 {lower_duration:.1f} 秒。你可以在下方明确接受当前短版，"
                "或从原素材补入片段后按目标生成。"
            )
    elif plan.estimated_duration > upper_duration:
        duration_notice = (
            f"> **⚠️ 当前候选时长过长：** 预计 {plan.estimated_duration:.1f} 秒，"
            f"任务书最多允许 {upper_duration:.1f} 秒。请删减片段。"
        )
    else:
        duration_notice = (
            f"> ✅ 时长检查通过：预计 {plan.estimated_duration:.1f} 秒，"
            f"允许范围 {lower_duration:.1f}–{upper_duration:.1f} 秒。"
        )
    timeline_rows = "\n".join(
        f"| {segment.order} | 片段 {candidate_number_by_id.get(segment.candidate_id, segment.order)} | "
        f"{html.escape(_source_name(orch, segment.source_asset_id))} | "
        f"{_format_seconds(segment.source_start)}–{_format_seconds(segment.source_end)} | "
        f"{_format_seconds(segment.output_start)}–{_format_seconds(segment.output_end)} | "
        f"{html.escape(segment.subtitle_text or '跟随转录')} |"
        for segment in plan.timeline_segments
    )
    return f"""
## 剪辑方案审核页

任务书 v{spec.version}：{html.escape(spec.purpose)}；受众：{html.escape(spec.audience)}；目标 {spec.target_duration:.0f}±{spec.duration_tolerance:.0f} 秒。

### 需求
{requirements}

{coverage_notice}

{duration_notice}

{batch_notice}

### 候选片段
已生成 **{len(analysis.candidate_clips)} 个**可审核片段。播放按钮和“为什么推荐与证据”都在下方片段卡片中，默认收起以便快速浏览。

### 粗剪时间线 v{plan.version}
| 顺序 | 片段 | 来源素材 | 原素材时间 | 成片时间 | 字幕 |
| --- | --- | --- | --- | --- | --- |
{timeline_rows}

预计成片时长：{plan.estimated_duration:.1f} 秒。请在下方“片段与时间线”中保留、删除、排序、修剪或修改字幕，然后点击“确认方案并生成成片”。
"""


def _candidate_failure_markdown(orch):
    """把候选失败原因转换成用户可操作的说明，不暴露内部异常和 ID。"""
    generation = {}
    rejected = []
    try:
        generation = json.loads(
            (orch.task_dir / "candidate_generation.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError, TypeError):
        generation = {}
    try:
        rejected_payload = json.loads(
            (orch.task_dir / "rejected_candidate_suggestions.json").read_text(encoding="utf-8")
        )
        rejected = rejected_payload.get("candidates", [])
    except (OSError, ValueError, TypeError):
        rejected = []

    flags = {
        flag
        for candidate in rejected
        for flag in candidate.get("risk_flags", [])
    }
    reasons = []
    if generation.get("degraded"):
        failed_count = len(generation.get("failed_requirement_ids", []))
        reasons.append(
            f"有 {failed_count or '部分'} 项需求的候选 AI 批次失败；"
            "成功批次不受影响，失败批次的规则建议已被禁止进入成片。"
        )
    if flags.intersection({
        "candidate_starts_inside_utterance",
        "candidate_ends_inside_utterance",
        "candidate_starts_mid_sentence",
        "candidate_ends_mid_sentence",
    }):
        reasons.append("检测到候选从半句话开始或在人物表达中途结束。")
    if not reasons:
        reasons.append("没有候选同时通过证据引用、语义边界和需求关联检查。")
    reason_lines = "\n".join(f"- {reason}" for reason in reasons)
    return f"""
## 当前没有达到可出片标准

系统已停止生成成片，没有把降级规则或不完整发言当作合格候选。

{reason_lines}

你可以重新分析，或把任务书中的重点人物、主题和必须内容补充得更具体。
"""


def _duration_action_markdown(orch):
    """解释当前时长缺口，并判断是否只需用户接受短版即可继续。"""
    spec = orch.status.requirement_spec
    plan = orch.status.edit_plan
    if not spec or not plan:
        return "", False
    lower = max(0.0, spec.target_duration - spec.duration_tolerance)
    missing_seconds = max(0.0, lower - plan.estimated_duration)
    covered = {
        requirement_id
        for segment in plan.timeline_segments
        for requirement_id in segment.matched_requirement_ids
    }
    missing_must = [
        item for item in spec.requirements
        if item.priority == "must" and item.id not in covered
    ]
    recommendations = []
    try:
        duration_plan = json.loads(
            (orch.task_dir / "duration_budget.json").read_text(encoding="utf-8")
        )
        recommendations = duration_plan.get("recommendations", [])
    except (AttributeError, OSError, ValueError, TypeError):
        recommendations = []
    recommendation_text = (
        "\n\n**AI 的补足建议：**\n" + "\n".join(
            f"- {html.escape(str(item))}" for item in recommendations
        )
        if recommendations else ""
    )
    if missing_seconds <= 0:
        return (
            f"✅ 当前预计 **{plan.estimated_duration:.1f} 秒**，已达到任务书允许的最低时长 "
            f"**{lower:.1f} 秒**。如果 AI 仍有漏选，也可以在下方人工补入片段。"
            f"{recommendation_text}",
            False,
        )
    if missing_must:
        return (
            f"⚠️ 当前预计 **{plan.estimated_duration:.1f} 秒**，还差约 **{missing_seconds:.1f} 秒**；"
            "并且仍有必须内容没有进入时间线。先点击“自动补选，接近目标时长”；也可以点击“＋ 自己补入片段”后播放原素材补入对应内容。"
            "必须内容不能通过接受短版跳过。",
            False,
        )
    return (
        f"⚠️ 当前预计 **{plan.estimated_duration:.1f} 秒**，比任务书允许的最低时长 "
        f"**{lower:.1f} 秒**少约 **{missing_seconds:.1f} 秒**。你可以：\n\n"
        "1. 点击“自动补选，接近目标时长”，系统从已转录素材中补选，不重复调用 AI。\n"
        "2. 点击“＋ 自己补入片段”，播放原素材并拖动选段范围。\n"
        "3. 如果现有内容已经完整，点击“接受当前时长并生成”；系统会记录这次人工例外。"
        f"{recommendation_text}",
        True,
    )


def live_review_duration(plan_state, transition, intro, outro, manual_rows, *card_values):
    """用户删选或拖动边界时立即重算；不能等点击生成才发现时长不足。"""
    if not plan_state:
        return gr.update(), gr.update()
    try:
        orch = _get_task_orchestrator(plan_state)
        spec = orch.status.requirement_spec
        rows = candidate_cards_to_rows(*card_values)
        selected = [row for row in rows if row[0] and float(row[4]) > float(row[3])]
        manual = _manual_segment_payloads(manual_rows, spec, len(selected))
        count = len(selected) + len(manual)
        seconds = sum(float(row[4]) - float(row[3]) for row in selected)
        seconds += sum(item["source_end"] - item["source_start"] for item in manual)
        seconds -= float(transition) * max(0, count - 1)
        seconds += 2.0 * sum(style != "none" for style in (intro, outro))
        selected_ids = {plan_state.get("candidate_labels", {}).get(row[2]) for row in selected}
        covered = {
            requirement_id for segment in orch.status.edit_plan.timeline_segments
            if segment.candidate_id in selected_ids for requirement_id in segment.matched_requirement_ids
        } | {requirement_id for item in manual for requirement_id in item["matched_requirement_ids"]}
        missing_must = any(item.priority == "must" and item.id not in covered for item in spec.requirements)
        shortage = max(0, spec.target_duration - seconds)
        text = f"当前选择 **{count} 段，预计 {seconds:.1f} 秒**；目标 **{spec.target_duration:.0f} 秒**（已扣除转场重叠）。"
        if shortage > spec.duration_tolerance:
            text += f"\n\n还差约 **{shortage:.1f} 秒**。点击“自动补选，接近目标时长”，或“＋ 自己补入片段”。"
        elif seconds > spec.target_duration + spec.duration_tolerance:
            text += "\n\n当前超过允许时长，可以删除一段或拖动边界缩短。"
        else:
            text += "\n\n✅ 当前时长在任务书允许范围内。"
        if missing_must:
            text += "\n\n⚠️ 有必须内容未保留，请补选对应内容。"
        return text, gr.update(visible=bool(count and shortage > spec.duration_tolerance and not missing_must))
    except (ValueError, TypeError, KeyError, AttributeError):
        return gr.update(), gr.update()


def create_requirement_review(
    video_path, user_input, scenario, target_duration, progress=gr.Progress(),
):
    """生成初步任务书、素材转录和对齐建议；候选分析仍等待用户确认。"""
    video_paths = _uploaded_video_paths(video_path)
    if not video_paths:
        return (
            "请先上传视频文件", None,
            *[gr.update() for _ in range(12)],
        )

    if not user_input or not user_input.strip():
        user_input = "请根据素材整理一版可审核的剪辑方案"

    progress(0.0, desc="启动中...")

    try:
        orch = _new_task_orchestrator()

        progress(0.08, desc="正在检查视频...")
        # 大白话描述里已经写了时长时，以用户的话为准；滑块只是没有写时长时的默认值。
        duration_is_explicit = bool(
            re.search(r"\d+(?:\.\d+)?\s*(?:分(?:钟|半)?|秒|s\b)", user_input, re.I)
        )
        overrides = (
            {} if duration_is_explicit else {"target_duration": int(target_duration)}
        )
        scenario_value = "school" if scenario == "学校活动" else "enterprise"
        compilation = orch.create_requirement_draft(
            video_paths,
            user_input,
            scenario=scenario_value,
            overrides=overrides,
            submitted_by="local-user",
            progress_callback=lambda fraction, message: progress(
                0.10 + 0.75 * fraction, desc=message
            ),
        )
        task_id = orch.task_dir.name
        _task_orchestrators[task_id] = orch
        spec = compilation.spec
        proposal = compilation.alignment_proposal
        needs_alignment_decision = False
        progress(1.0, desc="素材已转录，请确认需求和 AI 提问")
        summary = _requirement_review_markdown(
            spec,
            compilation.execution_brief,
            warnings=compilation.warnings,
        )
        summary = _material_set_markdown(orch) + "\n\n" + summary
        return (
            summary,
            {
                "task_id": task_id,
                "video_path": orch.video_path,
                "source_count": len(video_paths),
                "gate_id": orch.status.requirement_gate.id,
            },
            gr.update(visible=True),
            gr.update(value=_requirement_rows(spec), visible=True),
            gr.update(value=_editable_requirement_value(spec.purpose), visible=True),
            gr.update(value=_editable_requirement_value(spec.audience), visible=True),
            gr.update(value={
                "formal": "正式", "exciting": "燃向", "warm": "温馨",
                "funny": "轻松", "general": "自然清晰",
            }.get(spec.style, "自然清晰"), visible=True),
            gr.update(value=spec.need_subtitles, visible=True),
            gr.update(value=spec.need_bgm, visible=True),
            gr.update(visible=False),
            gr.update(visible=needs_alignment_decision),
            gr.update(value=_material_alignment_markdown(proposal)),
            gr.update(
                value=(proposal.suggested_requirement_text if proposal else ""),
                visible=needs_alignment_decision,
            ),
            gr.update(value=spec.target_duration),
        )

    except Exception as e:
        progress(1.0, desc="出错")
        return (
            f"## 错误\n\n{html.escape(str(e))}", None,
            *[gr.update() for _ in range(12)],
        )


def _parse_requirement_rows(requirement_rows, existing):
    """把用户可读表格转换为任务书条目，不暴露内部 ID。"""
    items = []
    for row in requirement_rows or []:
        if not row or len(row) < 3 or not str(row[2]).strip():
            continue
        try:
            row_number = int(float(row[0])) if row[0] not in {None, ""} else 0
        except (TypeError, ValueError):
            row_number = 0
        old = existing[row_number - 1] if 1 <= row_number <= len(existing) else None
        priority = PRIORITY_VALUES.get(str(row[1]).strip())
        if priority is None:
            raise ValueError("重要程度请填写：必须保留、建议保留、可有可无或禁止出现")
        acceptance = str(row[3]).strip() if len(row) > 3 and row[3] else None
        if priority in {"must", "prohibited"} and not acceptance:
            acceptance = (
                "成片中至少有一个已确认片段覆盖该要求"
                if priority == "must"
                else "最终候选和时间线不得包含该内容"
            )
        items.append({
            "id": old.id if old else None,
            "category": old.category if old else "content",
            "priority": priority,
            "description": str(row[2]).strip(),
            "acceptance_rule": acceptance,
            "status": "confirmed",
        })
    if not items:
        raise ValueError("请至少保留一条内容要求")
    return items


def revise_requirement_review(
    workflow_state, purpose, audience, target_duration, style, need_subtitles,
    requirement_rows,
):
    """将审核页修改保存成任务书新版本，旧 Gate 自动失效。"""
    if not workflow_state:
        return "请先生成需求任务书", workflow_state, gr.update(), gr.update(), gr.update()
    try:
        orch = _get_task_orchestrator(workflow_state)
        existing = list(orch.status.requirement_spec.requirements)
        items = _parse_requirement_rows(requirement_rows, existing)
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
        return (
            _requirement_review_markdown(
                revised,
                orch.status.execution_brief,
                saved=True,
            ),
            workflow_state,
            gr.update(value=_editable_requirement_value(revised.purpose), visible=True),
            gr.update(value=_editable_requirement_value(revised.audience), visible=True),
            gr.update(value=_requirement_rows(revised), visible=True),
        )
    except Exception as error:
        return f"## 任务书修改失败\n\n{html.escape(str(error))}", workflow_state, gr.update(), gr.update(), gr.update()


def confirm_and_analyze(
    workflow_state,
    purpose,
    audience,
    target_duration,
    style,
    need_subtitles,
    need_bgm,
    bgm_path,
    requirement_rows,
    allow_confirmed_override=False,
    mode_1="ignore", answer_1="",
    mode_2="ignore", answer_2="",
    mode_3="ignore", answer_3="",
    mode_4="ignore", answer_4="",
    mode_5="ignore", answer_5="",
    progress=gr.Progress(),
):
    """确认当前任务书版本后，执行转录、受限证据检索和候选生成。"""
    if not workflow_state:
        return (
            "请先创建需求任务书", gr.update(), None,
            *[gr.update() for _ in range(13)],
        )
    try:
        orch = _get_task_orchestrator(workflow_state)
        current_spec = orch.status.requirement_spec
        if not str(purpose or "").strip() or str(purpose).strip() == "待你确认":
            raise ValueError("请填写这支视频的用途")
        if not str(audience or "").strip() or str(audience).strip() == "待你确认":
            raise ValueError("请填写主要观看者")
        items = _parse_requirement_rows(
            requirement_rows, list(current_spec.requirements)
        )
        style_map = {
            "自然清晰": "general", "正式": "formal", "燃向": "exciting",
            "温馨": "warm", "轻松": "funny",
        }
        progress(0.03, desc="正在保存你确认的需求...")
        orch.revise_requirement_draft(
            updates={
                "purpose": str(purpose).strip(),
                "audience": str(audience).strip(),
                "target_duration": float(target_duration),
                "style": style_map.get(style, "general"),
                "need_subtitles": bool(need_subtitles),
                "need_bgm": bool(need_bgm or bgm_path),
            },
            requirements=items,
            actor_id="local-user",
        )
        workflow_state = {
            **workflow_state,
            "gate_id": orch.status.requirement_gate.id,
        }
        clarification_modes = [mode_1, mode_2, mode_3, mode_4, mode_5]
        clarification_texts = [answer_1, answer_2, answer_3, answer_4, answer_5]
        clarification_answers = _clarification_answer_map(
            orch,
            clarification_modes,
            clarification_texts,
        )
        progress(0.05, desc="正在确认任务书...")
        orch.confirm_requirement_draft(
            workflow_state["gate_id"],
            actor_id="local-user",
            clarification_answers=clarification_answers,
            allow_confirmed_override=bool(allow_confirmed_override),
        )
        progress(0.20, desc="转录已复用，正在一次性检索和排序候选...")
        script = orch.analyze_confirmed_requirement(
            workflow_state["video_path"], generate_previews=False
        )
        if not script.operations:
            return (
                _candidate_failure_markdown(orch),
                [],
                workflow_state,
                gr.update(visible=False),
                gr.update(choices=[], visible=False),
                *[gr.update(visible=False) for _ in range(6)],
                *[gr.update() for _ in range(5)],
            )
        progress(1.0, desc="请审核有证据的候选片段")
        state = {
            **workflow_state,
            "plan_id": orch.status.edit_plan.id,
            "plan_version": orch.status.edit_plan.version,
            "previews": {},
            "candidate_labels": _candidate_label_map(orch.status.edit_plan),
        }
        style_choices = [
            (f"推荐方案 {index}｜{proposal.rationale}", proposal.bundle_id)
            for index, proposal in enumerate(orch.status.style_proposals, start=1)
        ]
        duration_markdown, can_accept_short = _duration_action_markdown(orch)
        sources = list(orch.status.material_set.sources) if orch.status.material_set else []
        first_source = sources[0] if sources else None
        source_duration = max(0.1, first_source.duration if first_source else 0.1)
        default_end = min(15.0, source_duration)
        source_choices = [(item.filename, item.id) for item in sources]
        return (
            _review_markdown(orch),
            _timeline_rows(orch.status.edit_plan),
            state,
            gr.update(visible=True),
            gr.update(choices=style_choices, value=None, visible=bool(style_choices)),
            gr.update(visible=True),
            gr.update(value=duration_markdown),
            gr.update(visible=can_accept_short),
            gr.update(visible=True),
            gr.update(
                choices=source_choices,
                value=first_source.id if first_source else None,
                visible=bool(source_choices),
            ),
            gr.update(
                value=first_source.source_path if first_source else workflow_state["video_path"],
                visible=True,
            ),
            gr.update(minimum=0, maximum=source_duration, value=0, visible=True),
            gr.update(minimum=0, maximum=source_duration, value=default_end, visible=True),
            gr.update(choices=_manual_requirement_choices(orch.status.requirement_spec), value=[]),
            [],
            gr.update(value=_manual_segments_markdown([])),
        )
    except Exception as error:
        progress(1.0, desc="出错")
        return (
            f"## 分析失败\n\n{html.escape(str(error))}", [], workflow_state,
            gr.update(visible=False), gr.update(),
            *[gr.update(visible=False) for _ in range(6)],
            *[gr.update() for _ in range(5)],
        )


def _revise_plan_from_review_controls(
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
):
    """把当前界面选择保存为新计划版本，供本地或手机审核复用。"""
    if not plan_state:
        raise ValueError("请先生成剪辑方案")
    orch = _get_task_orchestrator(plan_state)
    selected_ids = []
    updates = {}
    candidate_labels = plan_state.get("candidate_labels", {})
    for row in timeline_rows or []:
        if len(row) < 5 or not _truthy(row[0]):
            continue
        label = str(row[2]).strip()
        candidate_id = candidate_labels.get(label)
        if not candidate_id:
            raise ValueError(f"无法识别“{label}”，请不要修改片段名称")
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
    manual_segments = _manual_segment_payloads(
        manual_segment_rows,
        orch.status.requirement_spec,
        len(selected_ids),
    )
    if not selected_ids and not manual_segments:
        raise ValueError("请至少保留或人工添加一个片段")
    revised = orch.revise_edit_plan(
        selected_candidate_ids=selected_ids,
        segment_updates=updates,
        manual_segments=manual_segments,
        delivery_updates=delivery_updates,
        actor_id="local-user",
    )
    return orch, revised


def fill_duration_from_controls(
    plan_state, timeline_rows, manual_segment_rows, style_bundle_id,
    bgm_path, bgm_volume, transition_duration, subtitle_style,
    intro_style, outro_style, title_text,
):
    """保留本页修改后自动补选；新增内容仍留在审核页，不会直接出片。"""
    try:
        orch = _get_task_orchestrator(plan_state)
        excluded = list(plan_state.get("excluded_ranges", []))
        label_map = plan_state.get("candidate_labels", {})
        by_id = {item.candidate_id: item for item in orch.status.edit_plan.timeline_segments}
        for row in timeline_rows or []:
            if len(row) < 3 or _truthy(row[0]):
                continue
            segment = by_id.get(label_map.get(str(row[2]).strip()))
            if segment:
                excluded.append({
                    "source_asset_id": segment.source_asset_id,
                    "start": segment.source_start, "end": segment.source_end,
                })
        orch, before = _revise_plan_from_review_controls(
            plan_state, timeline_rows, manual_segment_rows, style_bundle_id,
            bgm_path, bgm_volume, transition_duration, subtitle_style,
            intro_style, outro_style, title_text,
        )
        revised = orch.supplement_plan_duration(
            excluded_ranges=excluded, actor_id="local-user"
        )
        state = {
            **plan_state, "plan_version": revised.version, "previews": {},
            "candidate_labels": _candidate_label_map(revised), "excluded_ranges": excluded,
        }
        text, accept_short = _duration_action_markdown(orch)
        added = len(revised.timeline_segments) - len(before.timeline_segments)
        notice = (
            f"✅ 已补选 {added} 段，请在下方查看新增片段。原有删选、顺序与字幕修改已保留。"
            if added > 0 else
            "本轮没有找到可直接追加且不重复的完整片段。系统不会恢复你删除的内容或重复画面凑时长；可以手动补片、减少内容限制或接受短版。"
        )
        return (
            _review_markdown(orch), _timeline_rows(revised), state,
            notice + "\n\n" + text, gr.update(visible=accept_short), [],
            _manual_segments_markdown([]),
        )
    except Exception as error:
        raise gr.Error(str(error)) from error


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
    allow_short_duration=False,
):
    """只渲染用户确认后的剪辑计划。"""
    if not plan_state:
        return "请先生成剪辑方案", None, gr.update(), gr.update(), gr.update()
    try:
        progress(0.1, desc="正在应用你的片段选择...")
        orch, revised = _revise_plan_from_review_controls(
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
        )
        approved = orch.approve_current_plan(
            "local-user",
            allow_duration_exception=bool(allow_short_duration),
        )
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


def render_short_confirmed(
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
    """用户明确接受短版后渲染；其他所有计划校验仍然生效。"""
    return render_confirmed(
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
        progress=progress,
        allow_short_duration=True,
    )


def create_mobile_review_from_controls(
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
    ttl_hours,
):
    """保存当前界面版本并生成手机可访问的短期审核链接和二维码。"""
    try:
        orch, revised = _revise_plan_from_review_controls(
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
        )
        link = orch.create_mobile_review_link(
            base_url=MOBILE_REVIEW_BASE_URL,
            ttl_hours=int(ttl_hours),
            actor_id="local-user",
        )
        updated_state = {
            **plan_state,
            "plan_id": revised.id,
            "plan_version": revised.version,
        }
        return (
            "### 手机审核链接已生成\n\n"
            f"[在手机打开剪辑方案 v{revised.version}]({link.url})\n\n"
            f"链接将在 {link.expires_at.astimezone().strftime('%m月%d日 %H:%M')} 失效。"
            "请确保手机能访问上面显示的电脑地址；手机提交后，再点击“读取审核结果并生成”。",
            gr.update(value=link.qr_code_path, visible=bool(link.qr_code_path)),
            updated_state,
        )
    except Exception as error:
        return (
            f"### 手机审核链接生成失败\n\n{html.escape(str(error))}",
            gr.update(visible=False),
            plan_state,
        )


def apply_mobile_review_and_render(plan_state, progress=gr.Progress()):
    """读取当前计划版本的手机决定；只有批准后才渲染。"""
    if not plan_state:
        return "请先生成手机审核链接", None, gr.update(), gr.update(), gr.update()
    try:
        progress(0.2, desc="正在读取手机审核结果...")
        orch = _get_task_orchestrator(plan_state)
        submission = orch.apply_latest_mobile_review()
        if submission.decision != "approve":
            return (
                "## 审核者已退回修改\n\n"
                + html.escape(submission.comment or "请根据审核意见修改当前剪辑方案。"),
                None,
                gr.update(),
                gr.update(),
                gr.update(),
            )
        result = orch.render_approved_plan(
            orch.status.edit_plan,
            plan_state["video_path"],
        )
        progress(1.0, desc="完成！")
        if not result.success:
            return (
                "## 导出失败\n\n" + "\n".join(f"- {item}" for item in result.errors),
                None,
                gr.update(),
                gr.update(),
                gr.update(),
            )
        needs_resolution = bool(
            orch.status.delivery_report
            and orch.status.delivery_report.status == "needs_resolution"
        )
        return (
            f"## 手机审核已批准，渲染完成\n\n实际时长：{result.output_duration:.1f} 秒。\n\n"
            f"{_delivery_markdown(orch.status.delivery_report)}",
            result.output_path,
            gr.update(value=orch.status.edit_plan.execution_script.srt_subtitles, visible=True),
            gr.update(visible=needs_resolution, value=""),
            gr.update(visible=needs_resolution),
        )
    except Exception as error:
        return (
            f"## 尚未取得可用的手机审核结果\n\n{html.escape(str(error))}",
            None,
            gr.update(),
            gr.update(),
            gr.update(),
        )


def revoke_mobile_review(plan_state):
    if not plan_state:
        return "请先生成手机审核链接", gr.update(visible=False)
    try:
        orch = _get_task_orchestrator(plan_state)
        orch.revoke_latest_mobile_review_link(actor_id="local-user")
        return "✅ 当前手机审核链接已撤销。", gr.update(visible=False, value=None)
    except Exception as error:
        return f"撤销失败：{html.escape(str(error))}", gr.update()


def resolve_delivery_exception(plan_state, reason):
    if not plan_state:
        return "请先完成渲染", gr.update(), gr.update()
    try:
        orch = _get_task_orchestrator(plan_state)
        report = orch.resolve_delivery(actor_id="local-user", exception_reason=reason or "")
        return _delivery_markdown(report), gr.update(visible=False), gr.update(visible=False)
    except Exception as error:
        return f"## 交付异常处理失败\n\n{html.escape(str(error))}", gr.update(), gr.update()


def upload_reference_documents(plan_state, files, category_label):
    """加入活动资料，并报告可定位证据与待核对专有名词数量。"""
    if not plan_state:
        return "请先上传视频并生成任务书。"
    paths = _uploaded_video_paths(files)
    if not paths:
        return "请选择至少一份活动资料。"
    category_map = {
        "活动流程表": "agenda", "主持稿": "host_script",
        "人员与职务名单": "people", "奖项名单": "awards",
        "产品名称": "products", "组织与合作单位": "organizations",
        "专用术语": "terminology", "其他资料": "other",
    }
    try:
        orch = _get_task_orchestrator(plan_state)
        library = None
        for path in paths:
            library = orch.add_reference_document(
                path,
                category_map.get(category_label, "other"),
                actor_id="local-user",
            )
        pending = 0
        queue_path = orch.task_dir / "entity_review_queue.json"
        if queue_path.exists():
            queue = json.loads(queue_path.read_text(encoding="utf-8"))
            pending = sum(
                item.get("status") == "pending" for item in queue.get("items", [])
            )
        return (
            f"✅ 已加入 {len(paths)} 份资料；当前共有 "
            f"{len(library.evidence) if library else 0} 条可定位文字证据。"
            f"待人工核对的专有名词：{pending} 项。系统不会静默改写字幕。"
        )
    except Exception as error:
        return f"资料处理失败：{html.escape(str(error))}"


def export_editor_handoff(plan_state, adapter):
    """把已批准时间线导出为剪映交接包或 OTIO。"""
    if not plan_state:
        return "请先生成并批准剪辑方案。", None
    try:
        orch = _get_task_orchestrator(plan_state)
        result = orch.export_editor_package(adapter)
        if not result.success:
            return "导出失败：" + "；".join(result.errors), None
        if adapter == "jianying":
            archive_path = shutil.make_archive(
                str(Path(result.output_path).with_suffix("")),
                "zip",
                root_dir=result.output_path,
            )
            return "✅ 剪映交接包已生成，含顺序片段、字幕、清单和导入说明。", archive_path
        return "✅ OTIO 时间线已生成，可供兼容工具继续转换。", result.output_path
    except Exception as error:
        return f"导出失败：{html.escape(str(error))}", None


def create_ui():
    """
    创建 Gradio 界面
    """

    # 审核优先：配置区保持紧凑，证据、候选和时间线使用整页宽度。
    custom_css = """
    .gradio-container {
        max-width: 1320px !important;
        margin: auto !important;
        padding-bottom: 48px !important;
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
    #review-output {
        max-height: 720px;
        overflow-y: auto;
        overflow-x: hidden;
        padding: 20px 24px;
        border: 1px solid var(--border-color-primary);
        border-radius: 14px;
        background: var(--background-fill-secondary);
    }
    #review-output table {
        display: block;
        width: 100%;
        overflow-x: auto;
        white-space: normal;
    }
    #review-output blockquote {
        margin: 8px 0;
        padding: 10px 14px;
        border-left: 4px solid #667eea;
        background: rgba(102, 126, 234, 0.08);
    }
    #requirement-review {
        padding: 20px 24px;
        border: 1px solid rgba(102, 126, 234, 0.32);
        border-radius: 14px;
        background: var(--background-fill-secondary);
    }
    #requirement-review table {
        width: 100%;
        table-layout: fixed;
    }
    #requirement-review table th:first-child,
    #requirement-review table td:first-child {
        width: 28%;
    }
    #requirement-review h3 {
        margin-top: 22px;
    }
    .workflow-heading {
        margin-top: 24px !important;
    }
    .export-action {
        margin-top: 12px;
        padding: 14px;
        border: 1px solid rgba(102, 126, 234, 0.4);
        border-radius: 14px;
        background: rgba(102, 126, 234, 0.08);
    }
    .export-action button {
        min-height: 48px;
        font-size: 1.05rem;
        font-weight: 700;
    }
    .action-status {
        min-height: 38px;
        padding: 8px 12px;
        border-radius: 10px;
        background: rgba(102, 126, 234, 0.08);
    }
    .question-card {
        padding: 12px;
        margin: 8px 0;
        border: 1px solid var(--border-color-primary);
        border-radius: 12px;
        background: var(--background-fill-secondary);
    }
    .alignment-card {
        padding: 18px;
        margin: 14px 0;
        border: 2px solid rgba(102, 126, 234, 0.5);
        border-radius: 14px;
        background: rgba(102, 126, 234, 0.08);
    }
    .duration-decision-card {
        padding: 18px;
        margin: 14px 0;
        border: 2px solid rgba(245, 158, 11, 0.55);
        border-radius: 14px;
        background: rgba(245, 158, 11, 0.10);
    }
    .manual-add-card {
        padding: 18px;
        margin: 14px 0 20px;
        border: 1px solid rgba(102, 126, 234, 0.45);
        border-radius: 14px;
        background: rgba(102, 126, 234, 0.06);
    }
    .candidate-edit-card {
        padding: 16px;
        margin: 12px 0;
        border: 1px solid rgba(102, 126, 234, 0.35);
        border-radius: 14px;
        background: var(--background-fill-secondary);
    }
    .candidate-preview video {
        max-height: 360px;
        object-fit: contain;
        border-radius: 10px;
    }
    @media (max-width: 800px) {
        #requirement-review,
        #review-output {
            max-height: none;
            padding: 14px;
        }
    }

    /* 映证视觉系统：克制的品牌色、清晰的层级和适合长审核流程的留白。 */
    :root {
        --yz-ink: #182033;
        --yz-muted: #667085;
        --yz-line: #e5e9f2;
        --yz-indigo: #5b5ce2;
        --yz-indigo-deep: #3536a8;
        --yz-violet: #805ad5;
        --yz-shadow: 0 18px 45px rgba(28, 39, 76, 0.08);
    }
    body {
        background:
            radial-gradient(circle at 8% 0%, rgba(91, 92, 226, 0.10), transparent 28rem),
            radial-gradient(circle at 96% 8%, rgba(128, 90, 213, 0.08), transparent 24rem),
            #f6f7fb !important;
    }
    .gradio-container {
        max-width: 1260px !important;
        padding: 28px 24px 64px !important;
        color: var(--yz-ink);
    }
    .hero-shell {
        position: relative;
        overflow: hidden;
        padding: 42px 46px 38px;
        border: 1px solid rgba(255, 255, 255, 0.16);
        border-radius: 28px;
        color: #fff;
        background: linear-gradient(130deg, #171a38 0%, #3536a8 58%, #7251bd 100%);
        box-shadow: 0 28px 70px rgba(42, 44, 120, 0.22);
    }
    .hero-shell::after {
        content: "";
        position: absolute;
        width: 360px;
        height: 360px;
        right: -100px;
        top: -190px;
        border-radius: 50%;
        background: rgba(255, 255, 255, 0.10);
    }
    .hero-kicker {
        display: inline-flex;
        align-items: center;
        gap: 8px;
        margin-bottom: 18px;
        padding: 7px 12px;
        border: 1px solid rgba(255, 255, 255, 0.24);
        border-radius: 999px;
        color: rgba(255, 255, 255, 0.86);
        background: rgba(255, 255, 255, 0.08);
        font-size: 13px;
        letter-spacing: 0.08em;
    }
    .hero-title {
        position: relative;
        z-index: 1;
        margin: 0;
        color: #fff !important;
        font-size: clamp(36px, 5vw, 54px);
        line-height: 1.08;
        letter-spacing: -0.04em;
        font-weight: 760;
    }
    .hero-title span {
        color: #c9c9ff !important;
        font-weight: 620;
    }
    .hero-copy {
        position: relative;
        z-index: 1;
        max-width: 760px;
        margin: 18px 0 24px;
        color: rgba(255, 255, 255, 0.78);
        font-size: 16px;
        line-height: 1.8;
    }
    .hero-points {
        position: relative;
        z-index: 1;
        display: flex;
        flex-wrap: wrap;
        gap: 10px;
    }
    .hero-point {
        padding: 8px 12px;
        border-radius: 10px;
        color: rgba(255, 255, 255, 0.9);
        background: rgba(255, 255, 255, 0.09);
        font-size: 13px;
    }
    .process-rail {
        display: grid;
        grid-template-columns: repeat(4, 1fr);
        gap: 10px;
        margin: 18px 0 34px;
    }
    .process-item {
        display: flex;
        align-items: center;
        gap: 10px;
        padding: 12px 14px;
        border: 1px solid rgba(214, 219, 233, 0.86);
        border-radius: 13px;
        color: var(--yz-muted);
        background: rgba(255, 255, 255, 0.78);
        backdrop-filter: blur(12px);
        font-size: 13px;
        font-weight: 620;
    }
    .process-item b {
        display: grid;
        width: 24px;
        height: 24px;
        place-items: center;
        flex: 0 0 auto;
        border-radius: 8px;
        color: var(--yz-indigo);
        background: #eeeeff;
        font-size: 12px;
    }
    .workflow-heading {
        margin: 34px 0 14px !important;
    }
    .step-heading {
        display: flex;
        align-items: center;
        gap: 14px;
    }
    .step-index {
        display: grid;
        width: 46px;
        height: 46px;
        place-items: center;
        flex: 0 0 auto;
        border-radius: 14px;
        color: #fff;
        background: linear-gradient(140deg, var(--yz-indigo), var(--yz-violet));
        box-shadow: 0 10px 24px rgba(91, 92, 226, 0.22);
        font-size: 13px;
        font-weight: 760;
    }
    .step-title {
        color: var(--yz-ink);
        font-size: 22px;
        font-weight: 730;
        letter-spacing: -0.02em;
    }
    .step-description {
        margin-top: 3px;
        color: var(--yz-muted);
        font-size: 14px;
    }
    .intake-row,
    #requirement-review,
    #review-output,
    .delivery-panel {
        border: 1px solid var(--yz-line) !important;
        border-radius: 20px !important;
        background: rgba(255, 255, 255, 0.92) !important;
        box-shadow: var(--yz-shadow);
    }
    .intake-row,
    .delivery-panel {
        gap: 20px !important;
        padding: 20px !important;
    }
    .intake-column {
        min-width: min(100%, 360px) !important;
    }
    .upload-note {
        margin: 2px 4px 0;
        color: var(--yz-muted);
        font-size: 13px;
        line-height: 1.6;
    }
    .template-label {
        margin: 2px 0 -2px !important;
        color: var(--yz-muted);
        font-size: 13px;
        font-weight: 650;
    }
    .gradio-container .block,
    .gradio-container .form {
        border-color: var(--yz-line);
        border-radius: 14px;
    }
    .gradio-container textarea,
    .gradio-container input {
        line-height: 1.6;
    }
    .gradio-container button {
        border-radius: 12px !important;
        font-weight: 680 !important;
        transition: transform 160ms ease, box-shadow 160ms ease, filter 160ms ease;
    }
    .gradio-container button:hover {
        transform: translateY(-1px);
    }
    .gradio-container button.primary {
        border: 0 !important;
        background: linear-gradient(135deg, var(--yz-indigo), var(--yz-indigo-deep)) !important;
        box-shadow: 0 10px 22px rgba(73, 74, 190, 0.20);
    }
    .primary-action button {
        min-height: 50px;
        font-size: 15px !important;
    }
    #review-output {
        max-height: none;
        padding: 26px 30px;
    }
    #requirement-review {
        padding: 26px 30px;
        border-color: var(--yz-line) !important;
    }
    #review-output blockquote {
        border-left: 3px solid var(--yz-indigo);
        border-radius: 0 10px 10px 0;
        background: #f4f4ff;
    }
    #requirement-review h2,
    #review-output h2 {
        margin-top: 0;
        color: var(--yz-ink);
    }
    .export-action {
        padding: 18px 20px;
        border: 1px solid rgba(91, 92, 226, 0.24);
        border-radius: 18px;
        background: linear-gradient(135deg, #f7f7ff, #f1efff);
    }
    .action-status {
        min-height: 40px;
        padding: 9px 13px;
        border: 1px solid #e3e4ff;
        border-radius: 11px;
        color: #444a61;
        background: #f6f6ff;
    }
    .action-status p {
        margin: 0;
    }
    .question-card,
    .candidate-edit-card {
        border-color: var(--yz-line);
        border-radius: 18px;
        background: #fff;
        box-shadow: 0 12px 32px rgba(31, 43, 77, 0.06);
    }
    .question-card {
        padding: 16px;
    }
    .candidate-edit-card {
        padding: 20px;
        margin: 14px 0;
    }
    .alignment-card,
    .manual-add-card {
        padding: 20px;
        border: 1px solid rgba(91, 92, 226, 0.30);
        border-radius: 18px;
        background: linear-gradient(135deg, #fafaff, #f3f2ff);
    }
    .duration-decision-card {
        padding: 20px;
        border: 1px solid rgba(217, 139, 20, 0.38);
        border-radius: 18px;
        background: #fff9ed;
    }
    .candidate-preview video {
        border-radius: 14px;
    }
    .quiet-footer {
        margin-top: 42px;
        padding: 22px 4px 0;
        border-top: 1px solid var(--yz-line);
        color: var(--yz-muted);
        text-align: center;
        font-size: 13px;
        line-height: 1.8;
    }
    .quiet-footer b {
        color: var(--yz-ink);
    }
    footer {
        display: none !important;
    }
    @media (max-width: 800px) {
        .gradio-container {
            padding: 14px 12px 40px !important;
        }
        .hero-shell {
            padding: 30px 24px;
            border-radius: 22px;
        }
        .hero-copy {
            font-size: 14px;
        }
        .process-rail {
            grid-template-columns: repeat(2, 1fr);
        }
        .intake-row,
        .delivery-panel {
            padding: 14px !important;
        }
        #requirement-review,
        #review-output {
            padding: 18px;
        }
        .step-title {
            font-size: 19px;
        }
    }
    @media (max-width: 520px) {
        .process-rail {
            grid-template-columns: 1fr;
        }
    }
    """

    with gr.Blocks(
        title="映证｜活动视频需求与审核工作台",
        css=custom_css,
        theme=gr.themes.Soft(
            primary_hue="indigo",
            neutral_hue="slate",
            radius_size="lg",
        ),
    ) as demo:

        gr.Markdown(
            """
            <div class="hero-shell">
                <div class="hero-kicker">YINGZHENG · AI VIDEO WORKBENCH</div>
                <h1 class="hero-title">映证 <span>｜让每一次入选都有依据</span></h1>
                <p class="hero-copy">把模糊的活动视频要求整理成清晰任务书，逐段核对 AI 选片依据，确认无误后再生成可验收的粗剪。</p>
                <div class="hero-points">
                    <span class="hero-point">需求可修改</span>
                    <span class="hero-point">片段可预览</span>
                    <span class="hero-point">决策可追溯</span>
                    <span class="hero-point">成片可验收</span>
                </div>
            </div>
            """
        )
        gr.Markdown(
            """
            <div class="process-rail">
                <div class="process-item"><b>01</b> 上传与说明</div>
                <div class="process-item"><b>02</b> 确认任务书</div>
                <div class="process-item"><b>03</b> 审核候选</div>
                <div class="process-item"><b>04</b> 出片与验收</div>
            </div>
            """
        )

        gr.Markdown(
            """
            <div class="step-heading"><span class="step-index">01</span><div><div class="step-title">上传素材与说明目标</div><div class="step-description">先用大白话描述想要什么，AI 会帮你整理，不需要预先懂剪辑术语。</div></div></div>
            """,
            elem_classes=["workflow-heading"],
        )
        with gr.Row(equal_height=False, elem_classes=["intake-row"]):
            with gr.Column(scale=5, elem_classes=["intake-column"]):
                video_input = gr.File(
                    label="上传一个或多个视频",
                    file_count="multiple",
                    file_types=["video"],
                    type="filepath",
                )
                gr.Markdown(
                    "支持手机、相机和电脑素材；多段视频会分别转录和分析，只有入选片段在导出时组合。",
                    elem_classes=["upload-note"],
                )
            with gr.Column(scale=7, elem_classes=["intake-column"]):
                user_input = gr.Textbox(
                    label="简单说一句目标（可选）",
                    placeholder="例如：剪成约 3 分钟的精华版",
                    lines=3,
                )
                with gr.Row():
                    scenario = gr.Radio(
                        choices=["学校活动", "企业活动"],
                        value="学校活动",
                        label="应用场景",
                    )
                    target_duration = gr.Slider(
                        minimum=30,
                        maximum=600,
                        value=300,
                        step=30,
                        label="目标时长（秒）",
                    )
                submit_btn = gr.Button(
                    "理解素材并整理需求",
                    variant="primary",
                    size="lg",
                    elem_classes=["primary-action"],
                )
                requirement_action_status = gr.Markdown(
                    "点击后会在这里显示处理状态。",
                    elem_classes=["action-status"],
                )

        plan_state = gr.State(value=None)
        requirement_heading = gr.Markdown(
            """
            <div class="step-heading"><span class="step-index">02</span><div><div class="step-title">确认任务书与执行依据</div><div class="step-description">检查 AI 是否准确理解用途、受众和必须保留的内容，确认后才会分析素材。</div></div></div>
            """,
            elem_classes=["workflow-heading"],
            visible=False,
        )
        requirement_result_text = gr.Markdown(
            value="等待上传视频并生成任务书...",
            elem_id="requirement-review",
            visible=False,
        )

        with gr.Group(visible=False, elem_classes=["alignment-card"]) as alignment_group:
            alignment_result_text = gr.Markdown()
            alignment_editor = gr.Textbox(
                label="AI 建议的需求（可以先修改）",
                lines=6,
                info="这只是建议，选择前不会修改任务书。",
            )
            with gr.Row():
                adopt_alignment_btn = gr.Button("✅ 采用这个建议", variant="primary")
                edit_alignment_btn = gr.Button("✏️ 修改后采用")
                keep_alignment_btn = gr.Button("保持原任务书")

        clarification_groups = []
        clarification_modes = []
        clarification_answers = []
        clarification_confirm_buttons = []
        clarification_confirmation_statuses = []
        clarification_indices = []
        clarification_confirmation_state = gr.State(value={})
        for index in range(5):
            with gr.Group(visible=False, elem_classes=["question-card"]) as question_group:
                mode = gr.Radio(
                    choices=[("暂时忽略", "ignore"), ("补充说明", "supplement")],
                    value="ignore",
                    label=f"问题 {index + 1}",
                    info="忽略不会阻止继续；系统会把该项记录为暂不确定，后续验收时仍可人工复核。",
                    visible=False,
                )
                answer = gr.Textbox(
                    label="选择“补充说明”时填写",
                    placeholder="用一句话说明即可；选择暂时忽略时可以留空。",
                    lines=2,
                    visible=False,
                )
                confirm_question_btn = gr.Button("确认本题", variant="secondary", visible=False)
                question_confirmation_status = gr.Markdown(
                    "",
                    elem_classes=["action-status"],
                    visible=False,
                )
                clarification_groups.append(question_group)
                clarification_modes.append(mode)
                clarification_answers.append(answer)
                clarification_confirm_buttons.append(confirm_question_btn)
                clarification_confirmation_statuses.append(question_confirmation_status)
                clarification_indices.append(gr.State(value=index))
        clarification_override = gr.Checkbox(
            label="出现内容冲突时，明确用本次补充替换之前确认的内容",
            value=False,
            visible=False,
        )
        with gr.Row():
            purpose_editor = gr.Textbox(
                label="成片用途（必填）",
                placeholder="例如：学校运动会公众号回顾",
                visible=False,
            )
            audience_editor = gr.Textbox(
                label="观看对象（必填）",
                placeholder="例如：师生与家长",
                visible=False,
            )
        with gr.Row():
            style = gr.Dropdown(
                choices=["自然清晰", "正式", "燃向", "温馨", "轻松"],
                value="自然清晰",
                label="整体感觉",
                info="AI 会给出初始建议，你可以直接改。",
                visible=False,
            )
            need_subtitles = gr.Checkbox(
                value=True,
                label="生成中文字幕",
                visible=False,
            )
            need_bgm = gr.Checkbox(
                value=False,
                label="添加背景音乐",
                visible=False,
            )
        bgm_input = gr.Audio(
            label="上传背景音乐（可选；请确认拥有使用权）",
            sources=["upload"],
            type="filepath",
            visible=False,
        )
        requirement_editor = gr.Dataframe(
            headers=["序号", "重要程度", "要求内容", "如何验收"],
            datatype=["number", "str", "str", "str"],
            type="array",
            row_count=(1, "dynamic"),
            col_count=(4, "fixed"),
            label="可编辑的内容要求（重要程度：必须保留、建议保留、可有可无或禁止出现）",
            interactive=True,
            visible=False,
            wrap=True,
        )
        with gr.Row():
            revise_requirement_btn = gr.Button("💾 保存任务书修改", visible=False)
            confirm_requirement_btn = gr.Button(
                "✅ 确认这些需求并生成方案",
                variant="primary",
                visible=False,
            )
        analysis_action_status = gr.Markdown(
            "确认任务书后，这里会持续提示素材分析状态。",
            elem_classes=["action-status"],
            visible=False,
        )

        with gr.Accordion(
            "📎 活动流程表、名单与术语资料（可选）",
            open=False,
            visible=False,
        ) as reference_accordion:
            gr.Markdown(
                "可在任务书确认前上传。资料会保留行或单元格出处，只用于检索和生成待确认建议。"
            )
            reference_category = gr.Dropdown(
                choices=[
                    "活动流程表", "主持稿", "人员与职务名单", "奖项名单",
                    "产品名称", "组织与合作单位", "专用术语", "其他资料",
                ],
                value="活动流程表",
                label="资料类型",
            )
            reference_files = gr.File(
                label="上传 TXT、Markdown、CSV 或 JSON",
                file_count="multiple",
                file_types=[".txt", ".md", ".csv", ".json"],
                type="filepath",
            )
            add_reference_btn = gr.Button("加入本次任务的参考资料")
            reference_status = gr.Markdown("尚未上传活动资料。")

        with gr.Accordion(
            "🎨 高级：片头、字幕外观与声音细节",
            open=False,
            visible=False,
        ) as advanced_style_accordion:
            gr.Markdown("任务书中的整体感觉和是否添加字幕、音乐已在上方确认；这里只调表现细节。")
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
            bgm_volume = gr.Slider(
                minimum=0.0,
                maximum=0.5,
                value=0.15,
                step=0.05,
                label="背景音乐音量",
            )

        candidate_heading = gr.Markdown(
            """
            <div class="step-heading"><span class="step-index">03</span><div><div class="step-title">审核候选片段与时间线</div><div class="step-description">边看片段、边看依据；保留、删除、排序和补片都在这里完成。</div></div></div>
            """,
            elem_classes=["workflow-heading"],
            visible=False,
        )
        candidate_result_text = gr.Markdown(
            value="等待确认任务书并分析素材...",
            elem_id="review-output",
            visible=False,
        )
        with gr.Group(visible=False, elem_classes=["duration-decision-card"]) as duration_action_group:
            duration_action_text = gr.Markdown()
            with gr.Row():
                autofill_duration_btn = gr.Button("自动补选，接近目标时长", variant="primary")
                open_manual_btn = gr.Button("＋ 自己补入片段")
            accept_short_btn = gr.Button(
                "✅ 接受当前时长并生成成片",
                variant="secondary",
                size="lg",
                visible=False,
            )
        with gr.Accordion(
            "从原素材补入片段（可选）",
            open=False,
            visible=False,
            elem_classes=["manual-add-card"],
            elem_id="manual-add",
        ) as manual_segment_group:
            gr.Markdown(
                "只有现有方案缺少特定内容时才需要使用。播放原视频定位内容，"
                "再拖动开始和结束滑块选择范围；不需要手写时间。"
            )
            manual_source_selector = gr.Dropdown(
                choices=[],
                label="先选择要补片的来源素材",
                visible=False,
            )
            manual_source_preview = gr.Video(
                label="原素材｜拖动进度条寻找要补入的内容",
                autoplay=False,
                visible=True,
            )
            with gr.Row():
                manual_range_start = gr.Slider(
                    minimum=0,
                    maximum=1,
                    value=0,
                    step=0.1,
                    label="拖动选择开始位置",
                )
                manual_range_end = gr.Slider(
                    minimum=0,
                    maximum=1,
                    value=1,
                    step=0.1,
                    label="拖动选择结束位置",
                )
            manual_range_status = gr.Markdown(_manual_range_markdown(0, 1))
            manual_requirement_selector = gr.CheckboxGroup(
                choices=[],
                label="这段内容对应哪些任务书要求？",
                info="直接勾选，不需要填写要求序号。",
            )
            manual_annotation = gr.Textbox(
                label="为什么补入（可选）",
                placeholder="例如：补足获奖者领奖画面",
            )
            with gr.Accordion("字幕和片段标题（可选）", open=False):
                manual_subtitle = gr.Textbox(label="字幕修正")
                manual_title = gr.Textbox(label="片段小标题")
            with gr.Row():
                add_manual_segment_btn = gr.Button("➕ 将选中片段加入时间线", variant="primary")
                undo_manual_segment_btn = gr.Button("撤销上一次补入")
            manual_segment_editor = gr.State(value=[])
            manual_segment_summary = gr.Markdown(
                _manual_segments_markdown([]),
                elem_classes=["action-status"],
            )
        style_proposal_selector = gr.Dropdown(
            label="AI 推荐样式（可选）",
            info="只展示推荐理由；不选择则使用上方的手动设置。",
            visible=False,
        )
        transition_duration = gr.Radio(
            choices=[
                ("直接切换｜节奏清晰", 0.0),
                ("柔和淡化｜自然衔接", 0.35),
                ("缓慢淡化｜更舒缓", 0.7),
            ],
            value=0.35,
            label="片段之间怎么衔接？",
            visible=False,
        )
        timeline_state = gr.State(value=[])
        candidate_groups = []
        candidate_headers = []
        candidate_previews = []
        candidate_details = []
        candidate_preview_buttons = []
        candidate_preview_orders = []
        candidate_decisions = []
        candidate_orders = []
        candidate_starts = []
        candidate_ends = []
        candidate_subtitles = []
        candidate_titles = []
        candidate_context_origins = []
        candidate_boundary_statuses = []
        candidate_mark_starts = []
        candidate_mark_ends = []
        candidate_play_ranges = []
        for index in range(MAX_CANDIDATE_CARDS):
            with gr.Group(visible=False, elem_classes=["candidate-edit-card"]) as candidate_group:
                candidate_header = gr.Markdown()
                with gr.Accordion("为什么推荐与证据", open=False):
                    candidate_detail = gr.Markdown()
                with gr.Row(equal_height=False):
                    with gr.Column(scale=3):
                        with gr.Row():
                            candidate_decision = gr.Radio(
                                choices=[("保留到成片", "keep"), ("不使用", "drop")],
                                value="keep",
                                label="这个片段怎么处理？",
                            )
                            candidate_order = gr.Number(
                                value=index + 1,
                                precision=0,
                                label="在成片中的顺序",
                            )
                        with gr.Accordion("看着画面调整开头和结尾", open=False):
                            gr.Markdown("先加载右侧预览，再拖动滑块；画面会同步定位。也可以暂停在想要的画面，用右侧按钮设为剪辑点。")
                            candidate_start = gr.Slider(minimum=0, maximum=1, value=0, step=0.1, label="拖动调整开头")
                            candidate_end = gr.Slider(minimum=0, maximum=1, value=1, step=0.1, label="拖动调整结尾")
                            candidate_boundary_status = gr.Markdown()
                        candidate_subtitle = gr.Textbox(
                            label="字幕",
                            placeholder="留空则沿用语音识别字幕；需要纠错时直接填写。",
                            lines=2,
                        )
                        candidate_title = gr.Textbox(
                            label="片段小标题（可选）",
                            placeholder="例如：校长致辞、颁奖时刻",
                        )
                    with gr.Column(scale=2, min_width=320):
                        candidate_preview_button = gr.Button("加载片段预览与边界")
                        candidate_preview = gr.Video(
                            label=f"片段 {index + 1}（含前后 5 秒，便于微调）",
                            autoplay=False,
                            visible=False,
                            elem_classes=["candidate-preview"],
                            elem_id=f"candidate-video-{index}",
                        )
                        candidate_play_range = gr.Button("播放选中部分")
                        with gr.Row():
                            candidate_mark_start = gr.Button("当前画面设为开头")
                            candidate_mark_end = gr.Button("当前画面设为结尾")
                        # 该值也供浏览器定位播放器使用，不能用仅保存在服务端的 gr.State。
                        candidate_context_origin = gr.Number(value=0.0, visible=False)
                candidate_groups.append(candidate_group)
                candidate_headers.append(candidate_header)
                candidate_previews.append(candidate_preview)
                candidate_details.append(candidate_detail)
                candidate_preview_buttons.append(candidate_preview_button)
                candidate_preview_orders.append(gr.State(value=index + 1))
                candidate_decisions.append(candidate_decision)
                candidate_orders.append(candidate_order)
                candidate_starts.append(candidate_start)
                candidate_ends.append(candidate_end)
                candidate_subtitles.append(candidate_subtitle)
                candidate_titles.append(candidate_title)
                candidate_context_origins.append(candidate_context_origin)
                candidate_boundary_statuses.append(candidate_boundary_status)
                candidate_mark_starts.append(candidate_mark_start)
                candidate_mark_ends.append(candidate_mark_end)
                candidate_play_ranges.append(candidate_play_range)
        candidate_card_outputs = [
            *candidate_groups, *candidate_headers, *candidate_previews, *candidate_details,
            *candidate_decisions, *candidate_orders, *candidate_starts, *candidate_ends,
            *candidate_subtitles, *candidate_titles, *candidate_context_origins,
            *candidate_boundary_statuses,
        ]
        with gr.Group(
            visible=False,
            elem_classes=["export-action"],
        ) as candidate_export_group:
            gr.Markdown("**确认候选、时间线和样式后生成成片。系统会保存审核版本，并在导出后逐项验收。**")
            export_btn = gr.Button(
                "确认方案并生成成片",
                variant="primary",
                size="lg",
                visible=False,
                elem_classes=["primary-action"],
            )
            with gr.Accordion("也可以发到手机审核", open=False):
                gr.Markdown(
                    "系统会先保存你在本页的片段、顺序、字幕和样式，再生成只绑定这个版本的短期链接。"
                    "审核者只能批准或退回，不能在手机上剪片。"
                )
                mobile_ttl = gr.Radio(
                    choices=[("24 小时", 24), ("3 天", 72), ("7 天", 168)],
                    value=24,
                    label="链接有效期",
                )
                with gr.Row():
                    create_mobile_review_btn = gr.Button("生成手机审核链接")
                    apply_mobile_review_btn = gr.Button("读取审核结果并生成")
                    revoke_mobile_review_btn = gr.Button("撤销链接")
                mobile_review_status = gr.Markdown(
                    "需要同时启动手机审核服务，并把手机和电脑连接到可互相访问的网络。"
                )
                mobile_review_qr = gr.Image(
                    label="手机扫码审核",
                    visible=False,
                    interactive=False,
                    height=220,
                )

        delivery_heading = gr.Markdown(
            """
            <div class="step-heading"><span class="step-index">04</span><div><div class="step-title">查看成片与逐项验收</div><div class="step-description">最终视频和验收结论并排呈现；有异常时可以返回修改或记录例外。</div></div></div>
            """,
            elem_classes=["workflow-heading"],
            visible=False,
        )
        with gr.Row(
            equal_height=False,
            visible=False,
            elem_classes=["delivery-panel"],
        ) as delivery_panel:
            with gr.Column(scale=5):
                delivery_result_text = gr.Markdown(value="等待生成成片...")
                exception_reason = gr.Textbox(
                    label="交付例外原因",
                    placeholder="仅在逐项验收存在问题时填写；也可以修改时间线后重新生成。",
                    lines=3,
                    visible=False,
                )
                approve_exception_btn = gr.Button(
                    "接受例外并批准交付",
                    variant="stop",
                    visible=False,
                )
            with gr.Column(scale=7):
                output_video = gr.Video(label="成品视频", autoplay=False)
                subtitle_output = gr.Textbox(
                    label="SRT 字幕",
                    info="可以复制保存为 .srt 文件",
                    lines=5,
                    visible=False,
                )

        with gr.Group(
            visible=False,
            elem_classes=["export-action"],
        ) as editor_export_group:
            gr.Markdown(
                "### 交给其他剪辑软件继续精修\n"
                "剪映使用稳定交接包；Premiere、Resolve 等可从 OTIO 继续转换。"
            )
            with gr.Row():
                export_jianying_btn = gr.Button("下载剪映交接包")
                export_otio_btn = gr.Button("下载 OTIO 时间线")
            editor_export_status = gr.Markdown("请先批准并生成当前方案。")
            editor_export_file = gr.File(label="编辑器交接文件")

        gr.Markdown(
            """
            <div class="quiet-footer"><b>映证 MVP2</b> · 本地处理 · 人工审核 · 全程留痕<br>Python · OpenAI-compatible LLM · Faster-Whisper · FFmpeg · Gradio</div>
            """
        )

        # 绑定事件
        review_card_inputs = [component for fields in zip(
            candidate_decisions, candidate_orders, candidate_starts,
            candidate_ends, candidate_subtitles, candidate_titles,
        ) for component in fields]
        live_duration_inputs = [
            plan_state, transition_duration, intro_style, outro_style,
            manual_segment_editor, *review_card_inputs,
        ]
        for component in [
            *candidate_decisions, *candidate_starts, *candidate_ends,
            transition_duration, intro_style, outro_style,
        ]:
            component.input(
                fn=live_review_duration, inputs=live_duration_inputs,
                outputs=[duration_action_text, accept_short_btn], queue=False, api_name=False,
            )
        manual_segment_editor.change(
            fn=live_review_duration, inputs=live_duration_inputs,
            outputs=[duration_action_text, accept_short_btn], queue=False, api_name=False,
        )
        submit_started = submit_btn.click(
            fn=begin_requirement_generation,
            inputs=[],
            outputs=[requirement_action_status, submit_btn],
            queue=False,
            api_name=False,
        )
        submit_completed = submit_started.then(
            fn=create_requirement_review,
            inputs=[video_input, user_input, scenario, target_duration],
            outputs=[
                requirement_result_text,
                plan_state,
                confirm_requirement_btn,
                requirement_editor,
                purpose_editor,
                audience_editor,
                style,
                need_subtitles,
                need_bgm,
                revise_requirement_btn,
                alignment_group,
                alignment_result_text,
                alignment_editor,
                target_duration,
            ],
            api_name=False,
        )
        submit_completed.then(
            fn=reveal_requirement_stage,
            inputs=[],
            outputs=[
                requirement_heading,
                requirement_result_text,
                bgm_input,
                analysis_action_status,
                reference_accordion,
                advanced_style_accordion,
            ],
            queue=False,
            api_name=False,
        )
        questions_loaded = submit_completed.then(
            fn=load_clarification_questions,
            inputs=[plan_state],
            outputs=[
                clarification_override,
                *clarification_groups,
                *clarification_modes,
                *clarification_answers,
                clarification_confirmation_state,
                *clarification_confirmation_statuses,
            ],
            api_name=False,
        )
        add_reference_btn.click(
            fn=upload_reference_documents,
            inputs=[plan_state, reference_files, reference_category],
            outputs=[reference_status],
            api_name=False,
        )
        questions_loaded.then(
            fn=finish_requirement_generation,
            inputs=[],
            outputs=[requirement_action_status, submit_btn],
            queue=False,
            api_name=False,
        )
        alignment_decision_outputs = [
            requirement_result_text,
            plan_state,
            requirement_editor,
            purpose_editor,
            audience_editor,
            target_duration,
            style,
            need_subtitles,
            confirm_requirement_btn,
            revise_requirement_btn,
            alignment_group,
        ]
        for alignment_button, alignment_handler in (
            (adopt_alignment_btn, adopt_alignment),
            (edit_alignment_btn, edit_and_adopt_alignment),
            (keep_alignment_btn, keep_original_alignment),
        ):
            alignment_decided = alignment_button.click(
                fn=alignment_handler,
                inputs=[plan_state, alignment_editor],
                outputs=alignment_decision_outputs,
                api_name=False,
            )
            alignment_decided.then(
                fn=load_clarification_questions,
                inputs=[plan_state],
                outputs=[
                    clarification_override,
                    *clarification_groups,
                    *clarification_modes,
                    *clarification_answers,
                    clarification_confirmation_state,
                    *clarification_confirmation_statuses,
                ],
                api_name=False,
            )
        requirement_revised = revise_requirement_btn.click(
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
                requirement_result_text,
                plan_state,
                purpose_editor,
                audience_editor,
                requirement_editor,
            ],
            api_name=False,
        )
        requirement_revised.then(
            fn=load_clarification_questions,
            inputs=[plan_state],
            outputs=[
                clarification_override,
                *clarification_groups,
                *clarification_modes,
                *clarification_answers,
                clarification_confirmation_state,
                *clarification_confirmation_statuses,
            ],
            api_name=False,
        )
        for index in range(5):
            clarification_confirm_buttons[index].click(
                fn=confirm_clarification_choice,
                inputs=[
                    plan_state,
                    clarification_confirmation_state,
                    clarification_indices[index],
                    clarification_modes[index],
                    clarification_answers[index],
                ],
                outputs=[
                    clarification_confirmation_state,
                    clarification_confirmation_statuses[index],
                ],
                queue=False,
                api_name=False,
            )
            for component in (clarification_modes[index], clarification_answers[index]):
                component.change(
                    fn=reset_clarification_choice,
                    inputs=[clarification_confirmation_state, clarification_indices[index]],
                    outputs=[
                        clarification_confirmation_state,
                        clarification_confirmation_statuses[index],
                    ],
                    queue=False,
                    api_name=False,
                )
        analysis_started = confirm_requirement_btn.click(
            fn=begin_material_analysis,
            inputs=[],
            outputs=[analysis_action_status, confirm_requirement_btn],
            queue=False,
            api_name=False,
        )
        analysis_completed = analysis_started.then(
            fn=confirm_and_analyze,
            inputs=[
                plan_state,
                purpose_editor,
                audience_editor,
                target_duration,
                style,
                need_subtitles,
                need_bgm,
                bgm_input,
                requirement_editor,
                clarification_override,
                *[
                    component
                    for pair in zip(clarification_modes, clarification_answers)
                    for component in pair
                ],
            ],
            outputs=[
                candidate_result_text,
                timeline_state,
                plan_state,
                export_btn,
                style_proposal_selector,
                duration_action_group,
                duration_action_text,
                accept_short_btn,
                manual_segment_group,
                manual_source_selector,
                manual_source_preview,
                manual_range_start,
                manual_range_end,
                manual_requirement_selector,
                manual_segment_editor,
                manual_segment_summary,
            ],
            api_name=False,
        )
        candidate_cards_loaded = analysis_completed.then(
            fn=load_candidate_cards,
            inputs=[plan_state],
            outputs=candidate_card_outputs,
            api_name=False,
        )
        candidate_cards_loaded.then(
            fn=finish_material_analysis,
            inputs=[],
            outputs=[analysis_action_status, confirm_requirement_btn],
            queue=False,
            api_name=False,
        )
        candidate_cards_loaded.then(
            fn=reveal_candidate_stage,
            inputs=[],
            outputs=[
                candidate_heading,
                candidate_result_text,
                transition_duration,
                candidate_export_group,
            ],
            queue=False,
            api_name=False,
        )
        for index in range(MAX_CANDIDATE_CARDS):
            candidate_preview_buttons[index].click(
                fn=load_candidate_preview,
                inputs=[plan_state, candidate_preview_orders[index]],
                outputs=[candidate_previews[index]],
                api_name=False,
            )
            boundary_inputs = [
                plan_state, candidate_preview_orders[index], candidate_starts[index],
                candidate_ends[index], candidate_context_origins[index],
            ]
            for component, position in (
                (candidate_starts[index], "start - origin"),
                (candidate_ends[index], "end - origin - 0.15"),
            ):
                seek_js = """(state, order, start, end, origin) => {
                    const video = document.querySelector('#candidate-video-__INDEX__ video');
                    if (video) { video.pause(); video.currentTime = Math.max(0, __POSITION__); }
                    return [state, order, start, end, origin];
                }""".replace("__INDEX__", str(index)).replace("__POSITION__", position)
                component.input(
                    fn=describe_candidate_boundary, inputs=boundary_inputs,
                    outputs=[candidate_boundary_statuses[index]], js=seek_js,
                    queue=False, api_name=False,
                )
            for button, output in (
                (candidate_mark_starts[index], candidate_starts[index]),
                (candidate_mark_ends[index], candidate_ends[index]),
            ):
                marked = button.click(
                    fn=lambda origin, position: position,
                    inputs=[candidate_context_origins[index], output], outputs=[output],
                    js="""(origin, current) => {
                        const video = document.querySelector('#candidate-video-__INDEX__ video');
                        const range = video?.closest('.candidate-edit-card')?.querySelector('input[type="range"]');
                        let position = video ? Math.round((origin + video.currentTime) * 100) / 100 : current;
                        if (range) position = Math.max(Number(range.min), Math.min(Number(range.max), position));
                        return [origin, position];
                    }""".replace("__INDEX__", str(index)),
                    queue=False, api_name=False,
                )
                marked.then(
                    fn=describe_candidate_boundary, inputs=boundary_inputs,
                    outputs=[candidate_boundary_statuses[index]], queue=False, api_name=False,
                )
                marked.then(
                    fn=live_review_duration, inputs=live_duration_inputs,
                    outputs=[duration_action_text, accept_short_btn], queue=False, api_name=False,
                )
            candidate_play_ranges[index].click(
                fn=None,
                inputs=[candidate_starts[index], candidate_ends[index], candidate_context_origins[index]],
                outputs=[],
                js="""(start, end, origin) => {
                    const video = document.querySelector('#candidate-video-__INDEX__ video');
                    if (video && end > start) {
                        video.currentTime = Math.max(0, start - origin);
                        video.ontimeupdate = () => {
                            if (video.currentTime >= end - origin) { video.pause(); video.ontimeupdate = null; }
                        };
                        video.play().catch(() => {});
                    }
                    return [];
                }""".replace("__INDEX__", str(index)),
                queue=False, api_name=False,
            )
        open_manual_btn.click(
            fn=lambda: gr.update(visible=True, open=True),
            inputs=[], outputs=[manual_segment_group], queue=False, api_name=False,
        )
        autofill_rows = autofill_duration_btn.click(
            fn=candidate_cards_to_rows,
            inputs=[component for fields in zip(
                candidate_decisions, candidate_orders, candidate_starts,
                candidate_ends, candidate_subtitles, candidate_titles,
            ) for component in fields],
            outputs=[timeline_state], queue=False, api_name=False,
        )
        autofill_completed = autofill_rows.then(
            fn=fill_duration_from_controls,
            inputs=[
                plan_state, timeline_state, manual_segment_editor, style_proposal_selector,
                bgm_input, bgm_volume, transition_duration, subtitle_style,
                intro_style, outro_style, title_text,
            ],
            outputs=[
                candidate_result_text, timeline_state, plan_state, duration_action_text,
                accept_short_btn, manual_segment_editor, manual_segment_summary,
            ],
            api_name=False,
        )
        autofill_completed.then(
            fn=load_candidate_cards, inputs=[plan_state], outputs=candidate_card_outputs,
            api_name=False,
        )
        for range_component in (manual_range_start, manual_range_end):
            range_component.change(
                fn=_manual_range_markdown,
                inputs=[manual_range_start, manual_range_end],
                outputs=[manual_range_status],
                queue=False,
                api_name=False,
            )
        manual_source_selector.change(
            fn=select_manual_source,
            inputs=[plan_state, manual_source_selector],
            outputs=[manual_source_preview, manual_range_start, manual_range_end],
            queue=False,
            api_name=False,
        )
        add_manual_segment_btn.click(
            fn=append_manual_segment,
            inputs=[
                manual_segment_editor,
                manual_range_start,
                manual_range_end,
                manual_requirement_selector,
                manual_annotation,
                manual_subtitle,
                manual_title,
                plan_state,
                manual_source_selector,
            ],
            outputs=[manual_segment_editor, manual_segment_summary],
            queue=False,
            api_name=False,
        )
        undo_manual_segment_btn.click(
            fn=undo_manual_segment,
            inputs=[manual_segment_editor],
            outputs=[manual_segment_editor, manual_segment_summary],
            queue=False,
            api_name=False,
        )
        candidate_rows_collected = export_btn.click(
            fn=candidate_cards_to_rows,
            inputs=[
                component
                for fields in zip(
                    candidate_decisions,
                    candidate_orders,
                    candidate_starts,
                    candidate_ends,
                    candidate_subtitles,
                    candidate_titles,
                )
                for component in fields
            ],
            outputs=[timeline_state],
            queue=False,
            api_name=False,
        )
        render_completed = candidate_rows_collected.then(
            fn=render_confirmed,
            inputs=[
                plan_state,
                timeline_state,
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
                delivery_result_text,
                output_video,
                subtitle_output,
                exception_reason,
                approve_exception_btn,
            ],
            api_name=False,
        )
        render_completed.then(
            fn=reveal_delivery_stage,
            inputs=[],
            outputs=[delivery_heading, delivery_panel, editor_export_group],
            queue=False,
            api_name=False,
        )
        mobile_candidate_rows_collected = create_mobile_review_btn.click(
            fn=candidate_cards_to_rows,
            inputs=[
                component
                for fields in zip(
                    candidate_decisions,
                    candidate_orders,
                    candidate_starts,
                    candidate_ends,
                    candidate_subtitles,
                    candidate_titles,
                )
                for component in fields
            ],
            outputs=[timeline_state],
            queue=False,
            api_name=False,
        )
        mobile_candidate_rows_collected.then(
            fn=create_mobile_review_from_controls,
            inputs=[
                plan_state,
                timeline_state,
                manual_segment_editor,
                style_proposal_selector,
                bgm_input,
                bgm_volume,
                transition_duration,
                subtitle_style,
                intro_style,
                outro_style,
                title_text,
                mobile_ttl,
            ],
            outputs=[mobile_review_status, mobile_review_qr, plan_state],
            api_name=False,
        )
        mobile_render_completed = apply_mobile_review_btn.click(
            fn=apply_mobile_review_and_render,
            inputs=[plan_state],
            outputs=[
                delivery_result_text,
                output_video,
                subtitle_output,
                exception_reason,
                approve_exception_btn,
            ],
            api_name=False,
        )
        mobile_render_completed.then(
            fn=reveal_delivery_stage,
            inputs=[],
            outputs=[delivery_heading, delivery_panel, editor_export_group],
            queue=False,
            api_name=False,
        )
        revoke_mobile_review_btn.click(
            fn=revoke_mobile_review,
            inputs=[plan_state],
            outputs=[mobile_review_status, mobile_review_qr],
            queue=False,
            api_name=False,
        )
        short_candidate_rows_collected = accept_short_btn.click(
            fn=candidate_cards_to_rows,
            inputs=[
                component
                for fields in zip(
                    candidate_decisions,
                    candidate_orders,
                    candidate_starts,
                    candidate_ends,
                    candidate_subtitles,
                    candidate_titles,
                )
                for component in fields
            ],
            outputs=[timeline_state],
            queue=False,
            api_name=False,
        )
        short_render_completed = short_candidate_rows_collected.then(
            fn=render_short_confirmed,
            inputs=[
                plan_state,
                timeline_state,
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
                delivery_result_text,
                output_video,
                subtitle_output,
                exception_reason,
                approve_exception_btn,
            ],
            api_name=False,
        )
        short_render_completed.then(
            fn=reveal_delivery_stage,
            inputs=[],
            outputs=[delivery_heading, delivery_panel, editor_export_group],
            queue=False,
            api_name=False,
        )
        approve_exception_btn.click(
            fn=resolve_delivery_exception,
            inputs=[plan_state, exception_reason],
            outputs=[delivery_result_text, exception_reason, approve_exception_btn],
            api_name=False,
        )
        export_jianying_btn.click(
            fn=lambda state: export_editor_handoff(state, "jianying"),
            inputs=[plan_state],
            outputs=[editor_export_status, editor_export_file],
            api_name=False,
        )
        export_otio_btn.click(
            fn=lambda state: export_editor_handoff(state, "otio"),
            inputs=[plan_state],
            outputs=[editor_export_status, editor_export_file],
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
        server_name=GRADIO_SERVER_NAME,
        server_port=GRADIO_SERVER_PORT,
        share=False,  # 改成 True 可以生成公网链接
        show_error=True,
    )
