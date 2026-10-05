"""Diff 结构解析 + 语言调度（C5）。

解析 unified git diff，产出每文件的路径 / 状态 / 增删行数 / 语言 / 模块 / 行级变更，
并调度对应语言策略抽取符号。未识别语言降级为文件级（不抽取符号）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

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


def _parse_file_block(block: str) -> ParsedFile | None:
    lines = block.split("\n")
    new_path = None
    old_path = None
    is_rename = False
    rename_to = None

    for line in lines:
        if line.startswith("--- "):
            old_path = line[4:].strip()
        elif line.startswith("+++ "):
            new_path = line[4:].strip()
        elif line.startswith("rename from "):
            is_rename = True
            old_path = line[len("rename from "):].strip()
        elif line.startswith("rename to "):
            is_rename = True
            rename_to = line[len("rename to "):].strip()

    # 路径取 b/ 新路径优先
    path = new_path or old_path or ""
    path = path.replace("b/", "", 1).replace("a/", "", 1)
    if path in ("/dev/null", ""):
        path = rename_to or old_path or ""
        path = path.replace("a/", "", 1).replace("b/", "", 1)

    if not path:
        return None

    new_null = new_path == "/dev/null"
    old_null = old_path == "/dev/null"

    if old_null and not new_null:
        status = "added"
    elif new_null and not old_null:
        status = "deleted"
    elif is_rename:
        status = "renamed"
    else:
        status = "modified"

    additions = 0
    deletions = 0
    lc_lines: list[LineChange] = []
    in_hunk = False
    for line in lines:
        if line.startswith("@@"):
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith("+"):
            if not line.startswith("+++"):
                additions += 1
                lc_lines.append(LineChange(text=line[1:], change="added"))
        elif line.startswith("-"):
            if not line.startswith("---"):
                deletions += 1
                lc_lines.append(LineChange(text=line[1:], change="removed"))
        elif line.startswith(" "):
            lc_lines.append(LineChange(text=line[1:], change="context"))

    language = detect_language(path)
    module = derive_module(path)
    return ParsedFile(
        path=path, status=status, additions=additions, deletions=deletions,
        language=language, module=module, lines=lc_lines, block_text=block,
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
