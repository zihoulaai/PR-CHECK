"""OpenAIStyleKBAdapter：通用 OpenAI 风格向量检索的示例实现（D5 / C3）。

覆盖「自建 RAG / 兼容 OpenAI embeddings+search 的服务」这类供应商：约定
REST 检索端点 ``/v1/search``，鉴权沿用 ``Authorization: Bearer``。本实现作为
可插拔框架的第二个示例，验证「新增供应商只需实现 ``KnowledgeBase`` 并在注册表
登记」而无需改动业务层。

字段契约（与 MaaS 的差异点，演示供应商可插拔）：
- 检索请求体不含 ``focus``（通用检索服务不消费该语义字段），仅 ``index`` /
  ``project`` / ``query`` / ``top_k``；
- 端点路径为 ``/v1/search``（MaaS 为 ``/search``）。
响应结构约定与 MaaS 一致（``{hits:[...]}``），复用 ``parse_hits`` 统一解析并
按 ``project`` 双重过滤，保证 Evidence A/B 真实命中约束不被破坏。

若实际服务端点 / 字段不同，改本文件的端点与 ``_parse`` 即可，不影响其它模块。
"""
from __future__ import annotations

import uuid

import httpx

from app.adapters.base import KbDocInput
from app.adapters.query_text import build_query_text, parse_hits
from app.domain.schemas import KBHit, KBQuery
from app.errors import KbError


class OpenAIStyleKBAdapter:
    def __init__(self, base_url: str, api_key: str, index: str | None = None,
                 top_k: int = 5, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.index = index or ""
        self.top_k = top_k
        self.timeout = timeout

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
            timeout=self.timeout,
        )

    def search(self, query: KBQuery) -> list[KBHit]:
        if not query.project:
            raise KbError("KBQuery.project 必填，已拒绝跨项目检索。")
        try:
            with self._client() as c:
                resp = c.post(
                    "/v1/search",
                    json={
                        "index": self.index,
                        "project": query.project,
                        "query": build_query_text(query),
                        "top_k": self.top_k,
                    },
                )
                if resp.status_code >= 400:
                    raise KbError()
                data = resp.json()
        except httpx.HTTPError as exc:
            raise KbError(f"知识库检索失败：{exc}") from exc
        except ValueError as exc:  # 响应体非 JSON（网关错误页等）
            raise KbError(f"知识库返回了非 JSON 响应：{exc}") from exc

        try:
            return parse_hits(data, query.project)
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
            # 响应结构异常：同样降级为 KbError，交由 workflow 降级为基础自检
            raise KbError(f"知识库返回结构异常：{exc}") from exc

    def upload(self, doc: KbDocInput) -> str:
        doc_id = f"kb-{uuid.uuid4().hex[:12]}"
        try:
            with self._client() as c:
                resp = c.post(
                    "/v1/upload",
                    json={
                        "id": doc_id,
                        "index": self.index,
                        "project": doc.project,
                        "module": doc.module,
                        "doc_type": doc.doc_type,
                        "title": doc.title,
                        "content": doc.content,
                    },
                )
                if resp.status_code >= 400:
                    raise KbError("知识库文档上传失败。")
        except httpx.HTTPError as exc:
            raise KbError(f"知识库文档上传失败：{exc}") from exc
        except ValueError as exc:
            raise KbError(f"知识库返回了非 JSON 响应：{exc}") from exc
        return doc_id

    def delete(self, doc_id: str, *, project: str = "") -> None:
        try:
            with self._client() as c:
                resp = c.post(
                    "/v1/delete",
                    json={"id": doc_id, "index": self.index},
                )
                if resp.status_code >= 400:
                    raise KbError("知识库文档删除失败。")
        except httpx.HTTPError as exc:
            raise KbError(f"知识库文档删除失败：{exc}") from exc
