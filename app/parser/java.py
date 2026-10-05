"""Java 启发式符号抽取（C5）。

识别 class / interface / enum / record / method / field，以及 API 注解提示。
"""
from __future__ import annotations

import re

from app.parser.base import LanguageParser, LineChange, _MODEL_PATTERNS

_CLASS_RE = re.compile(
    r"^\s*(?:public|private|protected|abstract|final|static|sealed)?\s*"
    r"(class|interface|enum|record)\s+([A-Za-z_]\w*)"
)
# 方法签名：返回类型 + 名称 + (...)
_METHOD_RE = re.compile(
    r"^\s*(?:public|private|protected|static|final|abstract|synchronized|native|default)?\s*"
    r"(?:[\w<>\[\],\s.]+?)\s+([A-Za-z_]\w*)\s*\([^)]*\)\s*(?:throws[\w,\s.]*)?\{?\s*$"
)
# 字段声明（保守：以 ; 结尾、含类型、非控制流、非方法）；单一捕获组确保 group(1) 始终有值
_FIELD_RE = re.compile(
    r"^\s*(?:private|public|protected|static|final|transient|volatile)?\s*"
    r"(?:[\w<>\[\],\s.]+?)\s+([A-Za-z_]\w*)\s*(?:=[^;]*)?;"
)
_API_PATTERNS = [
    re.compile(p) for p in (
        r"@RestController", r"@GetMapping", r"@PostMapping", r"@PutMapping",
        r"@DeleteMapping", r"@PatchMapping", r"@RequestMapping", r"@Controller",
        r"@Service", r"@Entity", r"@Transactional",
    )
]


class JavaParser(LanguageParser):
    name = "java"
    symbol_patterns = [
        ("class", _CLASS_RE),
        ("method", _METHOD_RE),
        ("field", _FIELD_RE),
    ]
    api_patterns = _API_PATTERNS

    def is_data_model_file(self, path: str, lines: list[LineChange]) -> bool:
        if _MODEL_PATTERNS and any(p in path.lower() for p in _MODEL_PATTERNS):
            return True
        # 含 @Entity / 记录型 class 也视为数据模型
        for lc in lines:
            if "@Entity" in lc.text or "@Table" in lc.text:
                return True
        return False
