"""SQLite 持久化 CRUD：KB 文档 metadata。

所有敏感字段（Token）以加密字符串形式入参，本层不负责加密/解密。
"""
from __future__ import annotations

import json
from typing import Optional

from sqlmodel import select

from app.domain.models import KbDoc, _new_id
from app.storage.sqlite import session_scope


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
