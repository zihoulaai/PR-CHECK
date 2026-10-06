"""hook 子命令：模板定位、安装产物、源码 / 装机两种调用形态。

钩子模板已作为包内资源（app/hooks/pre-push）随 wheel 分发，装机形态下不再依赖
仓库里的 bin/pr_check_cli.py，而是用 `python -m app.cli` 唤起 CLI。
"""
from __future__ import annotations

import argparse
import os
import subprocess

import pytest

from app.cli import (
    CLI_MODULE,
    _hook_template,
    is_source_layout,
    main,
)


def _init_repo(path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.email=a@b.c", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", "init"],
        check=True, capture_output=True,
    )
    return str(path)


def _install_ns(project: str = "team/order", hook_name: str = "pre-push", base: str = "main"):
    return argparse.Namespace(
        project=project, base=base, hook_name=hook_name, fail_on=None,
        format="json", error_stream="stdout",
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = _init_repo(tmp_path / "repo")
    monkeypatch.chdir(root)
    return root


def test_hook_template_is_packaged_resource():
    """模板必须能从包内资源定位到（wheel 安装后同样成立）。"""
    tpl = _hook_template("pre-push")
    assert tpl is not None and tpl.is_file()
    assert "hooks" in tpl.parts
    assert _hook_template("no-such-hook") is None


def test_hook_install_source_layout_writes_cli_path(repo):
    """源码模式：配置里写 bin/pr_check_cli.py 绝对路径，钩子按原方式生效。"""
    assert is_source_layout() is True
    rc = main(["hook", "install", "--project", "team/order"])
    assert rc == 0

    cfg = open(os.path.join(repo, ".pr-check.hook"), "r", encoding="utf-8").read()
    assert "PR_CHECK_CLI=" in cfg
    cli_path = [l for l in cfg.splitlines() if l.startswith("PR_CHECK_CLI=")]
    assert cli_path and os.path.isfile(cli_path[0].split('"')[1])
    assert f'PR_CHECK_MODULE="{CLI_MODULE}"' in cfg
    assert 'PR_CHECK_PROJECT="team/order"' in cfg
    assert os.path.isfile(os.path.join(repo, ".git", "hooks", "pre-push"))


def test_hook_install_packaged_layout_uses_module(repo, monkeypatch):
    """装机模式：不写 CLI 脚本路径，改用 python -m app.cli（不依赖 PATH 上的 pr-check）。"""
    import app.cli as cli

    monkeypatch.setattr(cli, "is_source_layout", lambda: False)
    rc = main(["hook", "install", "--project", "team/order"])
    assert rc == 0

    cfg = open(os.path.join(repo, ".pr-check.hook"), "r", encoding="utf-8").read()
    assert "PR_CHECK_CLI=" not in cfg
    assert f'PR_CHECK_MODULE="{CLI_MODULE}"' in cfg
    assert os.path.isfile(os.path.join(repo, ".git", "hooks", "pre-push"))


def test_hook_uninstall_removes_and_is_idempotent(repo):
    dest = os.path.join(repo, ".git", "hooks", "pre-push")
    main(["hook", "install", "--project", "team/order"])
    assert os.path.isfile(dest)

    assert main(["hook", "uninstall"]) == 0
    assert not os.path.isfile(dest)
    # 重复卸载不应报错
    assert main(["hook", "uninstall"]) == 0


def test_hook_install_rejects_non_repo(tmp_path, monkeypatch):
    """非 git 目录安装钩子：返回 INVALID_REQUEST（退出码 2），不产生半成品。"""
    monkeypatch.chdir(tmp_path)
    assert main(["hook", "install", "--project", "team/order"]) == 2
    assert not os.path.exists(os.path.join(str(tmp_path), ".pr-check.hook"))


def test_hook_install_missing_template_raises(repo, monkeypatch):
    import app.cli as cli

    monkeypatch.setattr(cli, "_hook_template", lambda name: None)
    assert main(["hook", "install", "--project", "team/order"]) == 2
