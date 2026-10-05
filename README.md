# PR 提交前置自检 Agent (MVP)

开发者选择一个 PR/MR 后，系统自动获取元数据与 Diff，经规则型 Diff Parser 形成结构化变更画像（ChangeProfile），做基础工程风险提示，并在配置了项目知识库时通过一次 MaaS 知识检索叠加项目规范 / 接口约束 / 历史债务增强，最后由 LLM 生成结构化「PR 提交前置自检报告」（7 段）并以 Markdown 渲染、在单页 Web UI 展示。

> 本工具只做 PR 前置辅助自检，**不替代人工 Code Review**，无证据不下项目特定强结论。

## 架构

```
Web UI (static/index.html)
   │ REST
FastAPI 路由层 (/settings /projects /check /kb)
   ├─ Config / Secrets(Fernet) / SQLite 存储
   ├─ GitPlatformAdapter → GitLabAdapter | FakeGitLab
   ├─ KnowledgeBase → MaaSVectorKBAdapter | FakeKB
   ├─ Diff Parser + ChangeProfile Builder（Java/Python/TS/Go）
   └─ Agent Workflow（prompt + KBQuery + LLM + Evidence + Markdown）
```

## 运行

```bash
pip install -r requirements.txt
cp .env.example .env        # 填写 APP_ENCRYPTION_KEY（见文件内生成命令）与 LLM/KB（可选）
uvicorn app.main:app --reload --port 8000
# 打开 http://localhost:8000
```

离线 / 测试（无需真实凭据）：设置 `PR_CHECK_USE_FAKE=1` 即可使用 Fake 适配器。

## 配置

- `APP_ENCRYPTION_KEY`：Fernet 主密钥（仅部署注入，不进库）。
- `LLM_BASE_URL / LLM_MODEL / LLM_API_KEY`：OpenAI 兼容端点（可选）。
- `KB_BASE_URL / KB_API_KEY / KB_INDEX`：MaaS Vector KB（可选）。
- `SMALL_MAX_FILES / SMALL_MAX_LINES / MEDIUM_MAX_FILES / MEDIUM_MAX_LINES / KB_TOP_K`：阈值与 Top-K。

## 三档分析模式

| 模式 | 触发 | 行为 |
|---|---|---|
| full | ≤20 文件且 ≤800 行 | 完整 Diff 送 LLM 分析 |
| focused | 21–80 文件或 801–3000 行 | 仅保留高影响文件 Diff |
| summary_only | >80 文件或 >3000 行 | 仅摘要 + 基础风险 + 人工清单，不进完整 LLM |

## 测试与评估

```bash
pytest                      # 50+ 个用例（parser/evidence/kb_query/api/workflow/cli）
python tests/eval_harness.py # V2 离线评估指标
```

## 命令行工具（CLI）

无需启动 HTTP 服务，直接调用 `app` 内 pipeline，面向 AI 工具 / 自动化流程设计。

```bash
# 任意目录运行（Windows / macOS / Linux 通用）
python bin/pr_check_cli.py --help

# 对一段 diff 跑完整自检（离线 Mock 数据）
python bin/pr_check_cli.py check --diff pr.diff --fake --format json
# 或管道输入
cat pr.diff | python bin/pr_check_cli.py check --diff - --fake

# 仅看变更画像（确定性、无 LLM/KB，最快，推荐 AI 先取概览）
python bin/pr_check_cli.py profile --diff pr.diff --pretty

# 底层 diff 解析结果
python bin/pr_check_cli.py parse --diff pr.diff

# 走真实 GitLab MR（需凭据）
python bin/pr_check_cli.py check --project team/order --mr-iid 1234 \
    --gitlab-url https://gitlab.com --gitlab-token $GITLAB_TOKEN

# 配置就绪校验 / 版本
python bin/pr_check_cli.py config --check
python bin/pr_check_cli.py version
```

子命令：`check`（完整自检）/`profile`（变更画像）/`parse`（底层解析）/`version`/`config`。
输出默认 `--format json`（`ensure_ascii=false`，枚举转值，结构与 HTTP `/check` 的 `CheckReport` 一致），`--format md` 输出 7 段 Markdown。`*nix` 可用包装脚本 `bin/pr-check`。

### 退出码

| 退出码 | 含义 |
|---|---|
| 0 | 成功 |
| 2 | INVALID_REQUEST（参数/输入错误） |
| 3 | NOT_CONFIGURED（服务未配置） |
| 4 | GITLAB_*（GitLab 不可用/鉴权/无权限/未找到） |
| 5 | LLM_*（模型不可用/超时/限流/输出异常） |
| 6 | KB_UNAVAILABLE |
| 99 | INTERNAL_ERROR |

错误时统一输出信封 `{"error":{"code":"...","message":"..."}}`（默认 stdout，可用 `--error-stream stderr` 切换）。

### 被 AI 工具调用示例

CLI 以子进程方式调用，stdout 读取 JSON 即可解析 `report.doc_check / risk / project_rules / tech_debt / manual_checklist / kb_sources`：

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

## 关键契约

- 统一错误格式 `{error:{code,message}}`，绝不泄露 Token / 堆栈。
- Evidence 等级 A/B/C/N：A/B 须 source_refs 非空；C 仅弱化表述；N 须「无法判断」。
- 知识库检索按 `project` 强制过滤，跨项目拒绝。
- 失败降级：Git 失败即终止；KB 失败降级为基础自检；LLM 失败整体 502 不出半成品。
