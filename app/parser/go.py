"""Go 启发式符号抽取（C5）。

识别 package / type（struct/interface）/ func（含接收者方法），以及 Web 框架提示。
"""
from __future__ import annotations

import re

from app.parser.base import LanguageParser, LineChange, is_data_model_path

_PACKAGE_RE = re.compile(r"^\s*package\s+([A-Za-z_]\w*)")
_TYPE_RE = re.compile(r"^\s*type\s+([A-Za-z_]\w*)\s+(struct|interface)")
# 普通函数
_FUNC_RE = re.compile(r"^\s*func\s+([A-Za-z_]\w*)\s*\(")
# 接收者方法：func (r *T) Name(
_METHOD_RE = re.compile(r"^\s*func\s+\([^)]*\)\s+([A-Za-z_]\w*)\s*\(")
_API_PATTERNS = [
    re.compile(p) for p in (
        r"router\.(GET|POST|PUT|DELETE|PATCH|Handle)",
        r"gin\.", r"echo\.", r"fiber\.", r"http\.Handle",
        r"@Controller",  # 某些 Go 框架注解风格
    )
]


class GoParser(LanguageParser):
    name = "go"
    symbol_patterns = [
        ("type", _TYPE_RE),
        ("function", _FUNC_RE),
        ("method", _METHOD_RE),
        ("package", _PACKAGE_RE),
    ]
    api_patterns = _API_PATTERNS

    def is_data_model_file(self, path: str, lines: list[LineChange]) -> bool:
        if is_data_model_path(path):
            return True
        for lc in lines:
            if "type" in lc.text and "struct" in lc.text:
                return True
        return False
