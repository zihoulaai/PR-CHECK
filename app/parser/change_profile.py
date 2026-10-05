"""ChangeProfile Builder + 三档分析模式 + 重点 Diff 选择（C4 / D7）。

汇总 Diff 解析结果，判定变更类型与高影响特征，并依据规模阈值决定 analysis_mode。
"""
from __future__ import annotations

import re
from typing import Iterable

from app.config import Settings, get_settings
from app.domain.enums import AnalysisMode, ChangeType, HighImpactFeature
from app.domain.schemas import ChangeProfile, FileChange, PRMetadata, Symbol
from app.parser.base import detect_change_types
from app.parser.diff_parser import ParsedFile, get_language_parser

# 用于关键词提取的分词（去掉常见标点）
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# 重点 Diff 保留的高影响特征（Medium 模式）
_FOCUS_FEATURES = {
    HighImpactFeature.PUBLIC_API,
    HighImpactFeature.DATABASE,
    HighImpactFeature.CONFIGURATION,
    HighImpactFeature.PERMISSION,
    HighImpactFeature.TRANSACTION,
    HighImpactFeature.CACHE,
    HighImpactFeature.EXTERNAL_DEPENDENCY,
    HighImpactFeature.LOGGING,
}


def _extract_keywords(symbols: list[Symbol], files: list[FileChange], pr: PRMetadata) -> list[str]:
    raw: list[str] = []
    for s in symbols:
        raw.append(s.name)
    for f in files:
        raw.extend(re.findall(_TOKEN_RE, f.path))
    raw.extend(re.findall(_TOKEN_RE, pr.title))
    raw.extend(re.findall(_TOKEN_RE, pr.description))
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


def build_change_profile(pr: PRMetadata, parsed: list[ParsedFile],
                         settings: Settings | None = None) -> ChangeProfile:
    settings = settings or get_settings()
    files: list[FileChange] = []
    symbols: list[Symbol] = []
    change_types: list[ChangeType] = []
    high_impact: list[HighImpactFeature] = []
    modules: set[str] = set()

    for pf in parsed:
        fc = FileChange(
            path=pf.path, status=pf.status, additions=pf.additions,
            deletions=pf.deletions, module=pf.module, language=pf.language or "",
        )
        files.append(fc)
        if pf.module:
            modules.add(pf.module)

        lang_parser = get_language_parser(pf.language)
        if lang_parser is not None:
            symbols.extend(lang_parser.extract_symbols(pf.lines, pf.path))
        else:
            # 未识别语言：降级为文件级，不抽取符号
            pass

        cts, his = detect_change_types(pf.path, pf.lines, lang_parser, pf.language)
        change_types.extend(cts)
        high_impact.extend(his)

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
        modules=list(dict.fromkeys(modules)),
        files=files,
        symbols=uniq_symbols,
        change_types=_dedup_enums(change_types),
        high_impact_features=_dedup_enums(high_impact),
        keywords=_extract_keywords(symbols, files, pr),
        analysis_mode=analysis_mode,
        changed_lines=changed_lines,
    )


def _classify_mode(n_files: int, changed_lines: int, settings: Settings) -> AnalysisMode:
    # Large：文件数或行数超过中档上限
    if n_files > settings.medium_max_files or changed_lines > settings.medium_max_lines:
        return AnalysisMode.SUMMARY_ONLY
    # Small：文件数与行数都在小档上限内
    if n_files <= settings.small_max_files and changed_lines <= settings.small_max_lines:
        return AnalysisMode.FULL
    # 其余为中档（focused）
    return AnalysisMode.FOCUSED


def select_focused_diff(parsed: list[ParsedFile], profile: ChangeProfile) -> str:
    """Medium 模式：仅保留高影响文件对应的 diff 片段，控制 LLM 上下文规模。"""
    keep_features = set(profile.high_impact_features) & _FOCUS_FEATURES
    # 若没有任何重点特征，回退保留全部（避免空 context）
    if not keep_features:
        return "\n".join(pf.block_text for pf in parsed)

    kept: list[str] = []
    for pf in parsed:
        lang_parser = get_language_parser(pf.language)
        _, his = detect_change_types(pf.path, pf.lines, lang_parser, pf.language)
        if (set(his) & keep_features) or pf.status in ("added", "deleted"):
            kept.append(pf.block_text)
    return "\n".join(kept) if kept else "\n".join(pf.block_text for pf in parsed)
