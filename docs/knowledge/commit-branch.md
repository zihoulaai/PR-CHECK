# 提交与分支规范

> 适用范围：所有提交与 PR/MR。本文以 PR_CHECK 的纯 CLI 形态为样例，分支命名换成你们团队的 Git Flow / trunk-based 约定也能直接用。

## 1. 概述

提交信息是可追溯性的基础，分支策略决定发布节奏。本规范约定 commit message 格式、分支命名与 PR 标题要求。

## 2. 提交信息格式

- MUST 采用 `type(scope): subject` 形式，type 取自：`feat` / `fix` / `refactor` / `test` / `docs` / `chore` / `perf` / `style`。
- MUST subject 用祈使句、简洁说明「做了什么」，不超过 72 字符，不以句号结尾。
- SHOULD 复杂改动在空行后补 `body`，说明「为什么」而非「改了什么」；关联 issue 用 `Closes #123`。
- 禁止 `fix bug`、`update`、`wip` 这类无信息提交；禁止把不相关的改动压进同一个提交。

正例：

```
feat(kb): 增加 KB_PROVIDER 路由与 openai 适配器

将单一 MaaS 实现改为可插拔框架，新增供应商只需实现 KnowledgeBase 并登记。
Closes #45
```

反例：

```
fix
改了一堆东西
```

## 3. 分支策略

- MUST 功能分支从 `main` 切出，命名 `feat/<短描述>` / `fix/<短描述>`；禁止长期在 `main` 直接提交。
- SHOULD 一个 PR 聚焦一个关注点，避免「顺手」重构 unrelated 模块（此类改动会在自检中被归入业务变更，扩大召回范围）。
- 禁止在分支名中使用中文或空格（若你们团队允许中文分支名，记得同步更新 CI 校验）。

## 4. PR 标题

- MUST PR 标题能一句话说明本次变更意图，与首个提交 type 语义一致。
- SHOULD 标题包含受影响模块（如 `kb:`、`parser:`），便于 PR 自检按 `modules` / `focus` 归类。
- 禁止标题仅写 `update` / `patch`。

## 5. 与 PR 自检的关联

- 提交/分支/PR 标题中的模块前缀与 type 会参与 `modules`、`change_types`、`keywords` 的提取，触发 `focus=development_rule` 召回，本规范作为命中文档出现在 `project_rules` 段落。
- 若 PR 改动面过宽（多模块 + 依赖变更），自检会同时命中 `technical_debt` / `historical_risk`，提示拆 PR。
