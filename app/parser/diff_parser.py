"""Diff 结构解析 + 语言调度（C5）。

解析 unified git diff，产出每文件的路径 / 状态 / 增删行数 / 语言 / 模块 / 行级变更，
并调度对应语言策略抽取符号。未识别语言降级为文件级（不抽取符号）。

健壮性要点（对齐真实 git 输出）：
- 文件头只在首个 `@@` 之前扫描：hunk 内形如 `--- x` / `+++ y` 的内容行是「被删除/
  被新增的代码」，不是文件头，误判会污染 path 并把行数统计归零。
- 路径前缀用正则剥离 `a/` / `b/`，不用 str.replace：后者替换的是首个出现位置，
  `java/`、`data/`、`beta/` 等目录会被吃掉（src/main/java → src/main/jav）。
- 无 hunk 的块（二进制 `GIT binary patch`、纯权限变更 `old mode`/`new mode`、
  纯重命名）同样产出 ParsedFile，避免 changed_files 少算导致规模误判。
- 增删行数只统计 hunk 内的 `+` / `-`，不排除 `+++` / `---`：hunk 内的
  `--- foo` 是一条新增行（内容为 `-- foo`）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.domain.enums import ChangeType, HighImpactFeature
from app.parser.base import (
    LanguageParser,
    LineChange,
    derive_module,
    detect_language,
)
from app.parser.go import GoParser
from app.parser.java import JavaParser
from app.parser.python import PythonParser
from app.parser.typescript import TypeScriptParser

_REGISTRY: dict[str, LanguageParser] = {
    "java": JavaParser(),
    "python": PythonParser(),
    "typescript": TypeScriptParser(),
    "go": GoParser(),
}

_DEV_NULL = "/dev/null"
# git diff --git a/<old> b/<new>（old 非贪婪，路径含空格时仍能正确切分）
_DIFF_GIT_RE = re.compile(r"^a/(?P<old>.+?) b/(?P<new>.+)$")
# 路径前缀只从开头剥离一次
_PREFIX_RE = re.compile(r"^[ab]/")


def get_language_parser(language: str | None) -> LanguageParser | None:
    return _REGISTRY.get(language) if language else None


@dataclass
class ParsedFile:
    path: str
    status: str  # added / modified / deleted / renamed
    additions: int = 0
    deletions: int = 0
    language: str | None = None
    module: str = ""
    lines: list[LineChange] = field(default_factory=list)
    block_text: str = ""  # 该文件对应的原始 diff 片段
    # 以下两项由 build_change_profile 回填，供 select_focused_diff 复用，
    # 避免为选重点文件重复跑一遍 detect_change_types。
    change_types: list[ChangeType] = field(default_factory=list)
    high_impact: list[HighImpactFeature] = field(default_factory=list)


def _strip_prefix(path: str) -> str:
    return _PREFIX_RE.sub("", path.strip(), count=1)


def _split_header(lines: list[str]) -> tuple[list[str], list[str]]:
    """在首个 `@@` 处切分：返回 (文件头, hunk 行)。无 hunk 时 hunk 为空。"""
    for i, line in enumerate(lines):
        if line.startswith("@@"):
            return lines[:i], lines[i:]
    return lines, []


def _parse_header(header: list[str]) -> dict:
    """解析文件头。`--- ` / `+++ ` 优先，其次 rename，其次 diff --git 行。"""
    old_raw = new_raw = None
    rename_from = rename_to = None
    new_mode = delete_mode = False
    fallback: str | None = None

    for raw in header:
        line = raw.rstrip("\r")
        if line.startswith("--- "):
            old_raw = line[4:].strip()
        elif line.startswith("+++ "):
            new_raw = line[4:].strip()
        elif line.startswith("rename from "):
            rename_from = line[len("rename from "):].strip()
        elif line.startswith("rename to "):
            rename_to = line[len("rename to "):].strip()
        elif line.startswith("new file mode "):
            new_mode = True
        elif line.startswith("deleted file mode "):
            delete_mode = True
        elif line.startswith("diff --git ") and fallback is None:
            # 无 ---/+++ 的块（二进制 / 纯权限变更）只能从这里取路径
            m = _DIFF_GIT_RE.match(line[len("diff --git "):])
            if m:
                fallback = m.group("new")

    return {
        "old": old_raw, "new": new_raw,
        "rename_from": rename_from, "rename_to": rename_to,
        "new_mode": new_mode, "delete_mode": delete_mode,
        "fallback": fallback,
    }


def _resolve_path(info: dict) -> str | None:
    """确定文件路径：+++ > rename to > --- > diff --git 兜底。"""
    for candidate in (info["new"], info["rename_to"], info["old"],
                      info["rename_from"], info["fallback"]):
        if not candidate or candidate == _DEV_NULL:
            continue
        path = _strip_prefix(candidate)
        if path:
            return path
    return None


def _resolve_status(info: dict) -> str:
    if info["old"] == _DEV_NULL or info["new_mode"]:
        return "added"
    if info["new"] == _DEV_NULL or info["delete_mode"]:
        return "deleted"
    if info["rename_from"] or info["rename_to"]:
        return "renamed"
    return "modified"


def _count_hunk(hunk: list[str]) -> tuple[int, int, list[LineChange]]:
    """统计 hunk 内增删行与行级变更。

    此处不排除 `+++` / `---`：进入 hunk 后所有 `+` / `-` 开头行都是变更行，
    排除它们会把「删除一行以 `--` 开头的注释」这类改动统计为 0。
    """
    additions = deletions = 0
    lc_lines: list[LineChange] = []
    for raw in hunk:
        if raw.startswith("+"):
            additions += 1
            lc_lines.append(LineChange(text=raw[1:], change="added"))
        elif raw.startswith("-"):
            deletions += 1
            lc_lines.append(LineChange(text=raw[1:], change="removed"))
        elif raw.startswith(" "):
            lc_lines.append(LineChange(text=raw[1:], change="context"))
    return additions, deletions, lc_lines


def _parse_file_block(block: str) -> ParsedFile | None:
    lines = block.split("\n")
    header, hunk = _split_header(lines)
    info = _parse_header(header)

    path = _resolve_path(info)
    if not path:
        return None

    additions, deletions, lc_lines = _count_hunk(hunk)
    return ParsedFile(
        path=path,
        status=_resolve_status(info),
        additions=additions,
        deletions=deletions,
        language=detect_language(path),
        module=derive_module(path),
        lines=lc_lines,
        block_text=block,
    )


def parse_diff(diff_text: str) -> list[ParsedFile]:
    if not diff_text or not diff_text.strip():
        return []
    blocks = []
    current: list[str] = []
    for line in diff_text.split("\n"):
        if line.startswith("diff --git") and current:
            blocks.append("\n".join(current))
            current = []
        current.append(line)
    if current:
        blocks.append("\n".join(current))

    parsed: list[ParsedFile] = []
    for blk in blocks:
        pf = _parse_file_block(blk)
        if pf is not None:
            parsed.append(pf)
    return parsed
