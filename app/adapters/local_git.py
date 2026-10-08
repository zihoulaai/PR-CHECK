"""直连本地仓库 .git 的 Git 适配器（platform=local，ADR-002 多平台扩展）。

无需 Token：通过 `git` 子进程读取本地仓库的 diff 与元数据，复用 run_check 全流程
（变更画像 → 项目 KB 检索 → LLM 综合）。典型用途：开发者在 push 前对本地分支做自检。

- get_diff：git -C <repo> diff <base>...<source>，给出 source 自 base 分叉以来的变更；
- get_mr：从 git 合成 PRMetadata（源/目标分支、作者、标题、描述、更新时间）。

list_projects / list_mrs 在本地模式无意义（项目由 --project 显式指定以锁定 KB），返回空列表。
"""
from __future__ import annotations

import subprocess

from app.adapters.base import GitCredential, MRRef, PRMetadata, ProjectRef
from app.domain.enums import Platform
from app.errors import GitUnavailable


def _git(repo: str, *args: str) -> str:
    """在指定仓库执行 git 子命令，返回 stdout；失败统一抛 GitUnavailable。"""
    try:
        proc = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError as exc:
        raise GitUnavailable("未找到 git 可执行文件，请先安装 git。") from exc
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout).strip() or "git 命令执行失败"
        raise GitUnavailable(f"读取本地仓库失败：{msg}")
    return proc.stdout


def _current_branch(repo: str) -> str:
    out = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    return out or "HEAD"


class LocalGitAdapter:
    """platform=local 的 GitPlatformAdapter 实现；只读本地 .git，不发起任何网络请求。"""

    platform = Platform.LOCAL

    def list_projects(self, cred: GitCredential, *, search=None, page=1,
                      per_page=20):
        return []

    def list_mrs(self, cred: GitCredential, proj: ProjectRef, *, state="opened",
                 page=1, per_page=20):
        return []

    def get_mr(self, cred: GitCredential, ref: MRRef) -> PRMetadata:
        repo = cred.base_url
        # 优先用显式指定的 source（如 check --source <branch/ref>），避免 CI 里
        # 「对 ref X 自检」却用了当前 HEAD 的标题/作者，导致元数据与 diff 错配，
        # 进而让 LLM 编造「标题与变更不符」的误判风险。
        source_ref = ref.source_ref or _current_branch(repo)
        target_branch = ref.base_branch or "main"
        author = _git(repo, "log", "-1", "--pretty=%an <%ae>", source_ref).strip()
        title = _git(repo, "log", "-1", "--pretty=%s", source_ref).strip() or source_ref
        description = _git(repo, "log", "-1", "--pretty=%b", source_ref).strip()
        updated_at = _git(repo, "log", "-1", "--pretty=%ci", source_ref).strip()
        # 报告中展示真实分支名：source_ref 为 HEAD 时回退到当前分支。
        source_branch = source_ref if source_ref != "HEAD" else _current_branch(repo)
        project = ref.project.path or repo
        return PRMetadata(
            project=project, repository=repo, pr_id=0,
            title=title, description=description,
            source_branch=source_branch, target_branch=target_branch,
            author=author, updated_at=updated_at,
        )

    def get_diff(self, cred: GitCredential, ref: MRRef) -> str:
        repo = cred.base_url
        base = ref.base_branch or "main"
        source = ref.source_ref or "HEAD"
        return _git(repo, "diff", f"{base}...{source}")
