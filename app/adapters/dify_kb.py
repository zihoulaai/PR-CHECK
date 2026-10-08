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
- 维度差异：Dify 片段无 ``project`` / ``doc_type`` / ``module`` 字段。检索结果一律
  以 **document.id** 为来源标识，再用本地 KbDoc 元数据（上传时按同一 id 落库）
  回填 doc_type / module / title，并按真实 project 丢弃跨项目命中——
  见 ``_resolve_metadata``。本地无元数据时保留命中并置 ``metadata_resolved=False``，
  此时 project 仅为按查询回填，隔离强度不足以支撑跨项目结论。

上传：``POST /v1/datasets/{dataset_id}/document/create-by-text``，返回
``document.id``。Dify 索引为异步，adapter 仅返回 id，不阻塞等待（状态轮询由
调用方/人工负责）。

所有 HTTP 失败统一转 ``KbError``，交由 workflow 降级为基础自检。
"""
from __future__ import annotations

import logging
import re
import time

import httpx

from app.adapters.base import KbDocInput
from app.adapters.query_text import build_query_text
from app.domain.schemas import KBHit, KBQuery
from app.errors import KbError

logger = logging.getLogger(__name__)

# Dify 的 query 字段上限为 250 字符，留 2 字符余量。
_QUERY_MAX_CHARS = 248


_DOC_NAME_PROJECT_RE = re.compile(r"^\[(?P<project>[^\]\s]+)\]\s*(?P<title>.*)$")


def _split_doc_name(name: str) -> tuple[str, str]:
    """拆出上传时写入的 ``[project] 标题`` 归属标记。

    返回 (project, title)；无标记时 project 为空串，title 原样返回。
    """
    m = _DOC_NAME_PROJECT_RE.match((name or "").strip())
    if not m:
        return "", name or ""
    return m.group("project"), m.group("title")


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
        query_text = build_query_text(query, max_chars=_QUERY_MAX_CHARS)
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
            marked_project, marked_title = _split_doc_name(doc.get("name", ""))
            # id 取 **document.id** 而非 segment.id：分段 id 会随重新切片漂移，且
            # 与本地 KbDoc 的主键（上传时记录的 document.id）不是同一维度。历史上
            # 用 segment.id 导致 workflow._drop_stale 拿分段 id 去比文档级 stale 集合，
            # 永远命中不到 ——过期文档过滤对 Dify 完全失效。
            hits.append(KBHit(
                id=doc.get("id") or seg.get("id", ""),
                title=marked_title or doc.get("name", "") or "",
                doc_type="",
                module="",
                project=marked_project,   # 文档自带归属标记优先
                snippet=seg.get("content", ""),
                score=float(r.get("score", 0.0)),
            ))
        return self._resolve_metadata(hits, query)

    def _resolve_metadata(self, hits: list[KBHit], query: KBQuery) -> list[KBHit]:
        """确定每个命中的真实归属，并按归属丢弃跨项目命中。

        Dify 片段不携带 project / doc_type / module（见模块 docstring 的维度差异）。
        此前是「用 query.project 硬盖 project」——那只是名义隔离：同一 dataset 混入
        别的项目文档时无法识别，而 doc_type 恒为空又让报告第 7 段「知识来源」丢失
        类型与模块。归属按以下优先级确定：
          1. 文档名 ``[project] 标题`` 标记（upload 时写入，随文档走，不依赖本地库）；
          2. 本地 KbDoc 元数据（key = document.id，上传时 cli._kb_upload_one 落库）；
          3. 都没有 → 归属无法证明，保留命中并置 metadata_resolved=False。

        归属与 query.project 不一致的命中直接丢弃——这是「禁止跨项目检索」的真实
        兑现，而不是把别人的知识盖上本项目标签后照常引用。
        命中项与本地元数据不一致时以归属标记为准：metadata 被改过或串库时，
        文档自身携带的标记才是更强的证据。

        另：同一 document 的多个分段按 id 归并（保留最高分），否则 top_k 会被同一
        文档的多个分段占满，实际可引用的文档数变少。
        """
        # 同一 document 可能有多个分段命中：按 id 归并，保留最高分
        best: dict[str, KBHit] = {}
        for h in hits:
            if not h.id:
                continue
            prev = best.get(h.id)
            if prev is None or h.score > prev.score:
                best[h.id] = h
        hits = sorted(best.values(), key=lambda h: h.score, reverse=True)
        if not hits:
            return hits

        try:
            from app.storage.repo import get_kb_docs_by_ids

            docs = get_kb_docs_by_ids({h.id for h in hits})
        except Exception as exc:  # noqa: BLE001 - 元数据不可用不得丢弃检索结果
            logger.warning("kb_dify_metadata_unavailable type=%s", type(exc).__name__)
            docs = {}

        out: list[KBHit] = []
        for h in hits:
            marked = h.project          # 文档名归属标记
            doc = docs.get(h.id)        # 本地元数据
            if marked:
                # 归属已由文档自身证明：直接据此过滤，元数据只补维度
                if marked != query.project:
                    logger.info("kb_hit_dropped_cross_project doc=%s owner=%s",
                                h.id, marked)
                    continue
                out.append(h.model_copy(update={
                    "doc_type": (doc.doc_type if doc else "") or h.doc_type,
                    "module": (doc.module if doc else "") or h.module,
                    "project": marked,
                    "metadata_resolved": True,
                }))
                continue
            if doc is None:
                # 归属无法证明：保留命中（可能是有意注入的共享文档），但如实标记
                out.append(h.model_copy(update={"project": query.project}))
                continue
            if doc.project and doc.project != query.project:
                logger.info("kb_hit_dropped_cross_project doc=%s owner=%s",
                            h.id, doc.project)
                continue
            out.append(h.model_copy(update={
                "title": doc.title or h.title,
                "doc_type": doc.doc_type or h.doc_type,
                "module": doc.module or h.module,
                "project": doc.project or query.project,
                "metadata_resolved": True,
            }))
        return out

    def upload(self, doc: KbDocInput) -> str:
        if not self.dataset_id:
            raise KbError("Dify 适配器未配置 dataset_id（KB_INDEX）。")
        try:
            with self._client() as c:
                resp = c.post(
                    self._ds_path("document/create-by-text"),
                    json={
                        # 文档名带上 [project] 前缀：Dify 片段不带 project 维度，
                        # 而一个 dataset 可能混放多个项目的文档。上传时把归属写进
                        # 名称，检索时即可据此做强隔离——否则同名 dataset 里
                        # 别的项目的规范会被当作本项目知识引用成 A 级证据。
                        "name": f"[{doc.project}] {doc.title or doc.project}",
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

    def list_documents(self, project: str = "") -> list[dict]:
        """列出数据集内实际存在的文档：``GET /v1/datasets/{id}/documents``。

        与本地 KbDoc 注册表是两套视图。本地表只记录经 `kb upload` 上传的文档，
        因此**他人上传、元数据丢失或迁移遗留的文档在 `kb list` 里完全不可见**——
        它们会被检索命中并进入报告，却无法用 `kb delete` 清理（默认只删注册表里
        记录过的 id）。提供这个方法才能让「线上到底有什么」可查。

        返回原始 dict 列表（不映射成本地 schema：供应商字段可能更新，
        强转会因未知字段而整体失败）；调用方只读 id / name / word_count 等稳定字段。

        ``project`` 为 RoutingKB 转发用；单库形态下本适配器只服务一个 dataset，
        故忽略该参数（不按它过滤——过滤是 RoutingKB 的职责）。
        """
        try:
            with self._client() as c:
                resp = c.get(self._ds_path("documents?page=1&limit=100"))
                if resp.status_code >= 400:
                    raise KbError(f"Dify 文档列表失败：HTTP {resp.status_code}")
                data = resp.json()
        except httpx.HTTPError as exc:
            raise KbError(f"知识库文档列表失败：{exc}") from exc
        except ValueError as exc:
            raise KbError(f"Dify 返回了非 JSON 响应：{exc}") from exc
        docs = data.get("data") if isinstance(data, dict) else None
        return [d for d in (docs or []) if isinstance(d, dict)]

    def delete(self, doc_id: str, *, project: str = "") -> None:
        """删除 Dify 数据集内文档：``DELETE /v1/datasets/{dataset_id}/documents/{document_id}``。

        upload 返回的就是 Dify document.id，元数据里的 id 与之同源，可直接删除。

        ``project`` 被 RoutingKB 用来选库，单库形态下无需关心，故忽略。
        """
        try:
            with self._client() as c:
                resp = c.delete(self._ds_path(f"documents/{doc_id}"))
                if resp.status_code >= 400:
                    raise KbError(f"Dify 文档删除失败：HTTP {resp.status_code}")
        except httpx.HTTPError as exc:
            raise KbError(f"知识库文档删除失败：{exc}") from exc
