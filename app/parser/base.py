"""Diff Parser 基础：语言识别、模块推导、符号提取抽象。

C5 总体策略：Diff 结构解析 + 语言可插拔启发式规则，不使用 LLM 做符号抽取。
未识别语言降级为文件级 + 关键词（禁止假装识别 class/method）。
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import NamedTuple

from app.domain.enums import ChangeType, HighImpactFeature
from app.domain.schemas import Symbol

# 语言扩展名 -> 语言名
_EXT_MAP = {
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".scala": "scala",
    ".sc": "scala",
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "typescript",
    ".jsx": "typescript",
    ".mjs": "typescript",
    ".cjs": "typescript",
    ".go": "go",
}

# 配置类扩展名（CONFIG_CHANGE）
_CONFIG_EXT = {".yml", ".yaml", ".properties", ".toml", ".ini", ".env"}
# 依赖清单文件（DEPENDENCY_CHANGE）
_DEP_FILES = {
    "pom.xml", "build.gradle", "package.json", "package-lock.json",
    "yarn.lock", "pnpm-lock.yaml", "go.mod", "go.sum",
    "requirements.txt", "pyproject.toml",
}
# 测试路径/文件名特征（TEST_CHANGE）
_TEST_PATTERNS = ("test", "tests", "spec", "__test__", "_test.go", "Test.java")
# 数据模型路径特征（DATA_MODEL_CHANGE）
_MODEL_PATTERNS = ("entity", "model", "dto", "schema", "domain", "pojo", "dao", "vo", "bean")
# API/路由/控制器路径特征（API_CHANGE）；注意避免误匹配 resources 目录，故用 /api/ 而非 api
_API_PATH_PATTERNS = ("controller", "router", "handler", "endpoint", "/api/")
# 数据库迁移特征（DATABASE_CHANGE）
_DB_MIGRATION_PATTERNS = ("migration", "migrations", "flyway", "liquibase", "alembic")
# 日志调用特征（LOGGING_CHANGE）
_LOG_PATTERNS = (
    r"\blog\.", r"\blogger\.", r"\blogging\.", r"\bconsole\.",
    r"\bzap\.", r"\blogrus\.", r"slf4j",
)
_LOG_RE = re.compile("|".join(_LOG_PATTERNS))
# 数据库 DDL
_DDL_RE = re.compile(r"\b(CREATE|ALTER|DROP)\s+(TABLE|INDEX|VIEW)\b", re.IGNORECASE)


class LineChange(NamedTuple):
    text: str
    change: str  # added / removed / context


# 纯布局约定目录：这些段不携带业务语义，derive_module 会跳过它们。
# 刻意不包含 core / common / server / client —— 这些常常就是真实的模块名。
_LAYOUT_DIRS = {
    "src", "source", "sources", "main", "java", "kotlin", "scala", "groovy",
    "resources", "resource", "lib", "libs", "app", "apps", "packages", "pkg",
    "bin", "dist", "build", "out", "target", "static", "public", "private",
    "web", "www", "webapp", "module", "modules", "test", "tests", "testing",
    "node_modules", "web-inf", "meta-inf",
}


def detect_language(path: str) -> str | None:
    ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return _EXT_MAP.get(ext)


def derive_module(path: str) -> str:
    """推导模块名：跳过纯布局目录，取第一个有业务语义的目录段。

    只取第一级会让 src/ 布局的仓库 module 恒为 "src"，KB 的 modules 过滤与报告
    「涉及模块」都会失效。例：
        src/main/java/com/x/Foo.java        -> com
        src/refund/RefundController.java   -> refund
        app/adapters/base.py                -> adapters
        src/a/b.py                          -> a
    整条路径都是布局段时回退到第一级目录；只有单个文件名时回退到文件名。
    """
    norm = path.replace("\\", "/").strip("/")
    parts = [p for p in norm.split("/") if p]
    if not parts:
        return ""
    dirs = parts[:-1]  # 末段是文件名
    for d in dirs:
        if d.lower() not in _LAYOUT_DIRS:
            return d
    if dirs:
        return dirs[0]
    return parts[0]


def _iter_changed_lines(lines: list[LineChange]):
    for lc in lines:
        if lc.change in ("added", "removed"):
            yield lc.text, lc.change


class LanguageParser(ABC):
    """语言可插拔符号抽取策略。"""

    name: str = "unknown"
    # (kind, regex) 列表，正则需捕获符号名（第一个捕获组）
    symbol_patterns: list[tuple[str, re.Pattern]] = []
    # API / 路由 / 注解 提示正则
    api_patterns: list[re.Pattern] = []

    def extract_symbols(self, lines: list[LineChange], file_path: str) -> list[Symbol]:
        found: list[Symbol] = []
        seen = set()
        for kind, pat in self.symbol_patterns:
            for lc in lines:
                m = pat.search(lc.text)
                if not m:
                    continue
                name = m.group(1)
                key = (kind, name)
                if key in seen:
                    continue
                seen.add(key)
                found.append(Symbol(name=name, kind=kind, file=file_path, change=lc.change))
        return found

    def has_api_hint(self, lines: list[LineChange]) -> bool:
        for lc in lines:
            for pat in self.api_patterns:
                if pat.search(lc.text):
                    return True
        return False

    @abstractmethod
    def is_data_model_file(self, path: str, lines: list[LineChange]) -> bool:
        ...


# ===== 通用（跨语言）变更类型判定辅助 =====
def path_has_any(path: str, patterns: tuple[str, ...]) -> bool:
    low = path.lower().replace("\\", "/")
    return any(p in low for p in patterns)


# 关键词驱动的变更类型识别规则。
#
# 匹配对象是「小写化后的改动行全文」，因此不能用 \b 词边界：camelCase 标识符
# 小写化后单词会粘连（getAuthInfo -> getauthinfo），加 \b 会漏掉 getAuthInfo /
# cacheKey / transactionManager 这类最常见的写法。所以这里用「词干 + \w*」形式
# 保持足够宽的覆盖面，再用否定断言逐个排除真正会误判的英文词。
#
# 关键取舍（曾经的误报来源）：
#   auth(?!or)  裸子串 "auth" 会命中 author / authors / authored / authorName
#               （署名、模板变量、审计字段），把普通 POJO 判成权限变更。
#               排除 or 前缀后仍覆盖 checkauth / getauthtoken / userauth / oauth，
#               而 authorization / authenticate 由各自词干单独覆盖。
#   serializ    词干取 serializ 而非 serial，避免命中 Java 序列化样板
#               serialVersionUID（它表示「类实现了 Serializable」，不是序列化行为变更）。
_KEYWORD_RULES: tuple[tuple[tuple[re.Pattern, ...], ChangeType, HighImpactFeature], ...] = (
    (
        (re.compile(r"transaction\w*"), re.compile(r"事务")),
        ChangeType.TRANSACTION_CHANGE, HighImpactFeature.TRANSACTION,
    ),
    (
        (re.compile(r"cach\w*"), re.compile(r"缓存"), re.compile(r"redis\w*")),
        ChangeType.CACHE_CHANGE, HighImpactFeature.CACHE,
    ),
    (
        (re.compile(r"serializ\w*"), re.compile(r"反序列化")),
        ChangeType.SERIALIZATION_CHANGE, HighImpactFeature.SERIALIZATION,
    ),
    (
        (
            re.compile(r"auth(?!or)"),
            re.compile(r"authoriz\w*"),
            re.compile(r"authenticat\w*"),
            re.compile(r"@secured"),
            re.compile(r"permission"),
            re.compile(r"权限"),
        ),
        ChangeType.AUTH_CHANGE, HighImpactFeature.PERMISSION,
    ),
)


def detect_change_types(path: str, lines: list[LineChange], lang_parser: LanguageParser | None,
                        language: str | None) -> tuple[list[ChangeType], list[HighImpactFeature]]:
    """依据 C5 通用规则判定变更类型与高影响特征。"""
    change_types: list[ChangeType] = []
    high_impact: list[HighImpactFeature] = []

    fname = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""

    added_removed = list(_iter_changed_lines(lines))

    # CONFIG
    if ext in _CONFIG_EXT or path_has_any(path, ("config",)):
        change_types.append(ChangeType.CONFIG_CHANGE)
        high_impact.append(HighImpactFeature.CONFIGURATION)

    # DEPENDENCY
    if fname.lower() in _DEP_FILES:
        change_types.append(ChangeType.DEPENDENCY_CHANGE)
        high_impact.append(HighImpactFeature.EXTERNAL_DEPENDENCY)

    # TEST
    if path_has_any(path, _TEST_PATTERNS) or fname.endswith("Test.java") \
            or fname.startswith("test_") or fname.endswith("_test.py"):
        change_types.append(ChangeType.TEST_CHANGE)

    # DATABASE
    is_db = path_has_any(path, _DB_MIGRATION_PATTERNS) or ext == ".sql"
    if not is_db:
        for text, _ in added_removed:
            if _DDL_RE.search(text):
                is_db = True
                break
    if is_db:
        change_types.append(ChangeType.DATABASE_CHANGE)
        high_impact.append(HighImpactFeature.DATABASE)

    # LOGGING
    is_log = False
    for text, _ in added_removed:
        if _LOG_RE.search(text):
            is_log = True
            break
    if is_log:
        change_types.append(ChangeType.LOGGING_CHANGE)
        high_impact.append(HighImpactFeature.LOGGING)

    # DATA MODEL
    if lang_parser is not None and lang_parser.is_data_model_file(path, lines):
        change_types.append(ChangeType.DATA_MODEL_CHANGE)

    # API
    is_api = path_has_any(path, _API_PATH_PATTERNS)
    if not is_api and lang_parser is not None and lang_parser.has_api_hint(lines):
        is_api = True
    if is_api:
        change_types.append(ChangeType.API_CHANGE)
        high_impact.append(HighImpactFeature.PUBLIC_API)

    # 注释变更（启发式）：有改动且改动行几乎全是注释
    if _is_comment_only_change(lines, language):
        change_types.append(ChangeType.COMMENT_CHANGE)

    # 关键词驱动的 AUTH/CACHE/TRANSACTION/SERIALIZATION
    blob = " ".join(t for t, _ in added_removed).lower()
    for patterns, ct, hi in _KEYWORD_RULES:
        if any(p.search(blob) for p in patterns):
            if ct not in change_types:
                change_types.append(ct)
            if hi not in high_impact:
                high_impact.append(hi)

    # 去重并保持稳定顺序
    change_types = list(dict.fromkeys(change_types))
    high_impact = list(dict.fromkeys(high_impact))
    return change_types, high_impact


_COMMENT_PREFIX = {
    "java": ("//", "/*"),
    "kotlin": ("//", "/*"),
    "scala": ("//", "/*"),
    "python": ("#",),
    "typescript": ("//", "/*"),
    "go": ("//", "/*"),
}


def _is_comment_only_change(lines: list[LineChange], language: str | None) -> bool:
    changed = [(t, c) for t, c in _iter_changed_lines(lines)]
    if not changed:
        return False
    prefixes = _COMMENT_PREFIX.get(language or "", ("//", "#", "/*"))
    commentish = 0
    for text, _ in changed:
        stripped = text.strip()
        if not stripped:
            continue
        if any(stripped.startswith(p) for p in prefixes):
            commentish += 1
    # 超过 80% 改动行是注释才算注释变更
    return commentish > 0 and commentish / len(changed) > 0.8
