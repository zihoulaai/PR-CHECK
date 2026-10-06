"""DifyKBAdapter：Dify 知识库 API 示例实现（供应商可插拔框架第三家）。

Dify 知识库检索端点 ``POST /v1/datasets/{dataset_id}/retrieve`` 返回结构化文档
片段列表（``records``，每条含 ``segment.content`` / ``segment.id`` / ``score`` /
``document.name``），与 ``KnowledgeBase.search → list[KBHit]`` 契约天然契合——
无需像聊天型 KB（MaxKB）那样把答案"合成"成 hit，Evidence A/B 的"真实命中文档
片段"约束可严格成立。

契约要点（与 MaaS / OpenAI 风格的差异，演示供应商可插拔）：
- 鉴权：``Authorization: Bearer {api_key}``（dataset key）。
- ``KB_INDEX`` 承载 ``dataset_id``——Dify 检索必须指定知识库，无"全库检索"端点。
- 检索模式用 ``semantic_search``（可调），``top_k`` 来自配置。
- 维度差异：Dify 片段无 ``project`` / ``doc_type`` / ``module`` 字段。``project``
  通过 query 文本参与语义召回（``build_query_text`` 已含"项目：xxx"）；结果**不**
  再按 project 强过滤（否则无该维度的命中会全被丢弃），仅做 project 非空校验以
  满足工作流"禁止跨项目检索"的契约。``doc_type`` / ``module`` 留空，由上层按需。

上传：``POST /v1/datasets/{dataset_id}/document/create-by-text``，返回
``document.id``。Dify 索引为异步，adapter 仅返回 id，不阻塞等待（状态轮询由
调用方/人工负责）。

所有 HTTP 失败统一转 ``KbError``，交由 workflow 降级为基础自检。
"""
from __future__ import annotations

import time

import httpx

from app.adapters.base import KbDocInput
from app.adapters.query_text import build_query_text
from app.domain.schemas import KBHit, KBQuery
from app.errors import KbError


class DifyKBAdapter:
    def __init__(self, base_url: str, api_key: str, index: str | None = None,
                 top_k: int = 5, timeout: float = 30.0,
                 retrieval_mode: str = "semantic_search"):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.dataset_id = index or ""
        self.top_k = top_k
        self.timeout = timeout
        self.retrieval_mode = retrieval_mode

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
            timeout=self.timeout,
        )

    def _ds_path(self, suffix: str) -> str:
        if not self.dataset_id:
            raise KbError("Dify 适配器需要 KB_INDEX 作为 dataset_id。")
        return f"/datasets/{self.dataset_id}/{suffix}"

    def search(self, query: KBQuery) -> list[KBHit]:
        if not query.project:
            raise KbError("KBQuery.project 必填，已拒绝跨项目检索。")
        query_text = build_query_text(query)
        if len(query_text) > 248:  # Dify 的 query 字段上限为 250 字符
            query_text = query_text[:248]
        data: dict | None = None
        last_exc: Exception | None = None
        for attempt in range(3):  # 消化 Dify 服务端间歇 TLS/连接抖动
            try:
                with self._client() as c:
                    resp = c.post(
                        self._ds_path("retrieve"),
                        json={
                            "query": query_text,
                            "retrieval_mode": self.retrieval_mode,
                            "top_k": self.top_k,
                            "rerank_enable": False,
                        },
                    )
                if resp.status_code >= 400:
                    # 4xx 为确定性错误（参数/权限/配额），不重试
                    raise KbError(f"Dify 检索失败：HTTP {resp.status_code} {resp.text[:600]}")
                data = resp.json()
                break
            except httpx.HTTPError as exc:  # 传输层抖动（SSL 断开等），可重试
                last_exc = exc
                time.sleep(0.5 * (attempt + 1))
                continue
            except ValueError as exc:  # 响应体非 JSON（网关错误页等），可重试
                last_exc = exc
                time.sleep(0.5 * (attempt + 1))
                continue
        if data is None:
            raise KbError(f"知识库检索失败（已重试 3 次）：{last_exc}") from last_exc

        hits: list[KBHit] = []
        for r in (data.get("records", []) if isinstance(data, dict) else []):
            seg = r.get("segment", {}) or {}
            doc = seg.get("document", {}) or {}
            hits.append(KBHit(
                id=seg.get("id", ""),
                title=doc.get("name", "") or "",
                doc_type="",
                module="",
                project=query.project,
                snippet=seg.get("content", ""),
                score=float(r.get("score", 0.0)),
            ))
        return hits

    def upload(self, doc: KbDocInput) -> str:
        if not self.dataset_id:
            raise KbError("Dify 适配器未配置 dataset_id（KB_INDEX）。")
        try:
            with self._client() as c:
                resp = c.post(
                    self._ds_path("document/create-by-text"),
                    json={
                        "name": doc.title or doc.project,
                        "text": doc.content,
                        "indexing_technique": "high_quality",
                        "doc_form": "text_model",
                    },
                )
                if resp.status_code >= 400:
                    raise KbError(f"Dify 文档上传失败：HTTP {resp.status_code}")
                data = resp.json()
        except httpx.HTTPError as exc:
            raise KbError(f"知识库文档上传失败：{exc}") from exc
        except ValueError as exc:
            raise KbError(f"知识库返回了非 JSON 响应：{exc}") from exc
        document = data.get("document", {}) if isinstance(data, dict) else {}
        doc_id = document.get("id")
        if not doc_id:
            raise KbError("Dify 未返回 document.id。")
        return doc_id

    def delete(self, doc_id: str) -> None:
        """删除 Dify 数据集内文档：``DELETE /v1/datasets/{dataset_id}/documents/{document_id}``。

        upload 返回的就是 Dify document.id，元数据里的 id 与之同源，可直接删除。
        """
        try:
            with self._client() as c:
                resp = c.delete(self._ds_path(f"documents/{doc_id}"))
                if resp.status_code >= 400:
                    raise KbError(f"Dify 文档删除失败：HTTP {resp.status_code}")
        except httpx.HTTPError as exc:
            raise KbError(f"知识库文档删除失败：{exc}") from exc
