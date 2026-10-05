# PR_CHECK 使用文档（CLI-only）

面向**开发者、运维、AI 工具集成方**的端到端操作手册。读完本文即可完成：本地运行、通过 CLI 触发 PR 自检、管理知识库、理解报告。

> **定位**：本工具只做 PR 提交前**辅助自检**，**不替代人工 Code Review**，无证据不强下项目特定强结论。
> 纯命令行形态，无内置 Web 服务，无 Git 平台网络依赖。架构设计见 `docs/ADR-002-multi-platform.md`。

---

## 1. 安装与运行

```bash
pip install -r requirements.txt
cp .env.example .env          # 填写可选的 LLM/KB
python bin/pr_check_cli.py --help
```

离线 / 演示（无需真实凭据）：`python bin/pr_check_cli.py check --diff pr.diff --fake`

`*nix` 可用包装脚本 `bin/pr-check`（若存在）；Windows 直接 `python bin/pr_check_cli.py ...`。

---

## 2. 配置（`.env`）

| 变量 | 说明 | 必填 |
|---|---|---|
| `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` | OpenAI 兼容端点（如 MaaS / Azure / 本地 vLLM） | 否（未配则自检报告无 LLM 段落，仍返回基础风险） |
| `KB_BASE_URL` / `KB_API_KEY` / `KB_INDEX` | MaaS Vector KB（项目知识库） | 否 |
| `SMALL_MAX_FILES` / `SMALL_MAX_LINES` | 三档模式的「完整分析」阈值 | 否 |
| `MEDIUM_MAX_FILES` / `MEDIUM_MAX_LINES` | 三档模式的「聚焦分析」阈值 | 否 |
| `KB_TOP_K` | 知识检索 Top-K | 否 |

**失败降级契约**：Git 不可用 → 整体失败（不返回半成品）；KB 不可用 → 降级为基础自检（报告无知识段落）；LLM 不可用 → 整体失败（不返回半成品）。错误信封 `{error:{code,message}}` 绝不泄露 Token / 堆栈。

---

## 3. 输入方式（本地自检，无需 Git 平台账号）

`check` 支持两种本地输入，均**不发起任何网络请求、无需 Token**：

- **diff 文本**：`--diff <file|->`（或 `--input -` 管道），直接对已有 diff 跑自检；适合 CI / 离线。
- **本地仓库**：`--repo <path>` 直连 `.git`，读取当前分支相对 `--base` 的变更并合成元数据；适合 push 前自检。

---

## 4. 子命令总览

| 子命令 | 作用 |
|---|---|
| `check` | 对 diff 或真实 Git MR/PR 执行**完整**自检 |
| `hook` | 管理 git 钩子（pre-push 拦截），配合 `--fail-on` 闸门 |
| `kb` | 管理知识库文档（`upload` / `list`） |
| `version` | 版本信息 |

全局选项：`--error-stream {stdout,stderr}`（错误信封输出流，默认 stdout）、`--input FILE`（`-` 表管道，兼容 `--diff`）。

---

## 5. `check` 子命令

### 5.1 对一段 diff（离线或 diff 模式）

```bash
cat pr.diff | python bin/pr_check_cli.py check --diff - --fake
python bin/pr_check_cli.py check --diff pr.diff --format md      # 输出 7 段 Markdown
python bin/pr_check_cli.py check --diff pr.diff --title "..." --source-branch feat/x \
    --target-branch main --author alice --project team/order
```

### 5.3 直连本地仓库（无需 Token）

push 前对本地分支做自检，直接读 `.git`，无需任何 Git 平台账号：

```bash
# 读取当前分支相对 main 的变更（diff 取 base...HEAD）
python bin/pr_check_cli.py check --repo . --base main --project team/order

# 显式指定源引用（默认 HEAD，即当前分支最新提交）
python bin/pr_check_cli.py check --repo /path/to/repo --base develop --source HEAD --project team/order
```

- `--repo` 即走本地 `local` 适配器，直接读 `.git`，不发起任何网络请求。
- diff 计算：`git -C <repo> diff <base>...<source>`（source 自 base 分叉以来的变更）。
- 元数据（源/目标分支、作者、标题、描述）由 `git` 合成；`--project` 用于锁定知识库范围（不给则回退为仓库路径）。
- 未提交的工作区改动不在 `HEAD` 范围内；如需自检未提交改动，请用 `--diff -` 注入：
  `git -C <repo> diff main | python bin/pr_check_cli.py check --diff - --project team/order`

### 5.4 参数

| 参数 | 说明 |
|---|---|
| `--diff FILE` / `--input FILE` | diff 文件；`-` 表示从管道读取 |
| `--project` | 项目路径（用于 KB 检索范围与报告标题；可选） |
| `--title` / `--description` / `--source-branch` / `--target-branch` / `--author` | diff 模式附带的 PR 元数据 |
| `--repo` | 本地仓库路径（直连 `.git`，无需 Token） |
| `--base` | 本地模式目标分支（默认 main），用于计算 diff |
| `--source` | 本地模式源引用（默认 HEAD，即当前分支最新提交） |
| `--fake` | 使用离线 Fake 适配器 |
| `--format` | `json`（默认）或 `md` |
| `--pretty` | JSON 缩进美化 |

`check` 输入判定：`--repo` 给定走本地 `.git`；否则必须提供 `--diff/--input` 喂入 diff 文本。

---

## 6. 其它子命令

```bash
python bin/pr_check_cli.py version
```

---

## 7. `kb` 子命令（知识库）

原 Web 上传改为 CLI，保留完整 KB 检索能力：

```bash
# 上传知识文档（project 强制过滤，跨项目拒绝）
python bin/pr_check_cli.py kb upload --file api.md --project team/order \
    --doc-type api_document --module pay --title "支付接口"

# 列出已上传文档 metadata
python bin/pr_check_cli.py kb list --project team/order
python bin/pr_check_cli.py kb list --doc-type api_document
```

`kb upload` 参数：`--file`（必填）、`--project`（必填）、`--doc-type`（必填，见 `DocType`）、`--module`、`--title`。
`kb list` 参数：`--project` / `--module` / `--doc-type`（均可选过滤）。

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
| 3 风险提示 | `risk[]` | 工程风险，`{level, text, evidence_level, source_refs}` |
| 4 项目规范 | `project_rules[]` | 命中项目规范，`{item, verdict, evidence_level, source_refs}` |
| 5 技术债务 | `tech_debt[]` | 关联历史技术债务，`{item, verdict, evidence_level, source_refs}` |
| 6 人工清单 | `manual_checklist[]` | 需人工确认的事项（字符串列表） |
| 7 知识来源 | `kb_sources[]` | 本次引用的知识文档，`{id, title, doc_type, project, module}` |

**Evidence 等级强制规则**：`A`/`B` 必须 `source_refs` 非空**且每个 id 都真实命中本次知识库检索**；未命中的引用一律剔除，引用了不存在来源的 `A`/`B` 结论会降级为 `C`（措辞同时被弱化），确保「无据强结论」无法进入报告。`C` 仅作弱化表述；`N` 必须明确「无法判断」。无知识命中时 `project_rules` / `tech_debt` 为空数组（对应「无知识不强判」）。

`--format md` 即上述 7 段的 Markdown 渲染。

---

## 10. 测试与评估

```bash
pytest                      # 单测（parser/evidence/kb_query/workflow/cli/kb/local-git）
python tests/eval_harness.py # 离线评估指标
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
| 99 | `INTERNAL_ERROR` | 内部错误 |

错误时统一输出信封 `{"error":{"code":"...","message":"..."}}`（默认 stdout，可用 `--error-stream stderr` 切换）。

---

## 12. 被 AI 工具调用示例

CLI 以子进程方式调用，stdout 读取 JSON 即可解析报告字段：

```python
import subprocess, json

diff = open("pr.diff", encoding="utf-8").read()
proc = subprocess.run(
    ["python", "bin/pr_check_cli.py", "check", "--diff", "-", "--fake", "--format", "json"],
    input=diff, capture_output=True, text=True,
)
if proc.returncode != 0:
    err = json.loads(proc.stdout)
    raise RuntimeError(f"PR 自检失败：{err['error']['code']} {err['error']['message']}")
report = json.loads(proc.stdout)
for risk in report["risk"]:
    print(f"[{risk['level']}] {risk['text']}")
```

---

## 6. 提交拦截（git pre-push 钩子）

工具本身「只出报告、不拦截」——报告里即便有 HIGH 风险，`check` 仍返回退出码 0。要在开发者推送代码时自动拦截，需配合 git hook 与 `--fail-on` 闸门。

### 6.1 闸门（--fail-on）

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
python bin/pr_check_cli.py check --diff pr.diff --fake --fail-on risk:high --fail-on rule:violation
echo $?   # 命中 -> 7，未命中 -> 0
```

### 6.2 安装 pre-push 钩子

```bash
# 在仓库根目录执行（需为 git 仓库）
python bin/pr_check_cli.py hook install --project team/order --base main \
    --fail-on risk:high --fail-on rule:violation

# 卸载
python bin/pr_check_cli.py hook uninstall
```

`hook install` 会：
1. 在仓库根写入 `.pr-check.hook`（shell 可 source 的配置：PR_CHECK_PROJECT / PR_CHECK_BASE / PR_CHECK_FAIL_ON）；
2. 把 `hooks/pre-push` 复制到 `.git/hooks/pre-push` 并设为可执行。

此后每次 `git push`，钩子读取 `.pr-check.hook`，对当前分支相对 `main`（或 `origin/main`）的变更跑 `check`；命中闸门则中断推送并提示（可用 `git push --no-verify` 跳过，不推荐）。

> 注：钩子走本地 `.git` 直连，无需任何 Token；`--project` 仅用于锁定知识库范围。base 默认 `main`，可改；若本地无该分支则退化为 `origin/<base>`。

---

## 13. 常见问题（FAQ）

**Q1. 报告里没有 LLM 段落？**
A. 未配置 `LLM_*` 时自检仍返回基础风险段（降级契约）；配置后才会生成 LLM 段落。

**Q2. 错误返回非 0 但无 500？**
A. 正常业务错误（Git/LLM/KB/参数）都走统一信封 + 对应退出码（2–6/99），不应出现未捕获异常。

**Q3. 退出码 5（`LLM_INVALID_OUTPUT`）怎么排查？**
A. 表示模型返回的内容**无法被当作报告使用**，分两种：响应体不是 JSON（网关/代理拦截），或 JSON 可解析但字段结构不符合约定（字段缺失、枚举取值大小写不符、类型错误）。错误信封的 `message` 会列出具体字段路径与原因，据此判断是提示词问题还是模型能力问题。该场景不会返回半成品报告。
