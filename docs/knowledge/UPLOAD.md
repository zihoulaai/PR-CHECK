# 知识库文档上传与验证指南

本目录下的 6 份开发规范文档是 PR_CHECK 知识库的语料。它们以 PR_CHECK 自身约定为样例，同时保留通用性，套用到其它项目时把具体约定换成你们团队的即可。

文档与 `DocType` 的映射（必须落在 4 个枚举值内，否则不会被 PR 自检的 focus 召回）：

| 文件 | `--doc-type` | `--module` | 命中段落 |
|---|---|---|---|
| `api-contract.md` | `api_document` | `api` | `doc_check` / `project_rules` |
| `code-style.md` | `development_rule` | `core` | `project_rules` |
| `commit-branch.md` | `development_rule` | `process` | `project_rules` |
| `error-handling.md` | `development_rule` | `core` | `project_rules` / `risk` |
| `testing-quality.md` | `technical_debt` | `qa` | `tech_debt` / `risk` |
| `security-sensitive.md` | `historical_risk` | `security` | `risk` / `tech_debt` |

`doc_type` 决定 focus 召回分支：`kb_query.py` 默认召 `doc_sync + development_rule`；含 `API_CHANGE` 召 `api_document`；含业务变更召 `technical_debt + historical_risk`。上面 4 类全覆盖，能验证全部召回路径。

## 1. 前置条件

知识库服务（MaaS 或通用 OpenAI 风格）就绪后，在 `.env` 配置：

```bash
KB_BASE_URL=https://your-kb.example.com
KB_API_KEY=xxxxxxxx
KB_INDEX=optional-index        # 部分服务可留空
KB_PROVIDER=maas               # 或 openai；默认 maas
```

确认配置生效：

```bash
pr-check config show
# 关注 kb_configured: true
```

## 2. 逐份上传

`<PROJECT>` 换成你的项目标识（如 `team/order`），它用于按 project 强制过滤、跨项目拒绝。

```bash
pr-check kb upload --file docs/knowledge/api-contract.md      --project <PROJECT> --doc-type api_document      --module api     --title "API/接口契约规范"
pr-check kb upload --file docs/knowledge/code-style.md        --project <PROJECT> --doc-type development_rule  --module core    --title "代码风格与结构规范"
pr-check kb upload --file docs/knowledge/commit-branch.md     --project <PROJECT> --doc-type development_rule  --module process --title "提交与分支规范"
pr-check kb upload --file docs/knowledge/error-handling.md    --project <PROJECT> --doc-type development_rule  --module core    --title "错误处理与错误码规范"
pr-check kb upload --file docs/knowledge/testing-quality.md   --project <PROJECT> --doc-type technical_debt    --module qa      --title "测试与质量门禁规范"
pr-check kb upload --file docs/knowledge/security-sensitive.md --project <PROJECT> --doc-type historical_risk  --module security --title "安全与敏感信息规范"
```

列出已上传：`pr-check kb list --project <PROJECT>`。

## 3. 批量上传（PowerShell）

```powershell
$docs = @(
  @{f="api-contract.md";       t="api_document";      m="api";     title="API/接口契约规范"},
  @{f="code-style.md";         t="development_rule";  m="core";    title="代码风格与结构规范"},
  @{f="commit-branch.md";      t="development_rule";  m="process"; title="提交与分支规范"},
  @{f="error-handling.md";     t="development_rule";  m="core";    title="错误处理与错误码规范"},
  @{f="testing-quality.md";    t="technical_debt";    m="qa";      title="测试与质量门禁规范"},
  @{f="security-sensitive.md"; t="historical_risk";  m="security"; title="安全与敏感信息规范"}
)
foreach ($d in $docs) {
  pr-check kb upload --file "docs/knowledge/$($d.f)" --project <PROJECT> --doc-type $d.t --module $d.m --title $d.title
}
```

bash 等价写法：

```bash
while read -r f t m title; do
  pr-check kb upload --file "docs/knowledge/$f" --project <PROJECT> --doc-type "$t" --module "$m" --title "$title"
done <<'EOF'
api-contract.md       api_document      api     API/接口契约规范
code-style.md         development_rule  core    代码风格与结构规范
commit-branch.md      development_rule  process 提交与分支规范
error-handling.md     development_rule  core    错误处理与错误码规范
testing-quality.md    technical_debt    qa     测试与质量门禁规范
security-sensitive.md historical_risk  security 安全与敏感信息规范
EOF
```

## 4. 验证 KB 是否正常

上传完成后，在本仓库跑一次自检，看报告 7 段是否真实命中：

```bash
pr-check check --repo . --project <PROJECT> --format md
```

核对要点：
- `kb_sources` 出现本次引用的规范文档 id。
- `project_rules` / `tech_debt` / `risk` 有来自知识库的条目，且其 `source_refs` 与 `kb_sources` 一致（Evidence A/B 必须真实命中，伪造引用会被降级为 C）。
- 改动 `adapters/` 或对外 schema 时应命中 `api_document`；改 `tests/` 或依赖应命中 `technical_debt`；动日志/密钥路径应命中 `historical_risk` / `risk`。

降级与报错路径（无需真实 KB 也能验）：
- 不配 `KB_*`：`pr-check check --repo .` 仍出报告，仅无知识段落（降级基础自检）。
- 故意写错 `KB_PROVIDER=unknown`：应显式报错（退出码 6 `KB_UNAVAILABLE`），不会静默回落到别的库。
- 离线/演示：`pr-check check --diff pr.diff --fake` 走 FakeKB，与真实 KB 无关。
