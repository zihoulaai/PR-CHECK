"""hook 子命令：模板定位、安装产物、源码 / 装机两种调用形态。

钩子模板已作为包内资源（app/hooks/pre-push）随 wheel 分发，装机形态下不再依赖
仓库里的 bin/pr_check_cli.py，而是用 `python -m app.cli` 唤起 CLI。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

import pytest

from app.cli import (
    CLI_MODULE,
    _hook_template,
    _is_pr_check_hook,
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


def test_hook_template_is_lf_only():
    """POSIX 钩子不能带 CRLF：\r 会被 shell 当成参数的一部分（见 .gitattributes）。"""
    raw = _hook_template("pre-push").read_bytes()
    assert b"\r\n" not in raw


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


def test_hook_template_gate_and_format_selection():
    """阻断语义：仅退出码 7 拦截；基础设施故障告警放行；有进度行；
    报告格式按渲染器可用性在 md（glow 着色）与内置 text（无渲染器）间选择。"""
    raw = _hook_template("pre-push").read_text(encoding="utf-8")
    assert "PRCHECK_MANAGED_HOOK=1" in raw
    assert 'command -v glow' in raw
    assert 'REPORT_FMT="md"' in raw
    assert 'REPORT_FMT="text"' in raw
    assert '--format "$REPORT_FMT"' in raw
    assert 'RC" -eq 7' in raw
    assert "PR_CHECK_STRICT" in raw
    assert "正在自检" in raw


def test_hook_template_no_bare_var_before_multibyte():
    """bash 多字节 locale 缺陷回归：$VAR 后紧跟全角字符会吞掉变量值与该字符首字节
    （bash 3.2 实测，dash/zsh 无此问题）。模板中变量必须写 ${VAR} 花括号形式。"""
    import re

    raw = _hook_template("pre-push").read_text(encoding="utf-8")
    bad = re.findall(r"\$[A-Za-z_][A-Za-z0-9_]*[\u0080-\uffff]", raw)
    assert bad == []


def test_hook_template_reads_stdin_refs():
    """stdin 引用过滤：删除分支（local sha 全 0）与标签推送（refs/tags/）必须可识别。"""
    raw = _hook_template("pre-push").read_text(encoding="utf-8")
    assert "read -r local_ref local_sha remote_ref remote_sha" in raw
    assert "refs/tags/" in raw
    assert "tr -d 0" in raw  # 全 0 sha = 删除分支
    assert "[ -t 0 ]" in raw  # 交互式直跑不阻塞读 stdin


# ===== hook 端到端（stub CLI）=====
_STUB = """\
import json
import os
import sys

with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\\n")
sys.exit(int(os.environ.get("STUB_RC", "0")))
"""

_BRANCH_LINE = "refs/heads/feature/refund aaaa1111 refs/heads/main bbbb2222"
_DELETE_LINE = "refs/heads/feature/gone 0000000000000000000000000000000000000000 " \
               "refs/heads/feature/gone 0000000000000000000000000000000000000000"
_TAG_LINE = "refs/tags/v1.2.3 cccc3333 refs/tags/v1.2.3 dddd4444"


@pytest.fixture
def hooked_repo(repo, tmp_path):
    """装好钩子的仓库 + stub CLI（记录 argv 并按 STUB_RC 退出）+ 钩子配置。"""
    stub = tmp_path / "stub_cli.py"
    stub.write_text(_STUB, encoding="utf-8")
    log = tmp_path / "stub.log"
    # 先安装（install 会重写 .pr-check.hook），再覆写为 stub 配置
    assert main(["hook", "install", "--project", "team/order"]) == 0
    with open(os.path.join(str(repo), ".pr-check.hook"), "w", encoding="utf-8") as fh:
        fh.write(f'PR_CHECK_PYTHON="{sys.executable}"\n')
        fh.write(f'PR_CHECK_CLI="{stub}"\n')
        fh.write('PR_CHECK_PROJECT="team/order"\n')
        fh.write('PR_CHECK_BASE="main"\n')
        fh.write('PR_CHECK_FAIL_ON="risk:high rule:violation"\n')
    env = dict(os.environ)
    env["STUB_LOG"] = str(log)
    env.pop("STUB_RC", None)
    return repo, log, env


def _run_hook(repo, env, *, stdin: str):
    return subprocess.run(
        ["sh", os.path.join(str(repo), ".git", "hooks", "pre-push")],
        input=stdin, capture_output=True, text=True, cwd=str(repo), env=env,
    )


def _stub_calls(log) -> list[list[str]]:
    if not log.exists():
        return []
    import json

    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def test_hook_runs_check_on_branch_push(hooked_repo):
    """真实分支推送：调用 stub CLI 自检，参数含 --repo/--base/--project/--fail-on。"""
    repo, log, env = hooked_repo
    proc = _run_hook(repo, env, stdin=_BRANCH_LINE + "\n")
    assert proc.returncode == 0
    calls = _stub_calls(log)
    assert len(calls) == 1
    argv = calls[0]
    assert argv[0] == "check"
    assert "--repo" in argv and "--base" in argv
    assert "team/order" in argv and "risk:high" in argv and "rule:violation" in argv
    # capture_output 下 stdout 为管道（非终端）：无 glow 着色条件，走内置纯文本
    assert "--format" in argv and "text" in argv


def test_hook_skips_deletion_push(hooked_repo):
    """删除分支（local sha 全 0）：跳过自检，stub 不被调用。"""
    repo, log, env = hooked_repo
    proc = _run_hook(repo, env, stdin=_DELETE_LINE + "\n")
    assert proc.returncode == 0
    assert _stub_calls(log) == []
    assert "跳过" in proc.stderr


def test_hook_skips_tag_push(hooked_repo):
    """标签推送：跳过自检，stub 不被调用。"""
    repo, log, env = hooked_repo
    proc = _run_hook(repo, env, stdin=_TAG_LINE + "\n")
    assert proc.returncode == 0
    assert _stub_calls(log) == []


def test_hook_runs_on_mixed_tag_and_branch(hooked_repo):
    """混合推送（标签 + 真实分支）：只要有真实分支推送就在检。"""
    repo, log, env = hooked_repo
    proc = _run_hook(repo, env, stdin=_TAG_LINE + "\n" + _BRANCH_LINE + "\n")
    assert proc.returncode == 0
    assert len(_stub_calls(log)) == 1


def test_hook_skips_when_stdin_has_no_refs(hooked_repo):
    """git 只在确有更新时才跑钩子；空 stdin = 无可检引用，跳过。"""
    repo, log, env = hooked_repo
    proc = _run_hook(repo, env, stdin="")
    assert proc.returncode == 0
    assert _stub_calls(log) == []


def test_hook_blocks_on_gate_failed(hooked_repo):
    """闸门命中（退出码 7）：钩子以非零退出中断推送。"""
    repo, _, env = hooked_repo
    env["STUB_RC"] = "7"
    proc = _run_hook(repo, env, stdin=_BRANCH_LINE + "\n")
    assert proc.returncode == 1
    assert "拦截" in proc.stderr


def test_hook_allows_infra_failure_and_strict_blocks(hooked_repo):
    """自检工具自身故障（退出码 5）默认告警放行；PR_CHECK_STRICT=1 收紧为阻断。"""
    repo, _, env = hooked_repo
    env["STUB_RC"] = "5"
    proc = _run_hook(repo, env, stdin=_BRANCH_LINE + "\n")
    assert proc.returncode == 0
    assert "不阻断" in proc.stderr

    strict_env = dict(env)
    strict_env["PR_CHECK_STRICT"] = "1"
    proc2 = _run_hook(repo, strict_env, stdin=_BRANCH_LINE + "\n")
    assert proc2.returncode == 1


def _write_foreign_hook(repo) -> str:
    dest = os.path.join(repo, ".git", "hooks", "pre-push")
    with open(dest, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\necho foreign\n")
    return dest


def test_hook_install_backs_up_foreign_hook(repo):
    dest = _write_foreign_hook(repo)
    backup = dest + ".pr-check-backup"

    assert main(["hook", "install", "--project", "team/order"]) == 0
    assert os.path.isfile(backup)
    assert "foreign" in open(backup, encoding="utf-8").read()
    assert _is_pr_check_hook(dest)


def test_hook_install_upgrade_own_hook_no_double_backup(repo):
    dest = os.path.join(repo, ".git", "hooks", "pre-push")
    assert main(["hook", "install", "--project", "team/order"]) == 0
    assert not os.path.isfile(dest + ".pr-check-backup")
    # 覆盖自家钩子（升级场景）不产生备份
    assert main(["hook", "install", "--project", "team/order"]) == 0
    assert not os.path.isfile(dest + ".pr-check-backup")


def test_hook_uninstall_restores_backup_and_removes_config(repo):
    dest = _write_foreign_hook(repo)
    cfg = os.path.join(repo, ".pr-check.hook")
    assert main(["hook", "install", "--project", "team/order"]) == 0

    assert main(["hook", "uninstall"]) == 0
    assert "foreign" in open(dest, encoding="utf-8").read()  # 原钩子恢复
    assert not os.path.exists(dest + ".pr-check-backup")
    assert not os.path.exists(cfg)  # 配置残留已清


def test_hook_uninstall_keeps_foreign_hook(repo):
    dest = _write_foreign_hook(repo)
    assert main(["hook", "uninstall"]) == 0
    assert os.path.isfile(dest)  # 非本工具的钩子不得误删


def test_hook_install_warns_when_kb_unconfigured(repo, monkeypatch, capsys):
    """未配 KB 时 risk/rule 闸门不会触发，安装输出必须明示。"""
    import app.config as config_module

    class _NoKb:
        kb_base_url = None
        kb_api_key = None

    monkeypatch.setattr(config_module, "get_settings", lambda: _NoKb())
    assert main(["hook", "install", "--project", "team/order"]) == 0
    assert "未检测到知识库配置" in capsys.readouterr().out
