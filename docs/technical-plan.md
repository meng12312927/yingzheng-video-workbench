# 技术方案：智能视频粗剪助手（MVP）

> 版本：v1.0  
> 日期：2026-07-07  
> 目标：在本地 Windows 环境下，实现"长视频 + 模糊需求 → 自动粗剪成片"的 MVP 能力

---

## 1. 项目目标

本项目旨在为学校老师、企业行政、公众号编辑等非专业剪辑用户提供一个本地运行的 AI 视频粗剪助手。

### MVP 要达成的能力
- 支持导入单个长视频
- 支持输入一句话剪辑需求
- 自动完成：
  - 需求解析
  - 音频转写
  - 视频粗分段
  - 关键片段提取
  - 字幕生成与烧录
  - 视频拼接导出
- 输出一个可直接使用的 mp4 粗剪成片

### MVP 不做的事情
- 不做实时编辑器
- 不做云端服务
- 不做复杂多素材混剪
- 不做多机位处理
- 不做特效模板市场
- 不做专业时间线编辑

---

## 2. 总体架构

### 2.1 架构概览

系统采用**本地单机任务流架构**，核心流程为：

1. 用户导入视频
2. 用户输入需求
3. 系统解析需求并生成结构化参数
4. 提取音频并进行 ASR 转写
5. 基于转写文本进行分段与片段筛选
6. 生成字幕文件
7. 调用 ffmpeg 完成裁剪、拼接、烧录字幕
8. 导出最终视频

---

## 3. 模块划分

## 3.1 前端交互层
负责用户输入、任务进度展示、结果预览与导出。

### 功能
- 视频文件选择
- 需求文本输入
- 任务开始/取消
- 进度条展示
- 结果状态展示
- 输出文件路径展示

### 推荐实现
- 桌面端 UI
- 可选方案：
  - PySide6 / Qt
  - Electron + 本地后端
  - Streamlit（仅适合内部测试，不建议正式桌面版）

---

## 3.2 任务调度层
负责整个处理流程的状态管理与任务串联。

### 功能
- 任务创建
- 任务状态记录
- 阶段性进度更新
- 异常重试
- 中断处理

### 状态建议
- `PENDING`
- `RUNNING`
- `ASR_DONE`
- `SEGMENT_DONE`
- `SUBTITLE_DONE`
- `EXPORTING`
- `DONE`
- `FAILED`

---

## 3.3 需求理解模块
将用户输入的自然语言需求转换为结构化参数。

### 输入
- 用户需求文本

### 输出
- 结构化剪辑参数，例如：
  - 目标时长
  - 风格
  - 优先保留内容
  - 删除内容
  - 是否保留完整讲话
  - 是否需要字幕

### 实现方式
- 使用 LLM 解析
- 关键信息缺失时触发追问
- 输出 JSON 结构

### 示例输出
```json
{
  "target_duration_min": 5,
  "style": "formal",
  "priority": ["opening", "highlights"],
  "avoid": ["redundant", "static"],
  "subtitle": true
}
```

---

## 3.4 语音转写模块（ASR）

将视频中的语音转为带时间戳的文字，是整个分析链路的入口。

### 输入
- 视频文件路径

### 输出
- 带时间戳的转写文本，格式如下：
```json
[
  {"start": 0.0, "end": 3.5, "text": "各位老师同学们大家好"},
  {"start": 3.5, "end": 7.2, "text": "欢迎来到本届运动会"}
]
```

### 技术方案
- **Faster-Whisper**（本地部署）
- 模型选型：`large-v3`（准确率最高）
- 硬件加速：NVIDIA GPU（CUDA + float16）
- 语言：中文为主，支持自动检测
- 关键参数：
  - `beam_size=5`（搜索宽度，越大越准但越慢）
  - `vad_filter=True`（过滤静音段）

### 为什么不用云端 API
- 免费（一次部署永久使用）
- 离线可用（不受网络影响）
- 数据不出本地（隐私安全）
- RTX 4060 跑 large-v3 实时率可达 50x

---

## 3.5 内容分析模块

基于转写文本，识别视频结构并进行内容价值判断。

### 输入
- 转写文本（来自 3.4）
- 结构化需求参数（来自 3.3）

### 输出
- **粗分段结果**：视频被分为几个逻辑大段
  ```json
  [
    {"segment_name": "开幕式", "start": 0, "end": 600},
    {"segment_name": "田径比赛", "start": 600, "end": 1800}
  ]
  ```
- **关键片段列表**：每个片段的重要性打分
  ```json
  [
    {"start": 60.0, "end": 75.5, "importance": 0.95, "reason": "冠军冲刺瞬间"}
  ]
  ```

### 实现逻辑
1. **文本分块**：将长转录文本切成 LLM 能处理的段落（每段约 20 个句子，重叠 5 句防止截断）
2. **逐块分析**：每块送给 LLM 打分，识别高价值片段
3. **关键词加权**：用户指定的关注词出现时提升分数
4. **去重排序**：移除时间重叠超过 50% 的片段，按重要性降序排列

### 难点与对策
| 难点 | 对策 |
|------|------|
| 无语音画面（如纯跑步画面） | MVP 阶段通过环境音关键词（欢呼、哨声）间接判断；V2 加入视觉分析 |
| 超长视频（>2小时） | 分块分析 + 滑动窗口，控制 LLM 调用次数 |
| LLM 打分不稳定 | 温度参数设低（0.3），同一片段多次评估取平均 |

---

## 3.6 字幕生成与剪辑脚本模块

将分析结果转化为可执行的"施工图纸"。

### 输入
- 关键片段列表（来自 3.5）
- 用户需求（来自 3.3）

### 输出
- **SRT 字幕文件**：标准字幕格式，可直接烧录
  ```
  1
  00:00:00,000 --> 00:00:03,500
  选手冲过终点线，全场欢呼！
  ```
- **剪辑脚本 JSON**：时间轴编排方案
  ```json
  {
    "title": "运动会精彩集锦",
    "operations": [
      {"action": "title", "text": "精彩回顾", "duration": 2},
      {"action": "cut", "source_start": 60, "source_end": 75.5},
      {"action": "transition", "type": "fade", "duration": 0.5},
      {"action": "cut", "source_start": 120, "source_end": 140}
    ]
  }
  ```

### 实现逻辑
1. **片段筛选**：按重要性从高到低选取，直到累计时长接近目标
2. **时间排序**：选中片段在成片中按原片时间顺序排列
3. **转场决策**：硬切为主，情绪转折处加淡入淡出
4. **字幕生成**：从转写文本中提取对应时间段的文字，转为 SRT 格式

---

## 3.7 视频处理模块

执行实际的视频裁剪、拼接与字幕烧录。

### 输入
- 剪辑脚本（来自 3.6）
- 原视频文件

### 输出
- 成品 mp4 文件

### 技术方案
- **FFmpeg**（命令行调用）
- GPU 硬件编码：`h264_nvenc`（NVIDIA 显卡专属，比 CPU 快 5-10 倍）
- 处理步骤：
  1. 逐个裁剪：`ffmpeg -ss {start} -i input.mp4 -t {duration} -c:v h264_nvenc seg_N.mp4`
  2. 拼接：使用 concat demuxer 将所有片段合并
  3. 字幕烧录：`ffmpeg -i merged.mp4 -vf subtitles=subs.srt output.mp4`

### 画质保障策略
| 步骤 | 方案 | 画质损失 |
|------|------|---------|
| 裁剪 | 尽可能使用 `-c copy` 无损复制 | 0% |
| 拼接 | GPU 编码，CRF=18（接近无损） | <1% |
| 字幕烧录 | 唯一必须重新编码的步骤，CRF=18 | <1% |

---

## 4. 多 Agent 协作架构

系统采用 **5 个 Agent 分工协作** 的架构，每个 Agent 负责一个独立子任务，通过 Orchestrator（编排器）串联。

### 4.1 架构图

```
用户上传视频 + 输入需求 "剪成5分钟精华版"
              │
              ▼
    ┌─────────────────┐
    │   Orchestrator   │  ← 总调度：分发任务、传递结果、处理异常
    └────────┬────────┘
             │
    ┌────────▼────────┐
    │  Agent 1        │  需求理解
    │  RequirementAgent│  输入：用户自然语言
    │  (GPT-4o-mini)  │  输出：VideoRequirement（结构化参数）
    └────────┬────────┘
             │
    ┌────────▼────────┐
    │  Agent 2        │  内容分析
    │  AnalysisAgent  │  输入：视频 + 需求
    │  (Whisper+GPT)  │  输出：转录文本 + 关键片段列表
    └────────┬────────┘
             │
    ┌────────▼────────┐
    │  Agent 3        │  剪辑脚本生成
    │  ScriptAgent    │  输入：分析结果 + 需求
    │  (GPT-4o-mini)  │  输出：剪辑脚本 + SRT 字幕
    └────────┬────────┘
             │
    ┌────────▼────────┐
    │  Agent 5        │  质量审核（可选）
    │  ReviewAgent    │  检查：时长、逻辑、流畅度
    │  (GPT-4o-mini)  │  不合格 → 退回 Agent 3 重排
    └────────┬────────┘
             │
    ┌────────▼────────┐
    │  Agent 4        │  执行处理
    │  ExecutorAgent  │  输入：剪辑脚本 + 原视频
    │  (FFmpeg)       │  输出：成品 mp4
    └─────────────────┘
```

### 4.2 Agent 间数据协议

所有 Agent 之间的通信使用 **Pydantic 数据模型** 作为"合同"，保证格式统一：

| 传递方向 | 数据模型 | 关键字段 |
|---------|---------|---------|
| Agent 1 → 2, 3 | `VideoRequirement` | target_duration, video_type, style, focus_keywords |
| Agent 2 → 3 | `ContentAnalysis` | transcript, highlights (带 importance 打分) |
| Agent 3 → 4, 5 | `EditScript` | operations (裁剪/转场/字幕操作列表) |
| Agent 4 → Orchestrator | `ExecutionResult` | success, output_path, errors |

### 4.3 为什么用多 Agent 而不是一个 Agent

| 维度 | 单 Agent | 多 Agent |
|------|---------|---------|
| 单个 Prompt 长度 | 极长（需包含所有指令） | 每个 Agent 的 Prompt 短且专注 |
| 出错定位 | 难以排查 | 每个 Agent 独立，快速定位 |
| 迭代优化 | 改一处影响全局 | 独立优化单个 Agent |
| 可扩展性 | 差 | 随时增加新 Agent（如视觉分析） |
| 面试展示 | "我调了个 prompt" | "我设计了多 Agent 协作架构" |

---

## 5. 技术选型总览

| 层 | 技术 | 选型理由 |
|----|------|---------|
| 编程语言 | Python 3.11+ | AI/ML 生态最完善 |
| LLM 调用 | OpenAI API（GPT-4o-mini） | 便宜、JSON 模式稳定 |
| 语音识别 | Faster-Whisper large-v3 | 本地免费、GPU 加速 |
| 视频处理 | FFmpeg + MoviePy | 工业标准、命令行可控 |
| Agent 编排 | 自研 Orchestrator（Python） | 轻量、可理解、面试可讲 |
| 前端 UI | Gradio | 10 行代码出界面，适合 MVP |
| 数据校验 | Pydantic v2 | Agent 间通信的类型安全 |
| 配置管理 | python-dotenv | API Key 不出现在代码中 |

---

## 6. 目录结构

```
video-agent-pipeline/
├── docs/
│   ├── PRD.md                 # 产品需求文档
│   └── technical-plan.md      # 技术方案（本文件）
├── data/                      # 输入视频存放
├── output/                    # 成品输出
├── src/
│   ├── orchestrator.py        # 编排器（总调度）
│   ├── config.py              # 全局配置
│   ├── agents/                # 5 个 Agent
│   │   ├── base.py            # Agent 基类
│   │   ├── requirement_agent.py
│   │   ├── analysis_agent.py
│   │   ├── script_agent.py
│   │   ├── executor_agent.py
│   │   └── review_agent.py    # Agent 5（V2）
│   ├── tools/                 # 底层工具
│   │   ├── whisper.py         # 语音识别
│   │   ├── ffmpeg.py          # 视频处理
│   │   └── llm.py             # LLM 调用封装
│   ├── models/
│   │   └── schemas.py         # Pydantic 数据模型
│   └── ui/
│       └── app.py             # Gradio 界面
├── .env                       # API Key（不提交 git）
├── .env.example               # .env 模板
├── requirements.txt           # Python 依赖
└── run.py                     # 一键启动入口
```

---

## 7. 开发阶段规划（含学习路径）

本项目的特殊性在于：开发者同时也是 Python 初学者。因此计划按"学一点、写一点、理解一点"的节奏推进，每个模块先理解原理再写代码。

---

### Phase 0：地基（Day 1-3）

| 天数 | 学习内容 | 实际产出 |
|------|---------|---------|
| Day 1 | Python 是什么、变量/函数/列表/字典的概念 | 环境确认（Python/FFmpeg/CUDA/Git 安装） |
| Day 2 | `if` 判断、`for` 循环、缩进规则 | `config.py` —— 项目配置中心 |
| Day 3 | `import` 是什么、`class` 是什么、`self` 是什么 | PRD + 技术方案文档 |

---

### Phase 1：底层工具（Day 4-8）

每完成一个工具，立即可以独立测试。

| 天数 | 文件 | 学习重点 | 产出 |
|------|------|---------|------|
| Day 4 | `tools/whisper.py`（上） | `class`、`__init__`、`self`、属性 vs 方法 | 理解 WhisperTool 的类结构 |
| Day 5 | `tools/whisper.py`（下） | `list[dict]`、列表推导式、f-string 格式化 | 完成 `transcribe()` + `transcribe_with_speakers()` |
| Day 6 | `tools/ffmpeg.py`（上） | `@staticmethod`、`subprocess.run()`、`Path` 对象 | 理解 FFmpegTool 的设计 |
| Day 7 | `tools/ffmpeg.py`（下） | 正则表达式、`try/except`、`with open()` | 完成全部视频处理函数 |
| Day 8 | 缓冲 / 复习 | 两个文件对比：什么时候用普通方法、什么时候用静态方法 | whisper.py + ffmpeg.py 全部理解 |

---

### Phase 2：数据与大脑（Day 9-13）

| 天数 | 文件 | 学习重点 | 产出 |
|------|------|---------|------|
| Day 9 | `models/schemas.py` | Pydantic `BaseModel`、`Field`、`Optional`、类型校验 | 全部数据模型定义 |
| Day 10 | `tools/llm.py` | OpenAI API 调用、JSON Mode、重试机制 | LLM 调用封装 |
| Day 11 | `agents/base.py` | 继承（`class Xxx(BaseAgent)`）、`@abstractmethod`、日志 | Agent 基类 |
| Day 12 | `agents/requirement_agent.py` | Prompt Engineering、System Prompt 设计原则 | Agent 1：需求理解 |
| Day 13 | 缓冲 | 回顾 5 个数据模型 + 3 个工具的协作关系 | 理解"数据如何流动" |

---

### Phase 3：智能 Agent（Day 14-19）

| 天数 | 文件 | 学习重点 | 产出 |
|------|------|---------|------|
| Day 14 | `agents/analysis_agent.py`（上） | Whisper + LLM 混合调用、文本分块策略 | Agent 2 的思路和架构 |
| Day 15 | `agents/analysis_agent.py`（下） | 去重算法、关键词加权、滑动窗口 | Agent 2 完整实现 |
| Day 16 | `agents/script_agent.py` | 贪心算法（选片段）、SRT 格式、降级方案 | Agent 3：脚本生成 |
| Day 17 | `agents/executor_agent.py` | `tempfile`、`shutil`、异常处理链 | Agent 4：执行处理 |
| Day 18 | `orchestrator.py` | 编排器模式、状态机概念、Pipeline 设计 | 5 个 Agent 串联 |
| Day 19 | 缓冲 / 端到端测试 | 用测试视频跑通全流程，debug | 第一个成品视频 |

---

### Phase 4：产品化（Day 20-25）

| 天数 | 内容 | 学习重点 | 产出 |
|------|------|---------|------|
| Day 20 | `ui/app.py` | Gradio 基础、回调函数、进度条 | Web 界面上传 → 下载 |
| Day 21 | `run.py` | 命令行参数、入口函数设计 | 一键启动（CLI + UI 双模式） |
| Day 22 | 错误处理 + 日志 | `logging` 模块、日志级别、优雅降级 | 全局异常保护 |
| Day 23 | 性能优化 | Faster-Whisper 参数调优、FFmpeg GPU 编码验证 | 处理速度目标验证 |
| Day 24 | 换不同视频测试 | 运动会、竞赛、会议各一段 | 鲁棒性验证 |
| Day 25 | 缓冲 | 修 bug + 代码整理 | — |

---

### Phase 5：简历与面试（Day 26-30）

| 天数 | 内容 | 产出 |
|------|------|------|
| Day 26 | `README.md` | 项目介绍、效果展示、快速开始、架构图 |
| Day 27 | `docs/architecture.md` | 详细架构说明 + 数据流图 |
| Day 28 | Docker 化（可选加分项） | `Dockerfile` + 部署说明 |
| Day 29 | 简历话术 | STAR 版项目描述 + 技能关键词整理 |
| Day 30 | 模拟面试 | 15 个高频问题 + 逐行解释代码的能力验证 |

---

### 30 天学习曲线

```
Phase 0 ██ （基础概念，坡度最陡）
Phase 1 ████ （工具层，反复练习 class/self）
Phase 2 ██████ （更顺了，开始理解架构）
Phase 3 ████████ （核心逻辑，成就感最强）
Phase 4 ███ （产品化，相对简单）
Phase 5 ██ （收尾，输出为主）
```

> 说明：每天约 1-2 小时。Phase 0-1 建议不要跳，基础不牢后面代码看不懂。Phase 3 是最关键的一周，Agent 2 是整个系统里最复杂的模块。
