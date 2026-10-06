# 测试与质量门禁规范

> 适用范围：所有功能代码与重构。本文以 PR_CHECK 的 pytest 单测与评估脚本为样例，门禁阈值按你们团队的 CI 要求定即可。

## 1. 概述

测试是改动的回归护栏，质量门禁防止退化合入主干。本规范约定测试覆盖、边界与门禁三项要求。

## 2. 测试覆盖

- MUST 新增/修改业务逻辑必须配套单测；适配器边界（响应解析、重试语义、降级路径）必须有测试覆盖，不得让裸 `KeyError` / `JSONDecodeError` 冒泡成内部错误。
- MUST 测试只断言公开行为，不依赖私有实现细节；通过打桩（如 `httpx.MockTransport` / Fake 适配器）隔离网络与外部依赖。
- SHOULD 每类失败降级路径（瞬时故障重试、永久 4xx 不重试、结构异常降级）至少一条用例。
- 禁止提交「红测」或 `@skip` 掉关键路径蒙混过关。

正例（重试语义）：

```python
def test_rate_limit_is_retried(monkeypatch):
    assert _run_llm(monkeypatch, [429, 429, 429], 3) == ("LlmRateLimited", 3)
```

## 3. 质量门禁

- MUST CI 必须跑 `pytest` 全量且无回归后方可合入；新增依赖需同步 `requirements.txt` / `pyproject.toml` 并校验锁文件。
- SHOULD 设行覆盖 / 分支覆盖下限（比如 70% / 80%，按你们团队标准），低于下限阻断。
- SHOULD 提供离线评估入口（如 `python tests/eval_harness.py`），在无真实凭据时也能验证核心指标。

## 4. 测试独立性

- MUST 测试间互不影响，使用临时 SQLite 与 fixture 隔离，禁止共享可变全局状态跨用例串扰。
- SHOULD 配置读取通过 `lru_cache` 的 `get_settings()` 注入，测试内显式 `cache_clear()` 避免环境变量串味。

## 5. 与 PR 自检的关联

- 改动 `tests/`、`pyproject.toml`、CI 配置会落入 `dependency_changes` / 业务变更，触发 `focus=technical_debt` 与 `historical_risk` 召回，本规范进入 `tech_debt` 与 `risk` 段落。
- 若 PR 改动核心逻辑却无对应测试，或删除了既有门禁，自检的 `tech_debt` 会提示「历史测试债务」。
