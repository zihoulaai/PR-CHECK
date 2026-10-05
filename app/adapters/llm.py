"""LLMClient：OpenAI 兼容 Chat Completions 端点（D3 / M1）。

结构化输出优先级（M1）：JSON Mode（response_format=json_object）+ Pydantic 校验。
主输出 CheckReport JSON 文本，不是 Markdown。
错误码映射见 errors.py（M5）。
"""
from __future__ import annotations

import json
import time

import httpx

from app.errors import LlmInvalidOutput, LlmRateLimited, LlmTimeout, LlmUnavailable

# 可重试的瞬时故障状态码：限流、请求超时、服务端错误
_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
_BACKOFF_SECONDS = 1.0


def _is_retryable(status_code: int) -> bool:
    return status_code in _RETRYABLE_STATUS or status_code >= 500


def _raise_for_status(status_code: int, attempts: int = 1) -> None:
    """HTTP 状态码 → 领域错误的唯一映射（循环内与尝试耗尽后共用，避免两处判断漂移）。

    attempts>1 时在消息里补上已尝试次数，便于区分「偶发」与「持续故障」。
    """
    tried = f"（已尝试 {attempts} 次）" if attempts > 1 else ""
    if status_code == 429:
        raise LlmRateLimited()
    if status_code == 408:
        raise LlmTimeout()
    if status_code >= 500:
        raise LlmUnavailable(f"LLM 返回 {status_code}{tried}。")
    raise LlmUnavailable(f"LLM 返回错误状态码 {status_code}{tried}。")


def _extract_content(data) -> str:
    """从 OpenAI 兼容响应中取出 choices[0].message.content。

    结构异常（缺 choices / 空列表 / 非字符串）统一转 LlmInvalidOutput，
    避免裸 KeyError / IndexError 冒泡成 INTERNAL_ERROR（退出码 99）。
    """
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LlmInvalidOutput(f"LLM 响应结构异常：{exc}") from exc
    if not isinstance(content, str):
        raise LlmInvalidOutput(f"LLM 响应 content 非字符串：{type(content).__name__}")
    return content


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
        last_status: int | None = None
        for attempt in range(attempts):
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
                if resp.status_code >= 400:
                    # 429 / 408 / 5xx 属于可重试的瞬时故障：退避后重试；
                    # 其它 4xx 是请求本身的问题，重试无意义，直接失败。
                    if _is_retryable(resp.status_code) and attempt < attempts - 1:
                        last_status = resp.status_code
                        time.sleep(_BACKOFF_SECONDS * (attempt + 1))
                        continue
                    _raise_for_status(resp.status_code)
                data = resp.json()
                content = _extract_content(data)
                if not content or not content.strip():
                    raise LlmInvalidOutput("LLM 返回空内容。")
                # 进行最小化 JSON 校验
                try:
                    json.loads(content)
                except json.JSONDecodeError as exc:
                    raise LlmInvalidOutput(f"LLM 返回非 JSON：{exc}") from exc
                return content
            except json.JSONDecodeError as exc:
                # 响应体本身不是 JSON（网关错误页 / 代理拦截等）
                raise LlmInvalidOutput(f"LLM 响应体非 JSON：{exc}") from exc
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
        # 所有尝试耗尽：按最后的错误类型给出清晰、可诊断的信息
        # 错误消息只带异常类型名，不拼接原始异常串（可能含 URL / 连接细节）
        if isinstance(last_err, (httpx.ConnectTimeout, httpx.ConnectError)):
            raise LlmUnavailable(
                f"无法连接 LLM 服务（网络不可达 / 域名解析失败 / 连接被拒）："
                f"{type(last_err).__name__}")
        if isinstance(last_err, httpx.ReadTimeout):
            raise LlmTimeout(
                f"LLM 响应超时（模型可能较慢或生成内容过长）：{type(last_err).__name__}；"
                f"可增大 LLM_TIMEOUT_SECONDS（当前 {self.timeout}s）或换用更快的模型。")
        if last_status is not None:
            # 瞬时故障重试耗尽：保留原始错误码语义，便于调用方区分限流与服务故障
            _raise_for_status(last_status, attempts)
        if last_err is not None:
            word = "多次" if attempts > 1 else "一次"
            raise LlmUnavailable(f"LLM 请求{word}失败：{type(last_err).__name__}")
        raise LlmUnavailable("LLM 请求失败。")
