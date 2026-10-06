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

    effective_project = args.project

    if getattr(args, "repo", None):
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
    else:
        violations = []

    if args.format == "md":
        _emit((None, render_markdown(report)), args)
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
    p_check.add_argument("--project", help="项目路径（用于 KB 检索范围与报告标题；可选）。")
    p_check.add_argument("--title", help="PR 标题（diff 模式）。")
    p_check.add_argument("--description", help="PR 描述（diff 模式）。")
    p_check.add_argument("--source-branch", help="源分支（diff 模式）。")
    p_check.add_argument("--target-branch", help="目标分支（diff 模式）。")
    p_check.add_argument("--author", help="作者（diff 模式）。")
    p_check.add_argument("--repo", help="本地仓库路径（直连 .git，无需 Token）。")
    p_check.add_argument("--base", help="本地模式目标分支（默认 main），用于计算 diff。")
    p_check.add_argument("--source", help="本地模式源引用（默认 HEAD，即当前分支提交）。")
    p_check.add_argument("--fake", action="store_true", help="使用离线 Fake 适配器。")
    p_check.add_argument("--format", choices=["json", "md"], default="json")
    p_check.add_argument("--pretty", action="store_true", help="JSON 缩进美化。")
    p_check.add_argument(
        "--fail-on", action="append", metavar="SPEC",
        help="拦截闸门（可重复）：命中则返回 GATE_FAILED，git hook 据此中断推送。"
             "格式 section:value，如 risk:high / rule:violation / doc:confirm / doc:update "
             "/ debt:direct_match / debt:related。",
    )

    # version
    sub.add_parser("version", help="输出版本信息。")

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


def cmd_kb(args: argparse.Namespace) -> int:
    action = getattr(args, "kb_action", None)
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
        content = path.read_text(encoding="utf-8")
        doc_id = kb.upload(KbDocInput(
            project=args.project, module=args.module, doc_type=args.doc_type,
            title=args.title or path.name, content=content,
        ))
        # 元数据落 SQLite（与 KB 向量库分离存储，供 list / 检索过滤；与旧 Web /kb/docs 一致）
        from app.domain.models import KbDoc
        from app.storage.repo import insert_kb_doc
        insert_kb_doc(KbDoc(
            id=doc_id, project=args.project, module=args.module,
            doc_type=args.doc_type, title=args.title or path.name,
            status="active", snippet=content[:500],
        ))
        _emit({"id": doc_id, "project": args.project, "doc_type": args.doc_type,
               "title": args.title or path.name}, args)
        return EXIT_OK
    # list
    from app.storage.repo import list_kb_docs
    docs = list_kb_docs(project=args.project, module=args.module, doc_type=args.doc_type)
    _emit([{"id": d.id, "project": d.project, "module": d.module,
            "doc_type": d.doc_type, "title": d.title} for d in docs], args)
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
    shutil.copyfile(template, dest)
    os.chmod(dest, 0o755)
    mode = "源码脚本" if is_source_layout() else f"python -m {CLI_MODULE}"
    print(f"已安装 {hook_name} 钩子到 {dest}")
    print(f"配置写入 {cfg_path}（project={args.project}, base={args.base}, "
          f"fail-on={' '.join(fail_on)}，调用方式：{mode}）")
    return EXIT_OK


def _hook_uninstall(args: argparse.Namespace) -> int:
    root = _git_toplevel()
    dest = os.path.join(root, ".git", "hooks", args.hook_name)
    if os.path.isfile(dest):
        os.remove(dest)
        print(f"已移除钩子 {dest}")
    else:
        print(f"未找到钩子 {dest}（无需移除）")
    return EXIT_OK


def cmd_hook(args: argparse.Namespace) -> int:
    action = getattr(args, "hook_action", None)
    if action == "install":
        return _hook_install(args)
    if action == "uninstall":
        return _hook_uninstall(args)
    raise ValidationError(f"未知 hook 动作：{action}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        handler = {
            "check": cmd_check,
            "version": cmd_version,
            "kb": cmd_kb,
            "hook": cmd_hook,
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
