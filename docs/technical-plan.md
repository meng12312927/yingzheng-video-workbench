# 技术方案：活动视频 AI 需求、审核与验收工作台

> 版本：v3.4
> 状态：MVP2 已按本方案实现，现有 FFmpeg 粗剪流水线作为可替换执行后端
> 更新日期：2026-07-16
> 对齐文档：[PRD](PRD.md)

## 1. 技术目标

系统从固定的“LLM 分析后直接粗剪”流水线演进为面向活动视频的领域 Harness。它负责管理模型上下文、结构化需求、证据、决策、人工批准、执行状态和成片验收；FFmpeg 是当前默认执行器，不承担产品核心语义。

技术目标：

- 将模糊需求编译为稳定、可版本化、可验证的数据契约。
- 要求模型结论引用业务证据，避免只返回不可审计的自由文本。
- 将 AI 建议与用户批准分离，未经批准的计划不能正式渲染。
- 保存用户可理解的业务审计记录和独立的技术日志。
- 支持电脑处理长素材、电脑端完成需求与方案审核；移动审批延后到 MVP 验证后。
- 将验收规则前置到需求阶段，并在成片后逐项验证。
- 保留现有本地优先和确定性 FFmpeg 能力。
- 用有限多轮槽位补全代替一次性猜测需求，并用正式状态机管理推进、回退、恢复和幂等。
- 在保持视频为主素材的前提下，为转录、用户标注和后续 OCR/场景/音频证据建立统一的检索、重排与引用协议。
- 区分任务记忆、组织记忆和偏好建议；只有已确认事实才能跨阶段或跨任务复用。
- 通过可替换的模型、检索、存储和渲染边界支持本地单机 MVP 向组织化部署演进，但不提前引入未经验证的分布式复杂度。

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

### 2.4 执行器可替换但当前不泛化过度

MVP 继续使用 FFmpeg。技术上保留 `RenderBackend` 边界，但暂不实现 MCP、AE、Premiere 或 Resolve 适配器，避免为未验证需求增加复杂度。

### 2.5 复杂能力必须由指标驱动

Rerank、多模态模型、组织级数据库、服务化向量索引、本地大模型和模型微调都必须回答一个已测量的问题，例如 `must` 召回不足、专有名词返工高、无声关键画面漏检、任务无法恢复或敏感转录不能出本机。新能力必须与既有基线做同集对比并提供关闭和降级路径；不能仅因框架流行而替换已验证链路。

## 3. 目标架构

```text
电脑 Web UI / CLI
        │
        ▼
VideoProductionHarness
                       │
      ┌────────────────┼─────────────────┐
      ▼                ▼                 ▼
RequirementCompiler EvidenceRetriever CandidateAnalyzer ApprovalService
      │                │                 │
      └────────────────┼─────────────────┘
                       ▼
              AuditableEditPlanner
                       │
                 Approval Gate
                       │
                       ▼
                  RenderRouter
                       │
                 FFmpegBackend
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

Harness 不是自由对话式多 Agent 系统。各组件通过明确的数据契约协作，Orchestrator 仍是唯一流程控制者。

### 3.2 分阶段部署形态

MVP 2 保持单进程、本地任务目录和本地媒体处理，先完成业务闭环。接口边界按未来部署设计，但不要求当前拆服务。

V1.1 在真实任务验证通过后，可将任务、事件、审核和短期会话迁移到 PostgreSQL/Redis，并保留原始媒体和高码率素材在本机或受控对象存储。V1.2 只有出现多组织、多审核角色、并发任务或私有化交付需求时，才演进为以下形态：

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

- 单次最多返回 5 个澄清问题。
- `confirmed` 版本不可原地修改；修改必须生成新版本。
- `must` 和 `prohibited` 项必须具有可执行的验收规则或明确标记人工验收。

### 4.2.1 RequirementSlot 与 ClarificationTurn

多轮澄清使用结构化槽位作为事实来源，聊天消息只作为来源记录，不能直接代表当前有效需求：

```python
class RequirementSlot(BaseModel):
    key: str
    value: Any | None = None
    status: Literal["missing", "suggested", "needs_confirmation", "confirmed", "conflicted"]
    source_message_ids: list[str] = []
    confidence: float | None = None
    sensitive: bool = False

class ClarificationTurn(BaseModel):
    id: str
    task_id: str
    requirement_spec_version: int
    user_message: str
    extracted_slots: list[RequirementSlot]
    questions: list[str]
    created_at: datetime
```

`RequirementCompiler` 每轮读取当前槽位快照、最近必要消息和场景规则，输出槽位补丁而不是整份自由文本任务书。合并器使用确定性规则保护 `confirmed` 值：新模型建议只能形成 `conflicted` 或 `needs_confirmation`，不能覆盖用户确认值。任务书确认时将有效槽位确定性编译为 `RequirementSpec`；高风险槽位未确认或仍有冲突时不能通过 Gate。

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

MVP 2 只要求 `transcript` 和 `user_annotation`；其他类型预留但不承诺实现。转录证据由 ASR 片段按时间窗口合并生成，保留 `segment_ids`、源时间、原文和内容哈希，成为不可被模型改写的引用源。活动辅助资料使用 `source_uri` 和页码/行号等 `source_locator`；视觉和音频证据必须保留源时间与可预览资产。不同类型证据统一使用确认状态，模型置信度不能代替人工确认。

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

1. `raw_text + 场景规则 → RequirementSlot[] + RequirementSpec draft`。
2. `SlotCompletenessChecker` 确定缺失、冲突和高风险槽位；只有必要时进入 `awaiting_requirement_clarification`。
3. 每轮 `用户回答 → 槽位补丁 → 确定性合并 → 新任务书草稿`，已确认值不被模型静默覆盖。
4. 用户编辑任务书、解决所有阻塞槽位并批准需求版本。
5. `RequirementSpec → ExecutionBrief draft`；用户在同一次需求确认中查看并固化该说明，不增加独立审批点。
6. `search_evidence` / `expand_evidence` 工具调用 → 受限证据集 → `CandidateClip[]`。
7. `EvidenceValidator`、Schema 与领域规则校验。
8. 系统创建下一阶段可用的版本化输入。风格推荐只在用户启用样式配置时运行，且模型只能从 `StyleCatalog` 选择已有 ID。

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
- 模型调用失败时保留用户原始需求并进入人工编辑，不静默生成默认正式需求。
- 人工降级草稿必须标记 `RequirementCompilation.mode = manual_required`，显示失败警告和待确认问题，并把降级模式写入任务清单与业务审计；只有用户补充说明并批准 Gate 后才能进入证据分析。
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

### 7.3 受限工具调用与引用校验

1. 候选模型不能收到全文转录。它只能通过 `search_evidence(requirement_id, query, top_k)` 获得受限证据，并可调用 `expand_evidence(evidence_id)` 请求相邻上下文。
2. LLM 最终输出结构化候选及 `EvidenceCitation(requirement_id, evidence_id, quote, relation)`；`quote` 必须复制工具返回证据中的原文，不能改写实体字符。
3. `EvidenceValidator` 对原文和引文执行 Unicode NFKC 归一化，并忽略空白与受控标点差异，然后要求剩余引用仍是证据原文的连续子串；人名、职务、奖项和产品实体的字符不得模糊通过。
4. 验证需求 ID、证据 ID、当前批准范围、候选源时间、证据时间和素材边界。任何失败均标记 `invalid_evidence`，不得参与自动选段。
5. 语义相关性来自召回、重排和受限 LLM，只是建议；确定性代码不声称证明两个不同表述语义等价。

该规则与 PRD 保持一致，明确取消“编辑距离 5% 即自动通过”的宽松方案，避免短中文实体只差一个字时产生错误引用。

### 7.4 候选规划

去重、重叠、最短片段、`must` 覆盖和总时长由确定性代码处理；`must` 内容不因普通预算算法静默删除。用户在电脑端审核候选、证据、风险和粗剪时间线，可保留、删除、缩短、延长、替换、排序或手动添加片段；批准集合后才渲染。现有关键词加权只作为一路召回，不再作为需求覆盖、最终排序或候选理由的唯一依据。

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

MVP2 暴露给模型的工具只有 `search_evidence` 和 `expand_evidence`；`get_media_info` 与确定性校验属于 Harness 内部服务操作，不进入模型工具列表。后续可增加 `search_event_documents`、`extract_keyframes`、`ocr_frames`、`check_visual_quality` 和 `preview_clip`。`render_video`、处理交付例外和修改正式状态属于有副作用操作，必须由业务服务在有效 Gate 下调用，不能仅因模型发起 Function Calling 就执行。工具错误返回稳定错误码，并记录 trace、耗时和降级结果。

## 8. 审核与访问范围

### 8.1 电脑端

- 需求任务书编辑与确认。
- 每个阶段显示当前输入版本、系统/AI 建议、证据、风险、可操作项和下一阶段门禁状态。
- 候选列表、预览、证据和风险展示。
- 可查看当前 `ExecutionBrief`；用户编辑任务书后由确定性代码重新生成执行说明，避免直接编辑执行视图造成任务书与模型输入不一致。
- 以卡片或短预览展示最多 3 组风格建议；用户可选择整组风格或逐项替换片头、片尾、字幕和默认转场。
- 可查看全局交付规则以及逐段源时间、成片时间、标题卡、字幕和转场。
- 用户选择、字幕修正和计划汇总。
- 渲染与验收报告。

### 8.2 移动端（V1.1 以后）

二维码、局域网 token、过期、撤销和重复提交保护属于 V1.1，不进入 MVP。MVP 只要确保领域事件与 UI 无关、任务可从本地持久化产物恢复，为未来增加移动端留出接口。

### 8.3 隐私边界

- 原始视频默认不通过审核接口提供。
- 预览统一降分辨率、短片化并使用不可预测地址。
- 页面明确提示数据是否只在局域网传输。
- 云端模式上线前补充存储周期、访问控制和删除策略。

## 9. 渲染层

### 9.1 接口

```python
class RenderBackend(Protocol):
    def render(
        self,
        script: EditScript,
        video_path: str,
        output_path: str,
    ) -> ExecutionResult: ...
```

MVP 只实现 `FFmpegRenderBackend`，内部可以复用当前 `ExecutorAgent` 和 `FFmpegTool`，先完成接口提取，不改变已验证行为。

### 9.2 当前 FFmpeg 行为

- 输入预检要求可解码视频流和音频流。
- 每个片段重新编码，避免关键帧切割偏移。
- 支持拼接、淡转场、BGM、基础片头片尾和字幕烧录。
- macOS 优先 `h264_videotoolbox`，失败时回退 `libx264`。
- 最终检查文件、视频流和计划时长误差。

### 9.3 外部剪辑软件

MCP、AE、Premiere、Resolve 和 FastCut 不进入当前实施范围。未来只有在真实用户需要可编辑工程或高级包装时，才基于批准后的 `AuditableEditPlan` 增加 Adapter；外部软件不得成为需求、证据和批准的事实来源。

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
├── media_info.json
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
├── transcript.json
├── evidence.json
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
- 槽位补丁合并、已确认值保护、冲突检测、最多 5 个追问和重复问题过滤。
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
- 保存预检、风格、证据、候选、渲染和交付的版本化系统产物；人工 Gate 只用于条件需求确认与合并方案批准。
- 抽取 `FFmpegRenderBackend`。
- 实现 `VerificationEngine` 和 `DeliveryReport`。

### 阶段四：评测与可观测性

- 建立学校和企业各至少 3 个标注任务的离线评测集。
- 输出 `must` Recall@K、MRR/NDCG、证据引用有效率、幻觉引用率、人工修改率和耗时。
- 增加可选 Rerank 并与 BM25、RRF 做同集对比；无稳定收益时关闭。
- 对比“无需求编译/无证据检索”与完整链路，记录消融结果。
- 固化 Prompt、模型、工具调用、检索和校验的版本化日志。

### 阶段五：真实用户验证与 V1.1 决策

- 学校和企业各完成 3–5 个任务。
- 根据必须项召回、审核时间和返工数据调整需求包。
- 未达到 PRD 指标前不开发移动审核、完整剪辑器、MCP 或 AE Agent。

### 阶段六：V1.1 降低返工（通过阶段五门槛后）

- 抽象并迁移 PostgreSQL `TaskRepository/EventRepository`，Redis 仅承担缓存、幂等和锁。
- 支持任务恢复、局部重新分析、人物/职务/奖项确认和小规模活动辅助资料。
- 建立组织品牌、专有名词和发布限制的可管理记忆；所有记忆有来源、版本和删除入口。
- 增加 Docker Compose、健康检查、结构化日志和审核链接安全机制。

### 阶段七：V1.2 组织化与多模态（V1.1 有持续使用证据后）

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
