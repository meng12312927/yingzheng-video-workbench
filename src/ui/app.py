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
from src.tools.ffmpeg import FFmpegTool  # noqa: E402


# 运行时组件只加载一次；每个任务拥有独立 Orchestrator 状态和任务目录。
_runtime = None
_task_orchestrators = {}
MAX_CANDIDATE_CARDS = 12


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


def _requirement_review_markdown(spec, execution, warnings=None, saved=False):
    """生成统一的用户审核视图，不暴露内部枚举、ID 或提示词字段。"""
    warning_block = ""
    if warnings:
        warning_lines = "\n".join(f"- {html.escape(warning)}" for warning in warnings)
        warning_block = f"""
> **这份任务书需要你重点核对**
>
> AI 本次没有完整解析原始需求，已生成可手动修改的基础版本。

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

请确认下面的目标和内容取舍是否符合你的意思。确认前，AI 不会开始分析整段素材。{saved_notice}

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

确认无误后，点击下方“确认任务书并分析素材”。
"""


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
            gr.update(value=spec.purpose, visible=True),
            gr.update(value=spec.audience, visible=True),
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
    hidden_decisions = [gr.update(value="drop") for _ in range(MAX_CANDIDATE_CARDS)]
    hidden_numbers = [gr.update(value=0) for _ in range(MAX_CANDIDATE_CARDS)]
    hidden_text = [gr.update(value="") for _ in range(MAX_CANDIDATE_CARDS)]
    if not plan_state:
        return (
            *hidden_groups,
            *hidden_headers,
            *hidden_previews,
            *hidden_decisions,
            *hidden_numbers,
            *hidden_numbers,
            *hidden_numbers,
            *hidden_text,
            *hidden_text,
        )
    preview_paths = {}
    try:
        orch = _get_task_orchestrator(plan_state)
        plan = orch.status.edit_plan
        segments = list(plan.timeline_segments) if plan else []
        preview_paths = plan_state.get("previews", {}) or orch.preview_paths
    except Exception:
        segments = []
    group_updates = []
    header_updates = []
    preview_updates = []
    decision_updates = []
    order_updates = []
    start_updates = []
    end_updates = []
    subtitle_updates = []
    title_updates = []
    for index in range(MAX_CANDIDATE_CARDS):
        if index < len(segments):
            segment = segments[index]
            duration = segment.source_end - segment.source_start
            group_updates.append(gr.update(visible=True))
            header_updates.append(
                gr.update(
                    value=(
                        f"### 片段 {index + 1}\n"
                        f"原素材 {_format_seconds(segment.source_start)}–"
                        f"{_format_seconds(segment.source_end)}，约 {duration:.1f} 秒"
                    )
                )
            )
            preview_path = preview_paths.get(str(segment.order))
            preview_updates.append(
                gr.update(value=preview_path, visible=bool(preview_path))
            )
            decision_updates.append(gr.update(value="keep"))
            order_updates.append(gr.update(value=segment.order))
            start_updates.append(gr.update(value=round(segment.source_start, 2)))
            end_updates.append(gr.update(value=round(segment.source_end, 2)))
            subtitle_updates.append(gr.update(value=segment.subtitle_text or ""))
            title_updates.append(gr.update(value=segment.title_text or ""))
        else:
            group_updates.append(hidden_groups[index])
            header_updates.append(hidden_headers[index])
            preview_updates.append(hidden_previews[index])
            decision_updates.append(hidden_decisions[index])
            order_updates.append(hidden_numbers[index])
            start_updates.append(hidden_numbers[index])
            end_updates.append(hidden_numbers[index])
            subtitle_updates.append(hidden_text[index])
            title_updates.append(hidden_text[index])
    return (
        *group_updates,
        *header_updates,
        *preview_updates,
        *decision_updates,
        *order_updates,
        *start_updates,
        *end_updates,
        *subtitle_updates,
        *title_updates,
    )


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
        # Gradio 的 number 列会把默认空白单元格提交为 0；整行 0/0/0
        # 且其余字段为空时只是占位行，不代表用户添加了 0 秒片段。
        numeric_placeholders = row[:3] + [0] * max(0, 3 - len(row))
        remaining_cells = row[3:]
        try:
            is_default_placeholder = (
                all(float(value) == 0 for value in numeric_placeholders)
                and all(_is_blank_cell(value) for value in remaining_cells)
            )
        except (TypeError, ValueError):
            is_default_placeholder = False
        if is_default_placeholder:
            continue
        start = row[0] if len(row) > 0 else None
        end = row[1] if len(row) > 1 else None
        if _is_blank_cell(start) or _is_blank_cell(end):
            raise ValueError("人工补片未填写完整：请同时填写开始和结束时间，或者清空这一行")
        order = row[2] if len(row) > 2 else None
        requirement_numbers = row[3] if len(row) > 3 else ""
        segments.append({
            "source_start": float(start),
            "source_end": float(end),
            "order": (
                int(float(order))
                if not _is_blank_cell(order)
                else selected_count + len(segments) + 1
            ),
            "matched_requirement_ids": _requirement_ids_from_numbers(requirement_numbers, spec),
            "annotation": (
                str(row[4]).strip()
                if len(row) > 4 and not _is_blank_cell(row[4]) else ""
            ),
            "subtitle_text": (
                str(row[5]).strip()
                if len(row) > 5 and not _is_blank_cell(row[5]) else None
            ),
            "title_text": (
                str(row[6]).strip()
                if len(row) > 6 and not _is_blank_cell(row[6]) else None
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
        requirement_text = str(row[3]).replace(",", "、") if len(row) > 3 else ""
        note = str(row[4]).strip() if len(row) > 4 and row[4] else "未填写说明"
        lines.append(
            f"{index}. **{_format_seconds(row[0])} — {_format_seconds(row[1])}**"
            f"（{float(row[1]) - float(row[0]):.1f} 秒）｜对应要求 {requirement_text}｜{html.escape(note)}"
        )
    return "\n\n".join(lines)


def append_manual_segment(rows, start, end, requirement_numbers, annotation, subtitle, title):
    """把用户拖动选中的区间加入补片列表，不要求手工填写秒数。"""
    start_value = float(start or 0)
    end_value = float(end or 0)
    if end_value <= start_value:
        raise gr.Error("结束位置必须晚于开始位置，请重新拖动时间线")
    selected_requirements = [str(value) for value in (requirement_numbers or [])]
    if not selected_requirements:
        raise gr.Error("请至少勾选一条这段内容对应的任务书要求")
    updated = [list(row) for row in (rows or [])]
    updated.append([
        start_value,
        end_value,
        None,
        ",".join(selected_requirements),
        str(annotation or "").strip(),
        str(subtitle or "").strip(),
        str(title or "").strip(),
    ])
    return updated, _manual_segments_markdown(updated)


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
    return pending[:3]


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
        "⏳ 正在预检视频、转录素材并生成需求建议。视频较长时需要几分钟，请不要重复点击。",
        gr.update(value="⏳ 正在理解需求与素材...", interactive=False),
    )


def finish_requirement_generation():
    return (
        "✅ 素材理解与任务书处理结束，请查看第 2 步；如有优化建议，请先选择采用、修改或保持原要求。",
        gr.update(value="🧾 重新生成需求任务书", interactive=True),
    )


def begin_material_analysis():
    return (
        "⏳ 正在确认任务书，并读取或生成素材转录后检索候选，请不要重复点击。",
        gr.update(value="⏳ 正在分析素材...", interactive=False),
    )


def finish_material_analysis():
    return (
        "✅ 素材分析处理结束，请继续查看下方第 3 步的证据、候选片段和时间线。",
        gr.update(value="✅ 重新确认并分析素材", interactive=True),
    )


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
    candidate_cards = []
    requirement_by_id = {item.id: item.description for item in spec.requirements}
    generation = {}
    try:
        generation = json.loads(
            (orch.task_dir / "candidate_generation.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError, TypeError):
        generation = {}
    failed_descriptions = [
        requirement_by_id[item_id]
        for item_id in generation.get("failed_requirement_ids", [])
        if item_id in requirement_by_id
    ]
    batch_notice = (
        "> **⚠️ 部分需求的候选分析失败：** "
        + "；".join(html.escape(item) for item in failed_descriptions)
        + "。其他成功批次仍可审核，失败批次没有使用规则结果冒充 AI 候选。"
        if failed_descriptions else ""
    )
    candidate_number_by_id = {}
    for index, candidate in enumerate(analysis.candidate_clips, start=1):
        candidate_number_by_id[candidate.id] = index
        citations = "\n".join(
            f"> “{html.escape(citation.quote)}”"
            for citation in candidate.citations
        ) or "> 暂无可核验原文（该建议不会进入可导出时间线）"
        matched = "；".join(
            html.escape(requirement_by_id.get(item_id, "未匹配到任务书要求"))
            for item_id in candidate.matched_requirement_ids
        ) or "无"
        risks = []
        for flag in candidate.risk_flags:
            if flag in {"llm_unavailable", "candidate_generation_fallback"}:
                risks.append("智能候选生成未完成；此片段由证据检索规则推荐，请重点核对原文和时间范围")
            elif flag.startswith("evidence_not_retrieved"):
                risks.append("引用证据未通过检索范围检查")
            elif flag.startswith("invalid_evidence"):
                risks.append("引用原文未通过一致性检查")
            else:
                risks.append("需要人工复核")
        risk = "；".join(dict.fromkeys(risks)) or "证据与时间范围已通过自动检查"
        candidate_cards.append(
            f"### 片段 {index} · {_format_seconds(candidate.source_start)}–{_format_seconds(candidate.source_end)}\n\n"
            f"**对应要求：** {matched}\n\n"
            f"**为什么建议保留：** {html.escape(candidate.selection_reason)}\n\n"
            f"**可核验原文：**\n\n{citations}\n\n"
            f"**建议你重点看：** {html.escape(risk)}"
        )
    style_lines = "\n".join(
        f"- 方案 {index}：{html.escape(proposal.rationale)}"
        for index, proposal in enumerate(orch.status.style_proposals, start=1)
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
        f"{_format_seconds(segment.source_start)}–{_format_seconds(segment.source_end)} | "
        f"{_format_seconds(segment.output_start)}–{_format_seconds(segment.output_end)} | "
        f"{html.escape(segment.subtitle_text or '跟随转录')} |"
        for segment in plan.timeline_segments
    )
    return f"""
## 合并方案审核页

任务书 v{spec.version}：{html.escape(spec.purpose)}；受众：{html.escape(spec.audience)}；目标 {spec.target_duration:.0f}±{spec.duration_tolerance:.0f} 秒。

### 需求
{requirements}

{coverage_notice}

{duration_notice}

{batch_notice}

### 可选风格建议
{style_lines}

### 证据与候选
每个片段都展示对应要求、AI 入选理由和可以回到原素材核对的字幕原文。

{f'{chr(10)}{chr(10)}---{chr(10)}{chr(10)}'.join(candidate_cards)}

### 粗剪时间线 v{plan.version}
| 顺序 | 片段 | 原素材时间 | 成片时间 | 字幕 |
| --- | --- | --- | --- | --- |
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
    if missing_seconds <= 0:
        return (
            f"✅ 当前预计 **{plan.estimated_duration:.1f} 秒**，已达到任务书允许的最低时长 "
            f"**{lower:.1f} 秒**。如果 AI 仍有漏选，也可以在下方人工补入片段。",
            False,
        )
    if missing_must:
        return (
            f"⚠️ 当前预计 **{plan.estimated_duration:.1f} 秒**，还差约 **{missing_seconds:.1f} 秒**；"
            "并且仍有必须内容没有进入时间线。请在下方播放原素材并补入对应片段，"
            "必须内容不能通过接受短版跳过。",
            False,
        )
    return (
        f"⚠️ 当前预计 **{plan.estimated_duration:.1f} 秒**，比任务书允许的最低时长 "
        f"**{lower:.1f} 秒**少约 **{missing_seconds:.1f} 秒**。你可以：\n\n"
        "1. 如果现有内容已经完整，点击“接受当前时长并生成”；系统会记录这次人工例外。\n"
        "2. 如果还缺内容，直接在下方播放原素材、拖动选段范围并加入时间线。",
        True,
    )


def create_requirement_review(
    video_path, user_input, scenario, target_duration, style, keywords, need_subtitles,
    progress=gr.Progress(),
):
    """生成初步任务书、素材转录和对齐建议；候选分析仍等待用户确认。"""
    video_paths = _uploaded_video_paths(video_path)
    if not video_paths:
        return (
            "请先上传视频文件", None,
            *[gr.update() for _ in range(8)],
        )

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
            video_paths,
            user_input,
            scenario=scenario_value,
            overrides=overrides,
            submitted_by="local-user",
        )
        task_id = orch.task_dir.name
        _task_orchestrators[task_id] = orch
        spec = compilation.spec
        proposal = compilation.alignment_proposal
        needs_alignment_decision = bool(
            proposal is not None and proposal.requires_user_decision
        )
        progress(1.0, desc="请审核需求任务书")
        summary = _requirement_review_markdown(
            spec,
            compilation.execution_brief,
            warnings=compilation.warnings,
        )
        if len(video_paths) > 1:
            summary = (
                f"> ✅ 已按上传顺序将 **{len(video_paths)} 段素材**合成为一条分析时间线。\n\n"
                + summary
            )
        return (
            summary,
            {
                "task_id": task_id,
                "video_path": orch.video_path,
                "source_count": len(video_paths),
                "gate_id": orch.status.requirement_gate.id,
            },
            gr.update(visible=not needs_alignment_decision),
            gr.update(value=_requirement_rows(spec), visible=True),
            gr.update(value=spec.purpose, visible=True),
            gr.update(value=spec.audience, visible=True),
            gr.update(visible=not needs_alignment_decision),
            gr.update(visible=needs_alignment_decision),
            gr.update(value=_material_alignment_markdown(proposal)),
            gr.update(
                value=(proposal.suggested_requirement_text if proposal else ""),
                visible=needs_alignment_decision,
            ),
        )

    except Exception as e:
        progress(1.0, desc="出错")
        return (
            f"## 错误\n\n{html.escape(str(e))}", None,
            *[gr.update() for _ in range(8)],
        )


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
        items = []
        for row in requirement_rows or []:
            if not row or len(row) < 3 or not str(row[2]).strip():
                continue
            try:
                row_number = int(float(row[0])) if row[0] not in {None, ""} else 0
            except (TypeError, ValueError):
                row_number = 0
            old = existing[row_number - 1] if 1 <= row_number <= len(existing) else None
            priority_label = str(row[1]).strip()
            priority = PRIORITY_VALUES.get(priority_label)
            if priority is None:
                raise ValueError("重要程度请填写：必须保留、建议保留、可有可无或禁止出现")
            items.append({
                "id": old.id if old else None,
                "category": old.category if old else "content",
                "priority": priority,
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
        return (
            _requirement_review_markdown(
                revised,
                orch.status.execution_brief,
                saved=True,
            ),
            workflow_state,
            gr.update(value=revised.purpose, visible=True),
            gr.update(value=revised.audience, visible=True),
            gr.update(value=_requirement_rows(revised), visible=True),
        )
    except Exception as error:
        return f"## 任务书修改失败\n\n{html.escape(str(error))}", workflow_state, gr.update(), gr.update(), gr.update()


def confirm_and_analyze(
    workflow_state,
    clarification_confirmations,
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
            *[gr.update() for _ in range(12)],
        )
    try:
        orch = _get_task_orchestrator(workflow_state)
        clarification_modes = [mode_1, mode_2, mode_3, mode_4, mode_5]
        clarification_texts = [answer_1, answer_2, answer_3, answer_4, answer_5]
        _validate_clarification_confirmations(
            orch,
            clarification_confirmations,
            clarification_modes,
            clarification_texts,
        )
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
        progress(0.15, desc="正在转录并检索证据...")
        script = orch.analyze_confirmed_requirement(workflow_state["video_path"], generate_previews=True)
        if not script.operations:
            return (
                _candidate_failure_markdown(orch),
                [],
                workflow_state,
                gr.update(visible=False),
                gr.update(choices=[], visible=False),
                *[gr.update(visible=False) for _ in range(5)],
                *[gr.update() for _ in range(5)],
            )
        progress(1.0, desc="请审核有证据的候选片段")
        state = {
            **workflow_state,
            "plan_id": orch.status.edit_plan.id,
            "plan_version": orch.status.edit_plan.version,
            "previews": orch.preview_paths,
            "candidate_labels": _candidate_label_map(orch.status.edit_plan),
        }
        style_choices = [
            (f"推荐方案 {index}｜{proposal.rationale}", proposal.bundle_id)
            for index, proposal in enumerate(orch.status.style_proposals, start=1)
        ]
        duration_markdown, can_accept_short = _duration_action_markdown(orch)
        media_info = FFmpegTool.get_video_info(workflow_state["video_path"]) or {}
        source_duration = max(0.1, float(media_info.get("duration", 0.1)))
        default_end = min(15.0, source_duration)
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
            gr.update(value=workflow_state["video_path"], visible=True),
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
            *[gr.update(visible=False) for _ in range(5)],
            *[gr.update() for _ in range(5)],
        )


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
                    "支持手机、相机和电脑素材；多段视频将按列表顺序拼接。",
                    elem_classes=["upload-note"],
                )
            with gr.Column(scale=7, elem_classes=["intake-column"]):
                user_input = gr.Textbox(
                    label="你希望剪成什么样？",
                    placeholder="例如：帮我把运动会视频剪成3分钟精彩集锦，重点要冲刺和颁奖的画面",
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
                keywords = gr.Textbox(
                    label="重点内容（可选）",
                    placeholder="例如：冲刺，颁奖，领导讲话",
                )
                gr.Markdown("快捷填写", elem_classes=["template-label"])
                gr.Examples(
                    examples=[
                        ["帮我把运动会视频剪成3分钟精彩集锦"],
                        ["把知识竞赛的精彩回答剪出来，5分钟以内"],
                        ["会议录像太长，剪一个3分钟的精华版"],
                    ],
                    inputs=user_input,
                )
                submit_btn = gr.Button(
                    "生成需求任务书",
                    variant="primary",
                    size="lg",
                    elem_classes=["primary-action"],
                )
                requirement_action_status = gr.Markdown(
                    "点击后会在这里显示处理状态。",
                    elem_classes=["action-status"],
                )

        plan_state = gr.State(value=None)
        gr.Markdown(
            """
            <div class="step-heading"><span class="step-index">02</span><div><div class="step-title">确认任务书与执行依据</div><div class="step-description">检查 AI 是否准确理解用途、受众和必须保留的内容，确认后才会分析素材。</div></div></div>
            """,
            elem_classes=["workflow-heading"],
        )
        requirement_result_text = gr.Markdown(
            value="等待上传视频并生成任务书...",
            elem_id="requirement-review",
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
                confirm_question_btn = gr.Button("确认本题", variant="secondary")
                question_confirmation_status = gr.Markdown(
                    "请选择后确认本题。",
                    elem_classes=["action-status"],
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
                label="成片用途",
                placeholder="例如：学校运动会公众号回顾",
                visible=False,
            )
            audience_editor = gr.Textbox(
                label="观看对象",
                placeholder="例如：师生与家长",
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
                "✅ 确认任务书并分析素材",
                variant="primary",
                visible=False,
            )
        analysis_action_status = gr.Markdown(
            "确认任务书后，这里会持续提示素材分析状态。",
            elem_classes=["action-status"],
        )

        with gr.Accordion("🎨 成品样式与声音（确认任务书后可调整）", open=False):
            gr.Markdown("这些设置只影响最终成片，不会遮挡或改变上方任务书。")
            style = gr.Dropdown(
                choices=["自动判断", "正式", "燃向", "温馨", "轻松"],
                value="自动判断",
                label="剪辑风格",
            )
            need_subtitles = gr.Checkbox(value=True, label="烧录中文字幕")
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
            bgm_input = gr.Audio(
                label="背景音乐（可选）",
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

        gr.Markdown(
            """
            <div class="step-heading"><span class="step-index">03</span><div><div class="step-title">审核候选片段与时间线</div><div class="step-description">边看片段、边看依据；保留、删除、排序和补片都在这里完成。</div></div></div>
            """,
            elem_classes=["workflow-heading"],
        )
        candidate_result_text = gr.Markdown(
            value="等待确认任务书并分析素材...",
            elem_id="review-output",
        )
        with gr.Group(visible=False, elem_classes=["duration-decision-card"]) as duration_action_group:
            duration_action_text = gr.Markdown()
            accept_short_btn = gr.Button(
                "✅ 接受当前时长并生成成片",
                variant="secondary",
                size="lg",
                visible=False,
            )
        with gr.Group(visible=False, elem_classes=["manual-add-card"]) as manual_segment_group:
            gr.Markdown(
                "### ➕ 从原素材补入片段\n\n"
                "播放原视频定位内容，再拖动开始和结束滑块选择范围；不需要手写时间。"
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
        )
        timeline_state = gr.State(value=[])
        candidate_groups = []
        candidate_headers = []
        candidate_previews = []
        candidate_decisions = []
        candidate_orders = []
        candidate_starts = []
        candidate_ends = []
        candidate_subtitles = []
        candidate_titles = []
        for index in range(MAX_CANDIDATE_CARDS):
            with gr.Group(visible=False, elem_classes=["candidate-edit-card"]) as candidate_group:
                candidate_header = gr.Markdown()
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
                        gr.Markdown("**只在需要微调片段边界时修改下面两个时间。**")
                        with gr.Row():
                            candidate_start = gr.Number(label="从原视频第几秒开始", precision=2)
                            candidate_end = gr.Number(label="到原视频第几秒结束", precision=2)
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
                        candidate_preview = gr.Video(
                            label=f"片段 {index + 1} 预览",
                            autoplay=False,
                            visible=False,
                            elem_classes=["candidate-preview"],
                        )
                candidate_groups.append(candidate_group)
                candidate_headers.append(candidate_header)
                candidate_previews.append(candidate_preview)
                candidate_decisions.append(candidate_decision)
                candidate_orders.append(candidate_order)
                candidate_starts.append(candidate_start)
                candidate_ends.append(candidate_end)
                candidate_subtitles.append(candidate_subtitle)
                candidate_titles.append(candidate_title)
        with gr.Group(elem_classes=["export-action"]):
            gr.Markdown("**确认候选、时间线和样式后生成成片。系统会保存审核版本，并在导出后逐项验收。**")
            export_btn = gr.Button(
                "确认方案并生成成片",
                variant="primary",
                size="lg",
                visible=False,
                elem_classes=["primary-action"],
            )

        gr.Markdown(
            """
            <div class="step-heading"><span class="step-index">04</span><div><div class="step-title">查看成片与逐项验收</div><div class="step-description">最终视频和验收结论并排呈现；有异常时可以返回修改或记录例外。</div></div></div>
            """,
            elem_classes=["workflow-heading"],
        )
        with gr.Row(equal_height=False, elem_classes=["delivery-panel"]):
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

        gr.Markdown(
            """
            <div class="quiet-footer"><b>映证 MVP2</b> · 本地处理 · 人工审核 · 全程留痕<br>Python · OpenAI-compatible LLM · Faster-Whisper · FFmpeg · Gradio</div>
            """
        )

        # 绑定事件
        submit_started = submit_btn.click(
            fn=begin_requirement_generation,
            inputs=[],
            outputs=[requirement_action_status, submit_btn],
            queue=False,
            api_name=False,
        )
        submit_completed = submit_started.then(
            fn=create_requirement_review,
            inputs=[video_input, user_input, scenario, target_duration, style, keywords, need_subtitles],
            outputs=[
                requirement_result_text,
                plan_state,
                confirm_requirement_btn,
                requirement_editor,
                purpose_editor,
                audience_editor,
                revise_requirement_btn,
                alignment_group,
                alignment_result_text,
                alignment_editor,
            ],
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
                clarification_confirmation_state,
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
            outputs=[
                *candidate_groups,
                *candidate_headers,
                *candidate_previews,
                *candidate_decisions,
                *candidate_orders,
                *candidate_starts,
                *candidate_ends,
                *candidate_subtitles,
                *candidate_titles,
            ],
            api_name=False,
        )
        candidate_cards_loaded.then(
            fn=finish_material_analysis,
            inputs=[],
            outputs=[analysis_action_status, confirm_requirement_btn],
            queue=False,
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
        candidate_rows_collected.then(
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
        short_candidate_rows_collected.then(
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
        approve_exception_btn.click(
            fn=resolve_delivery_exception,
            inputs=[plan_state, exception_reason],
            outputs=[delivery_result_text, exception_reason, approve_exception_btn],
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
