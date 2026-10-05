# ADR-001 PR 提交前置自检 Agent MVP 架构

- **状态**：Accepted
- **日期**：2026-10-05
- **范围**：PR_CHECK 项目 MVP
- **关联文档**：`docs/MVP PRD.md`（产品定义）、`docs/glossary.md`（术语）

---

## 1. 背景（Context）

我们希望在正式 Code Review 之前，给开发者一个**轻量、低成本**的「PR 提交前置自检」辅助工具：自动拉取 PR 元信息与 Diff，形成结构化变更画像，做基础工程风险提示，并在配置项目知识库时通过一次检索叠加项目规范/历史风险增强，最终由 LLM 生成自检报告。

grilling 过程中锁定的硬约束：

- 绿色field 起步；MVP 先验证产品与 Agent 效果，**不提前引入微服务 / 消息队列 / 复杂任务编排**。
- 工具只做「前置辅助自检」，**不替代**人工 Code Review，不下「绝对无 Bug」结论。
- Git API 是数据源，**不是** Agent 自主工具；Agent 仅拥有 `KnowledgeBaseSearch`。
- 知识库是增强能力、**不是**运行前提；无证据不下项目特定强结论。
- **供应商解耦**：模型 / Endpoint / Vector KB 后端均对业务代码透明、可配置，不被具体供应商实现绑死。

---

## 2. 决策（Decision）

### 2.1 总体架构

```text
Web UI（轻量单页）
   │  ProjectRef + MRRef
   ▼
PR Check Service（Python / FastAPI，单体部署）
   ├─ Git API Adapter（实现 GitPlatformAdapter）  （GitLab.com + 自托管，base_url 可配置，只读）
   ├─ Diff Parser            （规则 + 语言可插拔，零额外 LLM 成本）
   ├─ Change Profile Builder
   ├─ Agent Workflow          （线性：Query 生成 → 一次 KB 检索 → LLM 综合）
   └─ Report Renderer         （CheckReport JSON → Markdown）
        │
        ├── LLMClient ─────────────► MaaS / LLM
        └── KnowledgeBaseSearch ────► MaaS Vector KB（或独立 Vector DB，对 Agent 透明）
```

### 2.2 核心接口契约

| 接口 | 签名 / 形态 | 说明 |
|------|------------|------|
| `ProjectRef` / `MRRef` | 内部值对象 | 浏览模式与手动输入模式归一后的引用；Git Adapter 不区分来源 |
| `GitPlatformAdapter` | `list_projects() / list_mrs(proj) / get_mr(ref) / get_diff(ref)` | Git 平台抽象接口；MVP 仅实现 `GitLabAdapter`，其余平台后续新增实现即可，Agent 流程不动 |
| `LLMClient` | `complete(system, user) -> text` | 模型名 / Endpoint / 鉴权全配置化，不在业务代码写死 |
| `KnowledgeBaseSearch` | `search(KBQuery) -> KBHit[]` | Agent 唯一工具；底层 Vector KB 对 Agent 透明 |
| `KBQuery` | 结构化复合查询（见 §3.1） | `project` 必填 |
| `KBHit` | `{id, title, doc_type, module, project, snippet, score}` | 检索命中项 |
| `ChangeProfile` | 文件/模块/符号/变更类型/高影响特征 | PRD §11 |
| `CheckReport` | 7 段结构化报告（见 §3.2） | Markdown 由其渲染 |

### 2.3 关键技术决策（D1–D15）

| ID | 决策 |
|----|------|
| D1 | 交付物 = 精炼 PRD + 本 ADR + Glossary；暂不写实现代码 |
| D2 | Python + FastAPI 单体；轻量单页 Web UI（React/Vue 优先）；不引入分布式 |
| D3 | `LLMClient` 抽象；模型/Endpoint/鉴权可配置；KB 对 Agent 透明 |
| D4 | GitLab.com + 自托管；经 `GitPlatformAdapter` 抽象，MVP 仅实现 GitLabAdapter；Token 后端加密、按项目隔离、永不进 LLM；仅申请 Read 权限 |
| D5 | 知识库可选增强；文档上传入库；人工指定/确认 `doc_type`；不做自动爬仓库/Wiki/历史 PR 建库 |
| D6 | Diff Parser 纯规则 + 语言可插拔；MVP 覆盖 Java/Python/TS(JS)/Go，其余降级为文件级+关键词 |
| D7 | 大 PR 三档阈值（≤20 文件且 ≤800 行 / 21–80 文件或 801–3000 行 / >80 文件或 >3000 行），可配置 |
| D8 | 单次 KB 检索；结构化 `KBQuery`；Top-K = 5（可配置） |
| D9 | Evidence 等级**结构化强制**（A/B/C/N + `source_refs`）；C 级强制弱化、N 级必须「无法判断」 |
| D10 | 报告结构化 `CheckReport` JSON；Markdown 由该 JSON 渲染 |
| D11 | 失败降级：Git 失败终止；KB 失败降级基础自检；LLM 失败整体失败 + 重试入口、不出半成品 |
| D12 | 浏览 + 手动输入归一为 `ProjectRef + MRRef` |
| D13 | 多项目知识隔离两层强制：`KBQuery.project` 必填 + KB Adapter/向量存储服务端按 project 过滤 |
| D14 | 单租户、无独立登录；按项目隔离；Token 后端加密保存、绝不进 LLM |
| D15 | 报告不持久化（ephemeral，当前会话展示即弃）；原始 Diff/Token/敏感字段不落库 |

---

### 2.4 GitPlatformAdapter 抽象

为延续 D3 的 Adapter 解耦模式，Git 接入也定义为接口，使 MVP 仅绑定 GitLab 但不积累多平台技术债：

```text
GitPlatformAdapter（接口）
  ├─ list_projects() -> ProjectRef[]
  ├─ list_mrs(proj: ProjectRef) -> MRRef[]
  ├─ get_mr(ref: MRRef) -> PRMetadata
  └─ get_diff(ref: MRRef) -> Diff
       │
       └─ GitLabAdapter（MVP 唯一实现：GitLab.com + 自托管，base_url 可配置，仅 Read）
```

- 方法统一以 `ProjectRef` / `MRRef` 为参数与返回，Git Adapter 不感知用户来源（浏览 or 手动输入）。
- Token 由后端加密存储、按项目注入，不进入 LLM / 报告 / KB。
- 后续支持 GitHub / Gerrit / Bitbucket：仅新增对应 `XxxAdapter` 实现，Agent Workflow、Diff Parser、Report Renderer 均不改动。

---

## 3. 契约细节

### 3.1 KBQuery / KBHit

```json
// KBQuery —— 单次复合查询，project 必填
{
  "project": "order-service",
  "modules": ["refund"],
  "pr_title": "...", "pr_description": "...",
  "key_files": ["..."], "key_symbols": ["RefundService.refund"],
  "change_types": ["API_CHANGE", "DATA_MODEL_CHANGE"],
  "api_changes": ["..."], "data_changes": ["..."],
  "config_changes": ["..."], "logging_changes": ["..."],
  "keywords": ["refund", "refund_status"],
  "focus": ["development_rule", "api_document", "technical_debt", "historical_risk", "doc_sync"]
}

// KBHit[] —— 返回（Agent 只消费 snippet + source_ref，不接触向量库内部）
[{ "id": "...", "title": "...", "doc_type": "technical_debt",
   "module": "refund", "project": "order-service", "snippet": "...", "score": 0.83 }]
```

### 3.2 CheckReport

```json
{
  "meta": {"pr_id": 1234, "generated_at": "...", "model": "<cfg>", "kb_used": true},
  "summary": "本次 PR 主要包括 ...",
  "doc_check": [{"item": "API文档", "verdict": "confirm", "basis": "...", "advice": "...", "evidence_level": "B", "source_refs": ["kb-123"]}],
  "risk": [{"level": "medium|low", "text": "...", "evidence": "C", "source_refs": []}],
  "project_rules": [{"item": "命名", "verdict": "ok|unknown", "evidence": "A|B|C|N", "source_refs": ["..."]}],
  "tech_debt": [{"item": "...", "verdict": "related", "evidence": "B", "source_refs": ["..."]}],
  "manual_checklist": ["API兼容性", "测试覆盖", "异常和边界条件", "数据库迁移", "配置同步", "日志敏感信息", "文档同步"],
  "kb_sources": [{"id": "...", "title": "...", "doc_type": "..."}]
}
```

映射 PRD §23 的 7 个固定段落；`project_rules` / `tech_debt` 在 `kb_used=false` 或空命中时为空数组（对应「无知识不强判」）。

---

## 4. 能力边界（In / Out of MVP）

**In**：Git API 获取 PR/Diff、Diff Parser、Change Profile、变更摘要、通用风险提醒、人工自查 Checklist、知识库检索（可选）、项目规范/历史风险增强（有知识时）、单次检索、结构化 Evidence。

**Out（明确不做，详见 PRD §29 / §5）**：非 GitLab 平台适配（GitHub / Gerrit / Bitbucket——接口已通过 `GitPlatformAdapter` 预留，仅未实现）、写代码 / Push / Approve / Merge、自动修复、自动测试、完整 Code Review、多轮 Agent Loop、Git 自动评论、跨项目读知识、自动建库、多用户登录、报告持久化。

---

## 5. 后果与权衡（Consequences）

- 供应商解耦带来少量 Adapter 样板代码，但避免 MVP 被具体模型 / Vector KB 绑死（D3 / D5）。
- 纯规则 Diff Parser 跨语言脆弱，但零 LLM 成本、可复现，符合「低成本」原则与 V1.2「转确定性规则」路线（D6）。
- Evidence 结构化强制增加 LLM 输出约束与后校验成本，但让「强结论准确率 / 误报率」可测（D9 / D10）。
- 单租户无登录简化 MVP；多用户场景需二次评估（D14）。
- 报告不持久化降低存储与合规复杂度，但放弃历史趋势（D15，后续可只存非敏感派生字段）。

---

## 6. 后续路线（Deferred，非 MVP）

V1 自动触发（Webhook）· V1.1 PR Comment · V1.2 确定性规则（Rules + Knowledge + LLM）· V2 多路 KB 检索 · 多 Git 平台（新增 `GitPlatformAdapter` 实现）· 多用户认证 · 报告历史。

---

## 7. 参考

- PRD：`docs/MVP PRD.md`
- Glossary：`docs/glossary.md`
