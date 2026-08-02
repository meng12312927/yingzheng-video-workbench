# 映证 MVP2 系统架构

映证不是让 LLM 直接调用 FFmpeg 的“全自动剪辑 Agent”。Orchestrator 作为 Harness 控制上下文、工具权限、版本和人工 Gate；模型只生成建议，确定性服务负责引用校验、状态迁移、计划批准和交付验收。

```mermaid
flowchart LR
    U["学校/企业操作者"] --> UI["Gradio 审核工作台"]
    UI --> O["Orchestrator / Harness"]

    O --> RC["需求编译器"]
    RC --> CQ["Clarification Agent\n最多 3 个动态问题"]
    O --> ASR["Whisper 转录"]
    ASR --> RA["Requirement Alignment Agent\n素材—需求对齐建议"]

    ASR --> EI["Evidence Index\nBM25 + Embedding + RRF"]
    EI --> CA["Candidate Agent\nsearch / expand evidence"]
    CA --> EV["Evidence Validator\nID、原文、时间边界"]

    EV --> EP["版本化 Edit Plan"]
    EP --> G["人工方案审核 Gate"]
    G -->|"修改"| EP
    G -->|"批准"| FF["FFmpeg Render Backend"]
    FF --> VE["Verification Engine"]
    VE --> DR["逐项 Delivery Report"]
    DR --> UI

    O <--> TS["TaskStore\n任务状态、版本、审计事件"]
    O --> MT["Model Trace\nPrompt、Token、工具与降级记录"]
```

## 关键工程边界

- LLM 不能直接修改任务状态、批准计划或触发正式渲染。
- 候选 Agent 只能看到受限工具返回的证据窗口，不能读取未授权的完整转录。
- 引文、需求关联和检索分数由代码根据证据 ID 补齐并验证，不信任模型自由字段。
- 用户修改任务书或剪辑计划会生成新版本，使依赖旧版本的批准失效。
- FFmpeg 是可替换执行后端；产品核心是需求编译、证据审核、人工决策和验收闭环。
