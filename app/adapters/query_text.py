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

# 检索文本的字符预算。
#
# Dify 的 query 字段上限 250 字符（adapters/dify_kb.py 会截断）。此前有两个叠加问题：
#   1) focus（唯一决定「查哪一类文档」的信号）排在所有内容之后；
#   2) 路径清单与模块清单完全不限长（只有 symbols/keywords 做了切片）。
# 于是任何非平凡 PR 的预算都会被前面的内容吃满，focus 与关键词被整段截掉——
# 「按文档类型聚焦检索」在实际运行中从未生效。故改为按优先级装配 + 逐段限额，
# 并把 focus 提到第二位。
_PATH_LIMIT_BUDGETED = 3
_PATH_LIMIT_DEFAULT = 10      # 与 KBQuery schema 的每类清单上限一致
_MODULE_LIMIT_BUDGETED = 5
# 变更类型 → 文本标签；顺序即优先级（越靠前越先尝试占预算）
_FILE_LIST_SECTIONS = (
    ("接口变更文件", "api_changes"),
    ("数据模型变更文件", "data_changes"),
    ("配置变更文件", "config_changes"),
    ("依赖变更文件", "dependency_changes"),
    ("日志变更文件", "logging_changes"),
)


def _join_limited(values, limit: int) -> str:
    if not values:
        return ""
    if len(values) <= limit:
        return ", ".join(values)
    return ", ".join(values[:limit]) + f" 等{len(values)}个"


def build_query_text(query: KBQuery, max_chars: int | None = None) -> str:
    """拼检索用的自然语言 query 文本。

    各变更类型的文件清单必须落到文本里：服务端若只消费结构化字段而忽略文本，
    召回会退化；因此接口 / 数据模型 / 配置 / 依赖 / 日志变更等文件清单都进文本。

    ``max_chars`` 为 None 时不限长（此前行为）。给定预算时按优先级逐段装配：
    装不下就整段丢弃（而非从尾部硬截，避免留下半截路径），并保证返回值长度
    不超过预算——调用方可直接依赖该不变量，无需再自行截断。
    """
    path_limit = _PATH_LIMIT_BUDGETED if max_chars else _PATH_LIMIT_DEFAULT

    # 优先级从高到低：项目与检索类型定位 → 变更概述 → 文件清单 → 符号与关键词
    parts: list[str] = [f"项目：{query.project}"]
    if query.focus:
        parts.append("重点查询：" + ", ".join(query.focus))
    if query.modules:
        mod_limit = _MODULE_LIMIT_BUDGETED if max_chars else len(query.modules)
        parts.append("模块：" + _join_limited(query.modules, mod_limit))
    if query.pr_title:
        parts.append(f"PR：{query.pr_title}")
    if query.change_types:
        parts.append("变更类型：" + ", ".join(query.change_types))
    for label, field in _FILE_LIST_SECTIONS:
        values = getattr(query, field, None)
        if values:
            parts.append(f"{label}：" + _join_limited(values, path_limit))
    if query.key_symbols:
        parts.append("关键符号：" + ", ".join(query.key_symbols[:15]))
    if query.keywords:
        parts.append("关键词：" + ", ".join(query.keywords[:20]))

    if not max_chars:
        return "\n".join(p for p in parts if p)

    out: list[str] = []
    used = 0
    for p in parts:
        if not p:
            continue
        cost = len(p) + 1  # 换行开销
        # 首段始终保留（项目标识是检索的基本约束），后续装不下就停
        if out and used + cost > max_chars:
            break
        out.append(p)
        used += cost
    text = "\n".join(out)
    return text[:max_chars]


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
