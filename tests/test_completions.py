"""shell 补全：completions 脚本 + 内部 __complete 候选生成（P3-3）。

核心约束：候选必须从 parser 树实时推导——写死命令名会在新增子命令后静默漂移。
"""
from __future__ import annotations

import pytest

from app.cli import main


def test_bash_script_registers_completion(capsys):
    assert main(["completions", "bash"]) == 0
    script = capsys.readouterr().out
    assert "complete -F _pr_check_completions pr-check" in script
    assert "__complete" in script  # 内部回调契约


def test_zsh_script_registers_completion(capsys):
    assert main(["completions", "zsh"]) == 0
    script = capsys.readouterr().out
    assert "#compdef pr-check" in script
    assert "compdef _pr_check pr-check" in script
    # "${(@)...}" 引号保留空当前词：非引用展开丢空词会让前缀错位
    assert '"${(@)words[2,$CURRENT]}"' in script


def test_fish_script_registers_completion(capsys):
    assert main(["completions", "fish"]) == 0
    script = capsys.readouterr().out
    assert "complete -c pr-check" in script
    assert "__complete" in script
    # 空当前词防护：命令替换吞掉行尾空词会导致前缀错位（同 zsh 的坑）
    assert "string match" in script


def test_completions_rejects_unknown_shell():
    with pytest.raises(SystemExit) as exc:
        main(["completions", "tcsh"])
    assert exc.value.code == 2  # argparse choices 校验


def test_candidates_top_level(capsys):
    assert main(["__complete", ""]) == 0
    cands = set(capsys.readouterr().out.split())
    assert {"check", "kb", "hook", "config", "version", "completions"} <= cands
    assert "--error-stream" in cands and "--input" in cands


def test_candidates_drill_into_subcommand(capsys):
    """钻到 kb 层：候选取 kb 的子动作，不含顶层命令。"""
    assert main(["__complete", "kb", ""]) == 0
    cands = set(capsys.readouterr().out.split())
    assert {"upload", "list", "import", "delete"} <= cands
    assert "check" not in cands and "version" not in cands
    assert "--project" not in cands  # --project 属 kb upload 层，不在 kb 层


def test_candidates_drill_two_levels(capsys):
    """kb 的动作层再钻一层无子命令：候选只剩当前层选项。"""
    assert main(["__complete", "kb", "upload", ""]) == 0
    cands = set(capsys.readouterr().out.split())
    assert {"--file", "--project", "--doc-type", "--module", "--title"} <= cands
    assert "upload" not in cands


def test_candidates_prefix_filter(capsys):
    assert main(["__complete", "ch"]) == 0
    assert capsys.readouterr().out.split() == ["check"]

    assert main(["__complete", "--e"]) == 0
    assert capsys.readouterr().out.split() == ["--error-stream"]


def test_candidates_choice_values(capsys):
    """选项值补全：上一个词是带 choices 的选项 → 候选即其可选值。"""
    assert main(["__complete", "check", "--format", ""]) == 0
    assert set(capsys.readouterr().out.split()) == {"json", "md"}

    assert main(["__complete", "check", "--format", "j"]) == 0
    assert capsys.readouterr().out.split() == ["json"]

    assert main(["__complete", "kb", "upload", "--doc-type", ""]) == 0
    values = set(capsys.readouterr().out.split())
    assert "api_document" in values and "development_rule" in values


def test_candidates_skip_option_values(capsys):
    """选项值恰好与子命令同名也不误下钻（--project check 之后仍在 check 层）。"""
    assert main(["__complete", "check", "--project", "check", ""]) == 0
    cands = set(capsys.readouterr().out.split())
    assert "--diff" in cands and "--format" in cands
    assert "upload" not in cands  # 未误入 kb 层
