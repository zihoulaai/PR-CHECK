"""KB 文档管理路由（C1 / D5）。

POST /kb/docs：人工上传知识文档（multipart），写入 MaaS Vector KB 并在 SQLite 记录 metadata。
GET /kb/docs：按 project / module / doc_type 查询已上传文档 metadata。

多项目知识隔离（D13）：KB 检索在 adapter 层按 project 强制过滤；上传时 project 必填。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile

from app.adapters.base import KbDocInput
from app.container import get_container
from app.domain.enums import DocType
from app.domain.models import KbDoc
from app.errors import NotConfiguredError, ValidationError
from app.storage.repo import insert_kb_doc, list_kb_docs

router = APIRouter(prefix="/kb", tags=["kb"])


@router.post("/docs")
async def upload_kb_doc(
    file: UploadFile = File(...),
    project: str = Form(...),
    module: str = Form(default=""),
    doc_type: str = Form(...),
    title: str = Form(default=""),
):
    if not project:
        raise ValidationError("project 为必填项。")
    try:
        DocType(doc_type)
    except ValueError:
        raise ValidationError(f"不支持的 doc_type：{doc_type}")

    content = await file.read()
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = content.decode("utf-8", errors="ignore")

    kb = get_container().kb
    if kb is None:
        raise NotConfiguredError("知识库未配置，无法上传文档。")

    doc_input = KbDocInput(
        project=project, module=module, doc_type=doc_type,
        title=title or (file.filename or "未命名文档"),
        content=text,
    )
    doc_id = kb.upload(doc_input)

    meta = KbDoc(
        id=doc_id, project=project, module=module, doc_type=doc_type,
        title=doc_input.title, status="active", snippet=text[:500],
    )
    insert_kb_doc(meta)
    return {"id": doc_id, "project": project, "module": module,
            "doc_type": doc_type, "title": meta.title, "status": "active"}


@router.get("/docs")
def get_kb_docs(
    project: str | None = Query(default=None),
    module: str | None = Query(default=None),
    doc_type: str | None = Query(default=None),
):
    docs = list_kb_docs(project=project, module=module, doc_type=doc_type)
    return {
        "items": [
            {"id": d.id, "project": d.project, "module": d.module,
             "doc_type": d.doc_type, "title": d.title, "status": d.status}
            for d in docs
        ]
    }
