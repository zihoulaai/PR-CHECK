"""GitLabAdapter：MVP 唯一 Git 平台实现（GitLab.com + 自托管，仅 Read）。

Token 由调用方注入（已解密），不进入日志 / LLM / 报告（D4 / S3）。
错误码映射见 errors.py（M5）。
"""
from __future__ import annotations

from urllib.parse import quote

import httpx

from app.adapters.base import (
    GitCredential,
    GitPlatformAdapter,
    MRItem,
    ProjectItem,
    KbDocInput,
)
from app.domain.schemas import MRRef, PRMetadata, ProjectRef
from app.errors import (
    GitLabAuthFailed,
    GitLabForbidden,
    GitLabUnavailable,
    MrNotFound,
    ProjectNotFound,
)


class GitLabAdapter:
    def __init__(self, timeout: float = 30.0):
        self._timeout = timeout

    def _client(self, cred: GitCredential) -> httpx.Client:
        return httpx.Client(
            base_url=cred.base_url.rstrip("/"),
            headers={"PRIVATE-TOKEN": cred.token, "Accept": "application/json"},
            timeout=self._timeout,
        )

    @staticmethod
    def _proj_id(proj: ProjectRef) -> str:
        if proj.id is not None:
            return str(proj.id)
        return quote(proj.path or "", safe="")

    def _request(self, client: httpx.Client, method: str, url: str, **kw) -> dict | list:
        try:
            resp = client.request(method, url, **kw)
        except httpx.TimeoutException as exc:
            raise GitLabUnavailable("GitLab 请求超时。") from exc
        except httpx.ConnectError as exc:
            raise GitLabUnavailable("无法连接 GitLab。") from exc
        except httpx.HTTPError as exc:
            raise GitLabUnavailable(f"GitLab 请求失败：{exc}") from exc

        if resp.status_code == 401:
            raise GitLabAuthFailed()
        if resp.status_code == 403:
            raise GitLabForbidden()
        if resp.status_code == 404:
            # 由调用方区分 project / mr
            raise _NotFound()
        if resp.status_code >= 400:
            raise GitLabUnavailable(f"GitLab 返回错误状态码 {resp.status_code}。")
        try:
            return resp.json()
        except Exception as exc:
            raise GitLabUnavailable("GitLab 返回无法解析的响应。") from exc

    def list_projects(self, cred: GitCredential, *, search=None, page=1, per_page=20):
        with self._client(cred) as c:
            try:
                data = self._request(
                    c, "GET", "/api/v4/projects",
                    params={"search": search or "", "page": page, "per_page": per_page,
                            "membership": True, "simple": True},
                )
            except _NotFound:
                raise ProjectNotFound()
        items = []
        for p in data:
            items.append(ProjectItem(
                id=p.get("id", 0),
                path=p.get("path", ""),
                path_with_namespace=p.get("path_with_namespace", ""),
                web_url=p.get("web_url", ""),
            ))
        return items

    def list_mrs(self, cred: GitCredential, proj: ProjectRef, *, state="opened",
                 page=1, per_page=20):
        with self._client(cred) as c:
            try:
                data = self._request(
                    c, "GET", f"/api/v4/projects/{self._proj_id(proj)}/merge_requests",
                    params={"state": state, "page": page, "per_page": per_page},
                )
            except _NotFound:
                raise ProjectNotFound()
        items = []
        for m in data:
            items.append(MRItem(
                iid=m.get("iid", 0),
                title=m.get("title", ""),
                source_branch=m.get("source_branch", ""),
                target_branch=m.get("target_branch", ""),
                updated_at=m.get("updated_at", ""),
                author=(m.get("author") or {}).get("username", ""),
            ))
        return items

    def get_mr(self, cred: GitCredential, ref: MRRef) -> PRMetadata:
        with self._client(cred) as c:
            try:
                m = self._request(
                    c, "GET",
                    f"/api/v4/projects/{self._proj_id(ref.project)}/merge_requests/{ref.iid}",
                )
            except _NotFound:
                raise MrNotFound()
        return PRMetadata(
            project=str(ref.project.path or ref.project.id or ""),
            repository=str(ref.project.path or ""),
            pr_id=m.get("iid", ref.iid),
            title=m.get("title", ""),
            description=m.get("description", "") or "",
            source_branch=m.get("source_branch", ""),
            target_branch=m.get("target_branch", ""),
            author=(m.get("author") or {}).get("username", ""),
            updated_at=m.get("updated_at", ""),
            web_url=m.get("web_url", ""),
        )

    def get_diff(self, cred: GitCredential, ref: MRRef) -> str:
        with self._client(cred) as c:
            try:
                data = self._request(
                    c, "GET",
                    f"/api/v4/projects/{self._proj_id(ref.project)}/merge_requests/{ref.iid}/changes",
                    params={"per_page": 100},
                )
            except _NotFound:
                raise MrNotFound()
        changes = data.get("changes", []) if isinstance(data, dict) else []
        parts = []
        for ch in changes:
            diff = ch.get("diff", "")
            if diff:
                parts.append(diff)
        return "\n".join(parts)


class _NotFound(Exception):
    """内部 404 标记，由调用方转换为 ProjectNotFound / MrNotFound。"""
    pass
