"""MaaSVectorKBAdapter：知识库可选增强（D5 / C3）。

底层 Vector KB 对 Agent 透明；project 必填，服务端按 project 过滤（D13）。
端点路径通过配置注入，不写死业务代码（C3）。
"""
from __future__ import annotations

import uuid

import httpx

from app.adapters.base import KbDocInput
from app.domain.schemas import KBHit, KBQuery
from app.errors import KbError


class MaaSVectorKBAdapter:
    def __init__(self, base_url: str, api_key: str, index: str | None = None,
                 timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.index = index or ""
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
                        "query": _build_query_text(query),
                        "top_k": 5,
                        "focus": query.focus,
                    },
                )
                if resp.status_code >= 400:
                    raise KbError()
                data = resp.json()
        except httpx.HTTPError as exc:
            raise KbError(f"知识库检索失败：{exc}") from exc

        hits: list[KBHit] = []
        for h in data.get("hits", []) if isinstance(data, dict) else []:
            # 服务端按 project 强制过滤；双重保险
            if h.get("project") and h["project"] != query.project:
                continue
            hits.append(KBHit(
                id=h.get("id", ""),
                title=h.get("title", ""),
                doc_type=h.get("doc_type", ""),
                module=h.get("module", ""),
                project=h.get("project", query.project),
                snippet=h.get("snippet", ""),
                score=float(h.get("score", 0.0)),
            ))
        return hits

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
        return doc_id


def _build_query_text(query: KBQuery) -> str:
    parts = [f"项目：{query.project}"]
    if query.modules:
        parts.append("模块：" + ", ".join(query.modules))
    if query.pr_title:
        parts.append(f"PR：{query.pr_title}")
    if query.change_types:
        parts.append("变更类型：" + ", ".join(query.change_types))
    if query.keywords:
        parts.append("关键词：" + ", ".join(query.keywords[:20]))
    if query.focus:
        parts.append("重点查询：" + ", ".join(query.focus))
    return "\n".join(parts)
