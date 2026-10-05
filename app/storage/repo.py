"""SQLite 持久化 CRUD：GitLab 连接与 KB 文档 metadata。

所有敏感字段（Token）以加密字符串形式入参，本层不负责加密/解密。
"""
from __future__ import annotations

from typing import Optional

from sqlmodel import select

from app.domain.models import GitConnection, KbDoc
from app.storage.sqlite import session_scope


# ===== GitLab 连接 =====
def list_git_connections() -> list[GitConnection]:
    with session_scope() as s:
        return list(s.exec(select(GitConnection)).all())


def get_git_connection(conn_id: str) -> Optional[GitConnection]:
    with session_scope() as s:
        return s.get(GitConnection, conn_id)


def upsert_git_connection(name: str, base_url: str, encrypted_token: str,
                          conn_id: str = "gitlab-default") -> GitConnection:
    with session_scope() as s:
        existing = s.get(GitConnection, conn_id)
        if existing is None:
            obj = GitConnection(id=conn_id, name=name, base_url=base_url,
                                encrypted_token=encrypted_token)
            s.add(obj)
        else:
            existing.name = name
            existing.base_url = base_url
            existing.encrypted_token = encrypted_token
        s.commit()
        return s.get(GitConnection, conn_id)


# ===== KB 文档 metadata =====
def insert_kb_doc(doc: KbDoc) -> KbDoc:
    with session_scope() as s:
        s.add(doc)
        s.commit()
        s.refresh(doc)
        return doc


def list_kb_docs(project: Optional[str] = None, module: Optional[str] = None,
                 doc_type: Optional[str] = None) -> list[KbDoc]:
    with session_scope() as s:
        stmt = select(KbDoc)
        if project:
            stmt = stmt.where(KbDoc.project == project)
        if module:
            stmt = stmt.where(KbDoc.module == module)
        if doc_type:
            stmt = stmt.where(KbDoc.doc_type == doc_type)
        return list(s.exec(stmt).all())
