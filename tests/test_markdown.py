"""Markdown 渲染测试：表格单元格消毒（`|` / 换行转义，P2-11）。

覆盖：_cell 转义语义、三段表格（doc_check / project_rules / tech_debt）在
含管道符与换行的 LLM 自由文本下仍保持列结构不撕裂、空 basis/advice 回落占位。
"""
from __future__ import annotations

from app.domain.enums import (DocCheckVerdict, EvidenceLevel, RiskLevel,
                              RuleVerdict, TechDebtVerdict)
from app.domain.schemas import (CheckReport, DocCheckItem, ReportMeta, RiskItem,
                                RuleItem, TechDebtItem)
from app.report.markdown import _cell, render_markdown
from app.report.plain import WIDTH, render_plain


def _report(**over) -> CheckReport:
    base = dict(meta=ReportMeta(project="team/order", model="fake"), summary="s")
    base.update(over)
    return CheckReport(**base)


def test_cell_escapes_pipe_and_flattens_newlines():
    assert _cell("a|b") == "a\\|b"
    assert _cell("line1\nline2") == "line1 line2"
    assert _cell("line1\r\nline2") == "line1 line2"
    assert _cell("line1\rline2") == "line1 line2"
    assert _cell(None) == ""


def _cols(row: str) -> int:
    """行内裸管道符数（先剔除转义序列 \\| 本身含有的 |）。"""
    return row.replace("\\|", "").count("|")


def test_doc_check_table_survives_pipe_and_newline():
    """item/basis/advice 含 | 与换行时：每行列数仍与表头一致。"""
    md = render_markdown(_report(doc_check=[
        DocCheckItem(item="接口 | 签名\n换行", verdict=DocCheckVerdict.UPDATE,
                     basis="依据 | 带\n管道", advice="建议\n第二行",
                     evidence_level=EvidenceLevel.A, source_refs=["kb-1"]),
    ]))
    table = [ln for ln in md.splitlines() if ln.startswith("|")]
    assert table, "应渲染表格"
    # 管道符被转义（\| 不含裸 |），换行被压平：每行裸列数与表头严格一致
    for row in table:
        assert _cols(row) == _cols(table[0]), f"行列数被破坏：{row!r}"
    assert "接口 \\| 签名 换行" in " ".join(table)


def test_rules_and_debt_tables_survive_pipe():
    report = _report(
        project_rules=[RuleItem(item="规范 | X", verdict=RuleVerdict.VIOLATION,
                                evidence_level=EvidenceLevel.B)],
        tech_debt=[TechDebtItem(item="债务 | Y", verdict=TechDebtVerdict.DIRECT_MATCH,
                                evidence_level=EvidenceLevel.C)],
    )
    md = render_markdown(report)
    rows = [ln for ln in md.splitlines() if ln.startswith("|")]
    assert any("规范 \\| X" in r for r in rows)
    assert any("债务 \\| Y" in r for r in rows)
    # 两个表各自列数一致（4 列 / 5 列），无撕裂行
    assert all(_cols(r) in (4, 5) for r in rows)


def test_empty_basis_and_advice_render_placeholder():
    md = render_markdown(_report(doc_check=[
        DocCheckItem(item="i", verdict=DocCheckVerdict.UNKNOWN,
                     evidence_level=EvidenceLevel.N),
    ]))
    rows = [ln for ln in md.splitlines() if ln.startswith("|")]
    assert rows[-1].count("—") >= 2  # basis/advice 占位


# ===== 纯文本渲染（无外部渲染器时的终端直读格式）=====

def test_plain_renders_seven_sections_without_markdown_syntax():
    txt = render_plain(_report(summary="变更摘要内容"))
    for i in range(1, 8):
        assert f"{i}. " in txt
    assert "变更摘要内容" in txt
    # 不含 Markdown 结构符号：标题井号、表格分隔、加粗、行内代码围栏
    assert "##" not in txt and "---" not in txt
    assert "**" not in txt and "`" not in txt


def test_plain_marks_violation_and_risk_levels():
    txt = render_plain(_report(
        risk=[RiskItem(level=RiskLevel.HIGH, text="SQL 注入",
                       location="a.py:10", evidence_level=EvidenceLevel.A,
                       source_refs=["kb-1"])],
        project_rules=[RuleItem(item="SQL 安全规范", verdict=RuleVerdict.VIOLATION,
                                evidence_level=EvidenceLevel.B, source_refs=["kb-1"])],
    ))
    assert "【高风险】" in txt and "a.py:10" in txt
    assert "✗ 发现疑似违反" in txt
    assert "[A] 已确认" in txt and "[kb-1]" in txt


def test_plain_flattens_multiline_free_text():
    """LLM 自由文本含换行：压平为一行片段，不破坏列表缩进。"""
    txt = render_plain(_report(doc_check=[
        DocCheckItem(item="接口", verdict=DocCheckVerdict.UPDATE,
                     basis="第一行\n第二行", advice="建议\n续行",
                     evidence_level=EvidenceLevel.C),
    ]))
    assert "依据：第一行 第二行" in txt
    assert "建议：建议 续行" in txt


def test_plain_wraps_long_lines_within_width():
    import unicodedata

    def cols(s):
        return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
                   for c in s)

    txt = render_plain(_report(summary="字" * 200))
    # 中文按 2 列计：折行以显示列宽为准（200 个汉字必然被切成多行）
    body = [ln for ln in txt.splitlines() if "字" in ln]
    assert len(body) >= 2
    assert all(cols(ln) <= WIDTH for ln in body)


def test_plain_empty_sections_have_human_placeholders():
    txt = render_plain(_report())
    assert "未识别到需要重点关注的风险" in txt
    assert "未做项目规范初检" in txt
