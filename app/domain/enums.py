"""冻结枚举（C4 / CheckReport Schema / ADR D9/D10）。

集中定义所有对外枚举，避免散落各处导致契约漂移。
"""
from __future__ import annotations

from enum import Enum


class ChangeType(str, Enum):
    API_CHANGE = "API_CHANGE"
    DATA_MODEL_CHANGE = "DATA_MODEL_CHANGE"
    DATABASE_CHANGE = "DATABASE_CHANGE"
    CONFIG_CHANGE = "CONFIG_CHANGE"
    DEPENDENCY_CHANGE = "DEPENDENCY_CHANGE"
    LOGGING_CHANGE = "LOGGING_CHANGE"
    COMMENT_CHANGE = "COMMENT_CHANGE"
    TEST_CHANGE = "TEST_CHANGE"
    AUTH_CHANGE = "AUTH_CHANGE"
    CACHE_CHANGE = "CACHE_CHANGE"
    TRANSACTION_CHANGE = "TRANSACTION_CHANGE"
    SERIALIZATION_CHANGE = "SERIALIZATION_CHANGE"


class HighImpactFeature(str, Enum):
    PUBLIC_API = "PUBLIC_API"
    DATABASE = "DATABASE"
    CONFIGURATION = "CONFIGURATION"
    PERMISSION = "PERMISSION"
    TRANSACTION = "TRANSACTION"
    CACHE = "CACHE"
    SERIALIZATION = "SERIALIZATION"
    EXTERNAL_DEPENDENCY = "EXTERNAL_DEPENDENCY"
    CONCURRENCY = "CONCURRENCY"
    LOGGING = "LOGGING"


class AnalysisMode(str, Enum):
    FULL = "full"
    FOCUSED = "focused"
    SUMMARY_ONLY = "summary_only"


class KbStatus(str, Enum):
    NOT_CONFIGURED = "not_configured"
    SUCCESS = "success"
    EMPTY = "empty"
    FAILED = "failed"


class EvidenceLevel(str, Enum):
    A = "A"
    B = "B"
    C = "C"
    N = "N"


class DocCheckVerdict(str, Enum):
    UPDATE = "update"
    CONFIRM = "confirm"
    NO_OBVIOUS_NEED = "no_obvious_need"
    UNKNOWN = "unknown"


class RuleVerdict(str, Enum):
    OK = "ok"
    VIOLATION = "violation"
    UNKNOWN = "unknown"


class TechDebtVerdict(str, Enum):
    DIRECT_MATCH = "direct_match"
    RELATED = "related"
    POSSIBLE = "possible"
    NONE_FOUND = "none_found"
    UNKNOWN = "unknown"


class RiskLevel(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class DocType(str, Enum):
    DEVELOPMENT_RULE = "development_rule"
    API_DOCUMENT = "api_document"
    TECHNICAL_DEBT = "technical_debt"
    HISTORICAL_RISK = "historical_risk"


class Platform(str, Enum):
    """Git 数据源路由键。

    当前仅支持 ``local``：直连本地仓库 ``.git``，无需 Token（读取 diff 与元数据）。
    """

    LOCAL = "local"  # 直连本地仓库 .git，无需 Token（读 diff + 元数据）


class MrState(str, Enum):
    """MR / PR 状态（兼容旧链路保留；当前仅本地自检不使用）。"""

    OPENED = "opened"
    OPEN = "open"
    CLOSED = "closed"
    MERGED = "merged"
    ALL = "all"
