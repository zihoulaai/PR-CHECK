"""Kotlin 启发式符号抽取（C5 / R5）。

与 Java 同属 JVM 生态，注解、目录布局、数据模型约定高度相似，故继承 JavaParser
复用 class/interface/enum/field 与 API 注解提示，仅按 Kotlin 语法差异补充：
- 类/对象声明：``data class`` / ``enum class`` / ``object`` / ``companion object``；
- 函数：``fun``（含扩展函数 ``fun String.foo()`` 与修饰符 suspend/override/...）；
- 属性：``val`` / ``var``（Java 的字段正则不适用）；
- 数据模型：额外识别 ``data class``。
"""
from __future__ import annotations

import re

from app.parser.base import LineChange, _MODEL_PATTERNS
from app.parser.java import JavaParser

# 类 / 接口 / 对象声明：修饰符可叠加（data class / sealed class / companion object ...）
_KOTLIN_CLASS_RE = re.compile(
    r"^\s*(?:(?:public|private|protected|internal|abstract|final|open|sealed|data|value|"
    r"annotation|enum|inline|inner|companion)\s+)*"
    r"(?:class|interface|object)\s+([A-Za-z_]\w*)"
)
# 函数：可选修饰符 + fun [<泛型>] [接收者.]名称(
_KOTLIN_FUN_RE = re.compile(
    r"^\s*(?:(?:public|private|protected|internal|open|override|abstract|suspend|inline|"
    r"operator|infix|tailrec|external|final)\s+)*"
    r"fun\s+(?:<[^>]*>\s*)?(?:[\w<>,.\[\]?]+\.)?([A-Za-z_]\w*)\s*\("
)
# 属性：val / var
_KOTLIN_PROP_RE = re.compile(
    r"^\s*(?:(?:private|public|protected|internal|lateinit|const|override)\s+)*"
    r"(?:val|var)\s+([A-Za-z_]\w*)"
)


class KotlinParser(JavaParser):
    name = "kotlin"
    symbol_patterns = [
        ("class", _KOTLIN_CLASS_RE),
        ("method", _KOTLIN_FUN_RE),
        ("field", _KOTLIN_PROP_RE),
    ]

    def is_data_model_file(self, path: str, lines: list[LineChange]) -> bool:
        if any(p in path.lower() for p in _MODEL_PATTERNS):
            return True
        for lc in lines:
            if "@Entity" in lc.text or "@Table" in lc.text or "data class" in lc.text:
                return True
        return False