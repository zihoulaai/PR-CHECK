"""共享 KB 检索辅助：query 文本构建与命中解析（供应商无关）。

- ``build_query_text``：把结构化 ``KBQuery`` 拼成自然语言检索文本。各供应商
  适配器默认复用此实现；若某供应商需要不同的召回字段组织，可在各自 adapter
  内 override（仍须保证 ``project`` 落入可过滤维度，满足 Evidence 约束）。
- ``parse_hits``：把检索响应统一转 ``KBHit`` 列表，并**按 project 强制过滤**
  （双重保险：服务端已过滤，adapter 再过滤一次，防止伪造的跨项目命中污染
  Evidence A/B 的真实来源判定）。结构异常向上抛，由调用方统一转 ``KbError``。
"""
from __future__ import annotations

from app.domain.schemas import KBHit, KBQuery


def build_query_text(query: KBQuery) -> str:
    """拼检索用的自然语言 query 文本。

    各变更类型的文件清单必须落到文本里：服务端若只消费结构化字段而忽略文本，
    召回会退化；因此接口 / 数据模型 / 配置 / 依赖 / 日志变更等文件清单都进文本。
    """
    parts = [f"项目：{query.project}"]
    if query.modules:
        parts.append("模块：" + ", ".join(query.modules))
    if query.pr_title:
        parts.append(f"PR：{query.pr_title}")
    if query.change_types:
        parts.append("变更类型：" + ", ".join(query.change_types))
    # 按变更类型给出文件清单：比裸路径更利于召回对应规范/技术债文档
    for label, paths in (
        ("接口变更文件", query.api_changes),
        ("数据模型变更文件", query.data_changes),
        ("配置变更文件", query.config_changes),
        ("依赖变更文件", query.dependency_changes),
        ("日志变更文件", query.logging_changes),
    ):
        if paths:
            parts.append(f"{label}：" + ", ".join(paths))
    if query.key_symbols:
        parts.append("关键符号：" + ", ".join(query.key_symbols[:15]))
    if query.keywords:
        parts.append("关键词：" + ", ".join(query.keywords[:20]))
    if query.focus:
        parts.append("重点查询：" + ", ".join(query.focus))
    return "\n".join(parts)


def parse_hits(data, project: str) -> list[KBHit]:
    """把检索响应转为 KBHit 列表；结构异常向上抛，由 search 统一转 KbError。"""
    hits: list[KBHit] = []
    raw_hits = data.get("hits", []) if isinstance(data, dict) else []
    for h in raw_hits:
        # 服务端按 project 强制过滤；双重保险
        if h.get("project") and h["project"] != project:
            continue
        hits.append(KBHit(
            id=h.get("id", ""),
            title=h.get("title", ""),
            doc_type=h.get("doc_type", ""),
            module=h.get("module", ""),
            project=h.get("project", project),
            snippet=h.get("snippet", ""),
            score=float(h.get("score", 0.0)),
        ))
    return hits
