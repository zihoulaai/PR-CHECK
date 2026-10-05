#!/usr/bin/env python3
"""PR_CHECK 命令行工具（独立可运行，无需启动 HTTP 服务）。

面向 AI 工具 / 自动化流程设计：
- 子命令：check / profile / parse / version / config
- 结构化输出：--format json（默认，便于 AI 解析）或 md（Markdown）
- 统一错误信封：{"error": {"code", "message"}}，配合退出码便于脚本判断
- stdin / 管道：--diff - 从标准输入读取 diff 文本
- 跨平台：纯标准库；Windows 用 `python bin/pr_check_cli.py ...`，*nix 可用 bin/pr-check

示例：
    # 对一段 diff 跑完整自检（离线 Mock 数据）
    cat pr.diff | python bin/pr_check_cli.py check --diff - --fake

    # 仅看变更画像（确定性、无 LLM/KB，最快）
    python bin/pr_check_cli.py profile --diff pr.diff --format json

    # 走真实 GitLab MR
    python bin/pr_check_cli.py check --project team/order --mr-iid 1234 \
        --gitlab-url https://gitlab.com --gitlab-token $GITLAB_TOKEN
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# 将仓库根加入 sys.path，确保任意工作目录下都能 import app
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.agent.workflow import build_profile_only, run_check, run_check_from_diff
from app.config import get_settings, is_kb_configured, is_llm_configured
from app.adapters.base import GitCredential
from app.domain.schemas import MRRef, ProjectRef
from app.errors import AppError, ValidationError
from app.parser.diff_parser import parse_diff
from app.report.markdown import render_markdown

VERSION = "1.0.0"

# ===== 退出码（与 app.errors 错误码语义对齐） =====
EXIT_OK = 0
EXIT_INVALID = 2
EXIT_NOT_CONFIGURED = 3
EXIT_GITLAB = 4
EXIT_LLM = 5
EXIT_KB = 6
EXIT_INTERNAL = 99

_CODE_TO_EXIT = {
    "INVALID_REQUEST": EXIT_INVALID,
    "NOT_CONFIGURED": EXIT_NOT_CONFIGURED,
    "GITLAB_UNAVAILABLE": EXIT_GITLAB,
    "GITLAB_AUTH_FAILED": EXIT_GITLAB,
    "GITLAB_FORBIDDEN": EXIT_GITLAB,
    "PROJECT_NOT_FOUND": EXIT_GITLAB,
    "MR_NOT_FOUND": EXIT_GITLAB,
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
    """输出结果：json（默认）或 md（Markdown 字符串）。"""
    fmt = getattr(args, "format", "json")
    if fmt == "md":
        # payload 约定为 (report, markdown) 或纯字符串
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


def cmd_check(args: argparse.Namespace) -> int:
    _maybe_enable_fake(args.fake)

    use_gitlab = args.mr_iid is not None or (args.project and not _diff_given(args))
    if use_gitlab:
        if not args.fake and not (args.gitlab_url and args.gitlab_token):
            raise ValidationError(
                "GitLab 路径需提供 --gitlab-url 与 --gitlab-token（或使用 --fake 离线联调）。"
            )
        cred = GitCredential(base_url=args.gitlab_url or "", token=args.gitlab_token or "")
        from app.container import get_container

        container = get_container()
        if container.git is None:
            raise AppError("GitLab 适配器未配置。", code="NOT_CONFIGURED")
        project = ProjectRef(path=args.project or None)
        mr_ref = MRRef(project=project, iid=args.mr_iid)
        report = run_check(cred, mr_ref)
    else:
        diff_text = _read_diff(args)
        report = run_check_from_diff(
            diff_text,
            project=args.project or "",
            title=args.title or "",
            description=args.description or "",
            source_branch=args.source_branch or "",
            target_branch=args.target_branch or "",
            author=args.author or "",
        )

    if args.format == "md":
        _emit((None, render_markdown(report)), args)
    else:
        _emit(report.model_dump(mode="json"), args)
    return EXIT_OK


def _diff_given(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "diff", None)) or bool(getattr(args, "input", None))


def cmd_profile(args: argparse.Namespace) -> int:
    diff_text = _read_diff(args)
    profile = build_profile_only(diff_text, project=args.project or "", title=args.title or "")
    _emit(profile.model_dump(mode="json"), args)
    return EXIT_OK


def cmd_parse(args: argparse.Namespace) -> int:
    diff_text = _read_diff(args)
    parsed = parse_diff(diff_text)
    files = [
        {
            "path": p.path,
            "status": p.status,
            "additions": p.additions,
            "deletions": p.deletions,
            "language": p.language,
            "module": p.module,
        }
        for p in parsed
    ]
    _emit({"files": files, "file_count": len(files)}, args)
    return EXIT_OK


def cmd_version(_args: argparse.Namespace) -> int:
    info = {
        "version": VERSION,
        "python": sys.version.split()[0],
        "components": ["diff-parser", "change-profile", "agent", "report"],
    }
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_config(args: argparse.Namespace) -> int:
    s = get_settings()
    data = s.model_dump()
    # 脱敏：密钥类字段不输出明文
    for secret_key in ("app_encryption_key", "llm_api_key", "kb_api_key"):
        if data.get(secret_key):
            data[secret_key] = "<set>"

    if args.check:
        result = {
            "llm_configured": is_llm_configured(s),
            "kb_configured": is_kb_configured(s),
            "fake_mode": os.getenv("PR_CHECK_USE_FAKE") == "1",
            "thresholds": {
                "small_max_files": s.small_max_files,
                "small_max_lines": s.small_max_lines,
                "medium_max_files": s.medium_max_files,
                "medium_max_lines": s.medium_max_lines,
            },
            "kb_top_k": s.kb_top_k,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))
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
    p_check = sub.add_parser("check", help="对 diff 或 GitLab MR 执行完整 PR 自检。")
    p_check.add_argument("--diff", metavar="FILE", help="diff 文件；- 表示从管道读取。")
    p_check.add_argument("--project", help="项目路径（diff 模式用于 KB/标题；GitLab 模式必填）。")
    p_check.add_argument("--mr-iid", type=int, help="MR 编号（走 GitLab 路径）。")
    p_check.add_argument("--title", help="PR 标题（diff 模式）。")
    p_check.add_argument("--description", help="PR 描述（diff 模式）。")
    p_check.add_argument("--source-branch", help="源分支（diff 模式）。")
    p_check.add_argument("--target-branch", help="目标分支（diff 模式）。")
    p_check.add_argument("--author", help="作者（diff 模式）。")
    p_check.add_argument("--gitlab-url", help="GitLab 基址（真实路径）。")
    p_check.add_argument("--gitlab-token", help="GitLab Token（真实路径）。")
    p_check.add_argument("--fake", action="store_true", help="使用离线 Fake 适配器。")
    p_check.add_argument("--format", choices=["json", "md"], default="json")
    p_check.add_argument("--pretty", action="store_true", help="JSON 缩进美化。")

    # profile
    p_profile = sub.add_parser("profile", help="仅解析 diff 输出变更画像（无 LLM/KB）。")
    p_profile.add_argument("--diff", metavar="FILE", required=True, help="diff 文件；- 表示从管道读取。")
    p_profile.add_argument("--project", help="项目路径。")
    p_profile.add_argument("--title", help="PR 标题。")
    p_profile.add_argument("--format", choices=["json"], default="json")
    p_profile.add_argument("--pretty", action="store_true")

    # parse
    p_parse = sub.add_parser("parse", help="输出底层 diff 解析结果（文件级）。")
    p_parse.add_argument("--diff", metavar="FILE", required=True, help="diff 文件；- 表示从管道读取。")
    p_parse.add_argument("--format", choices=["json"], default="json")
    p_parse.add_argument("--pretty", action="store_true")

    # version
    sub.add_parser("version", help="输出版本信息。")

    # config
    p_config = sub.add_parser("config", help="打印生效配置（脱敏）。")
    p_config.add_argument("--check", action="store_true", help="仅输出配置就绪校验。")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        handler = {
            "check": cmd_check,
            "profile": cmd_profile,
            "parse": cmd_parse,
            "version": cmd_version,
            "config": cmd_config,
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


if __name__ == "__main__":
    sys.exit(main())
