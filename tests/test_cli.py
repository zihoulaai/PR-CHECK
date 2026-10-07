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
from app.cli import cmd_check, cmd_version, main

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 子进程用例走 `python -m app.cli`（装机与源码形态同一入口），REPO_ROOT 需进 PYTHONPATH
CLI_MODULE = "app.cli"
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
        fail_on=None, ci=False, output=None,
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


def test_cli_check_text(capsys):
    rc = cmd_check(_ns(format="text"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "1. 变更摘要" in out
    assert "7. 知识库来源" in out
    # 纯文本不含 Markdown 结构符号（无渲染器也能直读）
    assert "##" not in out and "|---" not in out


def test_cli_version(capsys):
    rc = cmd_version(_ns())
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["version"]


def _run_module(args, env, stdin=None, cwd=None):
    """以 `python -m app.cli` 起子进程（装机/源码同一入口），带上仓库根 PYTHONPATH。"""
    run_env = dict(env)
    run_env["PYTHONPATH"] = REPO_ROOT + os.pathsep + run_env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", CLI_MODULE, *args],
        capture_output=True, text=True, env=run_env, input=stdin, cwd=cwd,
    )


def test_cli_missing_diff_invalid_request():
    """缺 diff：应返回 INVALID_REQUEST 错误信封且退出码 2。"""
    env = dict(os.environ, PR_CHECK_USE_FAKE="1")
    proc = _run_module(["check"], env)
    assert proc.returncode == 2
    body = json.loads(proc.stdout)
    assert body["error"]["code"] == "INVALID_REQUEST"


def test_cli_stdin_pipe():
    """管道输入：--diff - 从 stdin 读取 diff。"""
    env = dict(os.environ, PR_CHECK_USE_FAKE="1")
    with open(FIXTURE, "r", encoding="utf-8") as fh:
        diff_text = fh.read()
    proc = _run_module(["check", "--diff", "-", "--fake"], env, stdin=diff_text)
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


def test_cli_input_flag_after_subcommand():
    """--input 置于子命令之后同样可用（修位置陷阱）。"""
    env = dict(os.environ, PR_CHECK_USE_FAKE="1")
    proc = _run_module(["check", "--input", FIXTURE, "--fake"], env)
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["meta"]["analysis_mode"] in ("full", "summary_only")


def test_cli_input_flag_before_subcommand():
    """顶层 --input（子命令之前）旧用法保持兼容。"""
    env = dict(os.environ, PR_CHECK_USE_FAKE="1")
    proc = _run_module(["--input", FIXTURE, "check", "--fake"], env)
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["meta"]["analysis_mode"] in ("full", "summary_only")


def test_cli_check_md_renders_risk_location(container, capsys):
    """risk.location 有值：Markdown 以行内代码渲染「文件:行号」。"""
    container.llm = FakeLLM(report_override={
        "summary": "s",
        "risk": [{"level": "high", "text": "注意事务边界", "location": "src/pay/Refund.java:42",
                  "evidence_level": "C", "source_refs": []}],
    })
    rc = main(["check", "--diff", FIXTURE, "--format", "md"])
    assert rc == 0
    assert "`src/pay/Refund.java:42`" in capsys.readouterr().out


def test_cli_check_md_omits_missing_location(container, capsys):
    """risk.location 缺省：静默省略，不占位不报错。"""
    container.llm = FakeLLM(report_override={
        "summary": "s",
        "risk": [{"level": "high", "text": "注意事务边界", "evidence_level": "C", "source_refs": []}],
    })
    rc = main(["check", "--diff", FIXTURE, "--format", "md"])
    assert rc == 0
    assert "注意事务边界" in capsys.readouterr().out


def test_version_matches_pyproject():
    """cli.VERSION 与 pyproject.toml 对齐，防止分发元数据漂移。"""
    import tomllib
    from pathlib import Path

    from app.cli import VERSION
    data = tomllib.loads(Path(REPO_ROOT, "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["version"] == VERSION


def test_cli_ci_mode_writes_report_files_and_payload(capsys, tmp_path):
    """--ci：报告落盘 md+json，stdout 为 MR 评论 payload；无 --fail-on 时有风险也返回 0。"""
    from app.cli import EXIT_OK

    out = tmp_path / "pr-report.md"
    rc = cmd_check(_ns(ci=True, output=str(out)))
    assert rc == EXIT_OK

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "pr-check-ci-payload/1"
    assert payload["gate"]["blocked"] is False and payload["exit_code"] == 0
    assert payload["report"]["markdown"] == str(out)
    assert payload["report"]["json"] == str(tmp_path / "pr-report.json")

    md = out.read_text(encoding="utf-8")
    assert "## 1. 变更摘要" in md
    assert payload["note_body"] == md
    json_data = json.loads((tmp_path / "pr-report.json").read_text(encoding="utf-8"))
    assert json_data["meta"]["analysis_mode"] in ("full", "summary_only")


def test_cli_ci_mode_gate_blocks_only_when_fail_on_given(capsys, tmp_path):
    """--ci + 显式 --fail-on 命中：返回 7 且 payload blocked=true（非阻断模式把决定权交给流水线）。"""
    from app.cli import EXIT_GATE

    out = tmp_path / "r.md"
    rc = cmd_check(_ns(ci=True, output=str(out), fail_on=["doc:confirm"]))
    assert rc == EXIT_GATE
    payload = json.loads(capsys.readouterr().out)
    assert payload["gate"]["fail_on"] == ["doc:confirm"]
    assert payload["gate"]["blocked"] is True
    assert payload["gate"]["violations"]
    assert payload["exit_code"] == 7


def test_cli_ci_mode_subprocess_default_output(tmp_path):
    """子进程级：--ci 默认写 pr-check-report.md/.json（cwd 相对路径），payload 可解析。"""
    env = dict(os.environ, PR_CHECK_USE_FAKE="1")
    proc = _run_module(
        ["check", "--diff", FIXTURE, "--fake", "--ci"],
        env, cwd=str(tmp_path),
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["schema"] == "pr-check-ci-payload/1"
    assert "PR_CHECK[ci]" in proc.stderr
    assert (tmp_path / "pr-check-report.md").is_file()
    assert (tmp_path / "pr-check-report.json").is_file()


# ===== PR_CHECK_DEBUG 调试日志（P2-11） =====
def test_debug_env_enables_debug_logging(monkeypatch, capsys):
    """PR_CHECK_DEBUG=1：pr_check logger 打开 DEBUG 并输出到 stderr；stdout 契约不变。"""
    import logging

    monkeypatch.setenv("PR_CHECK_DEBUG", "1")
    try:
        rc = main(["version"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "PR_CHECK_DEBUG 已开启" in captured.err
        # stdout 仍是纯版本输出，不被调试日志污染
        assert "version" in captured.out
    finally:
        # 清理 handler / 级别，避免污染后续用例的 stderr 断言
        logger = logging.getLogger("pr_check")
        logger.setLevel(logging.NOTSET)
        for h in list(logger.handlers):
            logger.removeHandler(h)


def test_debug_env_unset_emits_no_debug_logs(monkeypatch, capsys):
    """未设置 PR_CHECK_DEBUG：不加 handler，stderr 无调试行。"""
    monkeypatch.delenv("PR_CHECK_DEBUG", raising=False)
    rc = main(["version"])
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.err == ""


def test_debug_env_accepts_true_value(monkeypatch, capsys):
    """PR_CHECK_DEBUG=true 同样生效（大小写不敏感）。"""
    import logging

    monkeypatch.setenv("PR_CHECK_DEBUG", "True")
    try:
        rc = main(["version"])
        assert rc == 0
        assert "PR_CHECK_DEBUG 已开启" in capsys.readouterr().err
    finally:
        logger = logging.getLogger("pr_check")
        logger.setLevel(logging.NOTSET)
        for h in list(logger.handlers):
            logger.removeHandler(h)
