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
