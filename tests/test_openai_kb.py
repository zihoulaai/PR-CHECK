"""OpenAIStyleKBAdapter 边界测试（与 test_adapters 中 MaaS 测试对称）。

验证：端点 /v1/search、请求体不含 focus、top_k 注入、跨项目过滤、异常降级、
upload 端点 /v1/upload。沿用 httpx.MockTransport 打桩。
"""
from __future__ import annotations

import httpx
import pytest

from app.adapters.openai_kb import OpenAIStyleKBAdapter
from app.domain.schemas import KBQuery
from app.errors import KbError


def _adapter_with_response(handler) -> OpenAIStyleKBAdapter:
    kb = OpenAIStyleKBAdapter("http://kb.local", "k", "idx")
    kb._client = lambda: httpx.Client(  # type: ignore[method-assign]
        base_url="http://kb.local", transport=httpx.MockTransport(handler))
    return kb


def test_search_request_body_and_endpoint():
    import json

    captured: dict = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"hits": [
            {"id": "a", "title": "A", "doc_type": "api_document",
             "project": "p", "snippet": "s", "score": 0.9},
        ]})

    kb = _adapter_with_response(handler)
    hits = kb.search(KBQuery(project="p", api_changes=["src/a.py"]))
    assert captured["url"].endswith("/v1/search")
    body = captured["json"]
    assert body["top_k"] == 5
    assert body["index"] == "idx"
    assert body["project"] == "p"
    assert "focus" not in body          # 通用 OpenAI 风格不消费 focus
    assert "src/a.py" in body["query"]  # 变更文件清单落到 query 文本
    assert [h.id for h in hits] == ["a"]


def test_search_filters_cross_project():
    def handler(request):
        return httpx.Response(200, json={"hits": [
            {"id": "a", "title": "A", "doc_type": "api_document", "project": "p"},
            {"id": "b", "title": "B", "doc_type": "api_document", "project": "other"},
        ]})

    kb = _adapter_with_response(handler)
    hits = kb.search(KBQuery(project="p"))
    assert [h.id for h in hits] == ["a"]


def test_search_requires_project():
    kb = _adapter_with_response(lambda r: httpx.Response(200, json={"hits": []}))
    with pytest.raises(KbError):
        kb.search(KBQuery(project=""))


@pytest.mark.parametrize("make_resp", [
    lambda: httpx.Response(200, text="<html>502</html>"),      # 非 JSON
    lambda: httpx.Response(503),                                # HTTP 错误
    lambda: httpx.Response(200, json={"hits": [{"id": "a", "score": "high"}]}),  # 结构异常
])
def test_search_errors_raise_kb_error(make_resp):
    kb = _adapter_with_response(lambda r: make_resp())
    with pytest.raises(KbError):
        kb.search(KBQuery(project="p"))


def test_upload_endpoint_and_body():
    import json

    captured: dict = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"id": "kb-ignored"})

    kb = _adapter_with_response(handler)
    from app.adapters.base import KbDocInput
    doc_id = kb.upload(KbDocInput(
        project="p", module="pay", doc_type="api_document",
        title="支付接口", content="..."))
    assert captured["url"].endswith("/v1/upload")
    body = captured["json"]
    assert body["project"] == "p"
    assert body["module"] == "pay"
    assert body["doc_type"] == "api_document"
    assert body["title"] == "支付接口"
    assert doc_id.startswith("kb-")
