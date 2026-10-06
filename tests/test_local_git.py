"""本地仓库直连（platform=local）测试。

验证：
- LocalGitAdapter.get_diff 通过 `git diff <base>...<source>` 取出分支相对目标的变更；
- LocalGitAdapter.get_mr 从 git 合成 PRMetadata（源/目标分支、作者、标题）；
- check --repo 端到端复用 run_check 全流程（--fake 提供离线 LLM/KB）。
"""
from __future__ import annotations

import argparse
import json
import subprocess

import pytest

from app.adapters.base import GitCredential
from app.adapters.local_git import LocalGitAdapter
from app.domain.enums import Platform
from app.domain.schemas import MRRef, ProjectRef


def _git(repo: "object", *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    )


@pytest.fixture
def local_repo(tmp_path) -> "object":
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "dev@example.com")
    _git(repo, "config", "user.name", "Dev")
    (repo / "a.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init: base file")
    _git(repo, "branch", "-M", "main")
    _git(repo, "checkout", "-q", "-b", "feature/x")
    (repo / "a.txt").write_text("base\nchanged\n", encoding="utf-8")
    (repo / "b.txt").write_text("new\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "feat: add b and tweak a")
    return repo


def _cred(repo) -> GitCredential:
    return GitCredential(base_url=str(repo), token="", platform=Platform.LOCAL.value)


def _ref(project: str = "team/order") -> MRRef:
    return MRRef(project=ProjectRef(path=project), iid=0,
                 base_branch="main", source_ref="HEAD")


def test_local_get_diff(local_repo):
    adapter = LocalGitAdapter()
    diff = adapter.get_diff(_cred(local_repo), _ref())
    assert "b.txt" in diff
    assert "changed" in diff


def test_local_get_mr_synthesizes_metadata(local_repo):
    adapter = LocalGitAdapter()
    pr = adapter.get_mr(_cred(local_repo), _ref())
    assert pr.source_branch == "feature/x"
    assert pr.target_branch == "main"
    assert "Dev" in pr.author
    assert "feat: add b and tweak a" in pr.title
    assert pr.project == "team/order"


def test_check_repo_end_to_end(local_repo, capsys):
    from app.cli import cmd_check

    ns = argparse.Namespace(
        fake=True, repo=str(local_repo), base="main", source="HEAD",
        project="team/order", diff=None, input=None,
        title="", description="", source_branch="", target_branch="",
        author="", format="json", pretty=False,
    )
    rc = cmd_check(ns)
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["meta"]["project"] == "team/order"
