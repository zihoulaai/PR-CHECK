"""CLI 测试：函数级（capsys）+ 子进程级（错误信封 / 退出码 / 管道）。

覆盖：check（fake+diff 文件 JSON、md 7 段）、profile、parse、version、
错误码 INVALID_REQUEST（缺 diff）、stdin 管道。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

import pytest

from bin.pr_check_cli import cmd_check, cmd_parse, cmd_profile, cmd_version

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(REPO_ROOT, "bin", "pr_check_cli.py")
FIXTURE = os.path.join(REPO_ROOT, "tests", "fixtures", "sample_refund.diff")


def _ns(**over):
    base = dict(
        diff=FIXTURE, input=None, project="", title="", description="",
        source_branch="", target_branch="", author="", mr_iid=None,
        gitlab_url="", gitlab_token="", fake=True, format="json", pretty=False,
    )
    base.update(over)
    return argparse.Namespace(**base)


def test_cli_check_fake_json(capsys):
    rc = cmd_check(_ns())
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["meta"]["analysis_mode"] in ("full", "summary_only")
    assert "summary" in out and "manual_checklist" in out


def test_cli_check_md(capsys):
    rc = cmd_check(_ns(format="md"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "## 1. 变更摘要" in out
    assert "## 7. 知识库来源" in out


def test_cli_profile_json(capsys):
    rc = cmd_profile(_ns())
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["changed_files"] == 2
    assert "change_types" in out and "files" in out


def test_cli_parse_json(capsys):
    rc = cmd_parse(_ns())
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["file_count"] == 2
    assert out["files"][0]["language"] == "java"


def test_cli_version(capsys):
    rc = cmd_version(_ns())
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["version"]


def test_cli_missing_diff_invalid_request():
    """缺 diff：应返回 INVALID_REQUEST 错误信封且退出码 2。"""
    env = dict(os.environ, PR_CHECK_USE_FAKE="1")
    proc = subprocess.run(
        [sys.executable, CLI, "check"],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 2
    body = json.loads(proc.stdout)
    assert body["error"]["code"] == "INVALID_REQUEST"


def test_cli_stdin_pipe():
    """管道输入：--diff - 从 stdin 读取 diff。"""
    env = dict(os.environ, PR_CHECK_USE_FAKE="1")
    with open(FIXTURE, "r", encoding="utf-8") as fh:
        diff_text = fh.read()
    proc = subprocess.run(
        [sys.executable, CLI, "check", "--diff", "-", "--fake"],
        input=diff_text, capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0
    body = json.loads(proc.stdout)
    assert body["meta"]["analysis_mode"] in ("full", "summary_only")
