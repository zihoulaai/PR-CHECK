"""适配器边界测试：LLM / KB 响应解析与重试语义。

这些路径决定了「LLM 输出异常 → 退出码 5」「KB 不可用 → 降级为退出码 0」
「瞬时故障是否重试」，必须保证不会以裸 KeyError / JSONDecodeError 冒泡成
INTERNAL_ERROR，也不会把 429/5xx 当成永久失败。
"""
from __future__ import annotations


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
