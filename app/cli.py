#!/usr/bin/env python3
"""PR_CHECK 命令行工具（包内入口，独立可运行，无需启动 HTTP 服务）。

三种等价调用方式：
- 装机形态：``pr-check <子命令>``（``uv tool install .`` / ``uvx`` 生成的 console script）
- 模块形态：``python -m app.cli <子命令>``（不依赖 PATH，git 钩子使用）
- 源码形态：``python bin/pr_check_cli.py <子命令>``（兼容旧用法，shim 转发到本模块）

面向 AI 工具 / 自动化流程设计：
- 子命令：check / kb / hook / version
- 结构化输出：--format json（默认，便于 AI 解析）或 md（Markdown）
- 统一错误信封：{"error": {"code", "message"}}，配合退出码便于脚本判断
- stdin / 管道：--diff - 从标准输入读取 diff 文本
- 跨平台：纯标准库

示例：
    # 对一段 diff 跑完整自检（离线 Mock 数据）
    cat pr.diff | pr-check check --diff - --fake

    # 直连本地仓库（无需 Token）：读取当前分支相对 main 的变更做自检
    pr-check check --repo . --base main --project team/order
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# 布局常量：装机形态下 __file__ 位于 <site-packages>/app/cli.py，上一级即包容器目录；
# 源码形态下上一级才是仓库根（能找到 bin/pr_check_cli.py）。据此区分调用形态（见 is_source_layout）。
_PKG_ROOT = Path(__file__).resolve().parent          # .../app
_LAYOUT_ROOT = _PKG_ROOT.parent                      # 源码时是仓库根，装机时是 site-packages
if str(_LAYOUT_ROOT) not in sys.path:
    sys.path.insert(0, str(_LAYOUT_ROOT))

# 装机形态（-m app.cli）下钩子用它唤起本 CLI，避免依赖 PATH 上是否存在 pr-check
CLI_MODULE = "app.cli"

from app.adapters.base import GitCredential, KbDocInput
from app.agent.workflow import run_check, run_check_from_diff
from app.container import get_container
from app.domain.enums import DocType, Platform
from app.domain.schemas import MRRef, ProjectRef
from app.errors import AppError, NotConfiguredError, ValidationError
from app.report.markdown import render_markdown
from app.report.plain import render_plain

VERSION = "1.0.0"

# ===== 退出码（与 app.errors 错误码语义对齐） =====
EXIT_OK = 0
EXIT_INVALID = 2
EXIT_NOT_CONFIGURED = 3
EXIT_GIT = 4
EXIT_LLM = 5
EXIT_KB = 6
EXIT_GATE = 7
EXIT_INTERNAL = 99

_CODE_TO_EXIT = {
    "INVALID_REQUEST": EXIT_INVALID,
    "NOT_CONFIGURED": EXIT_NOT_CONFIGURED,
    # 本地 Git 错误统一映射到退出码 4
    "GIT_UNAVAILABLE": EXIT_GIT,
    "GIT_AUTH_FAILED": EXIT_GIT,
    "GIT_FORBIDDEN": EXIT_GIT,
    "PROJECT_NOT_FOUND": EXIT_GIT,
    "MR_NOT_FOUND": EXIT_GIT,
    "LLM_UNAVAILABLE": EXIT_LLM,
    "LLM_TIMEOUT": EXIT_LLM,
    "LLM_RATE_LIMITED": EXIT_LLM,
    "LLM_INVALID_OUTPUT": EXIT_LLM,
    "KB_UNAVAILABLE": EXIT_KB,
}


def _read_stdin() -> str:
    """读取标准输入全文；处理 Windows 中文管道编码乱码。"""
    try:
        if hasattr(sys.stdin, "reconfigure"):
            sys.stdin.reconfigure(encoding="utf-8")
    except Exception:
        pass
    return sys.stdin.read()


def _read_diff(args: argparse.Namespace) -> str:
    """从 --diff <file|-> 或顶层 --input - 读取 diff；- 表示管道。"""
    source = getattr(args, "diff", None) or getattr(args, "input", None)
    if source == "-":
        return _read_stdin()
    if source:
        with open(source, "r", encoding="utf-8") as fh:
            return fh.read()
    raise ValidationError("需要提供 diff 来源：--diff <file> 或 --diff -（管道）。")


def _maybe_enable_fake(fake: bool) -> None:
    """--fake 离线联调：注入 Fake 适配器并重置容器缓存。"""
    if fake:
        os.environ["PR_CHECK_USE_FAKE"] = "1"
        from app.container import reset_container

        reset_container()


def _emit(payload, args: argparse.Namespace) -> None:
    """输出结果：json（默认）/ md（Markdown）/ text（纯文本）。"""
    fmt = getattr(args, "format", "json")
    if fmt in ("md", "text"):
        # payload 约定为 (report, 渲染文本) 或纯字符串
        if isinstance(payload, tuple):
            print(payload[1])
        else:
            print(payload)
        return
    text = payload[0] if isinstance(payload, tuple) else payload
    indent = 2 if getattr(args, "pretty", False) else None
    print(json.dumps(text, ensure_ascii=False, indent=indent))


def _emit_error(code: str, message: str, stream: str) -> int:
    body = {"error": {"code": code, "message": message}}
    text = json.dumps(body, ensure_ascii=False)
    if stream == "stderr":
        print(text, file=sys.stderr)
    else:
        print(text)
    return _CODE_TO_EXIT.get(code, EXIT_INTERNAL)


def _emit_ci_payload(args: argparse.Namespace, report, violations: list, fail_on: list) -> int:
    """CI 异步模式输出：报告落盘 + stdout 输出 MR 评论 payload。

    - Markdown 报告写 --output（默认 pr-check-report.md），全量 JSON 写同名 .json；
    - payload 含 schema / gate / exit_code / report 路径 / note_body（可直接作 MR 评论 body POST）；
    - 非阻断语义：未显式配 --fail-on 时 gate 不求值（调用方保证），有风险也返回 0；
      显式配了才命中返回 EXIT_GATE，把「是否失败」留给流水线配置。
    """
    md_text = render_markdown(report)
    out = Path(getattr(args, "output", None) or "pr-check-report.md")
    json_path = out.with_suffix(".json")
    out.write_text(md_text, encoding="utf-8")
    json_path.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    rc = EXIT_GATE if violations else EXIT_OK
    payload = {
        "schema": "pr-check-ci-payload/1",
        "project": report.meta.project,
        "pr_id": report.meta.pr_id,
        "analysis_mode": report.meta.analysis_mode,
        "kb_status": report.meta.kb_status,
        "gate": {"fail_on": list(fail_on), "blocked": bool(violations), "violations": list(violations)},
        "exit_code": rc,
        "report": {"markdown": str(out), "json": str(json_path)},
        "note_body": md_text,
    }
    indent = 2 if getattr(args, "pretty", False) else None
    print(json.dumps(payload, ensure_ascii=False, indent=indent))
    sys.stderr.write(
        f"PR_CHECK[ci]: 报告已写入 {out} 与 {json_path}；"
        f"闸门{'命中' if violations else '未命中'}（退出码 {rc}）\n"
    )
    return rc


def _run_remote_check(args: argparse.Namespace, platform: str, project: str):
    """远端平台（github/gitlab）只读自检：从平台 API 取 MR 元数据 + diff（R4）。

    参数优先级：CLI --git-base-url/--git-token > 配置 GIT_BASE_URL/GIT_TOKEN。
    --repo 为项目路径，--mr 为 MR/PR 编号（缺一即 INVALID_REQUEST，退出码 2）。
    """
    from app.config import get_settings

    repo = getattr(args, "repo", None)
    if not repo:
        raise ValidationError(
            f"platform={platform} 需要 --repo 指定项目路径（owner/repo 或 group/project）。")
    mr_number = getattr(args, "mr", None)
    if not mr_number:
        raise ValidationError(f"platform={platform} 需要 --mr 指定 MR/PR 编号。")

    try:
        s = get_settings()
    except NotConfiguredError:
        s = None
    cred = GitCredential(
        base_url=(getattr(args, "git_base_url", None)
                  or (s.git_base_url if s else "") or ""),
        token=(getattr(args, "git_token", None) or (s.git_token if s else "") or ""),
        platform=platform,
    )
    mr_ref = MRRef(project=ProjectRef(path=project or repo), iid=int(mr_number))
    return run_check(cred, mr_ref)


def cmd_check(args: argparse.Namespace) -> int:
    effective_project = args.project
    platform = (getattr(args, "platform", None) or Platform.LOCAL.value).strip().lower()

    # --fake 只注入 LLM/KB 的离线替代，远端平台仍会发真实 API 请求：
    # 显式拒绝该组合，避免"离线演示"意外产生真实网络调用（R4 审查问题1）。
    # 先于 _maybe_enable_fake：被拒绝时不留 PR_CHECK_USE_FAKE 进程内副作用。
    if getattr(args, "fake", False) and platform != Platform.LOCAL.value:
        raise ValidationError(
            f"--fake 不支持远端平台（platform={platform}）：离线演示请用 "
            f"--diff 或本地 --repo；远端只读拉取需真实 Token。")

    _maybe_enable_fake(args.fake)

    if platform != Platform.LOCAL.value:
        # 远端只读平台（R4）：--repo 为项目路径（owner/repo），--mr 为 MR/PR 编号
        report = _run_remote_check(args, platform, effective_project)
    elif getattr(args, "repo", None):
        # 本地仓库直连：读 .git 取 diff + 元数据，无需 Token
        cred = GitCredential(base_url=args.repo, token="", platform=Platform.LOCAL.value)
        project = ProjectRef(path=effective_project or args.repo)
        mr_ref = MRRef(
            project=project, iid=0,
            base_branch=getattr(args, "base", None) or "main",
            source_ref=getattr(args, "source", None) or "HEAD",
        )
        report = run_check(cred, mr_ref)
    else:
        diff_text = _read_diff(args)
        report = run_check_from_diff(
            diff_text,
            project=effective_project or "",
            title=args.title or "",
            description=args.description or "",
            source_branch=args.source_branch or "",
            target_branch=args.target_branch or "",
            author=args.author or "",
        )

    # 拦截闸门：命中 --fail-on 策略时返回 GATE_FAILED（仍输出完整报告便于定位）
    if getattr(args, "fail_on", None):
        from app.agent.gate import evaluate_gate, parse_gate_rules
        try:
            specs = parse_gate_rules(args.fail_on)
        except ValueError as exc:
            raise ValidationError(str(exc))
        violations = evaluate_gate(report, specs)
        # R2：记录闸门事件供 metrics 统计（默认开启，失败不影响自检）
        _record_gate_event(report, args.fail_on, violations)
    else:
        violations = []

    # CI 异步模式：报告落盘 + stdout 输出 MR 评论 payload，闸门语义见 _emit_ci_payload
    if getattr(args, "ci", False):
        return _emit_ci_payload(args, report, violations, args.fail_on or [])

    if args.format == "md":
        _emit((None, render_markdown(report)), args)
    elif args.format == "text":
        _emit((None, render_plain(report)), args)
    else:
        _emit(report.model_dump(mode="json"), args)

    if violations:
        sys.stderr.write("GATE FAILED: " + "; ".join(violations) + "\n")
        return EXIT_GATE
    return EXIT_OK


def _diff_given(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "diff", None)) or bool(getattr(args, "input", None))


def cmd_version(_args: argparse.Namespace) -> int:
    info = {
        "version": VERSION,
        "python": sys.version.split()[0],
        "components": ["diff-parser", "change-profile", "agent", "report"],
    }
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_config(args: argparse.Namespace) -> int:
    """config show：排查配置来源；config init：从内嵌模板生成 .env。

    init 默认落用户级配置目录（装机形态推荐位置，全形态稳定、不随 cwd 漂移）；
    想随仓库走可 ``--path .env``。已存在目标文件时拒绝覆盖，须显式 ``--force``，
    避免误冲掉已有凭据。
    """
    from app.config import (
        ENV_TEMPLATE,
        LEGACY_CWD_DB,
        USER_ENV_FILE,
        active_config_files,
        database_path,
        get_settings,
        is_kb_configured,
        is_llm_configured,
    )

    if getattr(args, "action", "show") == "init":
        target = Path(getattr(args, "path", None) or USER_ENV_FILE)
        overwritten = target.exists()
        if overwritten and not getattr(args, "force", False):
            raise ValidationError(
                f"{target} 已存在；确认覆盖请加 --force（防止误冲掉已填好的凭据）。")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(ENV_TEMPLATE, encoding="utf-8")
        _emit({"path": str(target), "created": True, "overwritten": overwritten}, args)
        return EXIT_OK

    s = get_settings()
    files = active_config_files()
    payload = {
        "config_files": files,                     # 按优先级升序，最后一个优先级最高
        "user_env_file": str(USER_ENV_FILE),
        "database_path": database_path(s),
        "app_env": s.app_env,
        "llm_configured": is_llm_configured(s),
        "kb_configured": is_kb_configured(s),
    }
    # 旧版默认把库建在 cwd：换了默认路径后老数据不会自动跟过来，这里提示一下。
    if LEGACY_CWD_DB.is_file() and Path(LEGACY_CWD_DB).resolve() != Path(database_path(s)).resolve():
        payload["legacy_db_hint"] = (
            f"检测到当前目录遗留 {LEGACY_CWD_DB}：默认数据已迁到用户状态目录，"
            f"如需沿用旧数据请执行 move/copy 后设置 DATABASE_URL。"
        )
    _emit(payload, args)
    return EXIT_OK


# ===== shell 补全（候选从 parser 树实时生成，新增子命令/选项零维护）=====
_BASH_COMPLETION = """\
# pr-check bash 补全（由 `pr-check completions bash` 生成，请勿手改）。
# 用法：source <(pr-check completions bash)，或把输出追加到 ~/.bashrc
_pr_check_completions() {
    local cmd cur candidates
    cmd="$1"
    cur="${COMP_WORDS[COMP_CWORD]}"
    # 把已输入的词连同当前部分词交给内部命令 __complete 过滤
    candidates="$("$cmd" __complete "${COMP_WORDS[@]:1:COMP_CWORD-1}" "$cur" 2>/dev/null)"
    COMPREPLY=( $(compgen -W "$candidates" -- "$cur") )
}
complete -F _pr_check_completions pr-check
"""

_ZSH_COMPLETION = """\
#compdef pr-check
# pr-check zsh 补全（由 `pr-check completions zsh` 生成，请勿手改）。
# 用法：source <(pr-check completions zsh)；或保存为 fpath 下文件 _pr-check
#（首行 #compdef 即自动注册）。
_pr_check() {
    local -a candidates
    # "${(@)words[2,$CURRENT]}" 必须带引号：非引用展开会丢掉为空的当前词，
    # 导致 __complete 拿上一个词当前缀（TAB 空前缀场景全错）。
    candidates=(${(f)"$(${words[1]} __complete "${(@)words[2,$CURRENT]}" 2>/dev/null)"})
    compadd -- $candidates
}
(( $+functions[compdef] )) && compdef _pr_check pr-check
"""

_FISH_COMPLETION = """\
# pr-check fish 补全（由 `pr-check completions fish` 生成，请勿手改）。
# 用法：pr-check completions fish | source
function __fish_pr_check_candidates
    set -l tokens (commandline -opc)
    set -l cmd $tokens[1]
    set -e tokens[1]
    # 命令替换可能吞掉 buffer 末尾的空当前词（TAB 空前缀场景），按行尾空格补回；
    # 已保留时重复补一个空串也无害（__complete 会把多余空串当历史词跳过）。
    if string match -q -- "* " (commandline)
        set tokens $tokens ""
    end
    $cmd __complete $tokens 2>/dev/null
end
complete -c pr-check -f -a '__fish_pr_check_candidates'
"""

_COMPLETION_SCRIPTS = {
    "bash": _BASH_COMPLETION,
    "zsh": _ZSH_COMPLETION,
    "fish": _FISH_COMPLETION,
}


def cmd_completions(args: argparse.Namespace) -> int:
    """打印 shell 补全脚本；候选由 parser 树实时生成，无静态脚本漂移。"""
    print(_COMPLETION_SCRIPTS[args.shell], end="")
    return EXIT_OK


def _subparser_choices(parser: argparse.ArgumentParser) -> dict:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    return {}


def _iter_completion_candidates(words: list[str]) -> list[str]:
    """按已输入词遍历 parser 树，产出补全候选（最后一个词为当前部分词，可为空）。

    - 逐词下钻：命中某层子命令名则进入对应子解析器；
    - 选项值补全：上一个词是带 choices 的选项时，候选即其可选值；
    - 否则候选 = 当前层子命令名 + 当前层选项开关（含 -h/--help）。
    """
    node = build_parser()
    for w in words[:-1]:
        choices = _subparser_choices(node)
        if choices and w in choices:
            node = choices[w]
    prefix = words[-1] if words else ""
    if len(words) >= 2:
        prev = words[-2]
        for action in node._actions:
            if prev in action.option_strings and action.choices:
                return [str(c) for c in action.choices if str(c).startswith(prefix)]
    candidates: list[str] = list(_subparser_choices(node))
    candidates += [opt for a in node._actions if a.option_strings for opt in a.option_strings]
    seen: set[str] = set()
    out: list[str] = []
    for c in candidates:
        if c.startswith(prefix) and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _complete_words(words: list[str]) -> int:
    for cand in _iter_completion_candidates(words):
        print(cand)
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pr-check",
        description="PR_CHECK 自检命令行工具（独立运行，便于 AI / 自动化调用）。",
    )
    parser.add_argument(
        "--error-stream", choices=["stdout", "stderr"], default="stdout",
        help="错误信封输出流；默认 stdout 便于 AI 解析，可选 stderr 避免污染正常输出。",
    )
    parser.add_argument(
        "--input", metavar="FILE",
        help="顶层输入来源（兼容 --diff）。- 表示从管道读取。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # check
    p_check = sub.add_parser("check", help="对 diff 或本地仓库执行完整 PR 自检。")
    p_check.add_argument("--diff", metavar="FILE", help="diff 文件；- 表示从管道读取。")
    p_check.add_argument("--input", metavar="FILE", default=argparse.SUPPRESS,
                         help="diff 文件（同 --diff；- 表示从管道读取）。可置于子命令之前或之后。")
    p_check.add_argument("--project", help="项目路径（用于 KB 检索范围与报告标题；可选）。")
    p_check.add_argument("--title", help="PR 标题（diff 模式）。")
    p_check.add_argument("--description", help="PR 描述（diff 模式）。")
    p_check.add_argument("--source-branch", help="源分支（diff 模式）。")
    p_check.add_argument("--target-branch", help="目标分支（diff 模式）。")
    p_check.add_argument("--author", help="作者（diff 模式）。")
    p_check.add_argument("--repo", help="本地仓库路径（platform=local 直连 .git）；"
                                        "platform=github/gitlab 时为项目路径（owner/repo 或 group/project）。")
    p_check.add_argument("--base", help="本地模式目标分支（默认 main），用于计算 diff。")
    p_check.add_argument("--source", help="本地模式源引用（默认 HEAD，即当前分支提交）。")
    p_check.add_argument("--platform", choices=[p.value for p in Platform], default=None,
                         help="Git 数据源：local（默认，直连 .git）/ github / gitlab（只读 API，需 GIT_TOKEN）。")
    p_check.add_argument("--mr", type=int, metavar="NUMBER",
                         help="远端平台 MR/PR 编号（platform=github/gitlab 时必填）。")
    p_check.add_argument("--git-base-url", dest="git_base_url", metavar="URL",
                         help="远端平台 API 基址（默认官方；企业版/自建填 https://<host>/api/v3 等）。")
    p_check.add_argument("--git-token", dest="git_token",
                         help="远端平台访问 Token（覆盖 GIT_TOKEN 配置）。")
    p_check.add_argument("--fake", action="store_true", help="使用离线 Fake 适配器。")
    p_check.add_argument("--format", choices=["json", "md", "text"], default="json",
                         help="输出格式：json（默认，结构化）/ md（Markdown）/ text（纯文本，终端直读无需渲染器）。")
    p_check.add_argument("--pretty", action="store_true", help="JSON 缩进美化。")
    p_check.add_argument(
        "--fail-on", action="append", metavar="SPEC",
        help="拦截闸门（可重复）：命中则返回 GATE_FAILED，git hook 据此中断推送。"
             "格式 section:value，如 risk:high / rule:violation / doc:confirm / doc:update "
             "/ debt:direct_match / debt:related。",
    )
    p_check.add_argument(
        "--ci", action="store_true",
        help="CI 异步模式：报告写入 --output（默认 pr-check-report.md）及同名 .json，"
             "stdout 改为输出 MR 评论 payload（schema/gate/note_body/report 路径）。"
             "非阻断语义：未显式传 --fail-on 时不因检出风险而失败，交由流水线决定。",
    )
    p_check.add_argument(
        "-o", "--output", metavar="FILE",
        help="报告输出文件（配合 --ci 使用；默认 pr-check-report.md，同时写同名 .json 全量报告）。",
    )

    # version
    sub.add_parser("version", help="输出版本信息。")

    # completions（shell 补全）
    p_completions = sub.add_parser(
        "completions", help="打印 shell 补全脚本（bash/zsh/fish），按提示 source 后生效。")
    p_completions.add_argument("shell", choices=["bash", "zsh", "fish"], help="目标 shell")

    # config（init 生成 .env / show 排查配置来源与数据落点）
    p_config = sub.add_parser("config", help="init：从内嵌模板生成 .env；show：显示生效配置文件、SQLite 路径与 LLM/KB 配置状态。")
    p_config.add_argument(
        "action", nargs="?", choices=["show", "init"], default="show",
        help="动作（默认 show；写成 pr-check config 亦可）。init 从内嵌模板生成 .env。",
    )
    p_config.add_argument(
        "--path", metavar="FILE",
        help="init 的目标路径（默认用户级 .env：~/.config/pr-check/.env 或 %%APPDATA%%\\pr-check\\.env）。",
    )
    p_config.add_argument(
        "--force", action="store_true",
        help="init 允许覆盖已存在的目标文件（默认拒绝，防止误冲掉已填好的凭据）。",
    )

    # feedback / metrics（R2：反馈采集与度量，本地记录）
    p_feedback = sub.add_parser(
        "feedback", help="对自检报告条目标记误报/有用（本地记录，供 metrics 统计）。")
    p_feedback.add_argument("--report-id", required=True, dest="report_id",
                            help="报告 meta.report_id（check 输出 / 报告 meta 中可见）。")
    p_feedback.add_argument("--item", required=True,
                            help="条目引用，格式 <section>:<index>，如 risk:0 / project_rules:1。")
    p_feedback.add_argument("--label", required=True, choices=["fp", "useful"],
                            help="fp=误报；useful=有用。")
    p_feedback.add_argument("--note", default="", help="备注（可选）。")
    p_feedback.add_argument("--project", default="",
                            help="项目（可选；用于 metrics --project 过滤）。")

    p_metrics = sub.add_parser(
        "metrics", help="统计反馈：误报率/有用率与闸门命中/驳回次数。")
    p_metrics.add_argument("--project", help="按项目过滤。")
    p_metrics.add_argument("--since", help="只统计该 ISO 时间（含）之后的记录。")

    # cache（R3：LLM 结果缓存管理）
    p_cache = sub.add_parser("cache", help="管理 LLM 结果缓存（R3，默认关闭）。")
    cache_sub = p_cache.add_subparsers(dest="cache_action", required=True)
    p_cache_clear = cache_sub.add_parser("clear", help="清空缓存（可按 --project 限定）。")
    p_cache_clear.add_argument("--project", help="只清该项目缓存。")

    # kb（知识库文档管理，替代原 Web /kb/docs）
    p_kb = sub.add_parser("kb", help="管理知识库文档。")
    kb_sub = p_kb.add_subparsers(dest="kb_action", required=True)
    p_kb_up = kb_sub.add_parser("upload", help="上传知识文档。")
    p_kb_up.add_argument("--file", required=True, help="文档文件")
    p_kb_up.add_argument("--project", required=True, help="项目（知识隔离强制过滤）")
    p_kb_up.add_argument("--doc-type", required=True, choices=[d.value for d in DocType])
    p_kb_up.add_argument("--module", default="")
    p_kb_up.add_argument("--title", default="")
    p_kb_list = kb_sub.add_parser("list", help="列出已上传文档。")
    p_kb_list.add_argument("--project")
    p_kb_list.add_argument("--module")
    p_kb_list.add_argument("--doc-type")
    p_kb_list.add_argument("--status", choices=["active", "stale"],
                           help="按状态过滤（默认不过滤；stale=被 --prune 标记的过期来源）。")
    p_kb_import = kb_sub.add_parser("import", help="批量导入目录下的文档（单篇失败不拖垮整批）。")
    p_kb_import.add_argument("--dir", required=True, help="待导入目录（递归遍历，跳过隐藏文件）")
    p_kb_import.add_argument("--project", required=True, help="项目（知识隔离强制过滤）")
    p_kb_import.add_argument("--doc-type", required=True, choices=[d.value for d in DocType],
                             help="本批统一的文档类型")
    p_kb_import.add_argument("--module", default="", help="模块名（可选）")
    p_kb_import.add_argument("--prune", action="store_true",
                             help="把本批未覆盖的既有 active 文档标记为 stale（过期），不再参与检索")
    p_kb_del = kb_sub.add_parser("delete", help="删除知识文档（向量库与本地元数据一并删除）。")
    p_kb_del.add_argument("--id", required=True, dest="doc_id", help="文档 id（kb list 输出）")
    p_kb_suggest = kb_sub.add_parser(
        "suggest", help="按当前变更画像建议需关注/需补录的知识文档（只读，不写入）。")
    p_kb_suggest.add_argument("--repo", default=".", help="本地仓库路径（默认当前目录）。")
    p_kb_suggest.add_argument("--base", default="main", help="目标分支（默认 main）。")
    p_kb_suggest.add_argument("--source", default="HEAD", help="源引用（默认 HEAD）。")
    p_kb_suggest.add_argument("--project", required=True,
                              help="项目（KB 文档按项目隔离，必填）。")
    p_kb_verify = kb_sub.add_parser(
        "verify", help="校验知识文档与代码是否漂移（只读，确定性符号匹配）。")
    p_kb_verify.add_argument("--repo", default=".", help="本地仓库路径（默认当前目录）。")
    p_kb_verify.add_argument("--project", help="只校验该项目（默认全部）。")

    # hook（git 钩子安装/卸载，配合 --fail-on 闸门实现推送拦截）
    p_hook = sub.add_parser("hook", help="管理 git 钩子（pre-push 拦截）。")
    hook_sub = p_hook.add_subparsers(dest="hook_action", required=True)
    p_hook_install = hook_sub.add_parser("install", help="安装 pre-push 钩子到 .git/hooks。")
    p_hook_install.add_argument("--project", required=True, help="知识库项目（KB 范围）。")
    p_hook_install.add_argument("--base", default="main", help="本地模式目标分支（默认 main）。")
    p_hook_install.add_argument("--fail-on", action="append", metavar="SPEC", default=None,
                                help="拦截闸门（可重复）；省略默认 risk:high rule:violation。"
                                     "如 risk:high / rule:violation / doc:confirm。")
    p_hook_install.add_argument("--hook-name", default="pre-push", help="钩子名（默认 pre-push）。")
    p_hook_un = hook_sub.add_parser("uninstall", help="移除已安装的 git 钩子。")
    p_hook_un.add_argument("--hook-name", default="pre-push", help="钩子名（默认 pre-push）。")

    return parser


def _kb_upload_one(kb, project: str, module: str, doc_type: str, title: str,
                   content: str) -> str:
    """上传单篇文档到向量库并落 metadata，返回 kb id（upload 与 import 共用）。"""
    doc_id = kb.upload(KbDocInput(
        project=project, module=module, doc_type=doc_type,
        title=title, content=content,
    ))
    # 元数据落 SQLite（与 KB 向量库分离存储，供 list / 检索过滤；与旧 Web /kb/docs 一致）
    from app.domain.models import KbDoc
    from app.storage.repo import insert_kb_doc
    insert_kb_doc(KbDoc(
        id=doc_id, project=project, module=module,
        doc_type=doc_type, title=title,
        status="active", snippet=content[:500],
    ))
    return doc_id


def cmd_kb(args: argparse.Namespace) -> int:
    action = getattr(args, "kb_action", None)
    # suggest / verify 只读本地元数据与仓库（不触向量库）→ 先于 kb is None 检查，
    # 使未配置 KB 凭据时也能做知识覆盖体检。
    if action == "suggest":
        return _kb_suggest(args)
    if action == "verify":
        return _kb_verify(args)
    kb = get_container().kb
    if kb is None:
        raise NotConfiguredError("知识库未配置（请设置 KB_BASE_URL / KB_API_KEY / KB_INDEX）。")
    if action == "upload":
        try:
            DocType(args.doc_type)
        except ValueError:
            raise ValidationError(f"不支持的 doc_type：{args.doc_type}")
        path = Path(args.file)
        if not path.exists():
            raise FileNotFoundError(args.file)
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            # 二进制 / 非 UTF-8 文件：友好报错（INVALID_REQUEST → rc 2），
            # 不再以 UnicodeDecodeError 裸抛落到 INTERNAL_ERROR（rc 99）
            raise ValidationError(
                f"文件不是 UTF-8 文本，无法上传：{args.file}（{exc}）。"
                "请先转换为 UTF-8 文本（如 txt / md）后再上传。"
            )
        doc_id = _kb_upload_one(
            kb, args.project, args.module, args.doc_type,
            args.title or path.name, content,
        )
        _emit({"id": doc_id, "project": args.project, "doc_type": args.doc_type,
               "title": args.title or path.name}, args)
        return EXIT_OK
    if action == "import":
        return _kb_import(kb, args)
    if action == "delete":
        # 先查本地元数据：id 不存在是用户输错（INVALID_REQUEST rc 2），
        # 而非让向量库先动刀再报错——本地记录是该文档归本工具管理的凭据。
        # 供应商删除失败抛 KbError → KB_UNAVAILABLE rc 6，本地元数据保留以便重试。
        from app.storage.repo import delete_kb_doc, list_kb_docs

        known = {d.id for d in list_kb_docs()}
        if args.doc_id not in known:
            raise ValidationError(
                f"本地不存在 id={args.doc_id} 的文档；请先 kb list 确认"
                f"（删除只接受本工具上传时记录的 id，防误删线上文档）。")
        kb.delete(args.doc_id)
        removed = delete_kb_doc(args.doc_id)
        _emit({"id": args.doc_id, "deleted": True,
               "local_meta_removed": removed}, args)
        return EXIT_OK
    # list
    from app.storage.repo import list_kb_docs
    docs = list_kb_docs(project=args.project, module=args.module,
                        doc_type=args.doc_type, status=getattr(args, "status", None))
    _emit([{"id": d.id, "project": d.project, "module": d.module,
            "doc_type": d.doc_type, "title": d.title, "status": d.status}
           for d in docs], args)
    return EXIT_OK


def _kb_import(kb, args: argparse.Namespace) -> int:
    """kb import：批量导入目录。单篇失败（读取/上传）记入 failed 继续，不拖垮整批。

    退出码：全部成功 0；有失败项 6（KB_UNAVAILABLE 语义的批量版）——stdout 仍是
    完整摘要 {imported, failed, marked_stale, total}，便于 CI 决策与排查。
    """
    root = Path(args.dir)
    if not root.is_dir():
        raise ValidationError(f"目录不存在：{args.dir}")
    files = sorted(p for p in root.rglob("*")
                   if p.is_file() and not p.name.startswith("."))
    imported: list[dict] = []
    failed: list[dict] = []
    for path in files:
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            failed.append({"file": str(path), "error": f"读取失败：{exc}"})
            continue
        try:
            doc_id = _kb_upload_one(kb, args.project, args.module,
                                    args.doc_type, path.stem, content)
        except Exception as exc:  # noqa: BLE001 - 批量导入单篇失败不拖垮整批
            failed.append({"file": str(path), "error": str(exc)})
            continue
        imported.append({"id": doc_id, "file": str(path), "title": path.stem})

    marked_stale: list[str] = []
    if args.prune:
        from app.storage.repo import mark_kb_docs_stale
        marked_stale = mark_kb_docs_stale(
            args.project, args.module or None, {i["id"] for i in imported},
        )

    _emit({"imported": imported, "failed": failed,
           "marked_stale": marked_stale, "total": len(files)}, args)
    sys.stderr.write(
        f"PR_CHECK[kb-import]: 共 {len(files)} 个文件：成功 {len(imported)}，"
        f"失败 {len(failed)}，标记过期 {len(marked_stale)}\n"
    )
    return EXIT_OK if not failed else EXIT_KB


# ===== KB 生命周期闭环（R1，只读） =====
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")
_BACKTICK_RE = re.compile(r"`([^`]+)`")
# 扫描仓库时跳过的目录（构建产物 / 依赖 / VCS 元数据）
_SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "dist", "build", "__pycache__",
    ".mypy_cache", ".pytest_cache", "target", "out", ".idea", ".vscode",
}
_MAX_FILE_BYTES = 512 * 1024
_MAX_TOTAL_BYTES = 20 * 1024 * 1024
_TOKEN_STOPWORDS = {
    "the", "and", "for", "with", "this", "that", "from", "must", "should",
    "when", "have", "will", "not", "are", "was", "were", "does", "http",
    "https", "true", "false", "null", "none",
}


def _read_repo_diff(repo: str, base: str, source: str) -> str:
    """复用本地 Git 适配器读 diff（不发起网络请求）。"""
    from app.adapters.base import GitCredential
    from app.adapters.local_git import LocalGitAdapter
    from app.domain.schemas import MRRef, ProjectRef

    cred = GitCredential(base_url=repo, token="", platform=Platform.LOCAL.value)
    ref = MRRef(project=ProjectRef(path=repo), iid=0,
                base_branch=base, source_ref=source)
    return LocalGitAdapter().get_diff(cred, ref)


def _repo_identifiers(repo: str) -> set[str]:
    """单次遍历仓库收集标识符集合（确定性、可复现；跳过依赖与产物目录）。"""
    found: set[str] = set()
    total = 0
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in files:
            path = os.path.join(root, name)
            try:
                if os.path.getsize(path) > _MAX_FILE_BYTES:
                    continue
                data = Path(path).read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            total += len(data)
            found.update(_IDENT_RE.findall(data))
            if total > _MAX_TOTAL_BYTES:
                return found
    return found


def _doc_tokens(text: str) -> list[str]:
    """从文档标题+摘要抽取候选符号：反引号内的整体 + ASCII 标识符（去停用词）。"""
    candidates: list[str] = []
    for span in _BACKTICK_RE.findall(text):
        candidates.extend(_IDENT_RE.findall(span))
    candidates.extend(_IDENT_RE.findall(text))
    out: list[str] = []
    for tok in candidates:
        if tok.lower() in _TOKEN_STOPWORDS:
            continue
        if tok.islower() and len(tok) < 5:
            continue
        if tok not in out:
            out.append(tok)
    return out[:30]


def _kb_suggest(args: argparse.Namespace) -> int:
    """按当前变更画像反查需关注 / 需补录的知识文档（只建议，不写入）。"""
    from app.domain.schemas import PRMetadata
    from app.parser.change_profile import build_change_profile
    from app.parser.diff_parser import parse_diff
    from app.storage.repo import list_kb_docs

    repo = args.repo
    if not Path(repo).is_dir():
        raise ValidationError(f"仓库路径不存在：{repo}")
    diff = _read_repo_diff(repo, args.base, args.source)
    profile = build_change_profile(PRMetadata(), parse_diff(diff))

    docs = list_kb_docs(project=args.project, status="active")
    changed_modules = {m for m in profile.modules if m}
    symbols = {s.name for s in profile.symbols if s.name}
    keywords = {k for k in profile.keywords if k}

    affected: list[dict] = []
    covered_modules: set[str] = set()
    for d in docs:
        haystack = f"{d.title} {d.snippet}"
        reasons: list[str] = []
        if d.module and d.module in changed_modules:
            reasons.append("module")
        if any(s in haystack for s in symbols):
            reasons.append("symbol")
        if any(k.lower() in haystack.lower() for k in keywords):
            reasons.append("keyword")
        if reasons:
            covered_modules.add(d.module)
            affected.append({"id": d.id, "title": d.title, "module": d.module,
                             "doc_type": d.doc_type, "matched_by": reasons})

    gaps = sorted(m for m in changed_modules if m not in covered_modules)
    _emit({
        "project": args.project, "repo": repo,
        "changed_modules": sorted(changed_modules),
        "affected_docs": affected, "coverage_gaps": gaps,
        "note": "只建议不写入：请人工确认后 kb upload 补录或更新对应文档。",
    }, args)
    return EXIT_OK


def _kb_verify(args: argparse.Namespace) -> int:
    """校验知识文档与代码是否漂移（只读，确定性符号匹配，非语义比对）。"""
    repo = args.repo
    if not Path(repo).is_dir():
        raise ValidationError(f"仓库路径不存在：{repo}")
    from app.storage.repo import list_kb_docs

    docs = [d for d in list_kb_docs(project=getattr(args, "project", None), status="active")
            if d.doc_type in ("api_document", "development_rule")]
    if not docs:
        _emit({"repo": repo, "checked": 0, "stale_suspects": [], "drift_suspects": [],
               "note": "无可校验的 api_document / development_rule 文档。"}, args)
        return EXIT_OK

    present = _repo_identifiers(repo)
    stale: list[dict] = []
    drift: list[dict] = []
    for d in docs:
        tokens = _doc_tokens(f"{d.title} {d.snippet}")
        if not tokens:
            continue
        missing = [t for t in tokens if t not in present]
        matched = len(tokens) - len(missing)
        rec = {"id": d.id, "title": d.title, "module": d.module, "doc_type": d.doc_type,
               "matched": matched, "total": len(tokens), "missing": missing[:10]}
        if matched == 0:
            stale.append(rec)
        elif missing:
            drift.append(rec)

    _emit({
        "repo": repo, "checked": len(docs), "scanned_identifiers": len(present),
        "stale_suspects": stale, "drift_suspects": drift,
        "note": "确定性符号匹配（非语义比对）：仅提示人工复核，不自动改动知识库。",
    }, args)
    return EXIT_OK


# ===== hook 子命令（git 钩子安装/卸载） =====

def _git_toplevel() -> str:
    import subprocess
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise ValidationError(f"当前目录不是 git 仓库，无法安装钩子：{exc}")
    return out.stdout.strip()


def is_source_layout() -> bool:
    """判断当前是否以「源码仓」布局运行（app 包旁存在 bin/pr_check_cli.py）。

    装机 / 可编辑安装形态下 hooks 与 CLI 都在包内；源码形态保留旧的脚本路径写法，
    二者写出的 .pr-check.hook 模板都会在 CLI 缺失时回退到 `-m app.cli`。
    """
    return (_LAYOUT_ROOT / "bin" / "pr_check_cli.py").is_file()


def _hook_template(hook_name: str) -> Path | None:
    """定位钩子模板：优先包内资源（随 wheel 分发），回退到源码仓 hooks/ 目录。"""
    try:
        from importlib.resources import files

        candidate = Path(str(files("app").joinpath("hooks", hook_name)))
        if candidate.is_file():
            return candidate
    except Exception:  # noqa: BLE001 - 资源不可用时回退文件系统查找
        pass
    for fallback in (_PKG_ROOT / "hooks" / hook_name, _LAYOUT_ROOT / "hooks" / hook_name):
        if fallback.is_file():
            return fallback
    return None


def _posix(p: str | Path) -> str:
    """路径转正斜杠：避免在 sh 的双引号字符串里被反斜杠转义影响。"""
    return str(p).replace(os.sep, "/")


def _is_pr_check_hook(path: str) -> bool:
    """判断 hook 文件是否由 pr-check 安装（模板内的 PRCHECK_MANAGED_HOOK 标记）。

    只读前 4KB 二进制匹配：抗 CRLF 与编码差异；文件不可读时视为非本工具钩子。
    """
    try:
        with open(path, "rb") as fh:
            return b"PRCHECK_MANAGED_HOOK" in fh.read(4096)
    except OSError:
        return False


def _hook_install(args: argparse.Namespace) -> int:
    import shutil

    root = _git_toplevel()
    hook_name = args.hook_name
    fail_on = args.fail_on or ["risk:high", "rule:violation"]

    template = _hook_template(hook_name)
    if template is None:
        raise ValidationError(f"未找到钩子模板：app/hooks/{hook_name}")

    # 写入仓库根目录配置（shell 可 source）。
    # 钩子在目标仓库目录下运行，无法推断 CLI 位置，source 形态写绝对路径；
    # 装机形态写 PR_CHECK_MODULE，由钩子用 `python -m app.cli` 唤起。
    cfg_path = os.path.join(root, ".pr-check.hook")
    python_path = _posix(sys.executable)
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write(f'PR_CHECK_PYTHON="{python_path}"\n')
        if is_source_layout():
            f.write(f'PR_CHECK_CLI="{_posix(_LAYOUT_ROOT / "bin" / "pr_check_cli.py")}"\n')
        f.write(f'PR_CHECK_MODULE="{CLI_MODULE}"\n')
        f.write(f'PR_CHECK_PROJECT="{args.project}"\n')
        f.write(f'PR_CHECK_BASE="{args.base}"\n')
        f.write('PR_CHECK_FAIL_ON="' + " ".join(fail_on) + '"\n')

    dest = os.path.join(root, ".git", "hooks", hook_name)
    backup = dest + ".pr-check-backup"
    backed_up = False
    if os.path.isfile(dest) and not _is_pr_check_hook(dest):
        if not os.path.isfile(backup):
            os.replace(dest, backup)
            backed_up = True
    shutil.copyfile(template, dest)
    os.chmod(dest, 0o755)
    mode = "源码脚本" if is_source_layout() else f"python -m {CLI_MODULE}"
    print(f"已安装 {hook_name} 钩子到 {dest}")
    if backed_up:
        print(f"原 {hook_name} 钩子已备份到 {backup}（卸载时自动恢复，或手动合并）")
    print(f"配置写入 {cfg_path}（project={args.project}, base={args.base}, "
          f"fail-on={' '.join(fail_on)}，调用方式：{mode}）")

    # 闸门默认 risk:high / rule:violation，二者都要求 A/B 级知识库证据；
    # 未配 KB 时永不触发，安装时必须明示，避免「以为受保护」的信任陷阱。
    from app.config import get_settings, is_kb_configured
    if not is_kb_configured(get_settings()):
        print("提示：未检测到知识库配置（KB_BASE_URL / KB_API_KEY）。")
        print("      未配置知识库时，--fail-on risk:* 与 rule:violation 依赖的 A/B 级证据不可得，闸门不会触发；")
        print("      配置并 kb upload 规范后请重跑 pr-check hook install。")
    return EXIT_OK


def _hook_uninstall(args: argparse.Namespace) -> int:
    root = _git_toplevel()
    dest = os.path.join(root, ".git", "hooks", args.hook_name)
    backup = dest + ".pr-check-backup"
    if os.path.isfile(dest):
        if not _is_pr_check_hook(dest):
            # 别人的钩子（husky / lefthook / 手写）：误删会静默破坏用户既有拦截
            print(f"警告：{dest} 不是 pr-check 安装的钩子，未删除。")
        else:
            os.remove(dest)
            if os.path.isfile(backup):
                os.replace(backup, dest)
                os.chmod(dest, 0o755)
                print(f"已移除钩子 {dest}，并恢复备份的原始 {args.hook_name}")
            else:
                print(f"已移除钩子 {dest}")
    else:
        print(f"未找到钩子 {dest}（无需移除）")
    # 清掉本工具的钩子配置残留
    cfg_path = os.path.join(root, ".pr-check.hook")
    if os.path.isfile(cfg_path):
        os.remove(cfg_path)
        print(f"已移除钩子配置 {cfg_path}")
    return EXIT_OK


def cmd_hook(args: argparse.Namespace) -> int:
    action = getattr(args, "hook_action", None)
    if action == "install":
        return _hook_install(args)
    if action == "uninstall":
        return _hook_uninstall(args)
    raise ValidationError(f"未知 hook 动作：{action}")


# ===== 反馈与度量（R2） =====
_TRUTHY_VALUES = {"1", "true", "yes", "on"}
_FALSY_VALUES = {"0", "false", "no", "off"}

# 可反馈的报告段（与 CheckReport 段落对应）
_FEEDBACK_SECTIONS = {"doc_check", "risk", "project_rules", "tech_debt", "manual_checklist"}


def _feedback_enabled() -> bool:
    """PR_CHECK_FEEDBACK=0/off 时关闭采集（默认开启，数据仅落本地 SQLite）。"""
    return os.getenv("PR_CHECK_FEEDBACK", "").strip().lower() not in _FALSY_VALUES


def _parse_item_ref(ref: str) -> tuple[str, int]:
    """解析 --item <section>:<index>；非法即 INVALID_REQUEST（rc 2）。"""
    section, sep, idx = ref.rpartition(":")
    section = section.strip()
    if not sep or section not in _FEEDBACK_SECTIONS:
        raise ValidationError(
            f"非法的 --item：{ref!r}（应为 <section>:<index>，"
            f"section 可选：{', '.join(sorted(_FEEDBACK_SECTIONS))}）。")
    try:
        index = int(idx)
    except ValueError:
        raise ValidationError(f"非法的条目序号：{idx!r}（应为非负整数）。")
    if index < 0:
        raise ValidationError(f"非法的条目序号：{index}（应为非负整数）。")
    return section, index


def _hash_key(*parts: str, n: int = 16) -> str:
    import hashlib

    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:n]


def _record_gate_event(report, fail_on: list[str], violations: list[str]) -> None:
    """带 --fail-on 的 check 记一条闸门事件（R2）。

    采集关闭或库异常时静默跳过——统计失败绝不影响自检主流程。
    """
    if not _feedback_enabled():
        return
    try:
        from app.domain.models import GateEvent
        from app.storage.repo import record_gate_event

        specs = " ".join(fail_on)
        rid = report.meta.report_id or ""
        record_gate_event(GateEvent(
            id=_hash_key(rid, specs, n=20), report_id=rid,
            project=report.meta.project or "", specs=specs,
            blocked=bool(violations),
        ))
    except Exception as exc:  # noqa: BLE001 - 统计失败不影响自检
        import logging

        logging.getLogger("pr_check").warning(
            "gate_event_record_failed type=%s", type(exc).__name__)


def cmd_feedback(args: argparse.Namespace) -> int:
    """对报告条目标记 fp/useful；id 由 (report_id, section, item_key) 派生 → 幂等覆盖。"""
    if not _feedback_enabled():
        raise ValidationError(
            "反馈采集已被 PR_CHECK_FEEDBACK 关闭（设为 1/true/on 可恢复）。")
    section, index = _parse_item_ref(args.item)
    item_key = _hash_key(section, str(index))
    from app.domain.models import ReportFeedback
    from app.storage.repo import upsert_feedback

    upsert_feedback(ReportFeedback(
        id=_hash_key(args.report_id, section, item_key, n=20),
        report_id=args.report_id, project=args.project or "",
        section=section, item_key=item_key, label=args.label,
        note=args.note or "",
    ))
    _emit({"report_id": args.report_id, "section": section, "item": args.item,
           "label": args.label, "recorded": True}, args)
    return EXIT_OK


def cmd_metrics(args: argparse.Namespace) -> int:
    """统计反馈（误报率 / 有用率 / 按段分布）与闸门命中/驳回。

    口径（显式声明，避免误读）：
    - 反馈是「条目级」计数；rate 的分母是反馈条目总数。
    - 闸门 hit 是「被该规则拦下的不同报告数」（同报告重复 check 不虚增）；
    - 驳回 = 被拦下且该报告收到过 fp 反馈（报告级近似，非按规则精确归因）。
    """
    from app.storage.repo import list_feedback, list_gate_events

    fb = list_feedback(project=args.project, since=args.since)
    ge = list_gate_events(project=args.project, since=args.since)

    total = len(fb)
    fp = sum(1 for r in fb if r.label == "fp")
    useful = sum(1 for r in fb if r.label == "useful")
    by_section: dict[str, dict[str, int]] = {}
    for r in fb:
        bucket = by_section.setdefault(r.section, {"fp": 0, "useful": 0})
        bucket["fp" if r.label == "fp" else "useful"] += 1

    fp_report_ids = {r.report_id for r in fb if r.label == "fp"}
    by_rule: dict[str, dict[str, int]] = {}
    blocked = 0
    for e in ge:
        if e.blocked:
            blocked += 1
        for spec in e.specs.split():
            b = by_rule.setdefault(spec, {"hit": 0, "rejected": 0})
            if e.blocked:
                b["hit"] += 1
                if e.report_id in fp_report_ids:
                    b["rejected"] += 1

    payload = {
        "project": args.project or None,
        "since": args.since or None,
        "data_available": bool(fb or ge),
        "feedback": {
            "total": total, "false_positive": fp, "useful": useful,
            "false_positive_rate": round(fp / total, 4) if total else None,
            "useful_rate": round(useful / total, 4) if total else None,
            "by_section": by_section,
        },
        "gate": {
            "evaluated_reports": len(ge), "blocked_reports": blocked,
            "by_rule": by_rule,
        },
    }
    if not (fb or ge):
        payload["note"] = (
            "暂无反馈数据。使用 `pr-check feedback --report-id <id> "
            "--item <section:index> --label fp|useful` 记录。")
    _emit(payload, args)
    return EXIT_OK


def cmd_cache(args: argparse.Namespace) -> int:
    """cache clear：清空 LLM 结果缓存（R3）。"""
    if getattr(args, "cache_action", None) != "clear":
        raise ValidationError(f"未知 cache 动作：{getattr(args, 'cache_action', None)}")
    from app.storage.repo import clear_cache

    removed = clear_cache(args.project)
    _emit({"cleared": removed, "project": args.project or None}, args)
    return EXIT_OK


_DEBUG_VALUES = {"1", "true", "yes", "on"}


def _setup_debug_logging() -> None:
    """PR_CHECK_DEBUG=1（true/yes/on 亦可）：把 pr_check logger 打开到 DEBUG 并输出到 stderr。

    用于排查降级原因（KB 失败跳过、stale 过滤跳过）、适配器异常类型与调用细节；
    不影响 stdout 的结构化输出（错误信封与报告仍按契约走 stdout/--error-stream）。
    """
    if os.getenv("PR_CHECK_DEBUG", "").strip().lower() not in _DEBUG_VALUES:
        return
    import logging

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    logger = logging.getLogger("pr_check")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    logger.debug("PR_CHECK_DEBUG 已开启：调试日志输出到 stderr")


def main(argv: list[str] | None = None) -> int:
    _setup_debug_logging()
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "__complete":
        # shell 补全内部命令：先于 argparse 直通。两个原因：REMAINDER 吞不掉以
        # - 开头的补全词（bpo-13922）；且不注册进 parser 树，--help 天然不可见。
        return _complete_words(list(argv[1:]))
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        handler = {
            "check": cmd_check,
            "version": cmd_version,
            "kb": cmd_kb,
            "hook": cmd_hook,
            "config": cmd_config,
            "completions": cmd_completions,
            "feedback": cmd_feedback,
            "metrics": cmd_metrics,
            "cache": cmd_cache,
        }[args.command]
        return handler(args)
    except AppError as exc:
        return _emit_error(exc.code, str(exc.args[0] if exc.args else exc.friendly_message),
                           args.error_stream)
    except ValidationError as exc:
        return _emit_error("INVALID_REQUEST", str(exc.args[0] if exc.args else "请求参数校验失败。"),
                           args.error_stream)
    except FileNotFoundError as exc:
        return _emit_error("INVALID_REQUEST", f"文件不存在：{exc.filename}", args.error_stream)
    except Exception as exc:  # noqa: BLE001 - 顶层兜底，避免泄露堆栈
        import logging

        logging.getLogger("pr_check").error("cli_unexpected type=%s", type(exc).__name__)
        return _emit_error("INTERNAL_ERROR", "命令执行发生内部错误，请稍后重试。", args.error_stream)


if __name__ == "__main__":  # python -m app.cli / python app/cli.py
    sys.exit(main())
