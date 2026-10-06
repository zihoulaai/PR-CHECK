# Dify 知识库适配器修复说明

`pr-check` 接入 Dify 知识库（`KB_PROVIDER=dify`）后，在 Demo 项目
（`D:\trae_workspace\Demo`）实测中发现三处会导致检索失败或报告降级的问题，
均已修复于 `app/adapters/dify_kb.py`。本文档供后续接入其它 Dify 知识库时复用。

## 修复 1：检索 query 超长被 Dify 拒绝

- **现象**：`HTTP 400 invalid_param ... String should have at most 250 characters`
- **根因**：Dify `POST /datasets/{id}/retrieve` 的 `query` 字段硬上限 250 字符；
  `build_query_text()` 生成的多字段语句（含项目路径、模块、变更文件清单）常超长。
- **修复**：`search()` 在发送前截断到 248 字符：

  ```python
  query_text = build_query_text(query)
  if len(query_text) > 248:  # Dify 的 query 字段上限为 250 字符
      query_text = query_text[:248]
  ```

- **影响范围**：仅 Dify 适配层（Dify 特有约束），不影响 MaaS / OpenAI 适配器。

## 修复 2：Dify 服务端间歇 TLS 断开（SSL UNEXPECTED_EOF）

- **现象**：`SSL: UNEXPECTED_EOF_WHILE_READING` 偶发，单纯重试可能恢复。
- **根因**：Dify 服务端 / 前置代理偶发断开连接，非代码逻辑问题。
- **修复**：`search()` 增加 3 次退避重试——捕获 `httpx.HTTPError`（传输层抖动）
  与响应体非 JSON 的 `ValueError`，`time.sleep(0.5 * (attempt + 1))` 后退避。
  **4xx 确定性错误（参数 / 权限 / 配额）不重试**，直接抛 `KbError`：

  ```python
  data: dict | None = None
  last_exc: Exception | None = None
  for attempt in range(3):  # 消化 Dify 服务端间歇 TLS/连接抖动
      try:
          with self._client() as c:
              resp = c.post(self._ds_path("retrieve"), json={...})
          if resp.status_code >= 400:
              raise KbError(f"Dify 检索失败：HTTP {resp.status_code} {resp.text[:600]}")
          data = resp.json()
          break
      except httpx.HTTPError as exc:
          last_exc = exc
          time.sleep(0.5 * (attempt + 1))
          continue
      except ValueError as exc:
          last_exc = exc
          time.sleep(0.5 * (attempt + 1))
          continue
  if data is None:
      raise KbError(f"知识库检索失败（已重试 3 次）：{last_exc}") from last_exc
  ```

- **影响范围**：提升 Dify 检索对网络抖动的韧性。

## 修复 3：绕过重排 LLM 超时

- **现象**：`HTTP 400 ... LLM_TIMEOUT`（Dify 重排 / rerank 模型超时）。
- **修复**：retrieve 请求体加 `"rerank_enable": False`，保留 `semantic_search`
  向量检索（embedding 不超时），仅关闭会超时的 rerank 重排模型：

  ```python
  json={
      "query": query_text,
      "retrieval_mode": self.retrieval_mode,
      "top_k": self.top_k,
      "rerank_enable": False,
  }
  ```

- **影响范围**：检索质量仍为语义召回，仅跳过 rerank 重排。

## Windows 下生成 diff 的坑（使用侧）

- **现象**：`pr-check` 读 diff 报 `UnicodeDecodeError`（实际为 UTF-16 解码失败）。
- **根因**：PowerShell `>` 重定向默认写 UTF-16 LE；管道还会叠加 UTF-8 BOM。
- **可复用命令**（避免 `pr-check` 读 diff 报解码错误）：

  ```powershell
  git -C <repo> diff <base>...<branch> | python -c "import sys; raw=sys.stdin.buffer.read(); raw=raw.replace(b'\xef\xbb\xbf', b''); open(r'<out>.diff','wb').write(raw)"
  ```

  关键点：用 `python` 二进制写（`wb`）+ 剥除所有 UTF-8 BOM，绕开 PowerShell 编码问题。

## 验证记录（Demo 项目）

| 分支 | diff 规模 | 分析模式 | 知识库状态 | 说明 |
|---|---|---|---|---|
| `feature/login` | 6 文件 / 80 行 | full | success（命中 `d2aabaeb`） | 标准完整分析 |
| `feature/edge-cases` | 93KB（含二进制 + 1702 行 Java） | focused | success | 超大 / 二进制 diff，KB 仍稳定命中 |
| `chore/no-change` | 0 字节（空 PR） | summary_only | not_configured | diff 为空 → 跳过深度分析，不触发 KB |
| `feature/batch-refactor` | 65KB / 96 文件 / 1520 行 | summary_only | not_configured | 规模超阈值 → 仅变更摘要，不跑 KB |

结论：`pr-check` 按 diff 规模自动分级，模式切换健壮；空 PR 与超阈值 PR 下 KB
不触发是合理设计。三处修复在非常规 diff 下同样稳定。
