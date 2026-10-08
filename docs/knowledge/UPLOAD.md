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

`doc_type` 决定 focus 召回分支：`kb_query.py` 默认召 `development_rule + api_document`；含任意业务变更（非纯测试/注释）再召 `technical_debt + historical_risk`。上面 4 类全覆盖，能验证全部召回路径。

> focus 取值必须落在 4 个 `DocType` 枚举值内。此前 `kb_query.py` 曾硬编码过 `doc_sync`——它不在枚举里，服务端按 `doc_type` 过滤时永远匹配不到，而 `or hits` 兜底会把这个失效静默吞掉。现在 focus 一律走 `DocType` 枚举，非法值无法再写进去。

## 1. 前置条件

知识库服务（MaaS 或通用 OpenAI 风格）就绪后，在 `.env` 配置：

```bash
KB_BASE_URL=https://your-kb.example.com
KB_API_KEY=xxxxxxxx
KB_INDEX=optional-index        # 部分服务可留空
KB_PROVIDER=maas               # maas / openai / dify；默认 maas
```

### 各供应商的差异（影响上传与召回效果）

| 供应商 | 上传写入 | 检索侧约束 |
|---|---|---|
| `maas` / `openai` | 提交结构化 `project` / `module` / `doc_type` | 响应自带这些维度，`doc_type` 可用于 focus 过滤 |
| `dify` | 文档名写入 `[project] 标题` 前缀作为归属标记 | 片段不带任何维度：靠**文档名标记 + 本地 `KbDoc` 元数据**（key = `document.id`）回填；检索文本受 250 字符上限约束，故 focus 排在高位并逐段限额 |

> `dify` 的 dataset 可能混放多个项目的文档。归属标记 + 本地元数据都无法确认归属的命中会被保留但标记 `metadata_resolved=false`，此时 `project` 只是按查询回填，**不足以支撑跨项目的 A 级证据**。

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

### 排查「无主文档」

`kb list` 只列**本地注册表**（经 `kb upload` 记录过的文档）。向量库里可能还有
他人上传、元数据丢失或迁移遗留的文档——它们**会被检索命中并进入报告**（在
`kb_sources` 里出现），却既不在 `kb list` 默认输出里，也删不掉。

用 `--remote` 列出向量库侧实际内容，并看 `managed` 标记：

```bash
pr-check kb list --remote
# managed=true  → 本地有记录，kb delete 可直接删
# managed=false → 无主文档，需要 --force
```

清理无主文档：

```bash
pr-check kb delete --id <DOC_ID> --force
```

> `--force` 是对默认护栏的显式越过：默认只删注册表记录过的 id，以防误删线上文档。
> 越过后向量库与本地元数据仍按「先向量库、后本地」的顺序处理——向量库删除失败会
> 抛 `KB_UNAVAILABLE`（退出码 6）并保留本地记录，不会出现「本地已删、线上还在」
> 这种无从重试的中间态。
> `--remote` 需要供应商支持列举（Dify 支持；MaaS / OpenAI 风格未实现，会显式报
> `不支持列举` 而非静默返回空表）。

## 多项目：按项目分库

同一知识库混放多个项目的文档时，检索会把**别的项目的规范**一起召回，并被当作
本项目的 A/B 级证据写进报告——本工具的整套证据契约都建立在「来源可溯源且归属正确」
之上，跨项目串档会从根上破坏它。

实测结论：Dify 的 `metadata_filtering` **接受参数但静默忽略**（`project=一个必然
不存在的值` 仍照常返回记录），文档也无法携带 project 元数据。因此唯一可靠手段是
**物理分库**：

```bash
# 同一份凭据（dataset 级密钥通常覆盖全部 dataset），每项目一个 dataset id
KB_DATASET_MAP='{"team/order":"<dataset-id-1>","pr-check":"<dataset-id-2>"}'
```

上传时按 project 路由到对应库：

```bash
pr-check kb upload --file docs/knowledge/api-contract.md --project team/order \
    --doc-type api_document --module api --title "API/接口契约规范"
```

删除与列举在分库模式下需要项目归属：

```bash
# 受管文档会自动从本地元数据反查 project，无需显式指定
pr-check kb delete --id <doc-id>

# 无主文档（--force）没有归属记录，必须显式指定，不猜——猜错就是删掉别的项目的数据
pr-check kb delete --id <doc-id> --force --project team/order

# 列举必须指明项目
pr-check kb list --remote --project team/order
```

### 严格路由：未绑定 = 没有知识库

配置了 `KB_DATASET_MAP` 后，项目未命中映射**不会回落到 `KB_INDEX`**，而是
`kb_status=no_dataset`，报告会明确写：

> 本项目未绑定知识库（KB_DATASET_MAP 未命中），本次为基础自检。闸门依赖 A/B 级证据，在此状态下不会触发。

这是刻意设计：若允许回落，「忘了给某项目建库」就会静默使用共享库——隔离形同虚设，
而且没有任何报错。用 `pr-check config show` 查看 `kb_routing` 与 `kb_datasets`
即可确认哪些项目还没建库。

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
