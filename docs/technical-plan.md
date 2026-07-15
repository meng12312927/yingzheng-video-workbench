# 技术方案：智能视频粗剪助手（MVP）

> 版本：v2.1
> 状态：已实现，待真实素材验收
> 更新日期：2026-07-15
> 对齐文档：[PRD](PRD.md)

## 1. 实现原则

系统是本地单进程流水线，而非自治 Agent 对话系统。LLM 仅用于三项文本不确定性决策：需求解析、转录文本中的候选片段识别，以及用户确认导出时的保守字幕校对；时长预算、时间范围校验、去重、字幕映射、预览、转场、混音、渲染和验收均由确定性 Python/FFmpeg 代码完成。

当前运行环境面向 macOS：Whisper 默认 `CPU + int8`，视频优先采用 VideoToolbox 编码，失败时回退 `libx264`。

## 2. 当前架构

```text
Gradio / CLI
  │
  ▼
VideoEditOrchestrator
  ├─ FFmpegTool：媒体预检
  ├─ RequirementAgent：用户文本 → VideoRequirement
  ├─ AnalysisAgent：Whisper 转录 → LLM 候选片段
  ├─ ScriptAgent：确定性选段 + SRT 时间轴重映射
  ├─ 生成低分辨率候选预览 → 用户确认保留片段
  ├─ ScriptAgent：对最终片段字幕作保守文本校对
  ├─ ExecutorAgent：裁剪 → 淡转场拼接 → 可选片头/片尾 → 可选 BGM 混音 → 字幕烧录
  └─ FFmpegTool：成片校验
       │
       └─ output/tasks/<task_id>/（任务产物）
```

`VideoEditOrchestrator.prepare()` 负责预检至方案生成；`confirm_and_render()` 只渲染确认后的片段。`run()` 保留为自动调用接口，CLI 与 Web UI 使用确认式流程。

## 3. 模块与数据契约

| 模块 | 主要输入 | 主要输出 | 已实现约束 |
| --- | --- | --- | --- |
| `FFmpegTool` | 视频路径 | `dict` 媒体信息 | 检查视频流、音频流、时长、分辨率；优先 `ffprobe`，缺失时用 FFmpeg 兼容解析 |
| `RequirementAgent` | 用户文本 | `VideoRequirement` | Pydantic 校验；时长 30–600 秒；解析异常使用默认需求 |
| `AnalysisAgent` | 视频、需求 | `ContentAnalysis` | 原始转录时间戳；120 秒文本块、15 秒重叠；候选范围校验与去重 |
| `ScriptAgent` | 分析、需求 | `EditScript` | 3–30 秒片段、按分数选择、按源时间排序、SRT 重映射 |
| `ExecutorAgent` | `EditScript`、视频 | `ExecutionResult` | 裁剪边界校验、统一编码、淡转场、可选片头/片尾、BGM 混音、字幕烧录、最终校验 |
| `VideoEditOrchestrator` | 任务输入 | 任务目录与结果 | 每阶段原子写入 JSON 产物和 manifest |

实际使用的数据模型位于 `src/models/schemas.py`：`VideoRequirement`、`TranscriptSegment`、`HighlightClip`、`ContentAnalysis`、`EditOperation`、`EditScript`、`ExecutionResult` 与 `PipelineStatus`。

## 4. 任务状态与文件

任务 manifest 当前使用以下状态：

```text
created → preflight_ok → requirement_ready → analyzed
        → awaiting_confirmation → rendering → succeeded
                                            └→ failed
```

任务目录包含：

```text
output/tasks/<task_id>/
├── manifest.json
├── media_info.json
├── requirement.json
├── analysis.json
├── edit_plan.json
├── confirmed_edit_plan.json      # 用户确认后生成
├── subtitles.srt                 # 开启字幕时生成
├── render.log
├── execution_result.json
└── final.mp4
```

JSON 文件先写入临时文件，再原子替换为目标文件，避免阶段写入中断时留下半份 JSON。当前版本保存产物用于排查；恢复未完成任务属于后续能力。

## 5. 分析和选段算法

1. Whisper 生成 `TranscriptSegment(start, end, text)`。
2. 转录按最多 120 秒切块，并与前一块重叠 15 秒。
3. 每块发送给 LLM 时附上允许的时间范围；代码只接受落在该范围内的 `HighlightClip`。
4. 用户关键词出现在候选文本时，重要性最多加 0.1。
5. 候选按重要性从高到低去重；与任一保留片段的短片段重叠比例超过 50% 时删除。
6. `ScriptAgent` 按预算选择候选（目标时长的最多 115%）、过滤少于 3 秒的候选，再按原视频时间排序。
7. 用户删除候选后，`apply_selection()` 重新计算成片时长和 SRT，而不是复用旧字幕时间。
8. 有转场时，成片预计时长减去每个相邻片段的转场时长；每个启用的片头或片尾增加 2 秒。片头会使 SRT 的起始偏移增加 2 秒；BGM 与字幕样式只影响渲染参数，不改变选段。

## 6. 字幕时间轴

对于每个确认片段 `[source_start, source_end]`，转录段先与该区间求交集，再将时间平移到已输出片段总时长 `output_offset`：

```text
output_start = output_offset + max(transcript_start, source_start) - source_start
output_end   = output_offset + min(transcript_end, source_end) - source_start
```

这保证保留源视频第 60 秒和第 120 秒的两个片段时，第二段字幕出现在成片的相邻位置，而不是第 120 秒。

## 7. 媒体处理与 macOS 回退

- 输入预检与导出校验优先使用 `ffprobe` JSON；没有独立 `ffprobe` 时，使用可找到的 FFmpeg 进行兼容解析。
- 每个片段均重新编码为 H.264/AAC，以避免关键帧切割偏移与 concat 参数不一致。
- 默认编码参数来自 `VIDEO_ENCODER=h264_videotoolbox`；VideoToolbox 创建编码器失败时，同一 FFmpeg 命令自动以 `libx264 -preset medium -crf 20` 重试。
- 使用 concat demuxer 合并片段；有 SRT 时使用 FFmpeg subtitles filter 烧录。
- 导出通过文件存在性、非零大小、视频流与计划时长（最大 `max(2 秒, 2%)` 偏差）校验。

## 8. 配置与安全

`.env.example` 记录可配置项：

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `OPENAI_API_KEY` | 空 | LLM 调用密钥，不写入任务产物或日志 |
| `OPENAI_MODEL` | `gpt-4o-mini` | 需求与候选分析模型 |
| `WHISPER_MODEL_SIZE` | `small` | 转录模型，可改为 `tiny` 或 `large-v3` |
| `WHISPER_DEVICE` | `cpu` | macOS 稳定默认设备 |
| `WHISPER_COMPUTE_TYPE` | `int8` | CPU 推理配置 |
| `VIDEO_ENCODER` | `h264_videotoolbox` | 视频编码器，可设置为 `libx264` |
| `GRADIO_SERVER_PORT` | `7860` | 本地 Web UI 端口 |

原始视频和 ASR 在本机处理；配置云端 LLM 时，会发送用户需求和转录文本，使用前应取得素材授权。

## 9. 已验证内容

`tests/test_pipeline_core.py` 目前覆盖：

1. 剪辑计划与 SRT 时间轴重映射。
2. 媒体预检。
3. 真实 FFmpeg 裁剪、淡转场拼接、BGM 混音、字幕烧录和输出校验。
4. 任务产物、确认式渲染与最终文件写入。
5. 基于时长的文本切块和高光去重。

本地验收命令：

```bash
.venv/bin/ruff check src tests run.py
.venv/bin/python -m pytest -q
.venv/bin/python -c "from src.ui.app import create_ui; assert create_ui() is not None"
```

真实视频上的 Whisper 下载、API 调用、识别质量、处理耗时与候选片段接受度，需要在用户素材和实际 API 配额下继续验收。

## 10. 后续技术工作

- 任务取消、任务恢复和转录/分析缓存。
- 真实素材基准集与性能/成本记录。
- 更细粒度的转录断句、字幕样式和转场模板。
- 场景切换、响度等视觉/音频特征，缓解无声画面盲区。
