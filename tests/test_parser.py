"""Diff Parser + ChangeProfile 测试（覆盖 ≥15 fixtures 的变更类型准确性）。"""
from __future__ import annotations

import pytest

from app.domain.schemas import PRMetadata
from app.parser.change_profile import build_change_profile
from app.parser.diff_parser import parse_diff
from tests.fixtures.cases import ALL_CASES


def _profile(case):
    pr = PRMetadata(project=case["pr"]["project"], title=case["pr"]["title"],
                    description=case["pr"].get("description", ""))
    parsed = parse_diff(case["diff"])
    return pr, parsed, build_change_profile(pr, parsed)


def test_parse_file_count_and_status():
    pr, parsed, _ = _profile(ALL_CASES[1])  # API 新增
    assert len(parsed) == 1
    assert parsed[0].status == "added"
    assert parsed[0].language == "java"


@pytest.mark.parametrize("case", ALL_CASES, ids=lambda c: c["name"])
def test_change_types_should_find(case):
    _, _, profile = _profile(case)
    cts = {t.value for t in profile.change_types}
    for expected in case["should_find"]:
        assert expected in cts, f"{case['name']} 未识别到 {expected}，实际 {cts}"


@pytest.mark.parametrize("case", ALL_CASES, ids=lambda c: c["name"])
def test_change_types_should_not_claim(case):
    _, _, profile = _profile(case)
    cts = {t.value for t in profile.change_types}
    # should_not_claim 中属于变更类型的条目不得出现
    for nc in case["should_not_claim"]:
        if nc in {"API_CHANGE", "DATA_MODEL_CHANGE", "DATABASE_CHANGE", "CONFIG_CHANGE",
                  "DEPENDENCY_CHANGE", "LOGGING_CHANGE", "TEST_CHANGE", "COMMENT_CHANGE"}:
            assert nc not in cts, f"{case['name']} 不应识别为 {nc}"


def test_large_pr_is_summary_only():
    _, _, profile = _profile(ALL_CASES[14 - 1])  # CASE_LARGE 索引 13
    assert profile.analysis_mode.value == "summary_only"
    assert profile.changed_files > 80


def test_added_deleted_status():
    pr, parsed, profile = _profile(ALL_CASES[3])  # API 删除
    assert parsed[0].status == "deleted"
    assert profile.deleted_files == 1


# ===== R5：Kotlin / Scala 语言扩展 =====
_KOTLIN_DIFF = (
    "diff --git a/src/main/kotlin/com/x/RefundController.kt "
    "b/src/main/kotlin/com/x/RefundController.kt\n"
    "--- a/src/main/kotlin/com/x/RefundController.kt\n"
    "+++ b/src/main/kotlin/com/x/RefundController.kt\n"
    "@@ -1 +1,8 @@\n"
    " package com.x\n"
    "+data class RefundDto(val id: Long, var amount: Long)\n"
    "+@RestController\n"
    "+class RefundController {\n"
    "+    private val retryLimit: Int = 3\n"
    "+    fun getRefund(id: Long): RefundDto = service.find(id)\n"
    "+    suspend fun refund(orderId: Long) {}\n"
    "+}\n"
)

_SCALA_DIFF = (
    "diff --git a/src/main/scala/com/x/Order.scala b/src/main/scala/com/x/Order.scala\n"
    "--- a/src/main/scala/com/x/Order.scala\n"
    "+++ b/src/main/scala/com/x/Order.scala\n"
    "@@ -1 +1,6 @@\n"
    " package com.x\n"
    "+sealed trait OrderState\n"
    "+case class Order(id: Long)\n"
    "+object OrderRepo {\n"
    "+  val cache = Map.empty[Long, Order]\n"
    "+}\n"
)


def _profile_from_diff(diff: str):
    pr = PRMetadata(project="team/x")
    parsed = parse_diff(diff)
    return parsed, build_change_profile(pr, parsed)


def test_kotlin_language_module_and_symbols():
    parsed, profile = _profile_from_diff(_KOTLIN_DIFF)
    assert parsed[0].language == "kotlin"
    assert parsed[0].module == "com"  # src/main/kotlin 为布局目录
    names = {(s.kind, s.name) for s in profile.symbols}
    assert ("class", "RefundDto") in names       # data class
    assert ("class", "RefundController") in names
    assert ("method", "getRefund") in names       # fun
    assert ("method", "refund") in names          # suspend fun
    assert ("field", "retryLimit") in names       # val 属性


def test_scala_language_module_and_symbols():
    parsed, profile = _profile_from_diff(_SCALA_DIFF)
    assert parsed[0].language == "scala"
    assert parsed[0].module == "com"
    names = {(s.kind, s.name) for s in profile.symbols}
    assert ("class", "OrderState") in names   # sealed trait
    assert ("class", "Order") in names        # case class
    assert ("class", "OrderRepo") in names    # object
    assert ("field", "cache") in names        # val


def test_kotlin_data_class_is_data_model_change():
    _, profile = _profile_from_diff(_KOTLIN_DIFF)
    assert "DATA_MODEL_CHANGE" in {t.value for t in profile.change_types}


def test_unknown_language_still_degrades_to_file_level():
    """未识别语言（R5 未覆盖的 .rs）：不抽符号，仅文件级 + 关键词，降级行为不被破坏。"""
    diff = (
        "diff --git a/src/lib.rs b/src/lib.rs\n"
        "--- a/src/lib.rs\n"
        "+++ b/src/lib.rs\n"
        "@@ -1 +1,2 @@\n"
        " fn main() {}\n"
        "+fn helper() {}\n"
    )
    parsed, profile = _profile_from_diff(diff)
    assert parsed[0].language is None
    assert profile.symbols == []
    assert profile.changed_files == 1
