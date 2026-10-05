"""LLMClient：OpenAI 兼容 Chat Completions 端点（D3 / M1）。

结构化输出优先级：JSON Schema（response_format=json_schema，服务端强约束形状）
→ 服务端不支持时自动降级 JSON Mode（response_format=json_object）
→ Pydantic 校验（见 app/agent/workflow._parse_sections）。
主输出 CheckReport JSON 文本，不是 Markdown。
错误码映射见 errors.py（M5）。
"""
from __future__ import annotations

import json
import logging
import time
from copy import deepcopy

import httpx

from app.errors import LlmInvalidOutput, LlmRateLimited, LlmTimeout, LlmUnavailable

logger = logging.getLogger("pr_check")

# 可重试的瞬时故障状态码：限流、请求超时、服务端错误
_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
_BACKOFF_SECONDS = 1.0

# 400 响应体中出现这些词，才判定为「服务端不支持该 response_format」并可降级；
# 其余 400（鉴权 / 模型名 / 上下文超长等）一律不降级，避免掩盖真实原因。
_UNSUPPORTED_FORMAT_HINTS = ("response_format", "json_schema", "json schema", "structured output")


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


class _FormatUnsupported(Exception):
    """服务端不接受 response_format=json_schema，需降级为 json_object。

    内部信号，不对外暴露；仅在 400 且响应体指向 response_format 相关时抛出。
    """


def _looks_like_unsupported_format(resp) -> bool:
    """判断 400 是否源于「不支持该 response_format」（而非鉴权 / 参数等其它错误）。

    读不到响应体（Mock / 网关空 body）时返回 False —— 宁可不降级也不掩盖真实原因。
    """
    try:
        text = getattr(resp, "text", "") or ""
    except Exception:  # noqa: BLE001 - 响应体读取失败按“无信息”处理
        return False
    low = text.lower()
    return any(hint in low for hint in _UNSUPPORTED_FORMAT_HINTS)


def _inline_refs(node, defs: dict, seen: tuple[str, ...]):
    """把 JSON Schema 里的本地 $ref（#/$defs/X）就地展开。

    各厂商 Structured Outputs 后端对 $ref / $defs 的支持程度不一，展开后是等价的
    纯内联 Schema，兼容性最好；seen 用于防御自引用导致的无限递归。
    """
    if isinstance(node, list):
        return [_inline_refs(x, defs, seen) for x in node]
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        key = ref.rsplit("/", 1)[-1]
        if key in defs and key not in seen:
            return _inline_refs(deepcopy(defs[key]), defs, seen + (key,))
        return {k: v for k, v in node.items() if k != "$ref"}
    return {k: (v if k == "$defs" else _inline_refs(v, defs, seen))
            for k, v in node.items()}


def inline_json_schema(schema: dict) -> dict:
    """返回去掉 $defs / $ref 的等价内联 Schema（不修改入参）。"""
    out = _inline_refs(deepcopy(schema), schema.get("$defs") or {}, ())
    out.pop("$defs", None)
    return out


def strict_json_schema(schema: dict) -> dict:
    """生成 strict 版 Schema：每个 object 的全部 properties 均为 required 且禁止附加属性。

    strict 模式下服务端强制模型输出所有字段；实测非 strict 时模型会省略
    doc_check / risk 等整段，故默认走 strict。
    """
    def walk(node):
        if isinstance(node, list):
            return [walk(x) for x in node]
        if not isinstance(node, dict):
            return node
        out = {k: walk(v) for k, v in node.items()}
        if out.get("type") == "object" and "properties" in out:
            out["required"] = sorted(out["properties"].keys())
            out["additionalProperties"] = False
        return out

    return walk(deepcopy(schema))


def _json_schema_format(schema: dict, name: str, *, strict: bool) -> dict:
    """构造 OpenAI 风格 Structured Outputs 载荷。

    schema 传入前已内联；strict=True 时另加 strict 标记并补全 required。
    """
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": strict,
            "schema": strict_json_schema(schema) if strict else schema,
        },
    }


class LLMClientImpl:
    def __init__(self, base_url: str, model: str, api_key: str,
                 timeout: int = 60, max_retries: int = 1,
                 response_schema: dict | None = None,
                 schema_name: str = "report_sections",
                 enable_thinking: bool | None = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        # None = 不发送 enable_thinking 字段（端点可能不认识它）
        self.enable_thinking = enable_thinking
        # LLM 输出形状契约（由上层传入 Pydantic model_json_schema）。
        # 为 None 时退化为纯 JSON Mode。
        self.response_schema = response_schema
        self.schema_name = schema_name
        # 预编译候选格式：strict schema → 宽松 schema → JSON Mode。
        # 降级游标持久化在实例上：某级被拒后就从下一级开始，不重复试探。
        if response_schema:
            inlined = inline_json_schema(response_schema)
            self._formats = [
                _json_schema_format(inlined, schema_name, strict=True),
                _json_schema_format(inlined, schema_name, strict=False),
                {"type": "json_object"},
            ]
        else:
            self._formats = [{"type": "json_object"}]
        self._fmt_index = 0

    def complete(self, system: str, user: str) -> str:
        """按「strict json_schema → json_schema → json_object」顺序取回结构化输出。

        仅当服务端明确以 400 拒绝当前格式时才降级到下一级。
        """
        last_err: Exception | None = None
        for i in range(self._fmt_index, len(self._formats)):
            fmt = self._formats[i]
            try:
                return self._request(system, user, fmt)
            except _FormatUnsupported as exc:
                last_err = exc
                self._fmt_index = i + 1
                logger.warning("llm_response_format_rejected from=%s fallback=%s",
                               fmt["type"] + ("/strict" if fmt.get("json_schema", {})
                                              .get("strict") else ""),
                               self._formats[i + 1]["type"] if i + 1 < len(self._formats) else "none")
        raise LlmUnavailable("LLM 请求失败：所有 response_format 均被拒绝。") from last_err

    def _request(self, system: str, user: str, response_format: dict) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
            "response_format": response_format,
        }
        if self.enable_thinking is not None:
            payload["enable_thinking"] = self.enable_thinking
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
                    # 400 且响应体指向 response_format → 该服务端/模型不支持 json_schema，交给 complete 降级
                    if resp.status_code == 400 and response_format.get("type") == "json_schema" \
                            and _looks_like_unsupported_format(resp):
                        raise _FormatUnsupported()
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
