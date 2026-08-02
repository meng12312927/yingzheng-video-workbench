from src.tools.ffmpeg import FFmpegTool


def test_source_merge_normalizes_and_keeps_input_order(monkeypatch, tmp_path):
    infos = {
        "camera.mp4": {
            "duration": 10.0,
            "width": 1920,
            "height": 1080,
            "has_audio": True,
        },
        "phone.mov": {
            "duration": 8.0,
            "width": 1080,
            "height": 1920,
            "has_audio": False,
        },
    }
    captured = {}
    monkeypatch.setattr(FFmpegTool, "get_video_info", staticmethod(infos.get))
    monkeypatch.setattr(FFmpegTool, "_find_ffmpeg", staticmethod(lambda: "ffmpeg"))

    def fake_run(command, description):
        captured["command"] = command
        captured["description"] = description
        return True

    monkeypatch.setattr(FFmpegTool, "_run_with_encoder_fallback", staticmethod(fake_run))

    output = tmp_path / "merged.mp4"
    assert FFmpegTool.concatenate_source_videos(
        ["camera.mp4", "phone.mov"], str(output)
    )

    command = captured["command"]
    assert command.index("camera.mp4") < command.index("phone.mov")
    filter_graph = command[command.index("-filter_complex") + 1]
    assert "concat=n=2:v=1:a=1" in filter_graph
    assert "anullsrc" in filter_graph
    assert "按上传顺序合并 2 段" in captured["description"]
