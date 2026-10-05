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
        attempts = max(1, self.max_retries)
        last_err: Exception | None = None
        for _ in range(attempts):
            try:
                # 连接超时单独设短（10s），快速暴露“网络不可达/域名解析失败/连接被拒”；
                # 读超时用用户配置时长（self.timeout），容纳慢模型的长生成。
                with httpx.Client(
                    timeout=httpx.Timeout(connect=10, read=self.timeout, write=30, pool=10)
                ) as c:
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
            except (httpx.ConnectTimeout, httpx.ConnectError) as exc:
                # 网络层不可达：明确“模型无法访问”，与读超时区分
                last_err = exc
                continue
            except httpx.ReadTimeout as exc:
                last_err = exc
                continue
            except httpx.TimeoutException as exc:
                last_err = exc
                continue
            except httpx.HTTPError as exc:
                last_err = exc
                continue
        # 根据最后的错误类型给出清晰、可诊断的信息
        if isinstance(last_err, (httpx.ConnectTimeout, httpx.ConnectError)):
            raise LlmUnavailable(
                f"无法连接 LLM 服务（网络不可达 / 域名解析失败 / 连接被拒）：{last_err}")
        if isinstance(last_err, httpx.ReadTimeout):
            raise LlmTimeout(
                f"LLM 响应超时（模型可能较慢或生成内容过长）：{last_err}；"
                f"可增大 LLM_TIMEOUT_SECONDS（当前 {self.timeout}s）或换用更快的模型。")
        word = "多次" if attempts > 1 else "一次"
        raise LlmUnavailable(f"LLM 请求{word}失败：{last_err}")
