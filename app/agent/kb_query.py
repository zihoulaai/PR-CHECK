"""KBQuery Builder：由 ChangeProfile 生成复合查询 + focus 推导（M2）。

focus 推导规则（M2，无需 LLM 决定）：
- 默认：development_rule + api_document
- 含任意业务变更（非纯测试/注释）→ 增加 technical_debt + historical_risk
- DATABASE_CHANGE / LOGGING_CHANGE 已落在 development_rule 范畴（默认包含）

focus 取值一律走 ``DocType`` 枚举，不再手写字符串：此前这里硬编码了
``doc_sync``，它并不在 DocType 的四个取值内，服务端按 doc_type 过滤时永远匹配不到，
而 ``or hits`` 兜底会把这个失效静默吞掉——focus 过滤形同虚设。
字段上限由 KBQuery schema 自动裁剪（M2）。
"""
from __future__ import annotations

from app.domain.enums import ChangeType, DocType
from app.domain.schemas import ChangeProfile, KBQuery, PRMetadata

_BUSINESS_TYPES = {
    ChangeType.API_CHANGE,
    ChangeType.DATA_MODEL_CHANGE,
    ChangeType.DATABASE_CHANGE,
    ChangeType.CONFIG_CHANGE,
    ChangeType.DEPENDENCY_CHANGE,
    ChangeType.LOGGING_CHANGE,
    ChangeType.AUTH_CHANGE,
    ChangeType.CACHE_CHANGE,
    ChangeType.TRANSACTION_CHANGE,
    ChangeType.SERIALIZATION_CHANGE,
}


def build_kb_query(pr: PRMetadata, profile: ChangeProfile) -> KBQuery:
    focus = {DocType.DEVELOPMENT_RULE.value, DocType.API_DOCUMENT.value}

    if any(ct in _BUSINESS_TYPES for ct in profile.change_types):
        focus.add(DocType.TECHNICAL_DEBT.value)
        focus.add(DocType.HISTORICAL_RISK.value)

    # 变更类型对应的文件清单直接取自 ChangeProfile（由 diff 解析的判定结果归集），
    # 不在这里按路径另写一套启发式：两套规则必然漂移，且旧实现把
    # resources/ 静态资源误判为 API 变更。
    return KBQuery(
        project=pr.project or pr.repository,
        modules=list(profile.modules[:10]),
        pr_title=pr.title,
        pr_description=pr.description,
        key_files=[f.path for f in profile.files][:20],
        key_symbols=[s.name for s in profile.symbols][:30],
        change_types=[ct.value for ct in profile.change_types],
        api_changes=profile.api_changes[:10],
        data_changes=profile.data_changes[:10],
        config_changes=profile.config_changes[:10],
        dependency_changes=profile.dependency_changes[:10],
        logging_changes=profile.logging_changes[:10],
        keywords=list(profile.keywords[:20]),
        focus=sorted(focus),
    )
