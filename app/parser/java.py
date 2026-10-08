"""Java 启发式符号抽取（C5）。

识别 class / interface / enum / record / method / field，以及 API 注解提示。
"""
from __future__ import annotations

import re

from app.parser.base import LanguageParser, LineChange, is_data_model_path

# 类型声明：捕获组 1 必须是**类型名**——LanguageParser.extract_symbols 统一取
# group(1) 作符号名（app/parser/base.py）。此前把关键字 (class|interface|enum|record)
# 放在组 1，导致所有 Java 类的符号名都是字面量 "class"，且被 (kind,name) 去重压成一条，
# Java 类名从未进入关键符号 / KB 查询 key_symbols。Kotlin/Scala/TS/Python/Go 的首个
# 捕获组都是名称，故只有 Java 症状明显且长期未被断言覆盖。
_CLASS_RE = re.compile(
    r"^\s*(?:public|private|protected|abstract|final|static|sealed)?\s*"
    r"(?:class|interface|enum|record)\s+([A-Za-z_]\w*)"
)
# 方法签名：返回类型 + 名称 + (...)
# 尾部允许同行方法体（{} / ;{ / }）：`public void refund() {}` 这类单行写法
# 此前匹配不到（`\{?\s*$` 无法匹配 "{}"），方法符号整条丢失。
_METHOD_RE = re.compile(
    r"^\s*(?:public|private|protected|static|final|abstract|synchronized|native|default)?\s*"
    r"(?:[\w<>\[\],\s.]+?)\s+([A-Za-z_]\w*)\s*\([^)]*\)\s*(?:throws[\w,\s.]*)?[{;]?\s*\}?\s*$"
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
        if is_data_model_path(path):
            return True
        # 含 @Entity / 记录型 class 也视为数据模型
        for lc in lines:
            if "@Entity" in lc.text or "@Table" in lc.text:
                return True
        return False
