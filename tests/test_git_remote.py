"""远端 Git 平台适配器测试（R4：github / gitlab 只读拉取）。

覆盖：
- GitHub / GitLab get_mr / get_diff / list_mrs 的字段映射与 URL 约定；
- 状态码映射（401→GitAuthFailed / 403→GitForbidden / 404→MrNotFound /
  5xx→GitUnavailable）与网络层降级（GitUnavailable）；
- Token 缺失 → GitAuthFailed（不静默发匿名请求）；
- 注册表路由（select_git_adapter / build_git）与未知平台报错；
- GitLab 逐文件 diff 拼装后能被 diff_parser 正常解析；
- CLI：远端平台缺 --repo / --mr → INVALID_REQUEST（rc=2）。
"""
from __future__ import annotations

import httpx
import pytest

from app.adapters import http_git
from app.adapters.base import GitCredential
from app.adapters.github import GitHubAdapter
from app.adapters.gitlab import GitLabAdapter
from app.adapters.registry import build_git
from app.config import Settings
from app.container import select_git_adapter
from app.domain.enums import Platform
from app.domain.schemas import MRRef, ProjectRef
from app.errors import (
    GitAuthFailed,
    GitForbidden,
    GitUnavailable,
    MrNotFound,
    ProjectNotFound,
)


class _Resp:
    def __init__(self, status_code: int = 200, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("响应体非 JSON")
        return self._payload


def _patch_client(monkeypatch, handler):
    """把 http_git 使用的 httpx.Client 换成返回 canned 响应的假客户端。"""
    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None, params=None):
            return handler(url, headers, params)

    monkeypatch.setattr("app.adapters.http_git.httpx.Client", _Client)


def _cred(token: str = "tok", base: str = "") -> GitCredential:
    return GitCredential(base_url=base, token=token, platform=Platform.GITHUB.value)


def _ref(path: str = "owner/repo", iid: int = 7) -> MRRef:
    return MRRef(project=ProjectRef(path=path), iid=iid)


# ===== GitHub =====
def test_github_get_mr_maps_fields(monkeypatch):
    payload = {
        "number": 7, "title": "增加退款接口", "body": "支持部分退款",
        "head": {"ref": "feature/refund"}, "base": {"ref": "main"},
        "user": {"login": "dev"}, "updated_at": "2026-10-05T12:00:00Z",
        "html_url": "https://github.com/owner/repo/pull/7",
    }
    _patch_client(monkeypatch, lambda url, h, p: _Resp(200, payload))
    pr = GitHubAdapter().get_mr(_cred(), _ref())
    assert pr.pr_id == 7
    assert pr.title == "增加退款接口"
    assert pr.source_branch == "feature/refund"
    assert pr.target_branch == "main"
    assert pr.author == "dev"
    assert pr.project == "owner/repo"
    assert pr.web_url.endswith("/pull/7")


def test_github_get_diff_uses_diff_accept_header(monkeypatch):
    seen = {}

    def handler(url, headers, params):
        seen["accept"] = headers.get("Accept")
        return _Resp(200, text="diff --git a/x b/x\n")

    _patch_client(monkeypatch, handler)
    diff = GitHubAdapter().get_diff(_cred(), _ref())
    assert diff.startswith("diff --git")
    assert seen["accept"] == "application/vnd.github.v3.diff"


def test_github_list_mrs_maps_state(monkeypatch):
    seen = {}
    payload = [{"number": 3, "title": "t", "head": {"ref": "f"}, "base": {"ref": "main"},
                "user": {"login": "u"}, "updated_at": ""}]

    def handler(url, headers, params):
        seen["params"] = params
        return _Resp(200, payload)

    _patch_client(monkeypatch, handler)
    mrs = GitHubAdapter().list_mrs(_cred(), ProjectRef(path="o/r"), state="opened")
    assert mrs[0].iid == 3 and mrs[0].source_branch == "f"
    assert seen["params"]["state"] == "open"  # opened -> open


def test_github_default_base_url(monkeypatch):
    seen = []
    _patch_client(monkeypatch, lambda url, h, p: seen.append(url) or _Resp(200, {"items": []}))
    GitHubAdapter().list_projects(_cred(base=""))
    assert seen[0].startswith("https://api.github.com/")


def test_remote_requires_token():
    with pytest.raises(GitAuthFailed):
        GitHubAdapter().get_mr(_cred(token=""), _ref())
    with pytest.raises(GitAuthFailed):
        GitLabAdapter().get_mr(_cred(token=""), _ref())


@pytest.mark.parametrize("status,exc", [
    (401, GitAuthFailed), (403, GitForbidden), (404, MrNotFound), (500, GitUnavailable),
])
def test_github_status_mapping(monkeypatch, status, exc):
    _patch_client(monkeypatch, lambda url, h, p: _Resp(status))
    with pytest.raises(exc):
        GitHubAdapter().get_mr(_cred(), _ref())


def test_github_list_mrs_404_is_project_not_found(monkeypatch):
    _patch_client(monkeypatch, lambda url, h, p: _Resp(404))
    with pytest.raises(ProjectNotFound):
        GitHubAdapter().list_mrs(_cred(), ProjectRef(path="o/r"))


def test_github_network_error_degrades(monkeypatch):
    def handler(url, headers, params):
        raise httpx.ConnectError("boom")

    _patch_client(monkeypatch, handler)
    with pytest.raises(GitUnavailable):
        GitHubAdapter().get_mr(_cred(), _ref())


def test_github_non_json_body_degrades(monkeypatch):
    _patch_client(monkeypatch, lambda url, h, p: _Resp(200, payload=None))
    with pytest.raises(GitUnavailable):
        GitHubAdapter().get_mr(_cred(), _ref())


def test_github_missing_project_path_raises(monkeypatch):
    _patch_client(monkeypatch, lambda url, h, p: _Resp(200, {}))
    with pytest.raises(ProjectNotFound):
        GitHubAdapter().get_mr(_cred(), MRRef(project=ProjectRef(id=1), iid=7))


# ===== GitLab =====
def test_gitlab_diff_is_parseable(monkeypatch):
    payload = {"changes": [
        {"old_path": "src/a.py", "new_path": "src/a.py", "new_file": False,
         "deleted_file": False, "renamed_file": False,
         "diff": "@@ -1 +1 @@\n-x = 1\n+x = 2\n"},
        {"old_path": "src/new.java", "new_path": "src/new.java", "new_file": True,
         "deleted_file": False, "renamed_file": False,
         "diff": "@@ -0,0 +1 @@\n+class New {}\n"},
    ]}
    _patch_client(monkeypatch, lambda url, h, p: _Resp(200, payload))
    diff = GitLabAdapter().get_diff(_cred(), _ref("grp/proj", 3))
    from app.parser.diff_parser import parse_diff

    parsed = parse_diff(diff)
    assert [f.path for f in parsed] == ["src/a.py", "src/new.java"]
    assert parsed[0].status == "modified"
    assert parsed[1].status == "added"


def test_gitlab_deleted_file_marked(monkeypatch):
    payload = {"changes": [
        {"old_path": "src/gone.py", "new_path": "src/gone.py", "new_file": False,
         "deleted_file": True, "renamed_file": False, "diff": "@@ -1 +0,0 @@\n-x\n"},
    ]}
    _patch_client(monkeypatch, lambda url, h, p: _Resp(200, payload))
    diff = GitLabAdapter().get_diff(_cred(), _ref("grp/proj", 3))
    from app.parser.diff_parser import parse_diff

    assert parse_diff(diff)[0].status == "deleted"


def test_gitlab_encodes_project_path(monkeypatch):
    seen = {}
    _patch_client(monkeypatch, lambda url, h, p: seen.update(url=url) or _Resp(200, {"title": "t"}))
    GitLabAdapter().get_mr(_cred(), _ref("grp/proj", 3))
    assert "grp%2Fproj" in seen["url"]


def test_gitlab_default_base_url(monkeypatch):
    seen = []
    _patch_client(monkeypatch, lambda url, h, p: seen.append(url) or _Resp(200, []))
    GitLabAdapter().list_projects(_cred(base=""))
    assert seen[0].startswith("https://gitlab.com/api/v4/")


def test_gitlab_uses_private_token_header(monkeypatch):
    seen = {}
    _patch_client(monkeypatch, lambda url, h, p: seen.update(headers=h) or _Resp(200, []))
    GitLabAdapter().list_projects(_cred(token="secret"))
    assert seen["headers"]["PRIVATE-TOKEN"] == "secret"


# ===== 注册表 / 路由 =====
def test_select_git_adapter_routes_remote():
    assert isinstance(select_git_adapter("github"), GitHubAdapter)
    assert isinstance(select_git_adapter("gitlab"), GitLabAdapter)
    assert select_git_adapter("local").__class__.__name__ == "LocalGitAdapter"


def test_build_git_unknown_platform_raises():
    with pytest.raises(GitUnavailable):
        build_git(Settings(), "bitbucket")


def test_build_git_local_from_default_settings():
    assert build_git(Settings()).__class__.__name__ == "LocalGitAdapter"


def test_container_injected_remote_adapter_wins(container):
    from app.adapters.fakes import FakeGitPlatform

    fake = FakeGitPlatform(platform=Platform.GITHUB)
    container.git_adapters[Platform.GITHUB.value] = fake
    assert select_git_adapter("github") is fake


# ===== CLI =====
def test_cli_remote_requires_repo():
    from app.cli import main

    assert main(["check", "--platform", "github", "--mr", "7"]) == 2


def test_cli_remote_requires_mr():
    from app.cli import main

    assert main(["check", "--platform", "github", "--repo", "owner/repo"]) == 2


def test_cli_fake_with_remote_platform_rejected(monkeypatch):
    """--fake + 远端平台显式拒绝（rc 2），不发真实请求、不残留 PR_CHECK_USE_FAKE。"""
    from app.cli import main

    monkeypatch.delenv("PR_CHECK_USE_FAKE", raising=False)
    rc = main(["check", "--fake", "--platform", "github",
               "--repo", "owner/repo", "--mr", "7"])
    assert rc == 2
    import os
    assert os.getenv("PR_CHECK_USE_FAKE") != "1"


def test_cli_remote_check_end_to_end(container, capsys):
    """注入 Fake 远端适配器：check --platform github 走全流程并复用 fake LLM/KB。"""
    import json

    from app.adapters.fakes import FakeGitPlatform
    from app.cli import main

    container.git_adapters[Platform.GITHUB.value] = FakeGitPlatform(
        platform=Platform.GITHUB, project_path="owner/repo")
    rc = main(["check", "--platform", "github", "--repo", "owner/repo", "--mr", "7"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["meta"]["project"] == "owner/repo"