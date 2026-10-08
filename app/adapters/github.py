"""GitHub 只读适配器（platform=github，R4）。

通过 GitHub REST API v3 拉取项目 / PR 元数据与 diff，用于 CI 中对齐真实 PR
（`--ci` 的 note_body 与真实 PR 编号 / 标题一致）。只读，无任何写操作（D5）。

- 连接参数优先取 GitCredential（base_url=API 基址、token），缺省回落到构造参数；
  构造参数来自配置（GIT_BASE_URL / GIT_TOKEN），CLI 的 --git-base-url / --git-token
  经 cred 覆盖配置。
- base_url 默认 https://api.github.com；GitHub Enterprise 指向 https://<host>/api/v3。
- get_diff 走 `Accept: application/vnd.github.v3.diff`，直接拿 unified diff 文本。
"""
from __future__ import annotations

from app.adapters.base import (
    GitCredential,
    MRItem,
    PRMetadata,
    ProjectItem,
)
from app.adapters.http_git import get, json_body, project_path, require_token
from app.domain.enums import Platform
from app.domain.schemas import MRRef, ProjectRef
from app.errors import GitUnavailable, MrNotFound, ProjectNotFound

_ACCEPT_JSON = "application/vnd.github+json"
_ACCEPT_DIFF = "application/vnd.github.v3.diff"
# GitHub PR state 取值：open / closed / all（merged 归入 closed）
_STATE_MAP = {"opened": "open", "open": "open", "closed": "closed",
              "merged": "closed", "all": "all"}


class GitHubAdapter:
    """GitHub REST API 只读实现。"""

    platform = Platform.GITHUB
    DEFAULT_BASE_URL = "https://api.github.com"

    def __init__(self, base_url: str = "", token: str = "", timeout: int = 30):
        self.base_url = base_url
        self.token = token
        self.timeout = timeout

    def _conn(self, cred: GitCredential) -> tuple[str, str]:
        base = (cred.base_url or self.base_url or self.DEFAULT_BASE_URL).rstrip("/")
        token = require_token(cred.token or self.token, "GitHub")
        return base, token

    def _headers(self, base_head: str, accept: str = _ACCEPT_JSON) -> dict:
        return {
            "Authorization": f"Bearer {base_head}",
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def list_projects(self, cred: GitCredential, *, search=None, page=1,
                      per_page=20) -> list[ProjectItem]:
        base, token = self._conn(cred)
        if search:
            url = f"{base}/search/repositories"
            params = {"q": search, "per_page": per_page, "page": page}
        else:
            url = f"{base}/user/repos"
            params = {"per_page": per_page, "page": page}
        data = json_body(get(url, headers=self._headers(token), params=params,
                             timeout=self.timeout))
        items = data.get("items") if isinstance(data, dict) else None
        rows = items if items is not None else (data or [])
        return [
            ProjectItem(
                id=int(r.get("id") or 0),
                path=r.get("name") or "",
                path_with_namespace=(r.get("full_name") or "").strip("/"),
                web_url=r.get("html_url") or "",
            )
            for r in rows if isinstance(r, dict)
        ]

    def list_mrs(self, cred: GitCredential, proj: ProjectRef, *, state="opened",
                 page=1, per_page=20) -> list[MRItem]:
        base, token = self._conn(cred)
        repo = project_path(proj)
        params = {"state": _STATE_MAP.get((state or "").lower(), "open"),
                  "per_page": per_page, "page": page}
        data = json_body(get(f"{base}/repos/{repo}/pulls",
                             headers=self._headers(token), params=params,
                             timeout=self.timeout, not_found=ProjectNotFound))
        return [
            MRItem(
                iid=int(p.get("number") or 0),
                title=p.get("title") or "",
                source_branch=(p.get("head") or {}).get("ref") or "",
                target_branch=(p.get("base") or {}).get("ref") or "",
                updated_at=p.get("updated_at") or "",
                author=(p.get("user") or {}).get("login") or "",
            )
            for p in (data or []) if isinstance(p, dict)
        ]

    def get_mr(self, cred: GitCredential, ref: MRRef) -> PRMetadata:
        base, token = self._conn(cred)
        repo = project_path(ref.project)
        data = json_body(get(f"{base}/repos/{repo}/pulls/{ref.pr_number}",
                             headers=self._headers(token), timeout=self.timeout,
                             not_found=MrNotFound))
        if not isinstance(data, dict):
            raise GitUnavailable("Git 平台响应体不是预期的 JSON 对象。")
        return PRMetadata(
            project=repo, repository=repo, pr_id=ref.pr_number,
            title=data.get("title") or "", description=data.get("body") or "",
            source_branch=(data.get("head") or {}).get("ref") or "",
            target_branch=(data.get("base") or {}).get("ref") or "",
            author=(data.get("user") or {}).get("login") or "",
            updated_at=data.get("updated_at") or "",
            web_url=data.get("html_url") or "",
        )

    def get_diff(self, cred: GitCredential, ref: MRRef) -> str:
        base, token = self._conn(cred)
        repo = project_path(ref.project)
        resp = get(f"{base}/repos/{repo}/pulls/{ref.pr_number}",
                   headers=self._headers(token, _ACCEPT_DIFF), timeout=self.timeout,
                   not_found=MrNotFound)
        return resp.text
