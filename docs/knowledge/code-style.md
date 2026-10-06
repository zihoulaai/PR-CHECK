# 代码风格与结构规范

> 适用范围：所有进入仓库的源码，尤其 `app/` 下的业务代码。
> 本文以 PR_CHECK 的分层约定为样例，套用到其它项目时换成你们团队的模块边界与依赖清单即可。

## 1. 概述

统一的代码风格降低评审成本，清晰的分层避免业务逻辑与基础设施耦合。本规范约定命名、目录结构、类型标注、模块边界与依赖收敛四项硬性要求。

## 2. 命名约定

- MUST 使用 `snake_case` 命名变量、函数、模块；使用 `PascalCase` 命名类与异常。
- MUST 常量使用 `UPPER_SNAKE_CASE`，不允许用魔法数字或魔法字符串（如退出码、错误码应定义为具名常量）。
- SHOULD 文件名与模块内主要公开符号语义对齐，避免 `utils.py` / `common.py` 这类无信息名称（换成你们团队的 `helpers`/`internal` 也行）。
- 禁止用单字母命名（循环变量 `i/j` 除外）或拼音缩写做公共符号名。

正例：

```python
EXIT_KB = 6
class KbError(AppError): ...
def build_kb_query(pr: PRMetadata, profile: ChangeProfile) -> KBQuery: ...
```

反例：

```python
def getdata(a, b): ...   # 无类型、无语义
CODE_6 = 6               # 与语义脱钩
```

## 3. 目录结构

- MUST 按职责分层，PR_CHECK 的分层为：`parser/`（diff 解析与变更画像）、`adapters/`（外部系统对接，Git/LLM/KB）、`agent/`（工作流与 query 构建）、`domain/`（枚举与 schema）、`report/`、`storage/`。
- MUST 业务规则只依赖抽象接口（如 `KnowledgeBase` Protocol），不依赖具体供应商实现；具体实现通过工厂/注册表注入。
- SHOULD 跨层调用方向单一（CLI → 业务层 → 适配器），禁止适配器反向 import 业务层造成循环依赖。
- 禁止在 `domain/` 引入 I/O 或框架依赖。

## 4. 类型标注与接口

- MUST 公共函数与方法的参数、返回值加类型标注；对外 schema 使用 `pydantic` 模型并冻结枚举，禁止自由字符串枚举。
- MUST 对外契约（如报告、错误信息）使用结构化模型，保证 LLM / 检索消费方能稳定解析。
- SHOULD 用 `Protocol` 表达适配器抽象，新增供应商实现协议即可，不改动调用方。

## 5. 依赖收敛

- MUST 锁定依赖为项目事实来源；PR_CHECK 仅依赖 `pydantic` / `pydantic-settings` / `httpx` / `sqlmodel`，持久化仅用 SQLite 存 KB metadata。
- 禁止为单个功能引入全新框架（如仅为上传引入 Web 框架）。需新增依赖时先评估是否可用现有库实现。

## 6. 与 PR 自检的关联

- 改动 `adapters/`、`domain/`、新增依赖（`requirements.txt` / `pyproject.toml`）会落入 `dependency_changes`，触发 `focus=development_rule` 与 `technical_debt` 召回，本规范即作为命中文档。
- 若出现循环 import、未标注公共类型、或引入了未声明的依赖，PR 自检的 `project_rules` 与 `tech_debt` 段落可能据此给出提示。
