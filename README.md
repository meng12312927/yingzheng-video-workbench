# 映证｜活动视频需求与审核工作台

映证帮助学校和企业活动团队把模糊要求编译成可审核的剪辑任务书；用户确认后，系统从带时间戳的转录证据中检索候选片段，展示原文引用与入选理由，再由用户审核并导出带字幕的 MP4。

当前版本已完成 [PRD](docs/PRD.md) 定义的 MVP2：本地需求编译与有限澄清、事件驱动状态机、BM25 + Embedding/RRF 混合证据检索、受限工具调用、候选引文校验、合并方案审核、FFmpeg 粗剪、逐项交付验收、任务恢复和离线评测。移动审批在真实用户验证后再评估。

## 能力边界

- 支持单个带音频的视频；中文转录、稳定证据 ID、本地 BM25、可选 Embedding + FAISS（无 FAISS 时使用 NumPy）和 RRF 融合、受限 `search_evidence`/`expand_evidence` 工具调用、候选原文引文校验、片段预览与确认、字幕烧录和 MP4 导出。
- 需求任务书和剪辑计划均版本化；旧批准在修改后自动失效。任务状态、业务审计、候选取舍、模型调用元数据和验收报告保存在本地任务目录，服务重启后可恢复。
- 电脑端同页展示需求、风格建议、证据、候选和粗剪时间线；可保留/删除、排序、修剪、修改字幕与标题。渲染后逐项检查文件、音视频流、时长、批准版本、字幕及需求覆盖，异常必须修订或填写原因接受例外。
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

浏览器访问终端显示的本地地址。使用流程：上传视频并描述需求 → 编辑/确认任务书与可见 AI 执行说明 → 转录并通过受限工具检索证据 → 在合并审核页核对候选、风险和时间线并修改 → 选择可选风格 → 确认导出 → 查看逐项交付报告；有异常时返回修订或填写例外原因。
默认会自动选择 7860–7959 中的可用端口；需要固定端口时，在 `.env` 中设置 `GRADIO_SERVER_PORT=7861` 后重试。

命令行模式：

```bash
.venv/bin/python run.py data/your-video.mp4 "剪成 3 分钟，突出开场和颁奖"
```

## 配置

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `LLM_API_KEY` | 无 | OpenAI 兼容服务的密钥，用于需求编译、受限证据工具调用生成候选，以及确认导出时的保守字幕校对 |
| `LLM_BASE_URL` | `https://api.openai.com/v1` | DeepSeek 使用 `https://api.deepseek.com` |
| `LLM_MODEL` | `gpt-4o-mini` | DeepSeek 可使用 `deepseek-v4-flash` |
| `LLM_TIMEOUT_SECONDS` | `60` | LLM 与 Embedding 单次请求超时秒数；失败按有限次数重试并记录降级 |
| `EMBEDDING_API_KEY` | 无 | 可选的 OpenAI-compatible Embedding 密钥；未配置时自动使用 BM25 |
| `EMBEDDING_BASE_URL` | `https://api.openai.com/v1` | Embedding 服务地址，可与聊天模型服务不同 |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | 用于转录证据的向量召回 |
| `WHISPER_MODEL_SIZE` | `small` | 可选 `tiny`、`small`、`large-v3` |
| `WHISPER_DEVICE` | `cpu` | macOS 默认使用 CPU |
| `WHISPER_COMPUTE_TYPE` | `int8` | CPU 推理配置 |
| `VIDEO_ENCODER` | `h264_videotoolbox` | 不可用时设置为 `libx264` |

## 验证

```bash
.venv/bin/python -m pytest -q
.venv/bin/python scripts/run_eval.py
```

自动测试覆盖需求版本与澄清、状态机幂等和恢复、审核门禁、证据构建与检索、候选引文校验、受限工具调用、可审计计划、交付验收、模型调用元数据，以及本地 FFmpeg 的预检、转场、BGM、字幕、片头片尾和成片读取。离线评测读取学校/企业各 3 个标注任务并写入 `output/evaluation_report.json`，比较 BM25、RRF 与 RRF + Rerank 的 Recall@K、MRR/NDCG、延迟、引用有效率、幻觉率和人工修改率，同时保留原始需求/无证据消融及 Embedding 故障降级结果。

## 学习路线

如果需要系统接管项目并准备 AI 应用开发实习，可按 [映证项目 21 天接管与 AI 应用开发实习冲刺计划](docs/learning-plan-21-days.md) 学习。计划按每天 6–9 小时设计，包含每日代码范围、通用知识、动手实验、测试、面试问题、每周闸门和三周后的继续学习路线。
