"""TypeScript / JavaScript 启发式符号抽取（C5）。

识别 class / interface / type / function / const-fn，以及路由/装饰器提示。
"""
from __future__ import annotations

import re

from app.parser.base import LanguageParser, LineChange, _MODEL_PATTERNS

_CLASS_RE = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_]\w*)")
_INTERFACE_RE = re.compile(r"^\s*(?:export\s+)?interface\s+([A-Za-z_]\w*)")
_TYPE_RE = re.compile(r"^\s*(?:export\s+)?type\s+([A-Za-z_]\w*)\s*=")
_FUNC_RE = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?function\s+([A-Za-z_]\w*)")
_CONST_FN_RE = re.compile(r"^\s*(?:export\s+)?const\s+([A-Za-z_]\w*)\s*=\s*\([^)]*\)\s*=>")
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
    ]
    api_patterns = _API_PATTERNS

    def is_data_model_file(self, path: str, lines: list[LineChange]) -> bool:
        if _MODEL_PATTERNS and any(p in path.lower() for p in _MODEL_PATTERNS):
            return True
        for lc in lines:
            low = lc.text.lower()
            if "interface" in low and ("dto" in low or "model" in low or "entity" in low):
                return True
        return False
