import subprocess
from pathlib import Path

from src.agents.script_agent import ScriptAgent
from src.agents.executor_agent import ExecutorAgent
from src.agents.analysis_agent import AnalysisAgent
from src.models.schemas import ContentAnalysis, EditOperation, EditScript, HighlightClip, TranscriptSegment, VideoRequirement
from src.orchestrator import VideoEditOrchestrator
from src.tools.ffmpeg import FFmpegTool


def test_script_plan_remaps_subtitles_to_output_timeline():
    analysis = ContentAnalysis(
        video_duration=120,
        transcript=[
            TranscriptSegment(start=10, end=14, text="第一段内容"),
            TranscriptSegment(start=60, end=66, text="第二段内容"),
        ],
        highlights=[
            HighlightClip(start=10, end=20, text="第一段", importance=0.9, category="highlight", reason="开场"),
            HighlightClip(start=60, end=70, text="第二段", importance=0.8, category="highlight", reason="高潮"),
        ],
    )
    requirement = VideoRequirement(target_duration=30, video_type="general", style="formal")

    script = ScriptAgent().run(analysis, requirement)

    assert script.estimated_duration == 20
    assert "00:00:00,000 --> 00:00:04,000" in script.srt_subtitles
    assert "00:00:10,000 --> 00:00:16,000" in script.srt_subtitles

    confirmed = ScriptAgent().apply_selection(script, analysis, [2], correct_subtitles=False)
    assert confirmed.estimated_duration == 10
    assert "00:00:00,000 --> 00:00:06,000" in confirmed.srt_subtitles


def test_media_probe_reads_generated_video(tmp_path: Path):
    ffmpeg = FFmpegTool._find_ffmpeg()
    input_path = tmp_path / "input.mp4"
    command = [
        ffmpeg,
        "-f", "lavfi", "-i", "color=c=black:s=320x240:d=2:r=24",
        "-f", "lavfi", "-i", "sine=frequency=1000:duration=2",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", "-y", str(input_path),
    ]
    subprocess.run(command, check=True, capture_output=True, text=True)

    info = FFmpegTool.get_video_info(str(input_path))

    assert info is not None
    assert info["has_audio"] is True
    assert info["width"] == 320
    assert 1.9 <= info["duration"] <= 2.1


def test_executor_renders_valid_video_with_remapped_subtitles(tmp_path: Path):
    ffmpeg = FFmpegTool._find_ffmpeg()
    input_path = tmp_path / "input.mp4"
    output_path = tmp_path / "output.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-f", "lavfi", "-i", "color=c=black:s=320x240:d=3:r=24",
            "-f", "lavfi", "-i", "sine=frequency=1000:duration=3",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", "-y", str(input_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    script = EditScript(
        title="测试",
        estimated_duration=2.0,
        operations=[
            EditOperation(order=1, action="cut", source_start=0.0, source_end=1.0),
            EditOperation(order=2, action="cut", source_start=1.5, source_end=2.5),
        ],
        srt_subtitles="1\n00:00:00,000 --> 00:00:01,000\n测试字幕\n",
    )

    result = ExecutorAgent().run(script, str(input_path), str(output_path))

    assert result.success, result.errors
    assert output_path.exists()
    assert 1.8 <= result.output_duration <= 2.2


def test_prepare_writes_recoverable_task_artifacts(tmp_path: Path, monkeypatch):
    ffmpeg = FFmpegTool._find_ffmpeg()
    input_path = tmp_path / "input.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-f", "lavfi", "-i", "color=c=black:s=320x240:d=4:r=24",
            "-f", "lavfi", "-i", "sine=frequency=1000:duration=4",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", "-y", str(input_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    analysis = ContentAnalysis(
        video_duration=4,
        transcript=[TranscriptSegment(start=0, end=3, text="测试")],
        highlights=[HighlightClip(start=0, end=3, text="测试", importance=0.9, category="highlight", reason="测试")],
    )
    requirement = VideoRequirement(target_duration=30, video_type="general", style="formal")
    script = ScriptAgent().run(analysis, requirement)

    class FakeAgent:
        def __init__(self, value):
            self.value = value

        def run(self, *args):
            return self.value

    orchestrator = VideoEditOrchestrator.__new__(VideoEditOrchestrator)
    orchestrator.status = None
    orchestrator.task_dir = None
    orchestrator.agent1 = FakeAgent(requirement)
    orchestrator.agent2 = FakeAgent(analysis)
    orchestrator.agent3 = ScriptAgent()
    orchestrator.agent4 = ExecutorAgent()
    monkeypatch.setattr("src.orchestrator.OUTPUT_DIR", tmp_path / "output")

    prepared = orchestrator.prepare(str(input_path), "测试")

    assert prepared == script
    assert (orchestrator.task_dir / "manifest.json").exists()
    assert (orchestrator.task_dir / "requirement.json").exists()
    assert (orchestrator.task_dir / "analysis.json").exists()
    assert (orchestrator.task_dir / "edit_plan.json").exists()

    result = orchestrator.confirm_and_render(prepared, [1], str(input_path))
    assert result.success, result.errors
    assert (orchestrator.task_dir / "final.mp4").exists()
    assert (orchestrator.task_dir / "subtitles.srt").exists()
    assert (orchestrator.task_dir / "render.log").exists()
    assert (orchestrator.task_dir / "execution_result.json").exists()


def test_analysis_chunking_and_deduplication_are_time_based():
    agent = AnalysisAgent.__new__(AnalysisAgent)
    transcript = [TranscriptSegment(start=float(second), end=float(second + 10), text=str(second)) for second in range(0, 300, 10)]
    chunks = agent._split_transcript(transcript, max_duration=120, overlap_duration=15)
    assert len(chunks) == 3
    assert chunks[1][0].start == 110

    clips = [
        HighlightClip(start=0, end=20, text="A", importance=0.9, category="highlight", reason="A"),
        HighlightClip(start=5, end=15, text="B", importance=0.8, category="highlight", reason="B"),
        HighlightClip(start=40, end=50, text="C", importance=0.7, category="highlight", reason="C"),
    ]
    deduplicated = agent._deduplicate(clips)
    assert [clip.text for clip in deduplicated] == ["A", "C"]


def test_subtitle_correction_only_updates_returned_segments(monkeypatch):
    analysis = ContentAnalysis(
        video_duration=20,
        transcript=[
            TranscriptSegment(start=0, end=3, text="我门今天开始"),
            TranscriptSegment(start=10, end=13, text="不应被处理"),
        ],
        highlights=[
            HighlightClip(
                start=0,
                end=5,
                text="开始",
                importance=0.9,
                category="highlight",
                reason="测试",
            )
        ],
    )
    script = EditScript(
        estimated_duration=5,
        operations=[EditOperation(order=1, action="cut", source_start=0, source_end=5)],
    )
    monkeypatch.setattr(
        "src.agents.script_agent.call_llm",
        lambda **_: {"segments": [{"index": 0, "text": "我们今天开始。"}]},
    )

    confirmed = ScriptAgent().apply_selection(script, analysis, [1])

    assert "我们今天开始。" in confirmed.srt_subtitles
    assert "不应被处理" not in confirmed.srt_subtitles


def test_intro_and_outro_extend_duration_and_shift_subtitles():
    analysis = ContentAnalysis(
        video_duration=10,
        transcript=[TranscriptSegment(start=0, end=2, text="开场内容")],
        highlights=[HighlightClip(start=0, end=4, text="开场", importance=0.9, category="highlight", reason="测试")],
    )
    script = EditScript(
        title="活动回顾",
        estimated_duration=4,
        operations=[EditOperation(order=1, action="cut", source_start=0, source_end=4)],
    )

    confirmed = ScriptAgent().apply_selection(
        script,
        analysis,
        [1],
        intro_style="title",
        outro_style="fade_black",
        title_text="夏季活动回顾",
        correct_subtitles=False,
    )

    assert confirmed.estimated_duration == 8
    assert confirmed.title_text == "夏季活动回顾"
    assert "00:00:02,000 --> 00:00:04,000" in confirmed.srt_subtitles


def test_executor_supports_transition_bgm_and_subtitle_style(tmp_path: Path):
    ffmpeg = FFmpegTool._find_ffmpeg()
    input_path = tmp_path / "input.mp4"
    bgm_path = tmp_path / "bgm.m4a"
    output_path = tmp_path / "output.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-f", "lavfi", "-i", "testsrc2=s=320x240:d=5:r=24",
            "-f", "lavfi", "-i", "sine=frequency=1000:duration=5",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", "-y", str(input_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [ffmpeg, "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "aac", "-y", str(bgm_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    script = EditScript(
        title="功能测试",
        estimated_duration=3.5,
        operations=[
            EditOperation(order=1, action="cut", source_start=0, source_end=2),
            EditOperation(order=2, action="cut", source_start=2.5, source_end=4.5),
        ],
        srt_subtitles="1\n00:00:00,000 --> 00:00:01,000\n字幕样式测试\n",
        transition_duration=0.5,
        subtitle_style="highlight",
        bgm_path=str(bgm_path),
        bgm_volume=0.1,
    )

    result = ExecutorAgent().run(script, str(input_path), str(output_path))

    assert result.success, result.errors
    assert output_path.exists()
    info = FFmpegTool.get_video_info(str(output_path))
    assert info and info["has_audio"]
    assert 3.2 <= result.output_duration <= 3.8


def test_executor_renders_selected_intro_and_outro(tmp_path: Path):
    ffmpeg = FFmpegTool._find_ffmpeg()
    input_path = tmp_path / "input.mp4"
    output_path = tmp_path / "with_cards.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-f", "lavfi", "-i", "color=c=green:s=320x240:d=3:r=24",
            "-f", "lavfi", "-i", "sine=frequency=800:duration=3",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", "-y", str(input_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    script = EditScript(
        title="测试片头",
        estimated_duration=6,
        operations=[EditOperation(order=1, action="cut", source_start=0, source_end=2)],
        intro_style="title",
        outro_style="fade_black",
        title_text="我的活动",
    )

    result = ExecutorAgent().run(script, str(input_path), str(output_path))

    assert result.success, result.errors
    assert 5.7 <= result.output_duration <= 6.3


def test_preview_generation_creates_small_playable_video(tmp_path: Path):
    ffmpeg = FFmpegTool._find_ffmpeg()
    input_path = tmp_path / "input.mp4"
    preview_path = tmp_path / "preview.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-f", "lavfi", "-i", "color=c=blue:s=640x480:d=2:r=24",
            "-f", "lavfi", "-i", "sine=frequency=600:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", "-y", str(input_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert FFmpegTool.create_preview(str(input_path), 0, 1.5, str(preview_path))
    info = FFmpegTool.get_video_info(str(preview_path))
    assert info and info["has_audio"]
    assert info["height"] == 360
