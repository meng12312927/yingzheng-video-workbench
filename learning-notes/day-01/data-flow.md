# Day 01 数据流

## 产品主流程

请在阅读 PRD 后用自己的话补全：

```text
用户上传视频并输入需求
→检查视频能否处理
llm把模糊需求编译为结构化任务书
→用户补充确认任务书
whisper把视频转录成带时间戳的文字
→evidencebuilder把文字整理成带id的证据
embedding和bm25检索向量库，rrf排名
candidateanalyzer根据证据生成候选片段
evidencevalidator检查片段时间是否合法
planner用代码生成可执行剪辑计划
→用户审核候选片段和剪辑计划
ffmpeg进行渲染
verification engine进行验证
最终 MP4 与交付报告
```

## 主要数据示例

| 阶段 | 数据示例 | 谁创建 | 谁读取 |
| --- | --- | --- | --- |
| 用户输入 | “剪成三分钟，必须有开幕和颁奖” | 用户 | RequirementCompiler |
| 结构化任务书 | “ 字典类型：时长：180s，必须有开幕和颁奖。priority：must”| RequirmentCompiler| CandidateAnalyzer、Planner、UI、Verification Enjine |
| 转录 | start：10，end：20，text：“” | WhisperTool| EvidenceBuilder |
| 证据 | ”Evidence_id“=1| EvidenceBuilder | EvidenceRetriever、CandidateAnalyzer、EvidenceValidator |
| 候选片段 | "select_reason"=| CandidatAnalyzer | EvidenceValidator、Planner、UI、用户|
| 剪辑计划 | 成片。。 | EditPlanService、ScriptAgent| UI、用户、VerificationEngine、RenderBackend|
| 最终结果 | final.mp4| FFmpegRenderBackend | VerificationEngine、UI、用户|

## 模块架构

请补全：

```text
Web UI / CLI
→ VideoProductionHarness
   ├──RequirmentCompiler
   ├──EvidenceRetriever
   ├──CandidateAnalyzer
   ├──ApprovalService
   └──Planner
   EditPlanService
   RenderBackend
   VerificationEngine
```

## 今天的判断

### 哪些步骤可能调用 LLM

-需求分析、候选片段分析、风格推荐、字幕校对

### 哪些步骤必须由纯代码完成

-视频预剪、Pydantic字段校验、状态机转移、版本审核、证据ID、BM25、FAISS向量查找、RRF排名融合、

### 哪些步骤必须等待用户

-需求有歧义时。候选片段和计划生成时。交付存在异常时

