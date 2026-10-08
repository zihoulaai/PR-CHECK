"""SQLite 持久化 CRUD：KB 文档 metadata + 反馈/闸门事件/分析缓存。

所有敏感字段（Token）以加密字符串形式入参，本层不负责加密/解密。

关于 ``col(...)``：SQLModel 的字段属性在静态类型里就是普通 ``str`` / ``int``
（``KbDoc.id == x`` 会被推成 ``bool`` 而非 SQL 表达式），这是它的元类决定的，
运行时正常但静态检查不认。因此凡是要进 ``where()`` / ``in_()`` 的列都用
``col(KbDoc.id)`` 包一层——它是 SQLModel 提供的类型化入口，返回真正的列表达式。
本文件统一走这条路径，避免逐处加 ``# type: ignore``。
"""
from __future__ import annotations


from sqlmodel import col, select

from app.domain.models import AnalysisCache, GateEvent, KbDoc, ReportFeedback
from app.storage.sqlite import session_scope
from datetime import UTC


# ===== KB 文档 metadata =====
def insert_kb_doc(doc: KbDoc) -> KbDoc:
    with session_scope() as s:
        s.add(doc)
        s.commit()
        s.refresh(doc)
        return doc


def list_kb_docs(project: str | None = None, module: str | None = None,
                 doc_type: str | None = None,
                 status: str | None = None) -> list[KbDoc]:
    with session_scope() as s:
        stmt = select(KbDoc)
        if project:
            stmt = stmt.where(col(KbDoc.project) == project)
        if module:
            stmt = stmt.where(col(KbDoc.module) == module)
        if doc_type:
            stmt = stmt.where(col(KbDoc.doc_type) == doc_type)
        if status:
            stmt = stmt.where(col(KbDoc.status) == status)
        return list(s.exec(stmt).all())


def list_stale_doc_ids(project: str | None = None) -> set[str]:
    """已标记 stale（过期）的文档 id；检索时用于剔除不再生效的来源。"""
    with session_scope() as s:
        stmt = select(KbDoc).where(col(KbDoc.status) == "stale")
        if project:
            stmt = stmt.where(col(KbDoc.project) == project)
        return {d.id for d in s.exec(stmt).all()}


def get_kb_docs_by_ids(ids) -> dict[str, KbDoc]:
    """按 id 批量取 KB 文档元数据，返回 {id: KbDoc}。

    供检索侧补齐供应商缺失的维度用：Dify 的检索片段不携带 project / doc_type /
    module（见 adapters/dify_kb.py 的维度差异说明），但上传时本工具已按
    document.id 落过完整元数据（cli._kb_upload_one），故可按 id 反查回填。
    """
    id_set = {i for i in ids if i}
    if not id_set:
        return {}
    with session_scope() as s:
        stmt = select(KbDoc).where(col(KbDoc.id).in_(id_set))
        return {d.id: d for d in s.exec(stmt).all()}


def delete_kb_doc(doc_id: str) -> bool:
    """删除本地 KB 元数据。返回是否删掉一行（False = 本地无此 id）。

    调用方（kb delete）先删向量库再删元数据：元数据缺失说明本地从未记录，
    不应阻断向量库侧的结果。
    """
    from sqlmodel import delete as sql_delete

    with session_scope() as s:
        result = s.exec(sql_delete(KbDoc).where(col(KbDoc.id) == doc_id))
        s.commit()
        return bool(result.rowcount)


def mark_kb_docs_stale(project: str, module: str | None = None,
                       keep_ids: set[str] | None = None) -> list[str]:
    """把 project（+module）下不在 keep_ids 中的 active 文档标记为 stale，返回被标记的 id。

    语义：调用方（kb import --prune）以「本批即事实源」为准——目录里已不存在的文档
    标记过期而非删除，保留审计线索；被标记的 id 不再参与 KB 检索（workflow 过滤）。
    """
    keep = keep_ids or set()
    with session_scope() as s:
        stmt = select(KbDoc).where(
            col(KbDoc.project) == project, col(KbDoc.status) == "active")
        if module:
            stmt = stmt.where(col(KbDoc.module) == module)
        marked: list[str] = []
        for doc in s.exec(stmt).all():
            if doc.id in keep:
                continue
            doc.status = "stale"
            s.add(doc)
            marked.append(doc.id)
        s.commit()
        return marked


# ===== 反馈 / 闸门事件（R2） =====
def _before_since(created_at: str, since: str | None) -> bool:
    """created_at 早于 since 下界（应排除）时返回 True；since 为空表示不过滤。

    拆成提前 return 而非 `bool(since) and created_at < since`：短路表达式里
    mypy 无法把 ``str | None`` 收窄成 ``str``，拆开后类型自然对齐（行为等价）。
    """
    if not since:
        return False
    return created_at < since


def upsert_feedback(rec: ReportFeedback) -> ReportFeedback:
    """按主键 upsert：同一 (report_id, section, item_key) 重复标记覆盖而非追加。"""
    with session_scope() as s:
        s.merge(rec)
        s.commit()
        return rec


def list_feedback(project: str | None = None, since: str | None = None
                  ) -> list[ReportFeedback]:
    with session_scope() as s:
        stmt = select(ReportFeedback)
        if project:
            stmt = stmt.where(col(ReportFeedback.project) == project)
        rows = list(s.exec(stmt).all())
    return [r for r in rows if not _before_since(r.created_at, since)]


def record_gate_event(ev: GateEvent) -> GateEvent:
    """按主键 upsert：同一 (report_id, specs) 只保留一条，重复 check 不虚增。"""
    with session_scope() as s:
        s.merge(ev)
        s.commit()
        return ev


def list_gate_events(project: str | None = None, since: str | None = None
                     ) -> list[GateEvent]:
    with session_scope() as s:
        stmt = select(GateEvent)
        if project:
            stmt = stmt.where(col(GateEvent.project) == project)
        rows = list(s.exec(stmt).all())
    return [r for r in rows if not _before_since(r.created_at, since)]


# ===== LLM 结果缓存（R3） =====
def get_cache_entry(cache_key: str) -> AnalysisCache | None:
    with session_scope() as s:
        return s.get(AnalysisCache, cache_key)


def put_cache_entry(entry: AnalysisCache) -> None:
    with session_scope() as s:
        s.merge(entry)
        s.commit()


def touch_cache_entry(cache_key: str) -> None:
    """命中计数 +1（观测用；失败不影响自检主流程，由调用方兜住异常）。"""
    from datetime import datetime

    with session_scope() as s:
        entry = s.get(AnalysisCache, cache_key)
        if entry is not None:
            entry.hit_count += 1
            entry.last_hit_at = datetime.now(UTC).isoformat()
            s.add(entry)
            s.commit()


def clear_cache(project: str | None = None) -> int:
    """清空缓存（可按 project 限定），返回删除条数。"""
    from sqlmodel import delete as sql_delete

    with session_scope() as s:
        stmt = sql_delete(AnalysisCache)
        if project:
            stmt = stmt.where(col(AnalysisCache.project) == project)
        result = s.exec(stmt)
        s.commit()
        return int(result.rowcount or 0)


def prune_cache(max_entries: int = 500) -> int:
    """超出上限时按创建时间淘汰最旧条目（粗略 LRU），返回删除条数。"""
    from sqlmodel import delete as sql_delete

    with session_scope() as s:
        rows = list(s.exec(select(AnalysisCache)).all())
        if len(rows) <= max_entries:
            return 0
        rows.sort(key=lambda e: e.created_at)
        victims = rows[: len(rows) - max_entries]
        for v in victims:
            s.exec(sql_delete(AnalysisCache).where(col(AnalysisCache.cache_key) == v.cache_key))
        s.commit()
        return len(victims)
