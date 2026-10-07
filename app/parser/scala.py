"""Scala 启发式符号抽取（C5 / R5）。

与 Java 同属 JVM 生态，复用 JavaParser 的数据模型路径启发式，按 Scala 语法差异补充：
- 类 / 特质 / 对象：``class`` / ``trait`` / ``object``（含 ``case class`` / ``sealed trait``）；
- 方法：``def``（修饰符含 implicit / lazy / override）；
- 属性：``val`` / ``var``。
API 注解提示沿用 Java（Scala 服务端同样使用 Spring / JAX-RS 风格注解）。
"""
from __future__ import annotations

import re

from app.parser.base import LineChange, _MODEL_PATTERNS
from app.parser.java import JavaParser

_SCALA_CLASS_RE = re.compile(
    r"^\s*(?:(?:private|protected|final|sealed|abstract|implicit|lazy|case|override)\s+)*"
    r"(?:class|trait|object)\s+([A-Za-z_]\w*)"
)
_SCALA_DEF_RE = re.compile(
    r"^\s*(?:(?:private|protected|final|override|implicit|lazy|abstract)\s+)*"
    r"def\s+([A-Za-z_]\w*)"
)
_SCALA_VAL_RE = re.compile(
    r"^\s*(?:(?:private|protected|final|override|implicit|lazy)\s+)*"
    r"(?:val|var)\s+([A-Za-z_]\w*)"
)


class ScalaParser(JavaParser):
    name = "scala"
    symbol_patterns = [
        ("class", _SCALA_CLASS_RE),
        ("method", _SCALA_DEF_RE),
        ("field", _SCALA_VAL_RE),
    ]

    def is_data_model_file(self, path: str, lines: list[LineChange]) -> bool:
        if any(p in path.lower() for p in _MODEL_PATTERNS):
            return True
        for lc in lines:
            if "@Entity" in lc.text or "@Table" in lc.text or "case class" in lc.text:
                return True
        return False