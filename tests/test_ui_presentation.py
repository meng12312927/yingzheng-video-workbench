from types import SimpleNamespace

from src.agents.requirement_agent import RequirementAgent
from src.models.schemas import ExecutionBrief, RequirementItem, RequirementSpec
from src.ui.app import (
    MAX_CANDIDATE_CARDS,
    _duration_action_markdown,
    _manual_segment_payloads,
    _manual_segments_markdown,
    _requirement_review_markdown,
    _uploaded_video_paths,
    append_manual_segment,
    candidate_cards_to_rows,
    create_ui,
)


def test_candidate_cards_convert_to_existing_plan_rows_without_internal_ids():
    values = []
    for index in range(MAX_CANDIDATE_CARDS):
        values.extend([
            "keep" if index == 0 else "drop",
            index + 1,
            1.25 + index,
            3.5 + index,
            "字幕修正" if index == 0 else "",
            "开场" if index == 0 else "",
        ])

    rows = candidate_cards_to_rows(*values)

    assert rows[0] == [True, 1, "片段 1", 1.25, 3.5, "字幕修正", "开场"]
    assert rows[1][0] is False
    assert not any("candidate_" in str(cell) for row in rows for cell in row)


def test_requirement_review_uses_user_facing_chinese_labels():
    requirement = RequirementItem(
        description="优先保留颁奖环节",
        priority="should",
    )
    spec = RequirementSpec(
        brief_id="brief-1",
        purpose="校园活动公众号回顾",
        audience="师生和家长",
        style="formal",
        target_duration=180,
        duration_tolerance=15,
        requirements=[requirement],
    )
    execution = ExecutionBrief(
        requirement_spec_id=spec.id,
        requirement_spec_version=spec.version,
        visible_instruction=RequirementAgent._build_visible_instruction(spec),
        included_requirement_ids=[requirement.id],
    )

    markdown = _requirement_review_markdown(spec, execution)

    assert "建议保留" in markdown
    assert "正式稳重" in markdown
    assert "AI 接下来会怎样处理" in markdown
    assert "给出依据" in markdown
    assert "`should`" not in markdown
    assert "formal" not in markdown


def test_blank_manual_segment_row_is_optional_and_ignored():
    requirement = RequirementItem(
        description="必须保留颁奖环节",
        priority="must",
        acceptance_rule="至少出现一次颁奖画面",
    )
    spec = RequirementSpec(
        brief_id="brief-2",
        target_duration=180,
        requirements=[requirement],
    )

    rows = [
        [float("nan"), float("nan"), float("nan"), "", "", "", ""],
        [0, 0, 0, "", "", "", ""],
    ]

    assert _manual_segment_payloads(rows, spec, selected_count=2) == []


def test_multiple_uploaded_videos_keep_their_list_order():
    uploads = [SimpleNamespace(path="second-camera.mp4"), "phone-video.mov"]

    assert _uploaded_video_paths(uploads) == ["second-camera.mp4", "phone-video.mov"]


def test_dragged_manual_segment_is_added_without_typing_timestamps():
    rows, markdown = append_manual_segment(
        [], 12.4, 27.9, ["1", "2"], "补足颁奖画面", "", "颁奖时刻"
    )

    assert rows == [[12.4, 27.9, None, "1,2", "补足颁奖画面", "", "颁奖时刻"]]
    assert "00:12.4" in markdown
    assert "00:27.9" in markdown
    assert "对应要求 1、2" in markdown
    assert "尚未补入" in _manual_segments_markdown([])


def test_duration_action_offers_explicit_short_version_choice():
    requirement = RequirementItem(description="保留活动总结", priority="should")
    spec = RequirementSpec(
        brief_id="brief-duration",
        target_duration=120,
        duration_tolerance=12,
        requirements=[requirement],
    )
    plan = SimpleNamespace(
        estimated_duration=102,
        timeline_segments=[
            SimpleNamespace(matched_requirement_ids=[requirement.id])
        ],
    )
    orch = SimpleNamespace(
        status=SimpleNamespace(requirement_spec=spec, edit_plan=plan)
    )

    markdown, can_accept_short = _duration_action_markdown(orch)

    assert can_accept_short
    assert "102.0 秒" in markdown
    assert "少约 **6.0 秒**" in markdown
    assert "接受当前时长并生成" in markdown
    assert "播放原素材" in markdown


def test_duration_action_never_allows_missing_must_item_to_be_bypassed():
    requirement = RequirementItem(
        description="必须保留颁奖",
        priority="must",
        acceptance_rule="至少一个片段覆盖颁奖",
    )
    spec = RequirementSpec(
        brief_id="brief-must-duration",
        target_duration=120,
        duration_tolerance=12,
        requirements=[requirement],
    )
    plan = SimpleNamespace(estimated_duration=102, timeline_segments=[])
    orch = SimpleNamespace(
        status=SimpleNamespace(requirement_spec=spec, edit_plan=plan)
    )

    markdown, can_accept_short = _duration_action_markdown(orch)

    assert not can_accept_short
    assert "必须内容不能通过接受短版跳过" in markdown


def test_ui_has_branded_hierarchy_and_plain_language_actions():
    demo = create_ui()
    config_text = str(demo.get_config_file())

    assert "YINGZHENG · AI VIDEO WORKBENCH" in config_text
    assert "让每一次入选都有依据" in config_text
    assert "上传素材与说明目标" in config_text
    assert "确认任务书与执行依据" in config_text
    assert "审核候选片段与时间线" in config_text
    assert "查看成片与逐项验收" in config_text
    assert "确认方案并生成成片" in config_text
