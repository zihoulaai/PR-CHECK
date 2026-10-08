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
# 拆分：短词走词级匹配（_TEST_WORDS），复合形态走子串匹配（_TEST_SUBSTRINGS）。
# 短词做裸子串匹配时极易跨词命中（la-test 含 test），是确定性误判的主要来源。
_TEST_WORDS = ("test", "tests", "spec")
_TEST_SUBSTRINGS = ("__test__", "_test.go", "Test.java")
# 向后兼容别名：仍有外部引用按「任一命中」语义使用（词级 ∪ 子串级）
_TEST_PATTERNS = _TEST_WORDS + _TEST_SUBSTRINGS
# 数据模型路径特征（DATA_MODEL_CHANGE）：全部为单词，按词级匹配
_MODEL_PATTERNS = ("entity", "model", "dto", "schema", "domain", "pojo", "dao", "vo", "bean")
# API/路由/控制器路径特征（API_CHANGE）
# controller/router/handler/endpoint 走词级匹配；/api/ 是路径形态（含斜杠），
# 保持子串匹配——沿用既有约定，避免误匹配 resources 目录。
_API_WORDS = ("controller", "router", "handler", "endpoint")
_API_SUBSTRINGS = ("/api/",)
_API_PATH_PATTERNS = _API_WORDS + _API_SUBSTRINGS
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


# ===== 路径词级匹配（替代短词的裸子串匹配）=====
#
# 背景：_MODEL_PATTERNS 含 2 字符的 vo / dao，_TEST_PATTERNS 含 test / spec，
# 用 `p in path.lower()` 匹配必然跨词命中，实测确定性误判：
#     src/evolution/Service.java  -> DATA_MODEL_CHANGE（evo-lution 内含 vo）
#     src/avoid/x.java            -> DATA_MODEL_CHANGE（a-void 内含 vo）
#     docs/latest.md              -> TEST_CHANGE（la-test 内含 test）
# 误判会沿 change_types -> KB focus -> 报告「涉及模块/文档核查」整条链路放大。
#
# 为什么不能用 `\b`：上文的 keyword 规则针对的是**小写化后的代码正文**（camelCase
# 小写后单词粘连），因此刻意不用词边界。路径不同——路径天然按 / _ - . 分段，
# 段内再按 camelCase 边界切开即可得到干净的「词」，两头都能兼顾：
#     OrderController -> {order, controller}  命中 controller（复合名不漏）
#     UserVO          -> {user, vo}           命中 vo（短名不漏）
#     evolution / avoid / latest -> 单词一个，均不含 vo / test（跨词不误判）
# 另按 rstrip('s') 归一，覆盖 controllers / models / tests / entities 等复数目录。
_PATH_SEP_RE = re.compile(r"[/_\-.\s\\]+")
_CAMEL_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")


def path_words(path: str) -> set[str]:
    """把路径切成「词」集合（小写，含复数归一）。"""
    words: set[str] = set()
    for seg in _PATH_SEP_RE.split(path.strip("/")):
        if not seg:
            continue
        for raw in _CAMEL_RE.findall(seg):
            low = raw.lower()
            words.add(low)
            if len(low) > 3 and low.endswith("s"):
                words.add(low[:-1])
    return words


def path_has_word(path: str, patterns: tuple[str, ...]) -> bool:
    """按「词」匹配路径。仅用于短 pattern（vo / test / controller 等）。

    长 pattern（migration / flyway / __test__ / /api/ 等）继续用 path_has_any
    子串匹配：它们足够长，跨词误判概率可忽略，改动收益低于回归风险。
    """
    words = path_words(path)
    return any(p in words for p in patterns)


def is_data_model_path(path: str) -> bool:
    """数据模型路径判定。各语言 parser 的 is_data_model_file 共用此实现。

    此前 6 个 parser 各自复制 `any(p in path.lower() for p in _MODEL_PATTERNS)`，
    同一处误判逻辑被复制了 6 份；收敛到此函数，一次修复六处生效。
    """
    return path_has_word(path, _MODEL_PATTERNS)


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


# 并发特征：只产出高影响特征，不新增 ChangeType。
#
# HighImpactFeature.CONCURRENCY 此前是**不可达取值**：定义在枚举里、也映射了
# checklist 项「并发安全（竞态、死锁）」，但没有任何规则产出它——于是这条最该在
# 并发改动时出现的提醒，在真实报告里永远不可能出现。
#
# 取词偏保守：只收实现层面的强信号与中文并发术语，不收 concurrent / concurrency
# 这类会出现在英文散文里的词（宁可漏报，不要把普通文案判成并发改动）。
_CONCURRENCY_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"synchroniz\w*"),      # Java synchronized
    re.compile(r"atomic\w*"),          # AtomicInteger / atomic<T>
    re.compile(r"reentrantlock"),
    re.compile(r"mutex"),
    re.compile(r"semaphore"),
    re.compile(r"waitgroup"),          # Go sync.WaitGroup
    re.compile(r"sync\.\w+"),          # Go sync 包
    re.compile(r"asyn\w*"),           # async / asyncio / asynchronous
    re.compile(r"await\b"),           # await（不含 "asyn"，需单列）
    # 已知局限：字符串字面量里的技术词会误命中（如 String name = "atomic";）。
    # 为此对全部关键词规则剥离引号内容会改变既有行为（中文关键词同样会丢），
    # 代价大于收益，故保留——误报的代价只是多一条并发自查项。
    re.compile(r"threadlocal"),
    re.compile(r"thread\w*"),          # threading / Thread
    re.compile(r"concurrenthashmap"),
    re.compile(r"并发"), re.compile(r"竞态"), re.compile(r"死锁"),
    re.compile(r"乐观锁"), re.compile(r"悲观锁"),
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
    if path_has_word(path, _TEST_WORDS) or path_has_any(path, _TEST_SUBSTRINGS) \
            or fname.endswith("Test.java") \
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
    is_api = path_has_word(path, _API_WORDS) or path_has_any(path, _API_SUBSTRINGS)
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

    # 并发特征（HighImpactFeature.CONCURRENCY）：只加高影响特征，不新增 ChangeType
    if any(pat.search(blob) for pat in _CONCURRENCY_PATTERNS):
        high_impact.append(HighImpactFeature.CONCURRENCY)

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
