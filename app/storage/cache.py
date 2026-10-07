"""LLM 结果缓存（R3）：整份 diff 级的「重复分析消除」。

刻意不做文件级增量——ChangeProfile 与人工 checklist 依赖全局 diff，
文件级拼接会改变画像语义，得不偿失（见 roadmap D2）。

cache_key 含 diff / model / prompt 版本 / KB 命中集，任一变化即失效：
- prompt 一改（SYSTEM_PROMPT 哈希变）→ 全量失效，不会拿旧结论糊弄；
- KB 命中集变化 → 该条失效，新知识不会被旧缓存掩盖。

默认关闭（PR_CHECK_CACHE=1 开启），避免首次使用者困惑于「为何秒回」。
读写异常一律降级为「未命中 / 不写入」，绝不影响自检主流程。
"""
from __future__ import annotations

import hashlib
import logging
import os

from app.domain.models import AnalysisCache
from app.domain.schemas import CheckReport
from app.storage.repo import (
    get_cache_entry,
    prune_cache,
    put_cache_entry,
    touch_cache_entry,
)

logger = logging.getLogger("pr_check")

_TRUTHY = {"1", "true", "yes", "on"}
DEFAULT_MAX_ENTRIES = 500


def cache_enabled() -> bool:
    return os.getenv("PR_CHECK_CACHE", "").strip().lower() in _TRUTHY


def prompt_version() -> str:
    from app.agent.prompt import SYSTEM_PROMPT

    return hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest()[:12]


def make_cache_key(diff_text: str, model: str, kb_ids: "set[str]") -> str:
    """缓存键：prompt 版本 + model + KB 命中 id 集合（排序）+ diff 全文。"""
    basis = "|".join([
        prompt_version(), model or "",
        ",".join(sorted(kb_ids)), diff_text,
    ])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def get_cached(cache_key: str) -> CheckReport | None:
    try:
        entry = get_cache_entry(cache_key)
    except Exception as exc:  # noqa: BLE001 - 缓存不可用即视为未命中
        logger.warning("cache_read_failed type=%s", type(exc).__name__)
        return None
    if entry is None:
        return None
    try:
        report = CheckReport.model_validate_json(entry.report_json)
    except Exception as exc:  # noqa: BLE001 - 脏数据当作未命中
        logger.warning("cache_decode_failed type=%s", type(exc).__name__)
        return None
    try:
        touch_cache_entry(cache_key)
    except Exception:  # noqa: BLE001 - 计数失败不影响命中
        pass
    return report


def put_cached(cache_key: str, report: CheckReport, model: str, project: str) -> None:
    try:
        put_cache_entry(AnalysisCache(
            cache_key=cache_key,
            report_json=report.model_dump_json(),
            model=model or "",
            prompt_version=prompt_version(),
            project=project or "",
        ))
        prune_cache(DEFAULT_MAX_ENTRIES)
    except Exception as exc:  # noqa: BLE001 - 写缓存失败不影响本次自检
        logger.warning("cache_write_failed type=%s", type(exc).__name__)