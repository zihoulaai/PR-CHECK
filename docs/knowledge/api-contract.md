# API / 接口契约规范

> 适用范围：所有对外暴露的接口（CLI 子命令、HTTP 端点、适配器协议）。本文以 PR_CHECK 的 CLI 契约与 `KnowledgeBase` 协议为样例，换成你们团队的 REST / RPC 版本策略也一样适用。

## 1. 概述

接口是系统间契约，一旦发布即承担兼容性责任。本规范约定命名、版本、破坏性变更与文档同步四件事。

## 2. 公共接口命名

- MUST 公共接口（CLI 子命令、函数、端点）命名稳定且语义自解释；CLI 子命令采用名词化（`check` / `kb` / `hook` / `config`），不混用动词短语。
- MUST 参数名与对外 schema 字段名保持一致（如 `KBQuery.project` 与检索请求体的 `project` 同义），避免调用方做映射。
- SHOULD 对外枚举使用冻结字符串常量（如 `DocType` 的 `api_document` / `development_rule`），新增值须向后兼容旧消费方。

## 3. 版本与兼容性

- MUST 破坏既有调用方的行为（改字段名、删必填参数、改错误码语义）视为破坏性变更，必须显式标注版本并给出迁移说明。
- SHOULD 新增可选字段而非修改既有字段；消费方对未知字段应容忍（`extra="ignore"`）。
- 禁止在未通知的情况下改变退出码或错误 `code` 的语义。

## 4. 结构化输出契约

- MUST 对外输出使用固定 schema（如报告 `ReportSections` 仅产 LLM 允许输出的字段，`meta` / `kb_sources` 由系统填充），校验失败即判输出异常，不返回半成品。
- MUST 适配器协议（如 `KnowledgeBase`：`search` / `upload`）签名稳定；新增供应商实现协议，不改动调用方。

## 5. 文档同步

- MUST 接口变更必须同步更新对应文档（README / USAGE / ADR），并在 PR 自检的 `doc_check` 中被核对。
- SHOULD 破坏性变更在 PR 标题或描述中显式标注 `breaking`，便于检索「接口文档一致性」。
- 禁止「代码改了、文档没动」的提交；这类不一致正是 PR 自检 `doc_check` 段落要抓的。

## 6. 与 PR 自检的关联

- 改动 `cli.py`、`adapters/` 公共方法签名、对外 schema（`schemas.py`）会落入 `api_changes`，触发 `focus=api_document` 召回，本规范作为命中文档进入 `doc_check` 与 `project_rules`。
- 若 PR 含「接口改了但文档未同步」的信号，`doc_check` 会标 `doc:confirm` / `doc:update`，可配置为拦截闸门。
