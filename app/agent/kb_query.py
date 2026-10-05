"""KBQuery Builder：由 ChangeProfile 生成复合查询 + focus 推导（M2）。

focus 推导规则（M2，无需 LLM 决定）：
- 默认：doc_sync + development_rule
- 含 API_CHANGE → 增加 api_document
- 含任意业务变更（非纯测试/注释）→ 增加 technical_debt + historical_risk
- DATABASE_CHANGE / LOGGING_CHANGE 已落在 development_rule 范畴（默认包含）
字段上限由 KBQuery schema 自动裁剪（M2）。
"""
from __future__ import annotations

from app.domain.enums import ChangeType
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
    focus = {"doc_sync", "development_rule"}

    if ChangeType.API_CHANGE in profile.change_types:
        focus.add("api_document")

    if any(ct in _BUSINESS_TYPES for ct in profile.change_types):
        focus.add("technical_debt")
        focus.add("historical_risk")

    # 变更类型专属数组（用文件路径做可检索文本）
    api_files, data_files, config_files, logging_files = [], [], [], []
    for f in profile.files:
        cts = [ct.value for ct in profile.change_types]
        # 文件级归类（近似）：依据路径/扩展名
        low = f.path.lower()
        if "controller" in low or "router" in low or "resource" in low or low.endswith(".yml") is False and "api" in low:
            api_files.append(f.path)
        if any(p in low for p in ("entity", "model", "dto", "schema", "domain")):
            data_files.append(f.path)
        if low.endswith((".yml", ".yaml", ".properties", ".toml", ".ini", ".env")) or "config" in low:
            config_files.append(f.path)

    return KBQuery(
        project=pr.project or (pr.path_with_namespace if hasattr(pr, "path_with_namespace") else "") or pr.repository,
        modules=list(profile.modules[:10]),
        pr_title=pr.title,
        pr_description=pr.description,
        key_files=[f.path for f in profile.files][:20],
        key_symbols=[s.name for s in profile.symbols][:30],
        change_types=[ct.value for ct in profile.change_types],
        api_changes=api_files[:10],
        data_changes=data_files[:10],
        config_changes=config_files[:10],
        logging_changes=logging_files[:10],
        keywords=list(profile.keywords[:20]),
        focus=sorted(focus),
    )
