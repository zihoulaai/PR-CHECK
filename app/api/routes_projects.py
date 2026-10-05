"""Projects / MR 浏览路由（C1）。

GET /projects：浏览 GitLab 项目列表。
GET /projects/{id}/mrs：浏览某项目的 opened MR 列表。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.adapters.base import GitCredential
from app.api.deps import resolve_git_credential
from app.container import get_container
from app.domain.schemas import ProjectRef
from app.errors import NotConfiguredError

router = APIRouter(prefix="/projects", tags=["projects"])


@router.get("")
def list_projects(
    connection_id: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=20, ge=1, le=100),
    search: str | None = Query(default=None),
    cred: GitCredential = Depends(resolve_git_credential),
):
    git = get_container().git
    if git is None:
        raise NotConfiguredError("GitLab 适配器不可用。")
    items = git.list_projects(cred, search=search, page=page, per_page=per_page)
    return {
        "items": [
            {"id": p.id, "path": p.path, "path_with_namespace": p.path_with_namespace,
             "web_url": p.web_url}
            for p in items
        ],
        "page": page,
        "per_page": per_page,
    }


@router.get("/{project_id}/mrs")
def list_mrs(
    project_id: int,
    connection_id: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=20, ge=1, le=100),
    state: str = Query(default="opened"),
    cred: GitCredential = Depends(resolve_git_credential),
):
    git = get_container().git
    if git is None:
        raise NotConfiguredError("GitLab 适配器不可用。")
    proj = ProjectRef(id=project_id)
    items = git.list_mrs(cred, proj, state=state, page=page, per_page=per_page)
    return {
        "items": [
            {"iid": m.iid, "title": m.title, "source_branch": m.source_branch,
             "target_branch": m.target_branch, "updated_at": m.updated_at,
             "author": m.author}
            for m in items
        ],
        "page": page,
        "per_page": per_page,
    }
