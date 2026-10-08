"""GitLab 只读适配器（platform=gitlab，R4）。

通过 GitLab REST API v4 拉取项目 / MR 元数据与 diff，用于 CI 中对齐真实 MR。
只读，无任何写操作（D5）。

- 连接参数优先取 GitCredential（base_url=API 基址、token），缺省回落到构造参数；
  构造参数来自配置（GIT_BASE_URL / GIT_TOKEN），CLI 的 --git-base-url / --git-token
  经 cred 覆盖配置。
- base_url 默认 https://gitlab.com/api/v4；自建 GitLab 指向 https://<host>/api/v4。
- 鉴权用 PRIVATE-TOKEN 头；项目用 URL 编码后的路径（group%2Fproject）标识。
- get_diff 走 `merge_requests/{iid}/changes`，按 GitLab 返回的逐文件 diff 重新
  拼装 unified diff（GitLab 的单文件 diff 已是 `@@` hunk 片段）。
"""
from __future__ import annotations

from urllib.parse import quote

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


def _pid(proj) -> str:
    """GitLab 项目标识：路径需 URL 编码（group/project → group%2Fproject）。"""
    return quote(project_path(proj), safe="")


def _assemble_diff(changes: list) -> str:
    """把 GitLab `changes` 数组拼回 unified diff（供 diff_parser 解析）。"""
    blocks: list[str] = []
    for ch in changes or []:
        if not isinstance(ch, dict):
            continue
        old = ch.get("old_path") or ch.get("new_path") or ""
        new = ch.get("new_path") or ch.get("old_path") or ""
        lines = [f"diff --git a/{old} b/{new}"]
        if ch.get("renamed_file"):
            lines += [f"rename from {old}", f"rename to {new}"]
        if ch.get("new_file"):
            lines += ["new file mode 100644", "--- /dev/null", f"+++ b/{new}"]
        elif ch.get("deleted_file"):
            lines += [f"--- a/{old}", "+++ /dev/null", "deleted file mode 100644"]
        else:
            lines += [f"--- a/{old}", f"+++ b/{new}"]
        if ch.get("diff"):
            lines.append(ch["diff"])
        blocks.append("\n".join(lines))
    return "\n".join(blocks) + ("\n" if blocks else "")


class GitLabAdapter:
    """GitLab REST API v4 只读实现。"""

    platform = Platform.GITLAB
    DEFAULT_BASE_URL = "https://gitlab.com/api/v4"

    def __init__(self, base_url: str = "", token: str = "", timeout: int = 30):
        self.base_url = base_url
        self.token = token
        self.timeout = timeout

    def _conn(self, cred: GitCredential) -> tuple[str, str]:
        base = (cred.base_url or self.base_url or self.DEFAULT_BASE_URL).rstrip("/")
        token = require_token(cred.token or self.token, "GitLab")
        return base, token

    @staticmethod
    def _headers(token: str) -> dict:
        return {"PRIVATE-TOKEN": token, "Accept": "application/json"}

    def list_projects(self, cred: GitCredential, *, search=None, page=1,
                      per_page=20) -> list[ProjectItem]:
        base, token = self._conn(cred)
        params = {"per_page": per_page, "page": page, "membership": "true"}
        if search:
            params["search"] = search
        data = json_body(get(f"{base}/projects", headers=self._headers(token),
                             params=params, timeout=self.timeout))
        return [
            ProjectItem(
                id=int(r.get("id") or 0),
                path=r.get("path") or "",
                path_with_namespace=r.get("path_with_namespace") or "",
                web_url=r.get("web_url") or "",
            )
            for r in (data or []) if isinstance(r, dict)
        ]

    def list_mrs(self, cred: GitCredential, proj: ProjectRef, *, state="opened",
                 page=1, per_page=20) -> list[MRItem]:
        base, token = self._conn(cred)
        params = {"state": state or "opened", "per_page": per_page, "page": page}
        data = json_body(get(f"{base}/projects/{_pid(proj)}/merge_requests",
                             headers=self._headers(token), params=params,
                             timeout=self.timeout, not_found=ProjectNotFound))
        return [
            MRItem(
                iid=int(m.get("iid") or 0),
                title=m.get("title") or "",
                source_branch=m.get("source_branch") or "",
                target_branch=m.get("target_branch") or "",
                updated_at=m.get("updated_at") or "",
                author=(m.get("author") or {}).get("name") or "",
            )
            for m in (data or []) if isinstance(m, dict)
        ]

    def get_mr(self, cred: GitCredential, ref: MRRef) -> PRMetadata:
        base, token = self._conn(cred)
        data = json_body(get(
            f"{base}/projects/{_pid(ref.project)}/merge_requests/{ref.pr_number}",
            headers=self._headers(token), timeout=self.timeout, not_found=MrNotFound,
        ))
        if not isinstance(data, dict):
            raise GitUnavailable("Git 平台响应体不是预期的 JSON 对象。")
        project = project_path(ref.project)
        return PRMetadata(
            project=project, repository=project, pr_id=ref.pr_number,
            title=data.get("title") or "", description=data.get("description") or "",
            source_branch=data.get("source_branch") or "",
            target_branch=data.get("target_branch") or "",
            author=(data.get("author") or {}).get("name") or "",
            updated_at=data.get("updated_at") or "",
            web_url=data.get("web_url") or "",
        )

    def get_diff(self, cred: GitCredential, ref: MRRef) -> str:
        base, token = self._conn(cred)
        data = json_body(get(
            f"{base}/projects/{_pid(ref.project)}/merge_requests/{ref.pr_number}/changes",
            headers=self._headers(token), timeout=self.timeout, not_found=MrNotFound,
        ))
        changes = data.get("changes") if isinstance(data, dict) else None
        return _assemble_diff(changes or [])
