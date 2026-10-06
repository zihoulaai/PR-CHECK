"""SQLite 持久化 CRUD：KB 文档 metadata。

所有敏感字段（Token）以加密字符串形式入参，本层不负责加密/解密。
"""
from __future__ import annotations

from typing import Optional

from sqlmodel import select

from app.domain.models import KbDoc
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
