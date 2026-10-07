"""SQLite 持久化 CRUD：KB 文档 metadata + 反馈/闸门事件/分析缓存。

所有敏感字段（Token）以加密字符串形式入参，本层不负责加密/解密。
"""
from __future__ import annotations

from typing import Optional

from sqlmodel import select

from app.domain.models import AnalysisCache, GateEvent, KbDoc, ReportFeedback
from app.storage.sqlite import session_scope


# ===== KB 文档 metadata =====
def insert_kb_doc(doc: KbDoc) -> KbDoc:
    with session_scope() as s:
        s.add(doc)
        s.commit()
        s.refresh(doc)
        return doc


def list_kb_docs(project: Optional[str] = None, module: Optional[str] = None,
                 doc_type: Optional[str] = None,
                 status: Optional[str] = None) -> list[KbDoc]:
    with session_scope() as s:
        stmt = select(KbDoc)
        if project:
            stmt = stmt.where(KbDoc.project == project)
        if module:
            stmt = stmt.where(KbDoc.module == module)
        if doc_type:
            stmt = stmt.where(KbDoc.doc_type == doc_type)
        if status:
            stmt = stmt.where(KbDoc.status == status)
        return list(s.exec(stmt).all())


def list_stale_doc_ids(project: Optional[str] = None) -> set[str]:
    """已标记 stale（过期）的文档 id；检索时用于剔除不再生效的来源。"""
    with session_scope() as s:
        stmt = select(KbDoc).where(KbDoc.status == "stale")
        if project:
            stmt = stmt.where(KbDoc.project == project)
        return {d.id for d in s.exec(stmt).all()}


def delete_kb_doc(doc_id: str) -> bool:
    """删除本地 KB 元数据。返回是否删掉一行（False = 本地无此 id）。

    调用方（kb delete）先删向量库再删元数据：元数据缺失说明本地从未记录，
    不应阻断向量库侧的结果。
    """
    from sqlmodel import delete as sql_delete

    with session_scope() as s:
        result = s.exec(sql_delete(KbDoc).where(KbDoc.id == doc_id))
        s.commit()
        return bool(result.rowcount)


def mark_kb_docs_stale(project: str, module: Optional[str] = None,
                       keep_ids: Optional[set[str]] = None) -> list[str]:
    """把 project（+module）下不在 keep_ids 中的 active 文档标记为 stale，返回被标记的 id。

    语义：调用方（kb import --prune）以「本批即事实源」为准——目录里已不存在的文档
    标记过期而非删除，保留审计线索；被标记的 id 不再参与 KB 检索（workflow 过滤）。
    """
    keep = keep_ids or set()
    with session_scope() as s:
        stmt = select(KbDoc).where(KbDoc.project == project, KbDoc.status == "active")
        if module:
            stmt = stmt.where(KbDoc.module == module)
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
def _before_since(created_at: str, since: Optional[str]) -> bool:
    """created_at 早于 since 下界（应排除）时返回 True；since 为空表示不过滤。"""
    return bool(since) and created_at < since


def upsert_feedback(rec: ReportFeedback) -> ReportFeedback:
    """按主键 upsert：同一 (report_id, section, item_key) 重复标记覆盖而非追加。"""
    with session_scope() as s:
        s.merge(rec)
        s.commit()
        return rec


def list_feedback(project: Optional[str] = None, since: Optional[str] = None
                  ) -> list[ReportFeedback]:
    with session_scope() as s:
        stmt = select(ReportFeedback)
        if project:
            stmt = stmt.where(ReportFeedback.project == project)
        rows = list(s.exec(stmt).all())
    return [r for r in rows if not _before_since(r.created_at, since)]


def record_gate_event(ev: GateEvent) -> GateEvent:
    """按主键 upsert：同一 (report_id, specs) 只保留一条，重复 check 不虚增。"""
    with session_scope() as s:
        s.merge(ev)
        s.commit()
        return ev


def list_gate_events(project: Optional[str] = None, since: Optional[str] = None
                     ) -> list[GateEvent]:
    with session_scope() as s:
        stmt = select(GateEvent)
        if project:
            stmt = stmt.where(GateEvent.project == project)
        rows = list(s.exec(stmt).all())
    return [r for r in rows if not _before_since(r.created_at, since)]


# ===== LLM 结果缓存（R3） =====
def get_cache_entry(cache_key: str) -> Optional[AnalysisCache]:
    with session_scope() as s:
        return s.get(AnalysisCache, cache_key)


def put_cache_entry(entry: AnalysisCache) -> None:
    with session_scope() as s:
        s.merge(entry)
        s.commit()


def touch_cache_entry(cache_key: str) -> None:
    """命中计数 +1（观测用；失败不影响自检主流程，由调用方兜住异常）。"""
    from datetime import datetime, timezone

    with session_scope() as s:
        entry = s.get(AnalysisCache, cache_key)
        if entry is not None:
            entry.hit_count += 1
            entry.last_hit_at = datetime.now(timezone.utc).isoformat()
            s.add(entry)
            s.commit()


def clear_cache(project: Optional[str] = None) -> int:
    """清空缓存（可按 project 限定），返回删除条数。"""
    from sqlmodel import delete as sql_delete

    with session_scope() as s:
        stmt = sql_delete(AnalysisCache)
        if project:
            stmt = stmt.where(AnalysisCache.project == project)
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
            s.exec(sql_delete(AnalysisCache).where(AnalysisCache.cache_key == v.cache_key))
        s.commit()
        return len(victims)
