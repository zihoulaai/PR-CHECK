"""适配器边界测试：LLM / KB 响应解析与重试语义。

这些路径决定了「LLM 输出异常 → 退出码 5」「KB 不可用 → 降级为退出码 0」
「瞬时故障是否重试」，必须保证不会以裸 KeyError / JSONDecodeError 冒泡成
INTERNAL_ERROR，也不会把 429/5xx 当成永久失败。
"""
from __future__ import annotations

import json

import httpx
import pytest

import app.adapters.llm as llm_mod
from app.adapters.llm import LLMClientImpl, _extract_content
from app.adapters.maas_kb import MaaSVectorKBAdapter, _to_hits
from app.domain.schemas import KBQuery
from app.errors import KbError, LlmInvalidOutput


# ===== LLM 响应结构 =====
def test_extract_content_ok():
    assert _extract_content({"choices": [{"message": {"content": "{}"}}]}) == "{}"


@pytest.mark.parametrize("data", [
    {},
    {"choices": []},
    {"choices": [{}]},
    {"choices": [{"message": {}}]},
    {"choices": "not-a-list"},
    None,
])
def test_extract_content_structure_error(data):
    with pytest.raises(LlmInvalidOutput):
        _extract_content(data)


def test_extract_content_non_string():
    with pytest.raises(LlmInvalidOutput):
        _extract_content({"choices": [{"message": {"content": {"a": 1}}}]})


# ===== LLM 重试语义：瞬时故障重试，永久 4xx 不重试 =====
class _Resp:
    def __init__(self, status_code: int):
        self.status_code = status_code

    def json(self):
        return {"choices": [{"message": {"content": '{"summary":"ok"}'}}]}


def _run_llm(monkeypatch, codes: list[int], max_retries: int):
    """按给定状态码序列驱动 LLMClientImpl，返回 (结果或异常类型名, 尝试次数)。"""
    seq = list(codes)
    attempts = {"n": 0}

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            attempts["n"] += 1
            return _Resp(seq.pop(0) if len(seq) > 1 else seq[0])

    monkeypatch.setattr(llm_mod.httpx, "Client", _Client)
    monkeypatch.setattr(llm_mod.time, "sleep", lambda s: None)
    client = LLMClientImpl("http://x", "m", "k", timeout=1, max_retries=max_retries)
    try:
        client.complete("s", "u")
        return "OK", attempts["n"]
    except Exception as exc:  # noqa: BLE001 - 断言异常类型
        return type(exc).__name__, attempts["n"]


def test_rate_limit_is_retried(monkeypatch):
    # 修复前 429 直接 raise，max_retries 完全不起作用
    assert _run_llm(monkeypatch, [429, 429, 429], 3) == ("LlmRateLimited", 3)


def test_server_error_is_retried(monkeypatch):
    # 修复前 5xx 直接 raise，不重试
    assert _run_llm(monkeypatch, [500, 500, 500], 3) == ("LlmUnavailable", 3)


def test_transient_error_then_success(monkeypatch):
    assert _run_llm(monkeypatch, [503, 200], 3) == ("OK", 2)


def test_request_timeout_is_retried(monkeypatch):
    assert _run_llm(monkeypatch, [408, 408], 2) == ("LlmTimeout", 2)


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_client_error_not_retried(monkeypatch, code):
    """4xx 是请求本身的问题，重试无意义，必须只尝试一次。"""
    assert _run_llm(monkeypatch, [code] * 3, 3) == ("LlmUnavailable", 1)


def test_no_retry_when_max_retries_is_one(monkeypatch):
    assert _run_llm(monkeypatch, [503, 503], 1) == ("LlmUnavailable", 1)


# ===== LLM 结构化输出：json_schema 优先 + 不支持时降级 =====
class _RespWithBody:
    def __init__(self, status_code: int, body: str = ""):
        self.status_code = status_code
        self.text = body

    def json(self):
        return {"choices": [{"message": {"content": '{"summary":"ok"}'}}]}


def _setup_client(monkeypatch, resp_seq, *, schema=None, max_retries: int = 1,
                  enable_thinking: bool | None = None):
    """装配一个 httpx.Client 被打桩的 LLMClientImpl，返回 (client, 每次请求的 payload)。"""
    seq = list(resp_seq)
    calls: list[dict] = []

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            calls.append(k["json"])
            return seq.pop(0) if len(seq) > 1 else seq[0]

    monkeypatch.setattr(llm_mod.httpx, "Client", _Client)
    monkeypatch.setattr(llm_mod.time, "sleep", lambda s: None)
    client = LLMClientImpl("http://x", "m", "k", timeout=1, max_retries=max_retries,
                           response_schema=schema, enable_thinking=enable_thinking)
    return client, calls


def _run_llm_schema(monkeypatch, resp_seq, *, schema=None, max_retries: int = 1):
    """驱动带 Schema 的 LLMClientImpl，返回 (每次请求的 payload, 结果或异常类型名)。"""
    client, calls = _setup_client(monkeypatch, resp_seq, schema=schema,
                                  max_retries=max_retries)
    try:
        client.complete("s", "u")
        return calls, "OK"
    except Exception as exc:  # noqa: BLE001 - 断言异常类型
        return calls, type(exc).__name__


_SAMPLE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "risk": {"type": "array", "items": {"$ref": "#/$defs/RiskItem"}},
    },
    "$defs": {"RiskItem": {"type": "object", "properties": {"level": {"enum": ["high", "low"]}}}},
}


def test_sends_strict_json_schema_when_schema_configured(monkeypatch):
    calls, result = _run_llm_schema(monkeypatch, [_RespWithBody(200)], schema=_SAMPLE_SCHEMA)
    assert result == "OK"
    fmt = calls[0]["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["name"] == "report_sections"
    assert fmt["json_schema"]["strict"] is True
    schema = fmt["json_schema"]["schema"]
    # $defs / $ref 已内联展开，兼容不支持 $ref 的后端
    assert "$defs" not in schema
    assert "$ref" not in json.dumps(schema)
    assert schema["properties"]["risk"]["items"]["properties"]["level"]["enum"] == ["high", "low"]
    # strict：字段全部必填、禁止附加属性
    assert schema["required"] == ["risk", "summary"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["risk"]["items"]["additionalProperties"] is False


def test_strict_rejected_falls_back_to_loose_schema(monkeypatch):
    """strict 被拒时先退到宽松 json_schema（而非直接退回 JSON Mode）。"""
    rejected = _RespWithBody(400, '{"message":"unsupported response_format: json_schema strict"}')
    calls, result = _run_llm_schema(
        monkeypatch, [rejected, _RespWithBody(200)], schema=_SAMPLE_SCHEMA)
    assert result == "OK"
    assert [c["response_format"]["json_schema"].get("strict") for c in calls] == [True, False]


def test_all_schema_rejected_falls_back_to_json_object(monkeypatch):
    rejected = _RespWithBody(400, '{"message":"unsupported response_format: json_schema"}')
    client, calls = _setup_client(
        monkeypatch, [rejected, rejected, _RespWithBody(200), _RespWithBody(200)],
        schema=_SAMPLE_SCHEMA)
    client.complete("s", "u")
    assert [c["response_format"]["type"] for c in calls] == [
        "json_schema", "json_schema", "json_object"]
    # 降级被实例记住：后续请求直接用 json_object，不再重复试探
    calls.clear()
    client.complete("s", "u")
    assert [c["response_format"]["type"] for c in calls] == ["json_object"]


def test_no_fallback_for_unrelated_client_error(monkeypatch):
    """400 但原因与 response_format 无关（如鉴权失败）：不降级，只尝试一次。"""
    calls, result = _run_llm_schema(
        monkeypatch, [_RespWithBody(400, '{"message":"Invalid token"}')] * 3,
        schema=_SAMPLE_SCHEMA)
    assert result == "LlmUnavailable"
    assert len(calls) == 1


def test_json_object_mode_without_schema(monkeypatch):
    calls, result = _run_llm_schema(monkeypatch, [_RespWithBody(200)])
    assert result == "OK"
    assert calls[0]["response_format"] == {"type": "json_object"}


def test_enable_thinking_only_sent_when_configured(monkeypatch):
    """未配置时不发送 enable_thinking，避免不支持该字段的端点 400。"""
    client, calls = _setup_client(monkeypatch, [_RespWithBody(200)])
    client.complete("s", "u")
    assert "enable_thinking" not in calls[0]

    client, calls = _setup_client(monkeypatch, [_RespWithBody(200)], enable_thinking=False)
    client.complete("s", "u")
    assert calls[0]["enable_thinking"] is False


# ===== KB 响应结构 =====
def test_to_hits_filters_cross_project():
    data = {"hits": [
        {"id": "a", "title": "A", "doc_type": "api_document", "project": "p"},
        {"id": "b", "title": "B", "doc_type": "api_document", "project": "other"},
    ]}
    hits = _to_hits(data, "p")
    assert [h.id for h in hits] == ["a"]


def test_to_hits_non_numeric_score():
    with pytest.raises((TypeError, ValueError)):
        _to_hits({"hits": [{"id": "a", "score": "high"}]}, "p")


def _adapter_with_response(handler) -> MaaSVectorKBAdapter:
    kb = MaaSVectorKBAdapter("http://kb.local", "k", "idx")
    kb._client = lambda: httpx.Client(  # type: ignore[method-assign]
        base_url="http://kb.local", transport=httpx.MockTransport(handler))
    return kb


def test_search_non_json_response_raises_kb_error():
    def handler(request):
        return httpx.Response(200, text="<html>502 Bad Gateway</html>")

    kb = _adapter_with_response(handler)
    with pytest.raises(KbError):
        kb.search(KBQuery(project="p"))


def test_search_malformed_hits_raises_kb_error():
    def handler(request):
        return httpx.Response(200, json={"hits": [{"id": "a", "score": "high"}]})

    kb = _adapter_with_response(handler)
    with pytest.raises(KbError):
        kb.search(KBQuery(project="p"))


def test_search_http_error_raises_kb_error():
    def handler(request):
        return httpx.Response(503)

    kb = _adapter_with_response(handler)
    with pytest.raises(KbError):
        kb.search(KBQuery(project="p"))


def test_search_requires_project():
    kb = _adapter_with_response(lambda r: httpx.Response(200, json={"hits": []}))
    with pytest.raises(KbError):
        kb.search(KBQuery(project=""))


# ===== 响应形状校验：远端返回对象时不得静默当成「空列表」=====
def test_json_items_rejects_non_list_shapes():
    """`data or []` 遇 dict 会静默产出「空列表」。

    看起来像「没有数据」，实际是「形状不对」——两者的排查方向完全不同：
    前者要去查参数与分页，后者要去查接口契约与错误响应。因此形状不对必须显式
    归一成空列表并可被单测钉住。
    """
    from app.adapters.http_git import json_items

    assert json_items([{"a": 1}, {"b": 2}]) == [{"a": 1}, {"b": 2}]
    # GitHub 搜索端点的报错形态
    assert json_items({"message": "Bad credentials", "documentation_url": "..."}) == []
    assert json_items(None) == []
    assert json_items("unexpected string") == []
    assert json_items(42) == []


def test_list_projects_survives_error_object():
    """远端返回 {"message": ...} 时 list_projects 应产出空列表而不是崩或误报「有数据」。"""
    from app.adapters.base import GitCredential
    from app.adapters.github import GitHubAdapter

    adapter = GitHubAdapter("https://api.github.com", "tok")
    adapter._conn = lambda cred: ("https://api.github.com", "tok")
    adapter._headers = lambda token: {}
    calls: list[str] = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return _FakeResp(200, {"message": "Bad credentials"})

    import app.adapters.github as gh

    original = gh.get
    gh.get = fake_get
    try:
        assert adapter.list_projects(GitCredential(base_url="", token="")) == []
    finally:
        gh.get = original


class _FakeResp:
    def __init__(self, status: int, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


def test_list_mrs_survives_error_object():
    import app.adapters.gitlab as gl
    from app.adapters.base import GitCredential
    from app.adapters.gitlab import GitLabAdapter
    from app.domain.schemas import ProjectRef

    adapter = GitLabAdapter("https://gitlab.example.com", "tok")
    adapter._conn = lambda cred: ("https://gitlab.example.com", "tok")
    original = gl.get
    gl.get = lambda url, **kwargs: _FakeResp(200, {"message": "404 Project Not Found"})
    try:
        assert adapter.list_mrs(
            GitCredential(base_url="", token=""), ProjectRef(path="a/b")) == []
    finally:
        gl.get = original
