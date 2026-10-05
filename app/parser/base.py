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


def detect_language(path: str) -> str | None:
    ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return _EXT_MAP.get(ext)


def derive_module(path: str) -> str:
    norm = path.replace("\\", "/").strip("/")
    parts = [p for p in norm.split("/") if p]
    if not parts:
        return ""
    # 取仓库内第一级目录作为模块；根目录文件无模块
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
    _kw = {
        ("@transactional", "transaction", "事务"): (ChangeType.TRANSACTION_CHANGE, HighImpactFeature.TRANSACTION),
        ("@cacheable", "cache", "缓存", "redis"): (ChangeType.CACHE_CHANGE, HighImpactFeature.CACHE),
        ("serialize", "反序列化", "serializ"): (ChangeType.SERIALIZATION_CHANGE, HighImpactFeature.SERIALIZATION),
        ("@preauthorize", "@secured", "permission", "权限", "auth"): (ChangeType.AUTH_CHANGE, HighImpactFeature.PERMISSION),
    }
    for kws, (ct, hi) in _kw.items():
        if any(k in blob for k in kws):
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
