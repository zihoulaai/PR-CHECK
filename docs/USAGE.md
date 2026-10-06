# PR_CHECK 使用文档（CLI-only）

面向**开发者、运维、AI 工具集成方**的端到端操作手册。读完本文即可完成：本地运行、通过 CLI 触发 PR 自检、管理知识库、理解报告。

> **定位**：本工具只做 PR 提交前**辅助自检**，**不替代人工 Code Review**，无证据不强下项目特定强结论。
> 纯命令行形态，无内置 Web 服务，无 Git 平台网络依赖。架构设计见 README「架构」章节。

---

## 1. 安装与运行

统一用 [uv](https://docs.astral.sh/uv/) 管理环境与命令。三种形态等价（`check` / `kb` / `hook` / `version` 一致）：

```bash
# A. 日常：装成全局命令（推荐，隔离环境）
uv tool install .
pr-check --help

# B. 一次性 / CI：免安装
uvx --from . pr-check check --diff pr.diff --fake

# C. 开发态：改代码 / 跑测试
uv sync --extra dev
uv run pr-check --help
```

未装 uv 或不想装机时，`python -m app.cli ...` 与旧路径 `pr-check ...`（兼容转发）同样可用；
`*nix` 还可用包装脚本 `bin/pr-check`。

```bash
cp .env.example .env          # 填写可选的 LLM/KB
```

离线 / 演示（无需真实凭据）：`pr-check check --diff pr.diff --fake`

---

## 2. 配置（`.env`）

| 变量 | 说明 | 必填 |
|---|---|---|
| `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` | OpenAI 兼容端点（如 MaaS / 硅基流动 / 本地 vLLM） | 否（未配则自检报告无 LLM 段落，仍返回基础风险） |
| `LLM_TIMEOUT_SECONDS` | 单次 LLM 请求超时（秒），默认 120 | 否 |
| `LLM_ENABLE_THINKING` | 推理模型是否输出思维链：留空=不发送该字段（兼容不支持的端点）；false=关闭（显著降低延迟与 token） | 否 |
| `KB_BASE_URL` / `KB_API_KEY` / `KB_INDEX` | 向量知识库（项目知识库）；端点/鉴权结构由 `KB_PROVIDER` 决定 | 否 |
| `KB_PROVIDER` | 知识库供应商：`maas`（默认，MaaS Vector KB）/ `openai`（通用 OpenAI 风格检索，自建 RAG 或兼容 OpenAI embeddings+search 的服务） | 否（默认 `maas`） |
| `SMALL_MAX_FILES` / `SMALL_MAX_LINES` | 三档模式的「完整分析」阈值 | 否 |
| `MEDIUM_MAX_FILES` / `MEDIUM_MAX_LINES` | 三档模式的「聚焦分析」阈值 | 否 |
| `KB_TOP_K` | 知识检索 Top-K（真正注入 adapter，不再硬编码） | 否（默认 5） |
| `DATABASE_URL` | SQLite 路径（存 KB 文档 metadata） | 否（默认用户状态目录，见下） |

**配置来源**（优先级从高到低）：当前目录 `.env` → 用户级 `%APPDATA%\pr-check\.env`（Linux/macOS：`$XDG_CONFIG_HOME/pr-check/.env`）→ 包/源码目录 `.env`。

装机形态（`uv tool install` / `uvx`）没有可读的项目根，**推荐把凭据放用户级**，避免每个仓库复制一份。
`DATABASE_URL` 默认落在用户状态目录（Windows `%LOCALAPPDATA%\pr-check\pr_check.db`、Linux/macOS `$XDG_STATE_HOME/pr-check/pr_check.db`），
换目录执行共用同一份 KB 元数据；旧版本默认 `sqlite:///./pr_check.db` 会随 cwd 漂移，如需保留旧行为显式写死即可。
排查配置来源与生效路径：

```bash
pr-check config show
# {"config_files": [...], "user_env_file": "...", "database_path": "...",
#  "llm_configured": true, "kb_configured": false, ...}
```

生成配置文件（装机形态没有 `.env.example` 可 cp，模板内嵌于代码、随 wheel 分发）：

```bash
pr-check config init                       # 生成到用户级 ~/.config/pr-check/.env（推荐）
pr-check config init --path .env           # 随仓库走（源码形态替代 cp .env.example .env）
pr-check config init --path ~/my.env --force   # 覆盖已存在文件（默认拒绝，防误冲掉凭据）
```

目标文件已存在而未传 `--force` 时返回 `INVALID_REQUEST`（退出码 2），原文件不受影响；父目录不存在会自动创建。
生成的模板与仓库根 `.env.example` 逐字节一致（`tests/test_config_paths.py` 有同步断言防漂移）。

**结构化输出**：优先 `response_format=json_schema` + `strict=true`（服务端按 `ReportSections` 的 JSON Schema 强制约束输出，Schema 中的 `$defs`/`$ref` 会自动内联展开并补全 `required`）。实测非 strict 时模型会省略 `doc_check` / `risk` 等整段，故默认 strict。降级链 `strict json_schema → json_schema → json_object`：仅当服务端以 400 且响应体指向 `response_format` 时降级一级，降级位置记入客户端实例，后续请求不再重复试探。无论哪种模式，输出仍会经 Pydantic 校验，不合契约即 `LLM_INVALID_OUTPUT`（退出码 5）。

**失败降级契约**：Git 不可用 → 整体失败（不返回半成品）；KB 不可用 → 降级为基础自检（报告无知识段落）；LLM 不可用 → 整体失败（不返回半成品）。错误信封 `{error:{code,message}}` 绝不泄露 Token / 堆栈。

---

## 3. 输入方式（本地自检，无需 Git 平台账号）

`check` 支持两种本地输入，均**不发起任何网络请求、无需 Token**：

- **diff 文本**：`--diff <file|->`（或 `--input -` 管道），直接对已有 diff 跑自检；适合 CI / 离线。
- **本地仓库**：`--repo <path>` 直连 `.git`，读取当前分支相对 `--base` 的变更并合成元数据；适合 push 前自检。

---

## 4. 子命令总览

| 子命令 | 作用 | 章节 |
|---|---|---|
| `check` | 对 diff 或本地仓库执行**完整**自检 | §5 |
| `kb` | 管理知识库文档（`upload` / `list` / `import` / `delete`） | §7 |
| `hook` | 管理 git 钩子（pre-push 拦截），配合 `--fail-on` 闸门 | §12 |
| `config` | `init` 生成 `.env` 模板；`show` 显示生效配置来源、SQLite 路径与 LLM/KB 配置状态 | §2、§6 |
| `completions` | 打印 shell 补全脚本（`bash` / `zsh` / `fish`），候选由 parser 树实时生成 | §6 |
| `version` | 版本信息 | §6 |

全局选项：`--error-stream {stdout,stderr}`（错误信封输出流，默认 stdout）、`--input FILE`（`-` 表管道，兼容 `--diff`）。

---

## 5. `check` 子命令

### 5.1 对一段 diff（离线或 diff 模式）

```bash
cat pr.diff | pr-check check --diff - --fake
pr-check check --diff pr.diff --format md      # 输出 7 段 Markdown
pr-check check --diff pr.diff --title "..." --source-branch feat/x \
    --target-branch main --author alice --project team/order
```

### 5.2 直连本地仓库（无需 Token）

push 前对本地分支做自检，直接读 `.git`，无需任何 Git 平台账号：

```bash
# 读取当前分支相对 main 的变更（diff 取 base...HEAD）
pr-check check --repo . --base main --project team/order

# 显式指定源引用（默认 HEAD，即当前分支最新提交）
pr-check check --repo /path/to/repo --base develop --source HEAD --project team/order
```

- `--repo` 即走本地 `local` 适配器，直接读 `.git`，不发起任何网络请求。
- diff 计算：`git -C <repo> diff <base>...<source>`（source 自 base 分叉以来的变更）。
- 元数据（源/目标分支、作者、标题、描述）由 `git` 合成；`--project` 用于锁定知识库范围（不给则回退为仓库路径）。
- 未提交的工作区改动不在 `HEAD` 范围内；如需自检未提交改动，请用 `--diff -` 注入：
  `git -C <repo> diff main | pr-check check --diff - --project team/order`

### 5.3 参数

| 参数 | 说明 |
|---|---|
| `--diff FILE` / `--input FILE` | diff 文件；`-` 表示从管道读取。`--input` 置于子命令之前或之后均可 |
| `--project` | 项目路径（用于 KB 检索范围与报告标题；可选） |
| `--title` / `--description` / `--source-branch` / `--target-branch` / `--author` | diff 模式附带的 PR 元数据 |
| `--repo` | 本地仓库路径（直连 `.git`，无需 Token） |
| `--base` | 本地模式目标分支（默认 main），用于计算 diff |
| `--source` | 本地模式源引用（默认 HEAD，即当前分支最新提交） |
| `--fake` | 使用离线 Fake 适配器 |
| `--format` | `json`（默认）或 `md` |
| `--pretty` | JSON 缩进美化（仅 `check` 支持） |
| `--fail-on SPEC` | 拦截闸门（可重复），命中返回退出码 7；详见 §12.1 |
| `--ci` | CI 异步模式：报告落盘 + stdout 输出 MR 评论 payload；详见 §13 |
| `-o`, `--output FILE` | 报告输出文件（配合 `--ci`；默认 `pr-check-report.md`，同时写同名 `.json`） |

`check` 输入判定：`--repo` 给定走本地 `.git`；否则必须提供 `--diff/--input` 喂入 diff 文本。

> `check` 本身**只出报告、不拦截**：即便报告有 HIGH 风险，未带 `--fail-on` 时仍返回退出码 0。

---

## 6. 其它子命令

```bash
pr-check version                 # 版本信息
pr-check config show             # 配置来源 / 数据库路径 / LLM·KB 配置状态（action 可省略）
pr-check config init             # 从内嵌模板生成 .env（详见 §2）
```

shell 补全（候选由 parser 树实时生成，新增子命令/选项零维护）：

```bash
source <(pr-check completions bash)      # bash
source <(pr-check completions zsh)       # zsh（也可存为 fpath 下 _pr-check）
pr-check completions fish | source       # fish
```

> `config` 子命令不接受 `--pretty`，输出即为单行 JSON。

---

## 7. `kb` 子命令（知识库）

原 Web 上传改为 CLI，保留完整 KB 检索能力：

```bash
# 上传知识文档（project 强制过滤，跨项目拒绝）
pr-check kb upload --file api.md --project team/order \
    --doc-type api_document --module pay --title "支付接口"

# 列出已上传文档 metadata
pr-check kb list --project team/order
pr-check kb list --doc-type api_document
pr-check kb list --project team/order --status stale   # 只看被 --prune 标记的过期来源
```

`kb upload` 参数：`--file`（必填）、`--project`（必填）、`--doc-type`（必填，取值 `development_rule` / `api_document` / `technical_debt` / `historical_risk`）、`--module`、`--title`。
`kb list` 参数：`--project` / `--module` / `--doc-type` / `--status`（`active` / `stale`，均可选过滤）。

> `--file` 只接受 UTF-8 文本（txt / md）；二进制或 GBK 等非 UTF-8 文件返回 `INVALID_REQUEST`（退出码 2）并指明解码失败位置，而非内部错误 99（FAQ Q7）。知识库未配置时 `kb upload` / `kb list` 直接返回退出码 3（`NOT_CONFIGURED`），不会静默成功。

### 删除文档（`kb delete`）

```bash
pr-check kb delete --id kb-3c550cf0fd17     # 删除向量库文档 + 本地元数据
# {"id": "kb-3c550cf0fd17", "deleted": true, "local_meta_removed": true}
```

安全语义（防误删线上文档）：

- **只接受本工具记录的 id**：本地元数据中不存在该 id → `INVALID_REQUEST`（退出码 2），绝不动向量库（防手滑删掉别人上传的文档）；
- 供应商删除失败 → `KB_UNAVAILABLE`（退出码 6），**本地元数据保留**以便重试（先删向量库再删元数据，任一步失败都不留半吊子状态）；
- 删除后 `kb list` 不再可见，检索（`check`）也不再命中该来源。

### 批量导入与过期来源标记（`kb import`）

```bash
# 递归导入整个目录（跳过隐藏文件；子目录一并遍历，title 取文件名去后缀）
pr-check kb import --dir docs/kb/pay --project team/order \
    --doc-type development_rule --module pay

# 以「本批即事实源」导入：目录里已不涉及的既有 active 文档标记 stale（不删除）
pr-check kb import --dir docs/kb/pay --project team/order \
    --doc-type development_rule --module pay --prune
```

`kb import` 参数：`--dir`（必填）、`--project`（必填）、`--doc-type`（必填，本批统一类型）、`--module`、`--prune`。

行为与退出码：

- **单篇失败不拖垮整批**：读取失败（二进制 / 非 UTF-8）或上传异常记入 stdout JSON 的 `failed`，其余继续导入；全部成功返回 **0**，有失败项返回 **6**（`KB_UNAVAILABLE` 的批量语义）——stdout 仍是完整摘要 `{imported, failed, marked_stale, total}`，stderr 有一行 `PR_CHECK[kb-import]: 共 N 个文件：成功 X，失败 Y，标记过期 Z` 汇总。
- **`--prune` 语义为标记而非删除**：把 project（+module）下不在本批的 active 文档标记 `stale`，保留审计线索；检索时被 `stale` 标记的来源不再进报告（无知识不强判契约不受污染）。被标记的 id 可由 `kb list --status stale` 查看。
- 本批文档不会误标：重复导入同批 + `--prune` 是安全的（keep_ids 保护）。
- **metadata 查询异常时放行检索结果**：本地 SQLite 故障不会丢掉有效知识，仅打 warning 跳过过滤。

### 知识库供应商（可插拔）

通过 `KB_PROVIDER` 选择底层向量库，业务层不感知差异；未配置凭据仍降级为基础自检：

```bash
# 默认 MaaS Vector KB（无需显式设置）
KB_BASE_URL=https://maas.example.com/kb  KB_API_KEY=xxx  pr-check check --repo .

# 通用 OpenAI 风格检索（自建 RAG / 兼容 OpenAI embeddings+search 的服务）
KB_PROVIDER=openai  KB_BASE_URL=https://rag.example.com  KB_API_KEY=xxx \
    pr-check check --repo .
```

> 新增供应商只需在 `app/adapters/registry.py` 的 `PROVIDERS` 登记一个实现
> `KnowledgeBase`（search / upload）的类；未知 `KB_PROVIDER` 会显式报错（退出码 6），不静默回落，避免误配用错库。

---

## 8. 三档分析模式

依据变更规模自动选择 LLM 投入程度（阈值可经 `SMALL_*` / `MEDIUM_*` 配置）：

| 模式 | 触发条件 | 行为 |
|---|---|---|
| `full` | ≤20 文件 且 ≤800 行 | 完整 Diff 送 LLM 分析 |
| `focused` | 21–80 文件 或 801–3000 行 | 仅保留高影响文件 Diff 送 LLM |
| `summary_only` | >80 文件 或 >3000 行 | 仅摘要 + 基础风险 + 人工清单，不进完整 LLM |
| `summary_only` | 0 文件 或 0 增删行 | 空 Diff、纯二进制/权限变更、纯重命名：无内容可分析，不调 LLM |

> `focused` 模式下若变更不含任何高影响特征，则保留全部 Diff（避免空 context）。
> 「高影响特征」覆盖 `HighImpactFeature` 全部取值（公共 API、数据库、配置、权限、事务、缓存、序列化、并发、外部依赖、日志）。

**模块推导**：跳过 `src` / `lib` / `app` / `main` / `java` / `resources` / `test` 等纯布局目录，取第一个有业务语义的目录段（`src/main/java/com/x/Foo.java` → `com`，`src/refund/RefundController.java` → `refund`）。`core` / `common` / `server` 等视为真实模块名，不跳过。整条路径都是布局段时回退到第一级目录。

---

## 9. 报告结构（7 段 + Evidence 等级）

`CheckReport` 包含 7 个内容段落（外加 `meta` 元信息）：

| 段 | 字段 | 内容 |
|---|---|---|
| 1 摘要 | `summary` | 一句话总览本次变更与自检结论 |
| 2 文档核查 | `doc_check[]` | API/接口文档与实现是否一致，`{item, verdict, basis, advice, evidence_level, source_refs}` |
| 3 风险提示 | `risk[]` | 工程风险，`{level, text, location, evidence_level, source_refs}`；`location` 为「文件:行号」定位（可选，LLM 参照 diff hunk 头推断，填不出则省略） |
| 4 项目规范 | `project_rules[]` | 命中项目规范，`{item, verdict, evidence_level, source_refs}` |
| 5 技术债务 | `tech_debt[]` | 关联历史技术债务，`{item, verdict, evidence_level, source_refs}` |
| 6 人工清单 | `manual_checklist[]` | 需人工确认的事项（字符串列表）。**与变更画像联动**：基础项（测试覆盖 / 异常和边界条件 / 文档同步）保底，命中变更主题（API、数据库、配置、依赖、事务、缓存、鉴权、序列化、日志、并发）时追加对应专项确认项，新增/删除/重命名文件另附文件操作确认项；LLM 输出非空时以 LLM 为准 |
| 7 知识来源 | `kb_sources[]` | 本次引用的知识文档，`{id, title, doc_type, project, module}` |

**Evidence 等级强制规则**：`A`/`B` 必须 `source_refs` 非空**且每个 id 都真实命中本次知识库检索**；未命中的引用一律剔除，引用了不存在来源的 `A`/`B` 结论会降级为 `C`（措辞同时被弱化），确保「无据强结论」无法进入报告。`C` 仅作弱化表述；`N` 必须明确「无法判断」。无知识命中时 `project_rules` / `tech_debt` 为空数组（对应「无知识不强判」）。

`--format md` 即上述 7 段的 Markdown 渲染。

---

## 10. 测试与评估

```bash
uv run pytest -q               # 单测（parser/evidence/kb_query/workflow/cli/kb/local-git）
uv run python tests/eval_harness.py   # 离线评估指标
```

离线契约测试（`tests/test_local_git.py`）直接对 `LocalGitAdapter` 断言 diff 计算与元数据合成，无需真实账号；`tests/eval_harness.py` 用 Fake 适配器跑端到端离线评估。

---

## 11. 退出码

| 退出码 | 错误码 | 含义 |
|---|---|---|
| 0 | — | 成功 |
| 2 | `INVALID_REQUEST` | 参数/输入错误 |
| 3 | `NOT_CONFIGURED` | 服务/连接未配置 |
| 4 | `GIT_*` | 本地 Git 不可用/鉴权/无权限/未找到 |
| 5 | `LLM_*` | 模型不可用/超时/限流/**输出结构异常** |
| 6 | `KB_UNAVAILABLE` | 知识库不可用 |
| 7 | `GATE_FAILED` | 命中 `--fail-on` 闸门（**非错误**：报告已正常输出，仅表示被闸门拦下） |
| 99 | `INTERNAL_ERROR` | 内部错误 |

错误时统一输出信封 `{"error":{"code":"...","message":"..."}}`（默认 stdout，可用 `--error-stream stderr` 切换）。

---

## 12. 提交拦截（git pre-push 钩子）

工具本身「只出报告、不拦截」——报告里即便有 HIGH 风险，`check` 仍返回退出码 0。要在开发者推送代码时自动拦截，需配合 git hook 与 `--fail-on` 闸门。

**hook 阻断语义**：装好钩子后，`git push` 时只有**闸门命中**（`check` 退出码 7 `GATE_FAILED`）会中断推送；而参数/Git/LLM/KB/内部错误（退出码 2/4/5/6/99）只告警放行——自检工具自身故障不应锁死全团队的推送。需要「失败也阻断」的强合规场景，设置 `PR_CHECK_STRICT=1` 即可收紧。

### 12.1 闸门（--fail-on）

`check --fail-on <spec>` 命中策略时返回专用退出码 7（`GATE_FAILED`），并在 stderr 打印命中原因；未命中返回 0。报告始终正常输出到 stdout，便于定位。

规则格式 `section:value`（可重复）：
- `risk:high` / `risk:medium` / `risk:low`：风险等级 ≥ 阈值即拦截（阈值越低越严，`risk:low` 会拦下所有**有据**风险）
- `rule:violation`：命中项目规范违反即拦截
- `doc:confirm` / `doc:update`：文档待确认/待更新即拦截
- `debt:direct_match` / `debt:related`：高度相关的技术债务即拦截

**证据门槛**：`risk:*` 只在风险项证据等级为 `A`/`B`（有可溯源知识库来源）时拦截。
`C` 级是「仅凭 Diff / 通用经验推断」、`N` 级是「无法判断」，凭推断阻断推送没有意义。
因此**未配置知识库时 `risk:*` 实际不会触发**——这是「无证据不强判」的直接后果，
需要该闸门生效请先配置 `KB_*` 并 `kb upload` 相关规范。
`rule:*` / `debt:*` 的强结论同样已由 Evidence 后校验强制要求 `A`/`B` 证据；
`doc:*` 的 `confirm`/`update` 本身即「建议确认」语义，不额外设门槛。

```bash
pr-check check --diff pr.diff --fake --fail-on risk:high --fail-on rule:violation
echo $?   # 命中 -> 7，未命中 -> 0
```

### 12.2 安装 pre-push 钩子

```bash
# 在仓库根目录执行（需为 git 仓库）
pr-check hook install --project team/order --base main \
    --fail-on risk:high --fail-on rule:violation

# 卸载
pr-check hook uninstall
```

`hook install` 会：
1. 在仓库根写入 `.pr-check.hook`（shell 可 source 的配置：PR_CHECK_PYTHON / PR_CHECK_PROJECT / PR_CHECK_BASE / PR_CHECK_FAIL_ON，
   外加 CLI 定位方式 `PR_CHECK_CLI`（源码形态）或 `PR_CHECK_MODULE`（装机形态））；
2. 把包内模板 `app/hooks/pre-push` 复制到 `.git/hooks/pre-push` 并设为可执行。

此后每次 `git push`，钩子读取 `.pr-check.hook`，对当前分支相对 `main`（或 `origin/main`）的变更跑 `check`；命中闸门则中断推送并提示（可用 `git push --no-verify` 跳过，不推荐）。钩子起检时先向 stderr 打印进度行，报告以 `--format md`（Markdown 7 段）输出到 stdout，便于人类阅读。

**stdin 引用过滤**：git 向 pre-push 钩子经 stdin 逐行传入 `<local ref> <local sha> <remote ref> <remote sha>`。钩子据此识别两类「没有相对 base 新增提交」的推送并跳过自检（stdout 提示后 `exit 0`，不阻断）：

- **删除分支**（`git push origin :feature/x`）：local sha 全 0；
- **标签推送**（`git push --tags` 或 `git push origin v1.2.3`）：remote ref 为 `refs/tags/*`。

混合推送（标签 + 真实分支）只要有真实分支更新照常在检。交互式直跑钩子（stdin 为终端，读会阻塞）同样跳过。

> **安装/卸载会保护既有钩子**：`hook install` 若探测到 `.git/hooks/<hook-name>` 已存在且非 pr-check 安装（无 `PRCHECK_MANAGED_HOOK` 标记），会先备份为 `<hook-name>.pr-check-backup` 再覆盖；`hook uninstall` 会移除钩子、自动恢复该备份，并删除 `.pr-check.hook` 配置。若目标钩子非 pr-check 安装且已手动改过内容，卸载时会跳过删除并告警，不会误删他人钩子。

> **调用方式自适应**：源码形态（`python bin/pr_check_cli.py`）安装时写入脚本绝对路径；
> 装机形态（`uv tool install` / `uvx`）写入 `PR_CHECK_MODULE=app.cli`，钩子用 `python -m app.cli` 唤起，
> 不依赖 PATH 上是否存在 `pr-check`。若解释器或模块不可用（工具被卸载 / 环境变更），
> 钩子只打印告警并放行——自检缺失不应成为推送阻断。

> 注：钩子走本地 `.git` 直连，无需任何 Token；`--project` 仅用于锁定知识库范围。base 默认 `main`，可改；若本地无该分支则退化为 `origin/<base>`。

---

## 13. CI 异步模式（`--ci`）

CI 场景的诉求与本地 pre-push 钩子相反：**不阻塞流水线**、把结果贴回 MR、报告留作 artifact。`check --ci` 即为此设计，由 CI job 在 push/MR 事件时独立触发：

```bash
pr-check check --repo . --base origin/main --project team/order --ci
```

相对普通 `check` 的行为变化：

1. **非阻断**：未显式传 `--fail-on` 时，即便检出 HIGH 风险也返回退出码 0——是否让流水线失败完全由 CI 配置决定（要拦就显式加 `--fail-on`，命中返回 7）；基础设施错误（LLM/KB 等）维持 §11 既有降级契约。
2. **双文件产物**：Markdown 报告写 `--output`（默认 `pr-check-report.md`），全量 JSON 写同名 `.json`（`pr-check-report.json`），可直接作为 CI artifact 上传。
3. **stdout 只出 payload**：一份 JSON 信封（可用 `--pretty` 缩进），字段如下——`note_body` 就是 MR 评论所需的 Markdown 全文：

| 字段 | 说明 |
|---|---|
| `schema` | payload 契约版本（当前 `pr-check-ci-payload/1`） |
| `project` / `pr_id` | 项目路径与 PR 编号（diff 模式为 0） |
| `analysis_mode` / `kb_status` | 实际分析档位与知识库状态（降级可由此看出） |
| `gate` | `{fail_on, blocked, violations}`：本次闸门配置与命中明细 |
| `exit_code` | 本次进程退出码（0 或 7） |
| `report` | 落盘的 markdown / json 文件路径 |
| `note_body` | Markdown 报告全文，可直接作为 MR 评论 body POST |

**GitLab CI 示例**（贴 MR 评论 + 留 artifact）：

```yaml
pr-check:review:
  stage: test
  image: python:3.12-slim
  variables:
    PR_CHECK_PROJECT: "team/order"
  script:
    - pip install pr-check          # 或 uv tool install / §1-B 免安装一次性格式
    - |
      pr-check check --repo . --base "origin/$CI_MERGE_REQUEST_TARGET_BRANCH_NAME" \
        --project "$PR_CHECK_PROJECT" --ci --fail-on risk:high rule:violation > payload.json
    - |
      python - <<'PY'
      import json, os, urllib.request
      payload = json.load(open("payload.json"))
      url = (f"{os.environ['CI_API_V4_URL']}/projects/{os.environ['CI_PROJECT_ID']}"
             f"/merge_requests/{os.environ['CI_MERGE_REQUEST_IID']}/notes")
      req = urllib.request.Request(
          url, method="POST",
          data=json.dumps({"body": payload["note_body"]}).encode(),
          headers={"PRIVATE-TOKEN": os.environ["GITLAB_TOKEN"],
                   "Content-Type": "application/json"})
      urllib.request.urlopen(req)
      PY
  artifacts:
    when: always                    # 评论发送失败也保留报告
    paths: [pr-check-report.md, pr-check-report.json, payload.json]
```

要点：

- CI 模式忽略 `--format`：两份文件固定为 md/json，payload 恒为 JSON；
- 退出码 7（显式 `--fail-on` 命中）会让 job 失败——这是「配了才拦」的显式选择，可在 `.gitlab-ci.yml` 里用 `allow_failure: true` 改回纯通知；
- 未配知识库时 `risk:*` / `rule:*` 不触发（证据门槛，见 §12.1）：CI 上要护栏，先配 `KB_*` 并 `kb upload`，或改用 `doc:confirm` / `doc:update`；
- 建议 `artifacts.when: always`：评论 POST 失败不影响报告留存，排查时直接下载 artifact。

---

## 14. Eval Harness（回归评测集）

对 `tests/fixtures/cases.py` 的 15 个构造用例（普通业务 / API 增删改 / 返回结构 / DB Schema / 配置 / 日志 / 依赖 / 历史债务命中 / 相似无关 / 无 KB / 无项目 / 大规模 / Prompt Injection）跑完整链路并计算多指标。每条用例自带语义标注：`should_find`（应识别的变更类型）、`should_not_claim`（不应强行声称的强结论）、`expected_sources`（配 KB 应引用的来源）、`must_be_unknown`（必须说无法判断之处）。

```bash
python tests/eval_harness.py           # Fake 模式：离线可复现（默认，CI 每日可跑）
python tests/eval_harness.py --strict  # CI 卡口：任一 FAIL 或用例异常退出码 1
python tests/eval_harness.py --real    # 真实 LLM 模式：需配齐 LLM_BASE_URL + LLM_MODEL + LLM_API_KEY
make eval                              # = Fake 模式；make eval-real = --real --strict
```

指标口径：

| 指标 | 含义 | 层 |
|---|---|---|
| Change Type Recall | `should_find` 的识别召回（画像层，无 LLM 参与，Fake/真实一致） | 画像 |
| No-Evidence Hallucination | KB 无命中时规范/债务段被置空（无知识不强判） | 报告 |
| Unknown Handling | `must_be_unknown` 标注的段落确实为空 | 报告 |
| Prompt Injection Robustness | 注入样例不得产出「完美 / 不存在 Bug」等结论 | 报告 |
| No-Strong-Claim | `should_not_claim` 关键词不出现在报告全文 | 报告 |
| Retrieval Relevance | 按 project 检索命中预期来源（Fake 预置；真实模式按线上 KB 内容人工核对，显式 skip） | 检索 |
| Report Validity | checklist 非空 + Markdown 表格列数不撕裂（跨版本合同快照） | 报告 |

要点：

- **`--real` 与 `PR_CHECK_USE_FAKE=1` 互斥**（退出码 2）；真实 LLM 未配齐三件套也返回 2，早失败优于跑出一份假回归；
- 真实模式下 Git 数据源仍是 harness 内的 FakeGitLab——case diff 是回归集输入，与 LLM 是否真实无关；
- 用例执行异常必须留痕计入 `cases_errored`，整轮不中断；`--strict` 时任何 FAIL/异常即失败；
- 画像层指标（Change Type Recall）不依赖 LLM，两种模式下应保持同一结果；真实模式的增量价值全在报告层（防幻觉 / 表格契约 / schema）。

---

## 15. 被 AI 工具调用示例

CLI 以子进程方式调用，stdout 读取 JSON 即可解析报告字段：

```python
import subprocess, json

diff = open("pr.diff", encoding="utf-8").read()
proc = subprocess.run(
    ["python", "-m", "app.cli", "check", "--diff", "-", "--fake", "--format", "json"],
    input=diff, capture_output=True, text=True,
)
if proc.returncode == 7:
    # 命中 --fail-on 闸门：报告已正常输出，可继续解析，只是流程被拦下
    pass
elif proc.returncode != 0:
    err = json.loads(proc.stdout)
    raise RuntimeError(f"PR 自检失败：{err['error']['code']} {err['error']['message']}")
report = json.loads(proc.stdout)
for risk in report["risk"]:
    print(f"[{risk['level']}] {risk['text']}")
```

> 退出码 7 单独处理：它不是故障，而是闸门命中——报告内容依然有效，不应按错误信封解析。

---

## 15. 常见问题（FAQ）

**Q1. 报告里没有 LLM 段落？**
A. 未配置 `LLM_*` 时自检仍返回基础风险段（降级契约）；配置后才会生成 LLM 段落。可用 `pr-check config show` 看 `llm_configured` 是否为 `true`。

**Q2. 错误返回非 0 但无堆栈信息？**
A. 正常业务错误（Git/LLM/KB/参数）都走统一信封 + 对应退出码（2–7/99），刻意不回显堆栈与 Token，以免凭据泄露到 CI 日志。

**Q3. 退出码 5（`LLM_INVALID_OUTPUT`）怎么排查？**
A. 表示模型返回的内容**无法被当作报告使用**，分两种：响应体不是 JSON（网关/代理拦截），或 JSON 可解析但字段结构不符合约定（字段缺失、枚举取值大小写不符、类型错误）。错误信封的 `message` 会列出具体字段路径与原因，据此判断是提示词问题还是模型能力问题。该场景不会返回半成品报告。

**Q4. 装了 `--fail-on risk:high` 却一直不拦？**
A. 多数情况是风险项证据等级只有 `C`（仅凭 Diff / 通用经验推断）。闸门要求 `A`/`B` 级证据，即必须有真实命中的知识库来源。请先配置 `KB_*` 并 `kb upload` 相关规范，再确认报告 `kb_status` 为 `success`。见 §12.1。

**Q5. `pr-check` 命令找不到？**
A. 三种安装形态都不会自动改 PATH。`uv tool install .` 需确认 `~/.local/bin` 在 PATH 中（可用 `uv tool dir --bin` 查询实际目录）；或在开发态用 `uv run pr-check ...`。

**Q6. 想排查降级原因 / 内部调用细节？**
A. 设 `PR_CHECK_DEBUG=1`（`true` / `yes` / `on` 亦可，大小写不敏感）。开启后 `pr_check` logger 输出 DEBUG 级日志到 **stderr**：KB 检索失败与跳过 stale 过滤的原因、适配器未预料异常的类型、CLI 顶层兜底的异常类型等。stdout 的结构化输出（报告 / 错误信封）不受影响，CI 解析不受干扰：

```bash
PR_CHECK_DEBUG=1 pr-check check --repo . --project team/order
```

**Q7. `kb upload` 上传 PDF / 二进制文件报错？**
A. 上传通道只接受 UTF-8 文本（txt / md）。二进制或 GBK 等非 UTF-8 文件会返回 `INVALID_REQUEST`（退出码 2），错误信息指明文件与解码失败位置——这是有意为之：向量库入库需要可检索文本，90/99 的内部错误信封没有排查价值。请先转换为 UTF-8 文本再上传。
