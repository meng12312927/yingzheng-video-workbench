# 智能视频粗剪助手

本地优先的中文活动视频粗剪 MVP：导入单条视频，描述剪辑目标，预览并确认 AI 候选片段后导出带字幕的 MP4。

## 能力边界

- 支持单个带音频的视频；中文转录、文本高光分析、候选片段预览与确认、字幕烧录和 MP4 导出。
- 可上传自备 BGM（循环低音量混入原声）、设置片段淡转场、可选片头/片尾和三种字幕样式；确认导出时可用 LLM 进行保守的字幕错别字/断句校对。
- 默认针对 macOS：Whisper 使用 CPU + int8，视频使用 Apple VideoToolbox 编码。
- 不包含多素材混剪、视觉语义理解、自动配乐或专业时间线。

## macOS 快速开始

```bash
brew install ffmpeg
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
# 在 .env.deepseek 中填入 LLM_API_KEY（或使用 OpenAI 配置）
.venv/bin/python run.py --ui
```

浏览器访问终端显示的本地地址。使用流程：上传视频 → 输入需求 → 生成方案 → 预览/勾选片段 → （可选）上传 BGM、设置转场、字幕样式、片头和片尾 → 确认导出。
默认会自动选择 7860–7959 中的可用端口；需要固定端口时，在 `.env` 中设置 `GRADIO_SERVER_PORT=7861` 后重试。

命令行模式：

```bash
.venv/bin/python run.py data/your-video.mp4 "剪成 3 分钟，突出开场和颁奖"
```

## 配置

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `LLM_API_KEY` | 无 | OpenAI 兼容服务的密钥，用于需求解析、文本高光分析和确认导出时的字幕校对 |
| `LLM_BASE_URL` | `https://api.openai.com/v1` | DeepSeek 使用 `https://api.deepseek.com` |
| `LLM_MODEL` | `gpt-4o-mini` | DeepSeek 可使用 `deepseek-v4-flash` |
| `WHISPER_MODEL_SIZE` | `small` | 可选 `tiny`、`small`、`large-v3` |
| `WHISPER_DEVICE` | `cpu` | macOS 默认使用 CPU |
| `WHISPER_COMPUTE_TYPE` | `int8` | CPU 推理配置 |
| `VIDEO_ENCODER` | `h264_videotoolbox` | 不可用时设置为 `libx264` |

## 验证

```bash
.venv/bin/python -m pytest -q
```

测试会验证剪辑计划、字幕校对与时间轴重映射、候选预览，以及本地 FFmpeg 的转场、BGM、字幕烧录和媒体预检能力。
