"""LLMClient：OpenAI 兼容 Chat Completions 端点（D3 / M1）。

结构化输出优先级（M1）：JSON Mode（response_format=json_object）+ Pydantic 校验。
主输出 CheckReport JSON 文本，不是 Markdown。
错误码映射见 errors.py（M5）。
"""
from __future__ import annotations

import json

import httpx

from app.adapters.base import LLMClient
from app.errors import LlmInvalidOutput, LlmRateLimited, LlmTimeout, LlmUnavailable


class LLMClientImpl:
    def __init__(self, base_url: str, model: str, api_key: str,
                 timeout: int = 60, max_retries: int = 1):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries

    def complete(self, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }
        last_err: Exception | None = None
        for _ in range(max(1, self.max_retries)):
            try:
                with httpx.Client(timeout=self.timeout) as c:
                    resp = c.post(
                        f"{self.base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {self.api_key}",
                                 "Content-Type": "application/json"},
                        json=payload,
                    )
                if resp.status_code == 429:
                    raise LlmRateLimited()
                if resp.status_code == 408 or resp.status_code >= 500:
                    raise LlmTimeout() if resp.status_code == 408 else LlmUnavailable(
                        f"LLM 返回 {resp.status_code}。")
                if resp.status_code >= 400:
                    raise LlmUnavailable(f"LLM 返回错误状态码 {resp.status_code}。")
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                if not content or not content.strip():
                    raise LlmInvalidOutput("LLM 返回空内容。")
                # 进行最小化 JSON 校验
                try:
                    json.loads(content)
                except json.JSONDecodeError as exc:
                    raise LlmInvalidOutput(f"LLM 返回非 JSON：{exc}") from exc
                return content
            except (LlmRateLimited, LlmTimeout, LlmInvalidOutput, LlmUnavailable):
                raise
            except httpx.TimeoutException as exc:
                last_err = exc
                continue
            except httpx.HTTPError as exc:
                last_err = exc
                continue
        raise LlmTimeout(f"LLM 请求多次失败：{last_err}") if isinstance(
            last_err, httpx.TimeoutException) else LlmUnavailable(
            f"LLM 请求失败：{last_err}")
