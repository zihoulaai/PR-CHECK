# 安全与敏感信息规范

> 适用范围：日志、配置、错误处理、对外消息、存储。本文以 PR_CHECK 的「不泄露 Token / 堆栈」契约为样例，换成你们团队的 PII / 密钥管理基线也成立。

## 1. 概述

敏感信息泄露常在日志与错误路径发生。本规范约定日志脱敏、密钥管理、PII 处理与依赖安全四项要求。

## 2. 日志脱敏

- MUST 日志不得记录 Token、API Key、密码、Cookie、Authorization 头原文。
- MUST 错误日志只记录类型与稳定上下文（如 `kb_search_failed`），不记录 `str(exc)` 中可能包含的堆栈或内部路径。
- SHOULD 打印请求/响应时剔除鉴权头与 body 中的敏感字段。

正例：

```python
logger.error("kb_search_failed")   # 无 token、无堆栈
```

反例：

```python
logger.error(f"kb failed: {api_key} {exc}")  # 泄露凭据与堆栈
```

## 3. 密钥管理

- MUST 凭据只来自配置（环境变量 / `.env`），绝不落库、不进报告、不进 LLM 上下文、不写进日志。
- MUST 配置文件按来源优先级加载（cwd > 用户级 > 包目录），不把含密钥的 `.env` 提交进仓库。
- SHOULD 凭据在内存中短驻留，传递仅在调用适配器瞬间存在。

## 4. PII 与对外消息

- MUST 对外错误信息、报告不得包含个人可识别信息（PII）。
- SHOULD 涉及用户数据的字段在落盘前做最小化（如 KB 文档 metadata 只存 project / module，不存正文全文）。

## 5. 依赖安全

- MUST 新增第三方依赖须评估来源与许可，禁止引入未声明的网络框架或会外传数据的库。
- SHOULD 定期审查依赖树（可用 `pip-audit` / `uv audit` 这类工具），高危漏洞须及时升级。

## 6. 与 PR 自检的关联

- 改动 `errors.py`、日志语句、`config.py`、新增依赖会落入 `logging_changes` / `dependency_changes` / 业务变更，触发 `focus=historical_risk` 与 `risk` 召回，本规范进入 `risk` 与 `tech_debt` 段落。
- 若 PR 在日志/错误路径出现敏感词或新增未声明依赖，`risk` 段落会据此提示，可配置为人工确认清单。
