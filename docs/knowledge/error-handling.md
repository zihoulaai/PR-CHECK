# 错误处理与错误码规范

> 适用范围：所有对外接口、CLI 退出路径、适配器与检索调用。本文以 PR_CHECK 的错误信封与 Evidence 等级为样例，错误码体系换成你们团队的统一错误目录也行。

## 1. 概述

错误必须可被机器解析、可被人类理解，且绝不泄露敏感信息。本规范约定错误码、错误信封、失败降级与 Evidence 等级四件事。

## 2. 错误码与退出码

- MUST 对外错误使用统一结构 `{error: {code, message}}`，`code` 为稳定字符串枚举（如 `KB_UNAVAILABLE` / `INVALID_REQUEST` / `NOT_CONFIGURED`），不得直接返回堆栈或原始异常类型。
- MUST CLI 退出码固定语义：成功 0、参数错误 2、未配置 3、Git 类 4、LLM 类 5、KB 不可用 6、闸门未过 7、内部错误 99。新增错误路径必须复用既有码，不得私自造码。
- SHOULD 错误 `code` 与退出码一一对应，便于调用方按 code 分支处理。

## 3. 错误信封与敏感信息

- MUST 任何错误消息不得包含 Token、密钥、内部堆栈、SQL 或文件路径细节。
- MUST 适配器捕获底层异常（如 `httpx.HTTPError`、JSON 解析失败）后转为统一业务错误，不得让裸异常冒泡成内部错误。
- 禁止把 `str(exc)` 原样写进对外 `message`（可能含敏感内容）。

正例：

```python
except httpx.HTTPError as exc:
    raise KbError(f"知识库检索失败：{exc}") from exc
```

反例：

```python
except Exception:
    raise AppError(open("secret").read())  # 泄露凭据/文件
```

## 4. 失败降级策略

- MUST 明确每类依赖的降级边界：Git 失败即终止；KB 失败降级为基础自检（报告无知识段落）；LLM 未配置降级为无综合段落，但已配置却调用失败则整体失败（不返回半成品）。
- SHOULD 降级行为写进文档，使调用方知道「少了哪个能力」而非「全挂了」。

## 5. Evidence 等级

- MUST 强结论（项目规范命中、技术债务）的 Evidence 等级须为 `A`/`B`，且每个 `source_refs` 真实命中本次检索；未命中的引用一律剔除，伪造引用降级为 `C`。
- MUST `C` 仅作弱化表述，`N` 必须明确「无法判断」，不得用 `C`/`N` 伪装成确证结论。

## 6. 与 PR 自检的关联

- 改动 `errors.py`、CLI 退出路径、或新增异常分支会落入 `api_changes` / `config_changes`，触发 `api_document` 与 `development_rule` 召回。
- 若新增依赖调用却未做降级封装、或错误消息可能泄露 token，`risk` / `project_rules` 段落会据此提示。
