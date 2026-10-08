"""ChangeProfile Builder + 三档分析模式 + 重点 Diff 选择（C4 / D7）。

汇总 Diff 解析结果，判定变更类型与高影响特征，并依据规模阈值决定 analysis_mode。
"""
from __future__ import annotations

import re

from app.config import Settings, get_settings
from app.domain.enums import AnalysisMode, ChangeType, HighImpactFeature
from app.domain.schemas import ChangeProfile, FileChange, PRMetadata, Symbol
from app.parser.base import detect_change_types
from app.parser.diff_parser import ParsedFile, get_language_parser

# 用于关键词提取的分词（去掉常见标点）
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# 连续 CJK 片段。中文没有空格，但**路径天然分段**：`/退款/退款控制器.java` 会被
# `/` 与 `.` 切开，得到「退款」「退款控制器」两个完整词——路径是中文项目里最可靠的
# 中文信号。
_CJK_RUN_RE = re.compile(r"[一-鿿]+")
# 单个来源贡献的 CJK token 上限，防止长描述挤占 40 个关键词的预算
_CJK_TOKENS_PER_SOURCE = 6

# 变更类型 → ChangeProfile 中对应的文件清单字段。
# 只有会流向 KB 检索（KBQuery）的类型才登记；未登记的类型仍计入 change_types，
# 不额外保留一份无人消费的文件清单。
_TYPE_TO_FIELD = {
    ChangeType.API_CHANGE: "api_changes",
    ChangeType.DATA_MODEL_CHANGE: "data_changes",
    ChangeType.CONFIG_CHANGE: "config_changes",
    ChangeType.DEPENDENCY_CHANGE: "dependency_changes",
    ChangeType.LOGGING_CHANGE: "logging_changes",
}

# 重点 Diff 保留的高影响特征（Medium 模式）
# 必须覆盖 HighImpactFeature 的全部取值：只要存在高影响特征却不在此集合内，
# select_focused_diff 会走「无重点特征」回退分支把整个 diff 送进 LLM，
# focused 模式反而退化为 full，控制上下文的初衷失效。
_FOCUS_FEATURES = set(HighImpactFeature)


def _cjk_tokens(text: str, limit: int = _CJK_TOKENS_PER_SOURCE) -> list[str]:
    """从文本中提取中文片段，作为关键词。

    为什么必须单独处理：``_TOKEN_RE`` 只认 ASCII 标识符，中文 PR 的标题 / 描述 /
    中文路径里没有任何 ASCII 词——此前它们的贡献恒为 0。对中文团队而言这是把最
    关键的一路信号排除在 KB 检索之外（``build_query_text`` 里有 ``PR：{title}``，
    但**没有描述，也没有中文路径段**）。

    按连续 CJK 片段切分，不做 2 字滑窗：中文没有空格，滑窗会产出大量跨词垃圾
    （「与缓」「的缓」），实测反而稀释了真正有信号的片段。片段整体保留即可——
    KB 检索是语义匹配，完整短语的信息量高于破碎窗口。
    """
    out: list[str] = []
    for run in _CJK_RUN_RE.findall(text or ""):
        if len(run) >= 2:  # 单字片段信息量过低
            out.append(run)
        if len(out) >= limit:
            break
    return out


def _extract_keywords(symbols: list[Symbol], files: list[FileChange], pr: PRMetadata) -> list[str]:
    raw: list[str] = []
    for s in symbols:
        raw.append(s.name)
    # 中文路径段排在标题 / 描述之前：路径分段天然可靠（`/退款/Refund.java`），
    # 而标题描述往往是整句、无空格，只能切出长片段。
    for f in files:
        raw.extend(_cjk_tokens(f.path, limit=3))
    raw.extend(_cjk_tokens(pr.title))
    raw.extend(_cjk_tokens(pr.description))
    for f in files:
        raw.extend(_TOKEN_RE.findall(f.path))
    raw.extend(_TOKEN_RE.findall(pr.title))
    raw.extend(_TOKEN_RE.findall(pr.description))
    seen: set[str] = set()
    out: list[str] = []
    for tok in raw:
        t = tok.strip()
        if len(t) <= 1:
            continue
        low = t.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(t)
    return out[:40]


def _rank_modules(files: list[FileChange],
                  impact_by_module: dict[str, int]) -> list[str]:
    """按相关性排序模块，而不是字典序。

    为什么要改：``modules`` 会被 KBQuery 截断（``modules[:10]``）。字典序下
    「改动 200 行的支付模块」可能排在「只改 1 行的 zzz 工具目录」之后而被截掉，
    于是检索 query 里恰好缺了最该命中的那个模块——截断把最有信息量的部分先扔掉。

    排序键：命中高影响特征的文件数 > 变更行数 > 文件数 > 名称（仅用于稳定排序，
    保证多次运行结果一致）。
    """
    lines_by_module: dict[str, int] = {}
    count_by_module: dict[str, int] = {}
    for f in files:
        if not f.module:
            continue
        lines_by_module[f.module] = lines_by_module.get(f.module, 0) + f.additions + f.deletions
        count_by_module[f.module] = count_by_module.get(f.module, 0) + 1
    return sorted(
        count_by_module,
        key=lambda m: (-impact_by_module.get(m, 0),
                       -lines_by_module.get(m, 0),
                       -count_by_module[m],
                       m),
    )


def build_change_profile(pr: PRMetadata, parsed: list[ParsedFile],
                         settings: Settings | None = None) -> ChangeProfile:
    settings = settings or get_settings()
    files: list[FileChange] = []
    symbols: list[Symbol] = []
    change_types: list[ChangeType] = []
    high_impact: list[HighImpactFeature] = []
    # 模块 → 命中高影响特征的文件数（_rank_modules 的排序键之一）
    impact_by_module: dict[str, int] = {}
    per_type: dict[str, list[str]] = {f: [] for f in _TYPE_TO_FIELD.values()}

    for pf in parsed:
        fc = FileChange(
            path=pf.path, status=pf.status, additions=pf.additions,
            deletions=pf.deletions, module=pf.module, language=pf.language or "",
        )
        files.append(fc)

        lang_parser = get_language_parser(pf.language)
        if lang_parser is not None:
            symbols.extend(lang_parser.extract_symbols(pf.lines, pf.path))
        else:
            # 未识别语言：降级为文件级，不抽取符号
            pass

        cts, his = detect_change_types(pf.path, pf.lines, lang_parser, pf.language)
        change_types.extend(cts)
        high_impact.extend(his)
        # 回填到 ParsedFile，供 select_focused_diff 复用，避免重复检测
        pf.change_types = list(cts)
        pf.high_impact = list(his)
        # 按变更类型归集文件：这是「哪些文件属于 API/配置/日志变更」的唯一事实来源，
        # KB 查询直接消费这些字段，不再各自用启发式重新推断路径。
        for ct in cts:
            bucket = _TYPE_TO_FIELD.get(ct)
            if bucket is not None:
                per_type[bucket].append(pf.path)
        if his and pf.module:
            impact_by_module[pf.module] = impact_by_module.get(pf.module, 0) + 1

    # 去重并保持出现顺序（Pydantic 模型不可哈希，需按关键字段去重）
    def _dedup_enums(seq):
        seen = set()
        out = []
        for x in seq:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return out

    seen_sym: set[tuple] = set()
    uniq_symbols = []
    for s in symbols:
        key = (s.name, s.kind, s.file)
        if key not in seen_sym:
            seen_sym.add(key)
            uniq_symbols.append(s)

    changed_lines = sum(f.additions + f.deletions for f in files)
    analysis_mode = _classify_mode(len(files), changed_lines, settings)

    return ChangeProfile(
        changed_files=len(files),
        added_files=sum(1 for f in files if f.status == "added"),
        deleted_files=sum(1 for f in files if f.status == "deleted"),
        renamed_files=sum(1 for f in files if f.status == "renamed"),
        modules=_rank_modules(files, impact_by_module),
        files=files,
        symbols=uniq_symbols,
        change_types=_dedup_enums(change_types),
        high_impact_features=_dedup_enums(high_impact),
        keywords=_extract_keywords(symbols, files, pr),
        analysis_mode=analysis_mode,
        changed_lines=changed_lines,
        **per_type,
    )


def _classify_mode(n_files: int, changed_lines: int, settings: Settings) -> AnalysisMode:
    # 无文件或无有效增删行（二进制 / 纯权限变更 / 空 diff / 纯重命名）：
    # 没有可交给 LLM 分析的内容，直接走 summary_only，避免拿空输入调模型。
    if n_files == 0 or changed_lines == 0:
        return AnalysisMode.SUMMARY_ONLY
    # Large：文件数或行数超过中档上限
    if n_files > settings.medium_max_files or changed_lines > settings.medium_max_lines:
        return AnalysisMode.SUMMARY_ONLY
    # Small：文件数与行数都在小档上限内
    if n_files <= settings.small_max_files and changed_lines <= settings.small_max_lines:
        return AnalysisMode.FULL
    # 其余为中档（focused）
    return AnalysisMode.FOCUSED


def select_focused_diff(parsed: list[ParsedFile], profile: ChangeProfile) -> str:
    """Medium 模式：仅保留高影响文件对应的 diff 片段，控制 LLM 上下文规模。

    依赖 build_change_profile 已把 change_types / high_impact 回填到 ParsedFile；
    未回填时按「无重点信息」处理（保留全部），不会误裁剪。
    """
    keep_features = set(profile.high_impact_features) & _FOCUS_FEATURES
    # 若没有任何重点特征，回退保留全部（避免空 context）
    if not keep_features:
        return "\n".join(pf.block_text for pf in parsed)

    kept: list[str] = []
    for pf in parsed:
        if (set(pf.high_impact) & keep_features) or pf.status in ("added", "deleted"):
            kept.append(pf.block_text)
    return "\n".join(kept) if kept else "\n".join(pf.block_text for pf in parsed)
