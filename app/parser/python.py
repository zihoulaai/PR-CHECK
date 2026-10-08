"""Python 启发式符号抽取（C5）。

识别 class / def / async def，以及 API 装饰器提示。
"""
from __future__ import annotations

import re

from app.parser.base import LanguageParser, LineChange, is_data_model_path

_CLASS_RE = re.compile(r"^\s*class\s+([A-Za-z_]\w*)")
_DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)")
_API_PATTERNS = [
    re.compile(p) for p in (
        r"@app\.(get|post|put|delete|patch)",
        r"@router\.(get|post|put|delete|patch)",
        r"@bp\.(get|post|put|delete|patch)",
        r"@dataclass",
    )
]


class PythonParser(LanguageParser):
    name = "python"
    symbol_patterns = [
        ("class", _CLASS_RE),
        ("function", _DEF_RE),
    ]
    api_patterns = _API_PATTERNS

    def is_data_model_file(self, path: str, lines: list[LineChange]) -> bool:
        if is_data_model_path(path):
            return True
        for lc in lines:
            low = lc.text.lower()
            if "class meta" in low or "@dataclass" in lc.text or "sa.column" in low \
                    or "sqlalchemy" in low or "models.model" in low:
                return True
        return False
