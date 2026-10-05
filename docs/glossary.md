# Glossary（术语表）

> PR 提交前置自检 Agent MVP 术语表。与 `docs/MVP PRD.md`、`docs/ADR-001-architecture.md` 配套。

---

## A

- **Agent Workflow**：PR Check Service 内部的线性处理流程（Query 生成 → 一次 KB 检索 → LLM 综合 → 报告渲染）。无 Reflection、无多轮 Loop。
- **api_document**：知识库 `doc_type` 之一，记录模块 API、字段、兼容性、调用约束的接口文档。

## B

- **Base Self-check Layer（基础自检层）**：不依赖知识库也运行的能力层，含变更摘要、变更类型识别、通用工程风险提示、人工自查 Checklist。
- **Base URL**：自托管 GitLab 实例地址；MVP 中可配置，使 Git API Adapter 同时支持 GitLab.com 与自托管。

## C

- **Change Profile（变更画像）**：Diff Parser 将原始 Diff 转化成的结构化事实集合（文件/模块/符号/变更类型/高影响特征）。见 PRD §11。
- **Change Type（变更类型）**：解析出的变更种类，如 `API_CHANGE`、`DATA_MODEL_CHANGE`、`CONFIG_CHANGE`、`LOGGING_CHANGE` 等。
- **CheckReport**：自检报告的结构化 JSON Schema（7 段 + `meta`），Markdown 由其渲染。见 ADR §3.2。

## D

- **development_rule**：知识库 `doc_type` 之一，记录命名/注释/日志/API/DB/配置/异常处理等开发规范。
- **Diff Parser**：轻量解析器，将大 Diff 转为结构化 Change Profile；只识别事实，不判 Bug。

## E

- **Evidence Level（证据等级）**：结论可信度分级。
  - **A**：知识库直接明确规定 / 记录 → 可「已确认 / 建议更新」。
  - **B**：知识库与当前 PR 高度相关 → 可「高度相关 / 建议重点关注」。
  - **C**：仅根据 Diff / 通用工程经验推断 → 只能「建议关注 / 建议人工确认」。
  - **N**：信息不足 → 必须「无法判断」。
  - 项目特定结论必须标注等级（ADR D9）。

## G

- **Git API Adapter**：后端组件，经 Git API **只读**获取 PR Metadata 与 Diff；非 Agent 工具。
- **Knowledge Enhancement Layer（知识增强层）**：配置知识库时，在一次 KB 检索之上叠加项目规范/接口/历史债务/历史风险增强的能力层。

## H

- **High-impact Feature（高影响特征）**：需重点关注的变更维度——Public API、Database、Configuration、Permission、Transaction、Cache、Serialization、External Dependency、Concurrency、Logging。
- **historical_risk**：知识库 `doc_type` 之一，记录历史事故/模块风险/架构风险/历史故障。

## K

- **KBHit**：一次知识库检索的命中项 `{id, title, doc_type, module, project, snippet, score}`。
- **KBQuery**：传给 `KnowledgeBaseSearch` 的结构化复合查询，含 `project`（必填）、`modules`、PR 信息、关键文件/符号、变更类型、`focus` 等。
- **KnowledgeBaseSearch**：Agent 唯一拥有的工具接口；`search(KBQuery) -> KBHit[]`；底层 Vector KB 对 Agent 透明。
- **Knowledge Base（知识库）**：可选增强组件，存放项目规范/接口文档/技术债务/历史风险的向量库。

## L

- **LLMClient**：LLM 调用抽象接口；模型名/Endpoint/鉴权全配置化，不在业务代码写死（ADR D3）。
- **Low Usage Cost（低使用成本）**：原则一——开发者直接选 PR，不手动导出 Diff / 复制 / 上传。

## M

- **MaaS**：模型即服务平台；MVP 中承载 LLM 推理与（可选）Vector KB。
- **MRRef / ProjectRef**：浏览与手动输入归一后的内部引用，Git Adapter 不区分来源（ADR D12）。

## P

- **PR Check Service**：后端服务主体，承载 Git API Adapter、Diff Parser、Change Profile Builder、Agent Workflow、Report Renderer。
- **Prompt Injection**：Diff 中的代码/注释/字符串属不可信数据，不得视为 Agent 指令（PRD §25 安全边界）。

## T

- **technical_debt**：知识库 `doc_type` 之一，记录技术债务/遗留问题/已知坑/历史 CR 问题。
- **Token（GitLab）**：只读 Personal/Project Access Token；后端加密存储、按项目隔离、永不进 LLM Context。

## V

- **Vector KB**：向量知识库；MVP 优先用 MaaS 提供，亦可独立 Vector DB，对 Agent 透明。
