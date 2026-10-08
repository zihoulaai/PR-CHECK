"""TypeScript / JavaScript 启发式符号抽取（C5）。

识别 class / interface / type / function / const-fn，以及路由/装饰器提示。
"""
from __future__ import annotations

import re

from app.parser.base import LanguageParser, LineChange, is_data_model_path

_CLASS_RE = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_]\w*)")
_INTERFACE_RE = re.compile(r"^\s*(?:export\s+)?interface\s+([A-Za-z_]\w*)")
_TYPE_RE = re.compile(r"^\s*(?:export\s+)?type\s+([A-Za-z_]\w*)\s*=")
_FUNC_RE = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?function\s+([A-Za-z_]\w*)")
_CONST_FN_RE = re.compile(r"^\s*(?:export\s+)?const\s+([A-Za-z_]\w*)\s*=\s*\([^)]*\)\s*=>")
# 类方法：要求 2+ 空格缩进（排除顶层调用）且带返回类型标注（排除裸函数调用）。
# 此前 TS 只抽 class / interface / type / function / const 箭头函数，
# 类方法（如 `refund(): void {}`）完全不进符号表。
_METHOD_RE = re.compile(
    r"^\s{2,}(?:public\s+|private\s+|protected\s+|readonly\s+|static\s+|async\s+|\*\s*)*"
    r"([A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*\([^)]*\)\s*:\s*[\w<>[\],\s.|]+\s*[{;]"
)
_API_PATTERNS = [
    re.compile(p) for p in (
        r"@Controller", r"@Get", r"@Post", r"@Put", r"@Delete", r"@Patch",
        r"router\.(get|post|put|delete|patch)", r"app\.(get|post|put|delete|patch)",
        r"@RequestMapping",
    )
]


class TypeScriptParser(LanguageParser):
    name = "typescript"
    symbol_patterns = [
        ("class", _CLASS_RE),
        ("interface", _INTERFACE_RE),
        ("type", _TYPE_RE),
        ("function", _FUNC_RE),
        ("function", _CONST_FN_RE),
        ("method", _METHOD_RE),
    ]
    api_patterns = _API_PATTERNS

    def is_data_model_file(self, path: str, lines: list[LineChange]) -> bool:
        if is_data_model_path(path):
            return True
        for lc in lines:
            low = lc.text.lower()
            if "interface" in low and ("dto" in low or "model" in low or "entity" in low):
                return True
        return False
