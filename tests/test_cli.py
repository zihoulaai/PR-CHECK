"""CLI 测试：函数级（capsys）+ 子进程级（错误信封 / 退出码 / 管道）。

覆盖：check（fake+diff 文件 JSON、md 7 段）、version、
错误码 INVALID_REQUEST（缺 diff）、stdin 管道。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

from app.adapters.fakes import FakeLLM
from bin.pr_check_cli import cmd_check, cmd_version, main

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(REPO_ROOT, "bin", "pr_check_cli.py")
FIXTURE = os.path.join(REPO_ROOT, "tests", "fixtures", "sample_refund.diff")


class _BrokenKB:
    """模拟适配器边界未预料的异常（响应体非 JSON）。"""

    def search(self, query):
        raise json.JSONDecodeError("bad body", "x", 0)

    def upload(self, doc):
        return "kb-x"


def _ns(**over):
    base = dict(
        diff=FIXTURE, input=None, project="", title="", description="",
        source_branch="", target_branch="", author="",
        fake=True, format="json", pretty=False,
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


def test_cli_llm_invalid_output_exits_5(container, capsys):
    """LLM 输出结构不合契约：退出码必须是 5（LLM_INVALID_OUTPUT），而非 99。"""
    container.llm = FakeLLM(report_override={
        "summary": "s", "risk": [{"level": "HIGH", "text": "t"}],
    })
    rc = main(["check", "--diff", FIXTURE, "--project", "team/order"])
    assert rc == 5
    body = json.loads(capsys.readouterr().out)
    assert body["error"]["code"] == "LLM_INVALID_OUTPUT"


def test_cli_kb_unavailable_exits_6(container, capsys):
    """KB 适配器抛出非 KbError：降级为基础自检并返回 0，不得变成 99。"""
    container.kb = _BrokenKB()
    rc = main(["check", "--diff", FIXTURE, "--project", "team/order"])
    assert rc == 0
    out = capsys.readouterr().out
    assert json.loads(out)["meta"]["kb_status"] == "failed"
