"""DifyKBAdapter 边界与元数据回填测试（与 test_openai_kb / test_adapters 对称）。

核心回归点是 Dify 检索响应的**维度缺失**与**来源标识错位**：
- 片段不携带 project / doc_type / module，必须靠本地 KbDoc（key = document.id）回填；
- 来源标识必须是 document.id 而非 segment.id，否则 workflow._drop_stale 拿分段 id
  去比文档级 stale 集合，过期文档过滤永远失效。
沿用 httpx.MockTransport 打桩。
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.adapters.dify_kb import DifyKBAdapter
from app.domain.schemas import KBQuery
from app.errors import KbError


def _adapter(handler) -> DifyKBAdapter:
    kb = DifyKBAdapter("http://kb.local", "k", "ds-1")
    kb._client = lambda: httpx.Client(  # type: ignore[method-assign]
        base_url="http://kb.local", transport=httpx.MockTransport(handler))
    return kb


def _records(*items):
    """构造 Dify retrieve 响应；items = (document_id, doc_name, score, content)。"""
    return {"records": [
        {"score": score, "segment": {
            "id": f"seg-{doc_id}", "content": content,
            "document": {"id": doc_id, "name": doc_name},
        }}
        for doc_id, doc_name, score, content in items
    ]}


def _seed_doc(doc_id: str, *, project: str, doc_type: str = "api_document",
              module: str = "", title: str = "", status: str = "active"):
    from app.domain.models import KbDoc
    from app.storage.repo import insert_kb_doc

    return insert_kb_doc(KbDoc(
        id=doc_id, project=project, module=module, doc_type=doc_type,
        title=title or doc_id, status=status))


# ===== 请求体 / 端点 =====
def test_search_endpoint_and_request_body(container):
    captured: dict = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content.decode())
        return httpx.Response(200, json=_records())

    kb = _adapter(handler)
    kb.search(KBQuery(project="pr-check", api_changes=["src/a.py"]))
    assert captured["url"].endswith("/datasets/ds-1/retrieve")
    body = captured["json"]
    assert body["top_k"] == 5
    assert "src/a.py" in body["query"]


def test_search_requires_project():
    kb = _adapter(lambda r: httpx.Response(200, json=_records()))
    with pytest.raises(KbError):
        kb.search(KBQuery(project=""))


# ===== 来源标识：必须是 document.id =====
def test_hit_id_is_document_id_not_segment_id(container):
    _seed_doc("doc-1", project="pr-check", doc_type="api_document",
              module="pay", title="支付接口")

    def handler(request):
        return httpx.Response(200, json=_records(("doc-1", "支付接口", 0.9, "内容")))

    kb = _adapter(handler)
    hits = kb.search(KBQuery(project="pr-check"))
    assert [h.id for h in hits] == ["doc-1"]
    assert not any(h.id.startswith("seg-") for h in hits)


def test_hit_falls_back_to_segment_id_when_document_missing(container):
    """老版本 Dify 响应可能不带 document.id：退回 segment.id，不得整条丢弃。"""
    def handler(request):
        return httpx.Response(200, json={"records": [
            {"score": 0.5, "segment": {"id": "seg-only", "content": "x"}},
        ]})

    kb = _adapter(handler)
    hits = kb.search(KBQuery(project="pr-check"))
    assert [h.id for h in hits] == ["seg-only"]
    assert hits[0].metadata_resolved is False  # 无本地元数据依据，如实标记


# ===== 元数据回填 =====
def test_metadata_backfilled_from_local_kbdoc(container):
    """Dify 片段无 doc_type / module；必须用本地元数据补齐，否则报告第 7 段全是空值。"""
    _seed_doc("doc-1", project="pr-check", doc_type="technical_debt",
              module="refund", title="退款缓存一致性问题")

    kb = _adapter(lambda r: httpx.Response(
        200, json=_records(("doc-1", "Dify 侧标题", 0.8, "退款缓存需失效策略"))))
    hit = kb.search(KBQuery(project="pr-check"))[0]
    assert hit.doc_type == "technical_debt"
    assert hit.module == "refund"
    assert hit.title == "退款缓存一致性问题"   # 以本地元数据为准，非 Dify 的 name
    assert hit.project == "pr-check"
    assert hit.metadata_resolved is True
    assert hit.snippet == "退款缓存需失效策略"  # 片段内容仍取 Dify


def test_cross_project_hit_is_dropped_by_real_metadata(container):
    """真隔离：本地元数据里 project 不匹配的命中必须丢弃，而非沿用 query.project 蒙混。"""
    _seed_doc("doc-mine", project="pr-check", doc_type="api_document")
    _seed_doc("doc-other", project="team/order", doc_type="api_document")

    kb = _adapter(lambda r: httpx.Response(200, json=_records(
        ("doc-mine", "mine", 0.9, "a"),
        ("doc-other", "other", 0.95, "b"),   # 分数更高，但属于别的项目
    )))
    hits = kb.search(KBQuery(project="pr-check"))
    assert [h.id for h in hits] == ["doc-mine"]


def test_hit_without_local_metadata_is_kept_but_flagged(container):
    """非本工具上传的文档没有本地元数据：保留命中，但不得声称拿到了 project。"""
    kb = _adapter(lambda r: httpx.Response(
        200, json=_records(("foreign", "外来文档", 0.7, "x"))))
    hit = kb.search(KBQuery(project="pr-check"))[0]
    assert hit.id == "foreign"
    assert hit.metadata_resolved is False
    assert hit.doc_type == ""


# ===== 文档名归属标记：上传时写入 [project]，检索时据此强隔离 =====
def test_upload_writes_project_marker_into_doc_name(container):
    captured: dict = {}

    def handler(request):
        captured["json"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"document": {"id": "doc-x"}})

    from app.adapters.base import KbDocInput

    kb = _adapter(handler)
    kb.upload(KbDocInput(project="pr-check", module="pay",
                         doc_type="api_document", title="支付接口", content="..."))
    # 归属必须随文档走：否则同名 dataset 里别的项目文档无法被识别出来
    assert captured["json"]["name"] == "[pr-check] 支付接口"


def test_doc_name_marker_beats_query_project(container):
    """文档自带归属标记时，以标记为准——即使本地元数据缺失或被写成了别的项目。"""
    kb = _adapter(lambda r: httpx.Response(200, json=_records(
        ("doc-a", "[team/order] 支付接口规范", 0.9, "a"),
        ("doc-b", "[pr-check] 退款接口规范", 0.8, "b"),
    )))
    hits = kb.search(KBQuery(project="pr-check"))
    # doc-a 属于别的项目：即使它分数最高也必须丢弃
    assert [h.id for h in hits] == ["doc-b"]
    assert hits[0].title == "退款接口规范"   # 标记前缀已从标题剥离
    assert hits[0].project == "pr-check"
    assert hits[0].metadata_resolved is True


def test_doc_name_marker_combines_with_local_metadata(container):
    """标记定归属，本地元数据补 doc_type / module。"""
    _seed_doc("doc-a", project="pr-check", doc_type="technical_debt",
              module="refund", title="本地标题")
    kb = _adapter(lambda r: httpx.Response(
        200, json=_records(("doc-a", "[pr-check] 退款缓存债务", 0.9, "a"))))
    hit = kb.search(KBQuery(project="pr-check"))[0]
    assert hit.doc_type == "technical_debt"
    assert hit.module == "refund"
    assert hit.title == "退款缓存债务"        # 标记后的标题优先于本地标题
    assert hit.metadata_resolved is True


def test_doc_name_marker_survives_wrong_local_project(container):
    """本地元数据串库时，文档自带标记才是更强的归属证据。"""
    _seed_doc("doc-a", project="team/order", doc_type="api_document")
    kb = _adapter(lambda r: httpx.Response(
        200, json=_records(("doc-a", "[pr-check] 规范", 0.9, "a"))))
    hits = kb.search(KBQuery(project="pr-check"))
    assert [h.id for h in hits] == ["doc-a"]
    assert hits[0].metadata_resolved is True


@pytest.mark.parametrize("name,expected", [
    ("[pr-check] 退款接口", ("pr-check", "退款接口")),
    ("[team/order] 支付规范", ("team/order", "支付规范")),
    ("无标记文档.md", ("", "无标记文档.md")),
    ("[未闭合 文档", ("", "[未闭合 文档")),
    ("", ("", "")),
])
def test_split_doc_name(name, expected):
    from app.adapters.dify_kb import _split_doc_name

    assert _split_doc_name(name) == expected


# ===== 归并 =====
def test_segments_of_same_document_are_merged(container):
    """同一文档多个分段：按 document.id 归并并保留最高分，否则 top_k 被单一文档占满。"""
    _seed_doc("doc-1", project="pr-check")
    payload = {"records": [
        {"score": 0.30, "segment": {"id": "seg-a", "content": "a",
                                     "document": {"id": "doc-1", "name": "n"}}},
        {"score": 0.70, "segment": {"id": "seg-b", "content": "b",
                                     "document": {"id": "doc-1", "name": "n"}}},
    ]}
    kb = _adapter(lambda r: httpx.Response(200, json=payload))
    hits = kb.search(KBQuery(project="pr-check"))
    assert len(hits) == 1
    assert hits[0].score == pytest.approx(0.70)
    assert hits[0].snippet == "b"


# ===== 过期文档过滤对 Dify 真正生效（本次修复的核心回归点）=====
def test_stale_document_is_filtered_out(container):
    """_drop_stale 比对的是文档级 stale 集合；命中项 id 必须是 document.id 才能对上。"""
    from app.agent.workflow import _drop_stale
    from app.domain.schemas import KBHit

    _seed_doc("doc-live", project="pr-check", status="active")
    _seed_doc("doc-stale", project="pr-check", status="stale")

    kb = _adapter(lambda r: httpx.Response(200, json=_records(
        ("doc-live", "live", 0.9, "a"),
        ("doc-stale", "stale", 0.8, "b"),
    )))
    hits = kb.search(KBQuery(project="pr-check"))
    assert {h.id for h in hits} == {"doc-live", "doc-stale"}  # 检索阶段都在

    kept = _drop_stale(hits, "pr-check")
    assert [h.id for h in kept] == ["doc-live"]                # 过滤阶段剔除 stale
    # 反向对照：若 id 仍是 segment id，stale 集合永远对不上
    segment_ids = [KBHit(id="seg-doc-stale", title="t", doc_type="") for _ in range(1)]
    assert _drop_stale(segment_ids, "pr-check") == segment_ids


# ===== 异常降级 =====
@pytest.mark.parametrize("make_resp", [
    lambda: httpx.Response(200, text="<html>502</html>"),   # 非 JSON
    lambda: httpx.Response(503),                            # HTTP 错误
])
def test_search_errors_raise_kb_error(make_resp):
    kb = _adapter(lambda r: make_resp())
    with pytest.raises(KbError):
        kb.search(KBQuery(project="pr-check"))


def test_upload_endpoint_and_body(container):
    captured: dict = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"document": {"id": "doc-x"}})

    from app.adapters.base import KbDocInput

    kb = _adapter(handler)
    doc_id = kb.upload(KbDocInput(project="pr-check", module="pay",
                                  doc_type="api_document", title="支付接口", content="..."))
    assert captured["url"].endswith("/datasets/ds-1/document/create-by-text")
    assert doc_id == "doc-x"

# ===== 列举向量库文档（list_documents）=====
def test_list_documents_returns_raw_provider_entries(container):
    """供应商字段可能更新，强映射到本地 schema 会因未知字段整体失败。"""
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(200, json={"total": 2, "data": [
            {"id": "d1", "name": "[pr-check] 规范", "word_count": 12,
             "indexing_status": "completed", "some_future_field": {"x": 1}},
            {"id": "d2", "name": "他人文档", "word_count": 3,
             "indexing_status": "completed"},
        ]})

    kb = _adapter(handler)
    docs = kb.list_documents()
    assert [d["id"] for d in docs] == ["d1", "d2"]
    assert docs[0]["word_count"] == 12


def test_list_documents_handles_empty_payload(container):
    kb = _adapter(lambda r: httpx.Response(200, json={"total": 0, "data": []}))
    assert kb.list_documents() == []


@pytest.mark.parametrize("make_resp", [
    lambda: httpx.Response(200, text="<html>502</html>"),
    lambda: httpx.Response(404),
])
def test_list_documents_errors_raise_kb_error(make_resp):
    kb = _adapter(lambda r: make_resp())
    with pytest.raises(KbError):
        kb.list_documents()


def test_adapters_without_list_documents_are_detectable():
    """CLI 据此判断「供应商不支持列举」，如实报错而不是返回空表。"""
    from app.adapters.openai_kb import OpenAIStyleKBAdapter

    assert callable(getattr(DifyKBAdapter, "list_documents", None))
    assert not callable(getattr(OpenAIStyleKBAdapter, "list_documents", None))
