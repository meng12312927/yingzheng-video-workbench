# 技术方案：可审核的 AI Editing Harness 与编辑器适配

> 版本：v4.0
> 状态：分素材 Editing Harness 核心链路已实现，进入质量与适配器迭代
> 更新日期：2026-09-02
> 对齐文档：[PRD](PRD.md)

## 1. 技术目标

系统从固定的“LLM 分析后直接粗剪”流水线演进为面向活动视频的 Editing Harness。它负责按原文件理解一段或多段素材、管理模型上下文、证据化访谈、结构化需求、剪辑决策、人工批准、通用时间线、编辑器交付和验收；FFmpeg 是预览与基础交付执行器，不承担产品核心语义。

技术目标：

- 将模糊需求编译为稳定、可版本化、可验证的数据契约。
- 要求模型结论引用业务证据，避免只返回不可审计的自由文本。
- 将 AI 建议与用户批准分离，未经批准的计划不能正式渲染。
- 保存用户可理解的业务审计记录和独立的技术日志。
- 支持电脑处理长素材和完整审核，并直接建设可撤销、可过期、幂等的手机轻量审核能力。
- 将验收规则前置到需求阶段，并在成片后逐项验证。
- 保留现有本地优先和确定性 FFmpeg 能力。
- 用有限多轮槽位补全代替一次性猜测需求，并用正式状态机管理推进、回退、恢复和幂等。
- 在保持视频为主素材的前提下，为转录、用户标注和后续 OCR/场景/音频证据建立统一的检索、重排与引用协议。
- 区分任务记忆、组织记忆和偏好建议；只有已确认事实才能跨阶段或跨任务复用。
- 通过可替换的模型、检索、存储、预览渲染和编辑器 Adapter 支持本地单机向组织化部署演进；PostgreSQL/Redis/Docker Compose 进入可选实现，但本地模式继续存在。

## 2. 设计原则

### 2.1 模型负责不确定性，代码负责约束

LLM 用于：

- 自然语言需求解析与有限追问。
- 从转录证据中识别候选片段。
- 解释候选与需求的关联。
- 保守字幕校对。
- 无法完全代码化的语义验收建议。

确定性代码用于：

- Schema 校验和需求版本固化。
- 时间范围、时长预算、重叠和去重。
- 字幕时间轴映射。
- 用户批准状态和权限门禁。
- 任务状态、文件版本和审计事件。
- 渲染调用与媒体输出检查。
- 可代码验证的验收规则。

### 2.2 不展示模型内部推理

系统展示可验证的业务依据：需求编号、引用文本、源时间、规则、置信度和操作结果；不保存或展示模型隐藏思维过程。

### 2.3 先计划、后批准、再执行

所有修改遵循：

```text
生成建议 → 结构化校验 → 用户审核 → 固化版本 → 执行 → 重新读取结果 → 验收
```

所有 AI 输出默认是 `draft` 或 `suggested`，不得在没有对应批准记录的情况下驱动下游阶段。用户可以对每份建议执行：接受、局部编辑、删除、补充、重新生成、标记风险或请求人工处理。

确定性系统检查（媒体预检、时间范围、时长计算、文件校验）不能被用户强制通过；其输入、结果和错误信息应可见并写入审计记录。

### 2.4 通用时间线与可替换执行器

`CanonicalTimeline` 是编辑决定的事实来源。`PreviewRenderer` 负责生成审核预览，首个实现继续使用 FFmpeg；`EditorAdapter` 负责验证并导出剪映交接包、OTIO 或后续专业剪辑软件格式。模型不得直接写剪映私有草稿或触发有副作用的软件自动化。

### 2.5 复杂能力必须可测、可关和可降级

Rerank、多模态模型、组织级数据库、服务化向量索引、本地大模型和模型微调均进入可实施路线，但每项必须提供独立开关、基线对比、错误隔离和本地降级路径。真实用户指标不再作为启动门槛，仍用于排序、参数选择、默认启用和删减；不能仅因框架流行而替换已验证链路。

## 3. 目标架构

```text
电脑 Web UI / 手机轻审核 / CLI
        │
        ▼
VideoProductionHarness
                       │
      ┌────────────────┼────────────────────────┐
      ▼                ▼                        ▼
SourceAnalyzer   EditingInterviewAgent   ApprovalService
      │                │                        │
      └────────────────┼─────────────────┘
                       ▼
            EvidenceRetriever / CandidatePlanner
                       │
             DurationPlanner / BoundaryGuard
                       │
                       ▼
              CanonicalTimeline + Gate
                       │
          ┌────────────┼──────────────┐
          ▼            ▼              ▼
 FFmpegPreview   HandoffPackage     OTIO Adapter
                       │
                       ▼
              VerificationEngine
                       │
         ┌─────────────┴─────────────┐
         ▼                           ▼
 DeliveryReport                 AuditTimeline
```

### 3.1 Harness 职责

- 为每个阶段选择最小必要上下文。
- 决定模型可调用的能力和输出 Schema。
- 管理任务状态、需求版本、批准状态和重试边界。
- 将领域规则转换为代码校验和模型指令。
- 在执行前后建立可追溯记录。
- 识别失败原因并决定失败、降级或等待人工处理。

Harness 不是自由对话式多 Agent 系统。各组件通过明确的数据契约协作，Orchestrator 仍是唯一流程控制者。每段素材独立分析，`EditingInterviewAgent` 只能基于素材大纲和受限检索工具提问；是否写入任务书始终由用户决定。

### 3.2 分阶段部署形态

本地模式继续保持单进程、本地任务目录和本地媒体处理，作为开发、隐私和降级基线。接口边界同步实现可选服务化，但不强制本地用户安装数据库。

V1.1 直接提取 `TaskRepository/EventRepository/ArtifactStore` 协议，并提供 PostgreSQL 可选实现；Redis 只保存短期会话、幂等键、锁和缓存。原始媒体和高码率素材保留在本机或受控对象存储。多组织、多审核角色、并发任务或私有化交付可演进为以下形态：

```text
Web UI
  │
  ▼
业务控制面（可采用 Spring Boot）
  ├── 用户/组织/权限
  ├── TaskStateMachine / ReviewGate / Audit
  ├── PostgreSQL / Redis
  └── Job API
          │
          ▼
Python AI 与媒体服务
  ├── RequirementCompiler / MemoryAssembler
  ├── Retrieval / Rerank / CandidateAnalyzer
  ├── Whisper / OCR / VLM
  ├── FFmpeg / Verification
  └── ModelGateway → 云端 API 或本地 vLLM
```

Spring Boot、Elasticsearch、Milvus、对象存储和独立任务队列属于可替换实现，不是产品成立的前置条件。若单体 Python 服务和本地 FAISS 已满足规模与可靠性指标，继续使用更简单的架构。

## 4. 核心领域模型

现有 `VideoRequirement`、`HighlightClip` 和 `EditScript` 逐步迁移到以下模型；迁移期允许兼容字段存在。

### 4.1 RequirementBrief

保存用户原始输入和场景：

```python
class RequirementBrief(BaseModel):
    id: str
    scenario: Literal["school", "enterprise"]
    raw_text: str
    submitted_by: str | None
    submitted_at: datetime
```

### 4.2 RequirementSpec 与 RequirementItem

```python
class RequirementItem(BaseModel):
    id: str
    category: str
    description: str
    priority: Literal["must", "should", "optional", "prohibited"]
    status: Literal["draft", "confirmed", "needs_confirmation"]
    acceptance_rule: str | None

class RequirementSpec(BaseModel):
    id: str
    version: int
    brief_id: str
    purpose: str
    audience: str
    target_duration: float
    duration_tolerance: float
    requirements: list[RequirementItem]
    open_questions: list[str]
    status: Literal["draft", "confirmed", "superseded"]
```

规则：

- 转录后的 `MaterialInterviewAgent` 由 Harness 先检索证据，再用一次结构化调用返回最多 5 个问题，也可以返回空数组；MVP 不运行无上限的 Agent 工具循环。
- `confirmed` 版本不可原地修改；修改必须生成新版本。
- `must` 和 `prohibited` 项必须具有可执行的验收规则或明确标记人工验收。

### 4.2.1 RequirementSlot 与 ClarificationTurn

多轮澄清使用结构化槽位作为事实来源，聊天消息只作为来源记录，不能直接代表当前有效需求：

```python
class RequirementSlot(BaseModel):
    key: str
    value: Any | None = None
    status: Literal["missing", "inferred", "confirmed", "conflict", "unknown"]
    source_type: str
    source_ref: str | None
    risk_level: Literal["low", "medium", "high"]
    question: str | None
    question_reason: str | None
    question_impact: str | None
    answer_hint: str | None
    question_source: str | None

class ClarificationQuestion(BaseModel):
    slot_key: str
    question: str
    reason: str
    impact: str
    answer_hint: str | None

class ClarificationTurn(BaseModel):
    id: str
    task_id: str
    requirement_spec_version: int
    user_message: str
    extracted_slots: list[RequirementSlot]
    questions: list[str]
    created_at: datetime
```

`RequirementCompiler` 只从用户原文和表单提取明确值，生成不臆测的任务书骨架与槽位快照。完成转录后，Harness 先检索相关证据，`MaterialInterviewAgent` 一次输出最多 5 个问题。模型不能创建任意事实或直接写入任务书；未知证据、重复问题和非法结构由代码丢弃。`RequirementClarificationService` 在用户最终统一确认时把所有回答作为槽位补丁合并，并保护 `confirmed` 值不被静默覆盖；忽略项记录为 `unknown`。

### 4.2.2 ExecutionBrief：用户可见的执行说明

`ExecutionBrief` 是从已确认 `RequirementSpec` 编译出来的用户可见业务指令，也是模型分析输入的可审计部分。它不等于完整系统 Prompt：完整 Prompt 还会附加内部安全规则、工具 Schema 和结构化输出约束，这些信息不暴露给用户。

```python
class ExecutionBrief(BaseModel):
    id: str
    requirement_spec_id: str
    requirement_spec_version: int
    version: int
    visible_instruction: str
    included_requirement_ids: list[str]
    evidence_scope: list[str]
    model_task: Literal["candidate_analysis", "subtitle_review", "semantic_verification"]
    prompt_template_version: str
    status: Literal["draft", "confirmed", "superseded"]
    confirmed_at: datetime | None
```

`visible_instruction` 应包含用户认可的任务目标、时长、风格、必须/禁止项和风险确认要求；`evidence_scope` 明确本次模型允许引用哪些证据类型和范围。模型调用记录同时保存 `ExecutionBrief` 版本和内部模板版本，从而区分“需求没有说清”与“模型执行偏差”。

### 4.2.2.1 ReviewGate：统一审核门禁

只有确实需要用户作业务决定、且会改变下游输入的产物才创建版本化审核门禁。MVP2 实际使用 `requirement`（仅有关键歧义时等待）和 `edit_plan` 两类 Gate；媒体预检、风格建议、证据构建、渲染前检查和无异常验收保存为可追溯系统产物，不伪装成人工审批：

```python
class ReviewGate(BaseModel):
    id: str
    task_id: str
    stage: Literal[
        "media_preflight", "requirement", "style", "evidence",
        "candidate", "edit_plan", "render", "delivery"
    ]
    target_type: str
    target_id: str
    target_version: int
    status: Literal["pending", "approved", "changes_requested", "rejected", "superseded"]
    actor_id: str | None
    comment: str | None
    created_at: datetime
    resolved_at: datetime | None
```

约束：

- 下游服务只读取同阶段已批准的目标版本。
- 用户编辑建议会创建新版本，并将旧 Gate 标记为 `superseded`。
- `changes_requested` 必须保留用户的具体修改或意见，供重新生成时作为输入。
- 高风险项目（人名、职务、奖项、`must` 覆盖、禁止内容）不得批量静默批准。
- 媒体预检通过后自动继续；失败直接进入 `failed`，不能通过人工批准绕过。

### 4.2.3 StyleCatalog 与 StyleProposal：风格库和建议

视觉样式使用受控目录，而不是由 LLM 输出任意特效名。样式目录中的每个条目必须可由当前执行器稳定实现，并可提供缩略图、短预览或文字说明。

```python
class StyleAsset(BaseModel):
    id: str
    version: str
    category: Literal["intro", "outro", "subtitle", "transition", "segment_title"]
    name: str
    description: str
    preview_path: str | None
    supported_backends: list[str]
    brand_scope: Literal["common", "school", "enterprise", "organization"]
    config: dict
    active: bool = True

class StyleBundle(BaseModel):
    id: str
    version: str
    name: str
    intro_style_id: str
    outro_style_id: str
    subtitle_style_id: str
    default_transition_style_id: str
    brand_scope: Literal["common", "school", "enterprise", "organization"]

class StyleProposal(BaseModel):
    id: str
    requirement_spec_version: int
    bundle_id: str
    rationale: str
    confidence: float
    status: Literal["suggested", "selected", "rejected"]
```

当前 `StyleCatalog` 只需注册已经支持的样式：`classic`、`clean`、`highlight` 字幕；`cut`、`fade` 转场；`none`、`title`、`fade_black` 片头片尾。后续品牌模板和高级渲染器可扩展目录，但不得让未支持的样式进入已批准计划。

推荐流程：

1. `RequirementSpec` 提供活动用途、场景、受众、风格与品牌范围。
2. 系统筛选适用的 `StyleBundle`，LLM 最多从中推荐 3 个并说明业务原因。
3. 用户通过缩略图或短预览选择一组，或逐项替换其中的片头、片尾、字幕和默认转场。
4. 选择结果写入 `DeliverySpec` 并记录 `AuditEvent`；需求或品牌范围变化时重新确认。

对于逐片段转场和标题卡，LLM 只能提出 `StyleAsset.id` 形式的建议和可查看理由；最终选择由用户决策记录固化。

### 4.2.4 DeliverySpec：全局交付规则

时长、字幕、片头片尾、转场和输出格式不能只存在于自由文本或 UI 局部状态中，必须进入可版本化规格：

```python
class SubtitleSpec(BaseModel):
    enabled: bool = True
    style: Literal["classic", "clean", "highlight"] = "classic"
    require_name_confirmation: bool = True

class TransitionSpec(BaseModel):
    kind: Literal["cut", "fade"] = "cut"
    duration: float = Field(default=0.0, ge=0.0, le=1.0)

class CardSpec(BaseModel):
    style_asset_id: str | None = None
    style: Literal["none", "title", "fade_black"] = "none"
    duration: float = Field(default=0.0, ge=0.0, le=10.0)
    text: str = ""

class DeliverySpec(BaseModel):
    target_duration: float
    duration_tolerance: float
    aspect_ratio: Literal["16:9", "9:16", "1:1"] = "16:9"
    frame_rate: float | None = None
    subtitles: SubtitleSpec
    bgm_path: str | None = None
    bgm_volume: float = Field(default=0.15, ge=0.0, le=0.5)
    intro: CardSpec
    outro: CardSpec
    default_transition: TransitionSpec
    style_bundle_id: str | None = None
    output_format: Literal["mp4"] = "mp4"
```

`DeliverySpec` 属于 `RequirementSpec` 的已确认交付约束。当前项目的 `subtitle_style`、`transition_duration`、`intro_style`、`outro_style`、`title_text`、BGM 和目标时长逐步迁移到这里，避免确认阶段以外的 UI 参数绕过版本和批准机制。`style_bundle_id` 与具体 `StyleAsset` 版本一起保存，保证未来样式库升级后旧任务仍可重放。

### 4.3 Evidence

```python
class Evidence(BaseModel):
    id: str
    source_asset_id: str | None
    type: Literal[
        "transcript", "user_annotation", "event_document",
        "ocr", "scene", "audio", "keyframe"
    ]
    source_start: float
    source_end: float
    content: str
    confidence: float
    source_uri: str | None = None
    source_locator: str | None = None
    confirmation_status: Literal["unreviewed", "confirmed", "rejected"] = "unreviewed"
    metadata: dict = {}
```

当前实现包含 `transcript`、`user_annotation` 和独立的 `ReferenceDocumentEvidence`。转录证据保留 `source_asset_id`、`segment_ids`、来源本地时间、原文和内容哈希；活动资料证据保留文件版本与行号、单元格或 JSON 路径。OCR、场景、音频和关键帧证据仍是后续多模态路线。任何来源的模型置信度都不能代替高风险文字的人工确认。

### 4.4 CandidateClip 与 Decision

```python
class CandidateClip(BaseModel):
    id: str
    source_start: float
    source_end: float
    matched_requirement_ids: list[str]
    evidence_ids: list[str]
    selection_reason: str
    confidence: float
    suggested_duration: float
    risk_flags: list[str]

class EvidenceCitation(BaseModel):
    requirement_id: str
    evidence_id: str
    quote: str
    relation: Literal["direct", "supporting"]
    retrieval_score: float

class Decision(BaseModel):
    id: str
    subject_id: str
    action: Literal["suggest", "keep", "delete", "replace", "edit_text"]
    actor_type: Literal["system", "user"]
    actor_id: str | None
    reason: str | None
    created_at: datetime
```

模型只能创建 `suggest`；`keep`、`delete`、手动片段、建议时长修改和最终文字确认必须形成独立用户决策。候选分析的输入必须引用已批准的 `ExecutionBrief`、证据范围和交付规格版本。每个候选携带至少一个 `EvidenceCitation`；`matched_requirement_ids` 与 `evidence_ids` 只是冗余索引，不能代替引文。

### 4.4.1 TimelineSegment：逐段呈现规则

候选片段被用户批准后，规划器生成 `TimelineSegment`。它保留源时间、成片时间、证据和呈现规则，成为用户审核与渲染的共同事实来源：

```python
class SegmentPresentation(BaseModel):
    subtitle_style: str | None = None
    title_card: CardSpec = Field(default_factory=CardSpec)
    transition_after: TransitionSpec | None = None
    transition_style_asset_id: str | None = None

class TimelineSegment(BaseModel):
    id: str
    order: int
    candidate_clip_id: str
    source_start: float
    source_end: float
    timeline_start: float
    timeline_end: float
    matched_requirement_ids: list[str]
    evidence_ids: list[str]
    segment_title: str | None = None
    presentation: SegmentPresentation
    user_notes: str | None = None
```

规则：

- `source_start/source_end` 必须落在源视频范围内。
- `timeline_start/timeline_end` 必须由确定性计算产生，不能由 LLM 任意填写。
- 每段至少关联一个候选和一个需求；无证据人工片段必须标识 `user_annotation` 证据。
- `title_card` 默认为 `none`。只有用户、组织模板或已确认需求明确要求时才生成每段标题卡。
- `transition_after` 作用于当前片段与下一片段的边界；最后片段不得有该字段。
- `transition_style_asset_id` 必须引用当前 `StyleCatalog` 中由所选渲染器支持的样式；没有明确选择时继承 `DeliverySpec.default_transition`。
- 相邻片段的转场重叠、全局片头片尾时长和片段标题卡时长都进入总时长计算。

### 4.5 Approval

MVP2 不再维护一份与 `ReviewGate` 重复的 `Approval` 模型。批准事实就是绑定 `target_id + target_version` 的 `ReviewGate(status="approved")`；交付例外则记录在 `DeliveryReport`、任务状态事件和 `AuditEvent` 中。

渲染门禁要求：存在与当前 `AuditableEditPlan` 版本完全匹配的有效 `approved` 记录，且其交付规格、候选集合和确定性渲染前检查均与该计划版本一致。自动检查不是额外人工 Gate，失败时直接拒绝渲染。

### 4.6 AuditableEditPlan

```python
class AuditableEditPlan(BaseModel):
    id: str
    version: int
    requirement_spec_id: str
    requirement_spec_version: int
    delivery_spec: DeliverySpec
    timeline_segments: list[TimelineSegment]
    candidate_ids: list[str]
    execution_script: EditScript
    estimated_duration: float
    status: Literal["draft", "approved", "superseded"]
```

现有 `EditScript` 在迁移期作为其执行视图：从批准后的 `AuditableEditPlan` 确定性生成，避免 LLM 直接控制 FFmpeg。`TimelineSegment` 是审核视图，`EditOperation` 是渲染视图；二者必须可相互追溯。

### 4.7 AuditEvent

```python
class AuditEvent(BaseModel):
    id: str
    task_id: str
    event_type: str
    actor_type: Literal["system", "user"]
    actor_id: str | None
    summary: str
    target_id: str | None
    metadata: dict = {}
    created_at: datetime
```

`AuditEvent` 只记录业务事件；模型请求耗时、命令行和异常堆栈进入 `system.log`。

### 4.7.1 MemoryRecord：可管理的任务与组织记忆

```python
class MemoryRecord(BaseModel):
    id: str
    scope: Literal["task", "organization", "user"]
    scope_id: str
    kind: Literal["confirmed_fact", "brand_rule", "entity_dictionary", "preference"]
    content: dict
    source_type: Literal["user_confirmation", "approved_plan", "organization_admin"]
    source_id: str
    version: int
    status: Literal["active", "superseded", "deleted"]
    expires_at: datetime | None = None
    created_at: datetime
```

MVP 的任务记忆由现有版本化任务产物投影得到，不新增独立向量记忆库。V1.1 才引入可管理的组织记忆；所有跨任务内容必须有明确来源、作用域、版本和删除入口。`MemoryAssembler` 按当前阶段构造“最近必要消息 + 已确认槽位 + 当前规格 + 相关组织规则”的最小上下文，禁止将全部历史消息或未确认模型推断无限追加到 Prompt。

### 4.8 VerificationResult 与 DeliveryReport

```python
class VerificationResult(BaseModel):
    requirement_id: str
    status: Literal["passed", "failed", "warning", "manual_review"]
    method: Literal["deterministic", "semantic", "human"]
    summary: str
    evidence_ids: list[str] = []

class DeliveryReport(BaseModel):
    task_id: str
    output_path: str
    requirement_spec_version: int
    edit_plan_version: int
    results: list[VerificationResult]
    approved_by: str | None
    generated_at: datetime
```

## 5. 目标任务状态机

与 PRD 4.0 对齐：MVP 仅三次显式人工决策——必要时确认需求、审核候选与时间线、处理交付异常。自动通过或可跳过的步骤不使用 `awaiting_*` 状态。

```text
created
  → preflight_ok                   （自动；失败 → failed，不可绕过）
  → requirement_draft              （自动生成任务书）
  → awaiting_requirement_clarification ← 仅当 open_questions 非空或存在 needs_confirmation 项时等待
  → requirement_confirmed          （用户确认需求版本；无歧义时自动跳过上一步）
  → style_recommended / style_skipped（自动，不增加审批）
  → analyzing                      （自动：转录、索引、检索、候选与时间线生成）
  → awaiting_review                ← MVP 唯一主决策 Gate（候选、证据、时间线在同一审核页）
  → plan_approved                  （用户批准候选集合与时间线）
  → rendering                      （自动）
  → verifying                      （自动）
  → awaiting_delivery_resolution   ← 仅当验收报告含 warning/failed/manual_review 时等待
  → succeeded                      （验收无异常，或用户填写原因接受例外）

任意处理阶段 → failed（保留已完成产物用于恢复）
已批准版本被修改 → 对应 ReviewGate 标记 superseded，回到 awaiting_review
awaiting_delivery_resolution 选择返回修订 → 保留旧报告并回到 awaiting_review
```

`evidence_indexed`、`delivery_report_generated` 等作为内部阶段事件和产物记录，不再与 PRD 的用户可见任务状态并列，避免 UI、服务和文档各维护一套状态语义。

状态迁移必须由服务层方法执行，不能由 UI 直接修改字段。实现采用显式 `TaskEvent`、转移表与 Guard，不让 LLM 决定任务状态：

```python
class TransitionRule(BaseModel):
    from_state: TaskState
    event: TaskEvent
    to_state: TaskState
    guard: str
    side_effect: str | None = None

class TaskEventRecord(BaseModel):
    id: str
    task_id: str
    event: TaskEvent
    expected_state_version: int
    idempotency_key: str
    actor_id: str | None
    payload: dict
    created_at: datetime
```

约束：

- 使用状态版本或持久化锁拒绝并发覆盖；事件的 `idempotency_key` 在任务范围内唯一。
- Guard 只读取已持久化的规格、Gate 和检查结果；模型文本不能直接作为 Guard 的真假值。
- 副作用成功后再提交对应完成事件；渲染、转录等长任务保留输入版本、尝试次数和结构化错误。
- 服务重启从任务快照和事件记录恢复；不可恢复错误进入 `failed`，可恢复错误保留重试入口和已完成产物。
- 需求、候选或时间线版本变化时，状态机统一撤销受影响 Gate，禁止各 UI 回调自行实现回退逻辑。

## 6. 需求编译流程

### 6.1 分阶段调用

避免使用一条超级 Prompt：

1. 代码从 `raw_text + 表单显式值` 建立不臆测的 `RequirementSlot[] + RequirementSpec draft`；未提供的用途、受众、风格和音乐保持待确认，不调用 LLM 补常识。
2. 本地 Whisper 分别生成每段素材的转录和证据窗口；相同素材按内容哈希和 Whisper 模型复用缓存。
3. 代码先检索紧凑证据，`MaterialInterviewAgent` 一次生成 0–5 个带原因和影响说明的问题，不进行多轮工具循环。
4. 素材摘要、AI 问题、用途、受众、目标时长、风格、字幕、音乐和内容要求在同一页面展示；每题可补充或忽略，不单独提交。
5. 用户统一点击确认后，Harness 一次保存答案和字段修改、生成新任务书版本并批准当前需求 Gate。
6. 候选阶段复用转录和共享 Embedding 矩阵；代码为全部要求检索后，只调用一次 LLM 批量排序和解释。
7. 确定性代码补齐引文、来源、时间和需求关联，并按时长预算增加可验证的检索候选；用户在同一方案页审核。
8. `EvidenceValidator`、Schema 与领域规则校验后创建下一阶段版本化输入；片头、片尾、字幕和转场只能从 `StyleCatalog` 的稳定 ID 选择。

### 6.2 场景知识包

MVP 用一个配置字典驱动学校/企业差异；等真实任务验证后再根据实际差异决定是否拆分为独立模块。

```python
DOMAIN_CONFIG = {
    "school": {
        "keywords": ["班级", "年级", "班主任", "校长", "同学"],
        "must_check": ["师生姓名", "奖项名称", "领导职务"],
        "privacy": ["未成年人面部"],
    },
    "enterprise": {
        "keywords": ["产品", "客户", "合作", "CEO", "发布会"],
        "must_check": ["发言人职务", "产品名称", "合作伙伴"],
        "privacy": ["对外发布限制"],
    },
}
```

`RequirementCompiler` 根据用户选择的场景加载对应配置：追加场景关键词到 BM25 检索、将 `must_check` 项写入任务书、在需求确认阶段对高风险名称要求人工确认。

### 6.3 输出校验

- Pydantic 校验所有模型输出。
- 未知优先级、非法时长或重复 ID 直接拒绝。
- 模型不得将未经用户确认的人名和职务标为已确认。
- 快速模式的需求骨架标记为 `material_assisted`：只采用原文和表单显式值，不把模型猜测写成用户事实。旧兼容接口真正调用失败时仍可使用 `manual_required`。
- 素材访谈 Agent 失败时单独记录 `ai_failed`，不得回退到伪装成 AI 的固定问题；用户仍可直接编辑同页任务书。
- 将转录、用户需求和工具返回结果作为不可信数据包，用明确分隔符传入模型；它们不能覆盖系统指令或工具权限。
- 每次调用记录模型、提示模板版本、工具调用参数、检索证据 ID、耗时、token 和结构化错误；不记录隐藏推理。

## 7. 证据分析与选段

### 7.1 多源证据构建

1. Whisper 生成 `TranscriptSegment`；系统把相邻片段合并为带稳定 ID、时间范围、原文和哈希的 `Evidence`。
2. 用户手动补片或标注生成 `user_annotation`，必须记录操作者和来源时间。
3. V1.1 可解析活动流程表、主持稿、人物/职务/奖项名单和产品资料为 `event_document`，保留文件版本与页码/行号，只用于本次活动或明确组织作用域。
4. V1.2 可增加 OCR、场景、音频事件和关键帧描述；模型生成的视觉描述保持 `unreviewed`，不得自动确认人物身份或高风险文字。

### 7.2 召回、融合与重排

`EvidenceRetriever` 以每条已确认 `RequirementItem` 为查询，执行：

```text
QueryBuilder
  → 证据类型/任务/时间/确认状态过滤
  → BM25 召回 + Embedding 召回
  → RRF 融合
  → 可选 EvidenceReranker
  → 相邻上下文扩展
```

MVP 优先使用本地 BM25、FAISS/NumPy 和 RRF。`EvidenceIndex` 定义 `index/upsert/delete/search` 协议；只有单任务内存索引无法满足数据量、过滤、持久化或并发指标时，才评估 Elasticsearch/Milvus。服务化后端必须与本地基线使用同一评测集比较，不能只证明“能启动”。

`EvidenceReranker` 可采用 Cross-Encoder 或受限 LLM，对需求相关性、信息完整度和确认状态二次排序；音画质量、重复度和风险由 `CandidateReranker` 在片段层处理。重排只改变候选顺序，不绕过 `must` 覆盖规则或人工 Gate。Rerank 不可用或超时时降级到 RRF，并写入可观测事件而不是静默伪装为完整链路。

### 7.3 批量排序与引用校验

1. 候选模型不接收全文转录。Harness 先为全部内容要求执行混合检索，保留最多 60 个备用证据窗口，只将前 18 个紧凑窗口一次性传入排序器。模型输入上限与本地备用池上限分离，避免为限制输出 Token 而限制可用成片时长。
2. 排序器单次只返回 `evidence_id`、`requirement_ids`、简短 `reason` 和 `confidence`，输出预算固定为 1400 tokens；禁止重复字幕全文和检索分数，从结构上避免输出上限问题。
3. Harness 根据受限证据索引确定性生成 `EvidenceCitation(requirement_id, evidence_id, quote, relation, retrieval_score)`；模型额外返回的引文、时间或分数字段一律忽略。
4. 模型失败、返回空列表或所选总时长不足时，Harness 从已经记录来源、时间和原文的检索池补足候选，并标记 `code_retrieved_review_required`。这些是可审核建议，不冒充 AI 高光，也不因模型故障禁止整份方案出片。
5. 代码把候选吸附到完整 ASR 段边界，并向前后补齐明显的连接词、因果关系和问答上下文；剩余边界启发式异常作为风险提示，不再单独构成硬删除条件。
6. `EvidenceValidator` 对代码生成的引文和证据执行 Unicode NFKC 归一化，并忽略空白与受控标点差异，然后要求剩余引用仍是证据原文的连续子串；人名、职务、奖项和产品实体的字符不得模糊通过。
7. 未知来源、素材越界、未知证据、证据不在候选范围或引文被篡改仍是硬错误，不得进入时间线。语义相关性和完整表达评分只是建议，不伪装成确定性证明。

该规则与 PRD 保持一致，明确取消“编辑距离 5% 即自动通过”的宽松方案，避免短中文实体只差一个字时产生错误引用。

### 7.4 候选规划

不能在候选时长简单相加达到目标后停止召回。ScriptAgent 从包含备用片段的候选池中选择不重叠区间，按目标时长而非最低容差努力补足，并预先扣除默认转场重叠；最终计划生成后再覆盖写入 `duration_budget.json`，该报告与时间线必须一致。审核区提供无需模型调用的 `supplement_plan_duration`：保留已审片段及字幕/顺序修改，排除用户已删除范围，再从已保存转录中补选，并以 `auto_add` 决策和新计划版本记录；不会直接渲染。

候选边界通过限于原候选前后 5 秒的双滑块修改，预览同步定位并显示附近原话；播放器当前画面也可直接设为开头/结尾。界面不再要求用户凭记忆填写秒数。

去重、重叠、最短片段、`must` 覆盖和总时长由确定性代码处理；`must` 内容不因普通预算算法静默删除。用户在电脑端审核候选、证据、风险和粗剪时间线，可保留、删除、缩短、延长、替换、排序或手动添加片段；批准集合后才渲染。现有关键词加权只作为一路召回，不再作为需求覆盖、最终排序或候选理由的唯一依据。

若计划仅有 `plan_duration_too_short` 一项错误，页面显示“接受当前时长并生成”和“从原素材补入片段”两条路径。前者生成绑定计划版本、预计时长、任务书最低时长和操作者的 `PlanApprovalException`；后端仍拒绝缺失 `must`、未知证据、越界片段等其他错误。交付验收只在实际成片时长与已批准短版计划基本一致时认可该例外，避免用一次授权掩盖新的渲染偏差。后者在审核区直接展示原素材播放器，用户以双滑块拖动选择片段范围并勾选对应任务书要求；前端保存结构化补片区间，后端据此生成 `user_annotation` 证据并重建计划版本，用户不需要手写起止秒数或内部要求 ID。

### 7.5 工具注册与治理

模型可调用能力由 `ToolRegistry` 显式注册，默认拒绝未注册工具。每个工具声明输入/输出 Schema、只读或副作用级别、允许状态、超时、最大调用次数、是否需要用户批准和审计字段。

```python
class ToolPolicy(BaseModel):
    name: str
    effect: Literal["read", "compute", "write", "external"]
    allowed_states: list[TaskState]
    requires_approval: bool = False
    timeout_seconds: float
    max_calls_per_run: int
```

快速模式不让候选排序模型循环调用工具：检索、相邻上下文扩展、媒体信息和确定性校验都由 Harness 先完成，再一次性提供紧凑证据。后续视觉理解可增加 `extract_keyframes`、`ocr_frames`、`check_visual_quality` 和 `preview_clip` 等受限工具，但 `render_video`、处理交付例外和修改正式状态始终由业务服务在有效 Gate 下调用。

## 8. 审核与访问范围

默认快速路径的性能预算：需求前不调用 LLM；转录后素材访谈 1 次、候选批量排序 1 次，可选语义检查不超过 2 次，因此常规任务控制在 2–4 次 LLM 调用。5 分钟素材的首个可审核方案目标为 3–5 分钟，人工审核 3–5 分钟，基础渲染 1–3 分钟；候选预览点击时生成，不计入首屏阻塞时间。

### 8.1 电脑端

- 需求任务书编辑与确认。
- 每个阶段显示当前输入版本、系统/AI 建议、证据、风险、可操作项和下一阶段门禁状态。
- 候选列表、预览、证据和风险展示。
- 可查看当前 `ExecutionBrief`；用户编辑任务书后由确定性代码重新生成执行说明，避免直接编辑执行视图造成任务书与模型输入不一致。
- 用同一任务书表单确认整体风格、字幕和音乐；片头、片尾、字幕外观、音量和默认转场折叠为高级设置。
- 可查看全局交付规则以及逐段源时间、成片时间、标题卡、字幕和转场。
- 用户选择、字幕修正和计划汇总。
- 渲染与验收报告。

### 8.2 手机轻量审核（V1.1 直接实施）

手机端只提供任务书、候选预览、风险、意见和批准，不实现手机时间线编辑器。审核链接使用短期 Token、最小任务范围、过期、撤销和重复提交幂等保护；低码率代理可供审核，原始素材默认留在本地或受控存储。领域事件继续与 UI 无关，电脑端和手机端复用同一 Gate 与审计接口。

### 8.3 隐私边界

- 原始视频默认不通过审核接口提供。
- 预览统一降分辨率、短片化并使用不可预测地址。
- 页面明确提示数据是否只在局域网传输。
- 云端模式上线前补充存储周期、访问控制和删除策略。

## 9. 渲染层

### 9.1 接口

```python
class PreviewRenderer(Protocol):
    def render_preview(self, timeline: CanonicalTimeline, output_path: str) -> ExecutionResult: ...

class EditorAdapter(Protocol):
    def validate(self, timeline: CanonicalTimeline) -> list[VerificationResult]: ...
    def export(self, timeline: CanonicalTimeline, output_dir: str) -> EditorExportResult: ...
```

迁移期保留 `FFmpegRenderBackend` 兼容层，并实现 `FFmpegPreviewRenderer`、`HandoffPackageAdapter` 和可选 `OTIOAdapter`。内部复用当前 `ExecutorAgent` 与 `FFmpegTool`，不改变已验证的基础渲染行为。

### 9.2 当前 FFmpeg 行为

- 输入支持一段或多段视频。每段素材生成独立 `SourceAsset`，分别预检、转录、摘要和索引；分析前不统一规格、不拼接长视频。没有音轨的单段素材保存 `no_audio` 状态且不阻断其他素材。
- `material_set.json` 保存原文件、媒体信息和各自分析状态；转录、证据、候选和人工补片统一使用 `source_asset_id + source-local time`。
- 预览或交付时，FFmpeg 根据 `CanonicalTimeline` 从不同原素材取段，按批准顺序统一规格并组合。至少一段有音频即可进入语音流程；无语音素材仍保留给用户标注和后续视觉证据。
- 每个片段重新编码，避免关键帧切割偏移。
- 支持拼接、淡转场、BGM、基础片头片尾和字幕烧录。
- macOS 优先 `h264_videotoolbox`，失败时回退 `libx264`。
- 最终检查文件、视频流和计划时长误差。

### 9.3 外部剪辑软件

v4.0 直接实现通用 `EditorAdapter`、剪映稳定交接包和 OTIO 导出。Premiere、Resolve、Final Cut 等后续通过 OTIO/FCP XML/EDL 适配；剪映私有草稿和 UI 自动化只能作为关闭默认值、版本绑定的实验能力。外部软件不得成为需求、证据和批准的事实来源。

## 10. 验收引擎

### 10.1 确定性检查

- 输出文件存在且非空。
- 文件可解析并包含视频流。
- 音频要求与实际音轨一致。
- 成片时长符合目标误差。
- 所有渲染片段来自已批准候选。
- 每个 `TimelineSegment` 的源时间、成片时间、字幕样式和转场可映射到批准后的计划版本。
- `must` 项至少关联一个已批准并实际进入成片的片段。
- `prohibited` 项没有被用户或系统标记为进入成片。
- 字幕开关、片头片尾和确认文字与计划一致。

### 10.2 语义和人工检查

- 语义检查只能返回建议和证据，不能覆盖确定性失败。
- 人名、职务、奖项和品牌文字未确认时返回 `manual_review`。
- 无法从当前证据判断的视觉风格要求返回 `warning` 或 `manual_review`。

### 10.3 完成条件

任务只有在以下条件满足时才进入 `succeeded`：

- 渲染成功。
- 所有确定性检查完成。
- 没有未解释的 `failed` 项。
- 交付报告已生成。
- 最终批准状态与当前输出版本匹配。
- 存在 `warning`、`failed` 或 `manual_review` 时，用户必须选择返回修订，或填写例外说明并显式批准交付。

## 11. 任务产物

```text
output/tasks/<task_id>/
├── manifest.json
├── material_set.json             # 全部 SourceAsset 与聚合状态
├── sources/
│   ├── asset_001/
│   │   ├── source_manifest.json
│   │   ├── transcript.json
│   │   ├── evidence.json
│   │   └── material_summary.json
│   └── asset_002/
├── material_profile.json
├── requirement_brief.json
├── clarification_turns.jsonl
├── requirement_slots_v1.json
├── requirement_spec_v1.json
├── requirement_spec_v2.json
├── execution_brief_v1.json
├── execution_brief_v2.json
├── legacy_requirement_v1.json
├── delivery_spec_v1.json
├── style_proposals_v1.json
├── retrieval_trace.json
├── candidate_generation.json
├── candidate_clips.json
├── rejected_candidate_suggestions.json
├── decisions.json
├── review_gates.json
├── edit_plan_v1.json
├── edit_plan_v2.json
├── confirmed_edit_plan.json        # 迁移期 EditScript 执行视图
├── audit_events.jsonl
├── task_state.json
├── task_events.jsonl
├── model_calls.jsonl
├── previews/
├── subtitles.srt
├── subtitles_v2.srt
├── render.log
├── render_v2.log
├── execution_result.json
├── execution_result_v2.json
├── verification_report.json
├── verification_report_v2.json
├── delivery_report.json
├── delivery_report_v2.json
└── final_v2.mp4
```

JSON 继续使用临时文件加原子替换；只追加的任务事件、审计事件和模型调用记录使用 JSONL，并在写入后刷新。MVP 不同时引入数据库和复杂分布式架构。

MVP2 使用单一 `TaskStore` 隔离原子 JSON 产物和追加式日志，不提前拆出空洞的 Repository 层。V1.1 若真实使用证明需要多用户、手机审核或并发写入，再提取 `TaskRepository`、`EventRepository` 与 `ArtifactStore` 协议，并迁移到 PostgreSQL 保存任务、版本、Gate、事件和组织记忆；Redis 只用于短期会话缓存、幂等键、限流和任务锁。视频、预览和模型产物仍进入受控文件或对象存储，Redis 不是事实来源。

## 12. API 边界

建议将现有 UI 函数逐步下沉为服务接口。`search_evidence` / `expand_evidence` 是模型 tool calling 的内部方法，不暴露为 HTTP API。

```text
POST /tasks
GET  /tasks/{id}
POST /tasks/{id}/events
POST /tasks/{id}/requirements/draft
POST /tasks/{id}/requirements/clarify
POST /tasks/{id}/requirements/confirm
GET  /tasks/{id}/candidates
POST /tasks/{id}/decisions
POST /tasks/{id}/candidates/generate
POST /tasks/{id}/plans/{version}/approve
POST /tasks/{id}/render
GET  /tasks/{id}/verification
GET  /tasks/{id}/audit-events
```

第一阶段可继续单进程运行，但 UI、服务和领域逻辑不得通过全局内存对象共享唯一状态；所有审核需要用任务 ID 和版本重新读取持久化数据。`POST /events` 和有副作用接口要求幂等键与预期状态版本；内部 Tool Calling 接口不直接暴露给浏览器。

## 13. 安全与可靠性

- LLM 密钥不写入任务文件或日志。
- 用户文本、转录和模型输出在展示前转义，避免页面注入。
- 所有文件路径限定在任务目录或明确上传目录。
- 审批 token 使用高熵随机值，只保存摘要并支持撤销。
- 每次审批绑定目标 ID 和版本，版本变化自动使旧审批失效。
- 渲染接口需要幂等保护，避免重复点击产生多个任务。
- LLM 与 Embedding 调用设置显式超时和有限重试；MVP2 单进程不另建并发队列，服务化后再按实测容量设置并发上限。降级必须记录原因，不使用吞掉全部异常的静默回退。
- LLM 调用记录 provider、模型、提示模板版本、输入规格版本、耗时、token、工具轨迹和结构化错误，不记录隐藏推理或密钥。
- MVP2 由 `RequirementClarificationService`、`EditPlanService.validate` 和 `VerificationEngine` 分阶段执行高风险确认、禁止项、版本绑定和交付检查；真实规则增长后再抽取独立 `CompliancePolicyEngine`，模型始终只能产生风险建议。
- 组织记忆和辅助资料按组织、任务和角色过滤；任何检索后端都必须在召回前应用作用域与权限条件。
- 失败状态保留已完成产物，供恢复和排查。

### 13.1 ModelGateway 与本地模型

MVP2 以 `src/tools/llm.py` 的 `call_llm`、`call_llm_with_tools` 和 `embed_texts` 作为轻量 Gateway：Agent 不直接实例化 provider SDK，统一使用 OpenAI-compatible 配置、结构化输出、Function Calling、有限重试、token/耗时与错误记录。只有接入第二类非兼容 provider 或需要统一限流时，才升级为完整 `ModelGateway` 类。

V1.2 若隐私或成本指标证明有必要，可用 vLLM 部署适合中文任务的 Qwen/DeepSeek 等开源模型，并与当前云端基线比较需求槽位准确率、`must` 召回、引用有效率、TTFT、吞吐、P95 延迟、显存和单任务成本。TGI 或其他推理框架只作为同协议替代项，不在没有基准的情况下同时维护。模型微调仅在积累足够“原始需求—确认任务书”或“候选—人工决策”数据后进行，且必须优于 Prompt/RAG 基线才进入默认链路。

### 13.2 容器化与可观测性

MVP 提供可重复的本地安装和环境检查；每条模型调用记录独立 `trace_id` 并绑定 `task_id/spec_version`，记录阶段耗时、token、工具名、错误和降级。V1.1 可增加 Docker Compose、队列时间和渲染资源指标。只有出现多机部署、弹性伸缩或明确运维需求时再评估 Kubernetes/Spring Cloud。

## 14. 测试策略

### 14.1 单元测试

- 需求优先级、版本和确认规则。
- `ReviewGate` 的版本绑定、旧版本失效和不可跳关规则。
- AI 动态问题的允许槽位校验、最多 3 个追问、重复过滤，以及槽位补丁合并、已确认值保护和冲突检测。
- 状态转移表、Guard、非法跳转、状态版本和幂等事件。
- Evidence 时间范围和候选引用完整性。
- Unicode NFKC/受控标点归一化以及短中文实体不得模糊通过。
- `must` 覆盖与 `prohibited` 冲突检测。
- 受限工具的证据授权范围、未知工具拒绝和全文不可见约束。
- 审批版本绑定、修改失效和交付异常回退。
- 审计事件生成。
- 验收状态计算。

### 14.2 集成测试

- 原始需求到确认任务书。
- 多轮澄清中“新增信息/重复信息/冲突信息/不知道”的完整链路。
- 每个审核阶段的“修改 → 生成新版本 → 旧批准失效 → 重新批准”链路。
- 任务进程重启后从快照和事件恢复；重复提交不重复渲染。
- 转录到证据化候选。
- Rerank 或向量服务不可用时降级并产生可观测记录。
- 电脑审核产生的领域事件可以从任务目录重放；移动端以后复用同一领域接口。
- 未批准计划被渲染门禁拒绝。
- 批准后渲染、验收和交付报告完整。
- 需求或计划修改后旧批准自动失效。

### 14.3 评测数据集

评测分两层，使用不同的数据来源和指标。

**离线标注评测（阶段四，开发者自己标注）：**

学校和企业各标注至少 3 个任务。MVP2 仓库内的去标识评测夹具包含人工需求文本、稳定证据窗口和人工标记的基准片段集合（含起止时间、证据 ID 和需求 ID）；获授权的原始视频留在本机，不提交仓库。离线评测只跑检索、引用校验和候选评测，不跑渲染，产出：

- `must` Recall@K（基准 `must` 片段中至少被前 K 个候选覆盖的比例）。
- MRR 与 NDCG@K，用于衡量真正相关片段是否排在前面。
- 证据引用有效率（通过 `EvidenceValidator` 校验的引用数 ÷ 总引用数）。
- 幻觉引用率（引用内容在证据原文中无法匹配的比例）。
- 人工修改率（候选中被用户删除/替换/编辑的比例）。

这些指标用于离线调优 Prompt、检索参数、Rerank 和选段算法，不要求用户参与。至少保留 BM25、BM25 + Embedding/RRF、RRF + 可选 Rerank 三组可复现实验，并记录准确性、P50/P95 延迟和失败降级；没有稳定收益的复杂链路不进入默认配置。

**真实用户评测（阶段五，验证 PRD 假设）：**

学校和企业各完成 3–5 个真实任务，记录：

- 必须环节召回率（以操作者写下的基准清单为准）。
- 候选接受率、删除率、人工补片比例。
- 字幕、人名和奖项修正次数。
- 审核耗时（从打开审核页到提交批准/修改的时间）。
- 与原剪映流程相比的节省时间。
- 返工次数和未满足需求数量。

离线评测和真实评测的指标分别记录，不得混合计算。

## 15. 实施顺序

### 阶段一：领域模型与需求确认

- 新增 `RequirementBrief`、`RequirementSpec` 和版本机制。
- 将现有 `RequirementAgent` 改为需求编译器。
- 增加 `RequirementSlot`、多轮澄清、冲突处理和任务书确认页面。
- 将 PRD 状态实现为事件驱动状态机，覆盖非法跳转、幂等和本地恢复。
- 为关键动作生成 `AuditEvent`。

### 阶段二：证据化候选与工具调用

- 将转录持久化为带稳定 ID、哈希和时间范围的可引用证据。
- 实现关键词/BM25 + Embedding/FAISS 混合检索和 RRF 融合。
- 实现受限 `search_evidence`、`expand_evidence` 工具调用和结构化候选输出。
- 实现原文引文、证据范围、时间边界、需求 ID 与 `must` 覆盖校验。
- UI 展示证据、风险和覆盖情况；改造 `ScriptAgent`，优先保证 `must` 项覆盖。

### 阶段三：批准与验收

- 新增计划版本、批准门禁和批准失效规则。
- 保存预检、风格、证据、候选、渲染和交付的版本化系统产物；人工 Gate 只用于条件需求确认与剪辑方案批准。
- 抽取 `FFmpegRenderBackend`。
- 实现 `VerificationEngine` 和 `DeliveryReport`。

### 阶段四：评测与可观测性

- 建立学校和企业各至少 3 个标注任务的离线评测集。
- 输出 `must` Recall@K、MRR/NDCG、证据引用有效率、幻觉引用率、人工修改率和耗时。
- 增加可选 Rerank 并与 BM25、RRF 做同集对比；无稳定收益时关闭。
- 对比“无需求编译/无证据检索”与完整链路，记录消融结果。
- 固化 Prompt、模型、工具调用、检索和校验的版本化日志。

### 阶段五：真实用户验证与优先级校准

- 学校和企业各完成 3–5 个任务。
- 根据必须项召回、审核时间、人工补片和返工数据调整默认开关、参数和实施顺序。
- 指标不阻塞已经批准的 V1.1 工程路线，但未经验证不得宣称节省时间或减少返工。

### 阶段六：V1.1 Editing Harness 与降低返工（核心能力已实现，持续优化）

- 实现多素材独立分析、素材大纲、受限上下文工具和证据化剪辑访谈。
- 实现完整表达边界、问答上下文保护、确定性时长规划和候选多样性。
- 实现 `CanonicalTimeline`、FFmpeg 预览、剪映交接包和 OTIO Adapter。
- 抽象并迁移 PostgreSQL `TaskRepository/EventRepository`，Redis 仅承担缓存、幂等和锁。
- 支持任务恢复、局部重新分析、人物/职务/奖项确认和小规模活动辅助资料。
- 建立组织品牌、专有名词和发布限制的可管理记忆；所有记忆有来源、版本和删除入口。
- 增加手机轻审核、Docker Compose、健康检查、结构化日志和审核链接安全机制。

### 阶段七：V1.2 组织化与多模态

- 按漏检和返工数据依次验证 OCR、场景、音频事件、关键帧描述，不一次性接入所有模型。
- 若本地 FAISS 无法满足规模/过滤/并发指标，再比较 Elasticsearch/Milvus 等服务化索引。
- 通过 `ModelGateway` 比较云端模型与 vLLM 本地 Qwen/DeepSeek 的质量、隐私、延迟和成本。
- 出现多组织、多角色和并发生产需求时，可用 Spring Boot 承担业务控制面，Python 保留 AI 与媒体服务。
- 只有积累足够人工确认数据且 Prompt/RAG 达到基线后，才对需求抽取或候选排序开展微调实验。

## 16. 迁移约束

- 保留现有 FFmpeg 渲染和字幕时间轴测试，防止方向调整破坏已实现能力。
- 现有 `VideoRequirement` 可作为 `RequirementSpec` 的兼容执行视图，不一次性删除。
- 现有 `HighlightClip` 逐步增加稳定 ID、需求引用和证据引用。
- 现有 `EditScript` 保留为执行 DTO，由批准后的计划生成。
- Gradio UI 的进程内对象只作运行时组件与任务缓存；业务状态已落入任务目录，缓存丢失后通过任务 ID、快照和版本化产物恢复。
- 文档和 UI 不再将“多 Agent”作为用户价值宣传；技术实现是否使用 Agent 不影响产品定位。
- LangChain、LlamaIndex、Spring Cloud、Kubernetes、Elasticsearch、Milvus 和微调框架均不得仅为技术栈覆盖而引入；每次替换必须保留协议边界、基线结果、降级路径和迁移说明。
