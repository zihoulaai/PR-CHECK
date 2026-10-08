"""MaaSVectorKBAdapter：知识库可选增强（D5 / C3）。

底层 Vector KB 对 Agent 透明；project 必填，服务端按 project 过滤（D13）。
端点路径通过配置注入，不写死业务代码（C3）。
"""
from __future__ import annotations

import uuid

import httpx

from app.adapters.base import KbDocInput
from app.adapters.query_text import build_query_text, parse_hits
from app.domain.schemas import KBHit, KBQuery
from app.errors import KbError

# 向后兼容别名：旧单测仍从本模块 import 这两个符号。
# 定义放在文件末尾（见下方定义），此处不重复赋值——同名符号在模块级被定义两次
# 会让 mypy 报 no-redef，且读者会误以为上面那份才是真正被调用的实现。
__all__ = ["MaaSVectorKBAdapter", "parse_hits", "build_query_text"]


class MaaSVectorKBAdapter:
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
                    "/search",
                    json={
                        "index": self.index,
                        "project": query.project,
                        "query": build_query_text(query),
                        "top_k": self.top_k,
                        "focus": query.focus,
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
            return _to_hits(data, query.project)
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
            # 响应结构异常：同样降级为 KbError，交由 workflow 降级为基础自检
            raise KbError(f"知识库返回结构异常：{exc}") from exc

    def upload(self, doc: KbDocInput) -> str:
        doc_id = f"kb-{uuid.uuid4().hex[:12]}"
        try:
            with self._client() as c:
                resp = c.post(
                    "/upload",
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
                    "/delete",
                    json={"id": doc_id, "index": self.index},
                )
                if resp.status_code >= 400:
                    raise KbError("知识库文档删除失败。")
        except httpx.HTTPError as exc:
            raise KbError(f"知识库文档删除失败：{exc}") from exc


def _to_hits(data, project: str) -> list[KBHit]:  # pragma: no cover
    return parse_hits(data, project)


# pragma: no cover - 兼容别名
def _build_query_text(query: KBQuery, max_chars: int | None = None) -> str:
    return build_query_text(query, max_chars=max_chars)
