"""KBQuery Builder 测试（M2）：focus 推导、字段上限与查询文本。"""
from __future__ import annotations

from app.adapters.maas_kb import _build_query_text
from app.adapters.query_text import build_query_text as _canonical_query_text
from app.agent.kb_query import build_kb_query
from app.domain.enums import ChangeType, DocType
from app.domain.schemas import ChangeProfile, FileChange, PRMetadata, Symbol


def _profile(change_types, keywords=None):
    return ChangeProfile(
        changed_files=1, modules=["refund"], files=[FileChange(path="a.java", status="added")],
        symbols=[Symbol(name="RefundController", kind="class")],
        change_types=change_types, keywords=keywords or ["refund"],
    )


def test_focus_default_and_api():
    pr = PRMetadata(project="order-service", title="增加退款接口")
    q = build_kb_query(pr, _profile([ChangeType.API_CHANGE]))
    assert "development_rule" in q.focus
    assert "api_document" in q.focus
    assert q.project == "order-service"


def test_focus_values_are_all_valid_doc_types():
    """回归：focus 曾硬编码 "doc_sync"，它不在 DocType 的取值内。

    服务端按 doc_type 过滤时永远匹配不到该值，"or hits" 兜底又会把这个失效静默吞掉，
    等于 focus 过滤形同虚设。故 focus 必须全部是合法 DocType。
    """
    valid = {d.value for d in DocType}
    for types in ([ChangeType.API_CHANGE], [ChangeType.DATABASE_CHANGE],
                  [ChangeType.TEST_CHANGE], []):
        q = build_kb_query(PRMetadata(project="p", title="t"), _profile(types))
        assert q.focus, types
        assert set(q.focus) <= valid, (types, set(q.focus) - valid)
        assert "doc_sync" not in q.focus


def test_pure_test_change_keeps_focus_narrow():
    """纯测试 / 纯注释变更不属业务变更：不应把技术债与历史风险卷进检索范围。"""
    q = build_kb_query(PRMetadata(project="p", title="补测试"),
                       _profile([ChangeType.TEST_CHANGE]))
    assert "technical_debt" not in q.focus
    assert "historical_risk" not in q.focus


def test_focus_business_adds_debt_risk():
    pr = PRMetadata(project="order-service", title="改数据库")
    q = build_kb_query(pr, _profile([ChangeType.DATABASE_CHANGE]))
    assert "technical_debt" in q.focus
    assert "historical_risk" in q.focus


def test_field_caps():
    pr = PRMetadata(project="order-service", title="x" * 500)
    big_kw = [f"k{i}" for i in range(100)]
    q = build_kb_query(pr, _profile([ChangeType.API_CHANGE], keywords=big_kw))
    assert len(q.pr_title) <= 200
    assert len(q.keywords) <= 20
    assert len(q.key_files) <= 20
    assert len(q.key_symbols) <= 30


def test_change_type_file_lists_passed_through():
    """各变更类型的文件清单直接来自 ChangeProfile，不再按路径另做推断。"""
    prof = ChangeProfile(
        changed_files=4, modules=["refund"],
        files=[FileChange(path=f"src/refund/F{i}.java", status="modified") for i in range(4)],
        change_types=[ChangeType.API_CHANGE, ChangeType.CONFIG_CHANGE,
                      ChangeType.DEPENDENCY_CHANGE, ChangeType.LOGGING_CHANGE],
        api_changes=["src/refund/F0.java"],
        config_changes=["src/refund/F1.yml"],
        dependency_changes=["pom.xml"],
        logging_changes=["src/refund/F2.java"],
    )
    q = build_kb_query(PRMetadata(project="order-service"), prof)
    assert q.api_changes == ["src/refund/F0.java"]
    assert q.config_changes == ["src/refund/F1.yml"]
    assert q.dependency_changes == ["pom.xml"]
    assert q.logging_changes == ["src/refund/F2.java"]


def test_query_text_contains_change_type_file_lists():
    """KB 服务端只强制按 project 过滤，文件清单必须落到 query 文本里才有效。"""
    prof = ChangeProfile(
        changed_files=2, modules=["refund"],
        files=[FileChange(path="src/refund/RefundController.java", status="modified")],
        change_types=[ChangeType.API_CHANGE],
        api_changes=["src/refund/RefundController.java"],
        config_changes=["src/refund/application.yml"],
        dependency_changes=["pom.xml"],
        symbols=[Symbol(name="RefundController", kind="class")],
        keywords=["refund"],
    )
    q = build_kb_query(PRMetadata(project="order-service", title="退款接口"), prof)
    text = _build_query_text(q)
    assert "src/refund/RefundController.java" in text
    assert "src/refund/application.yml" in text
    assert "pom.xml" in text
    assert "RefundController" in text


def test_query_text_omits_empty_sections():
    text = _build_query_text(build_kb_query(
        PRMetadata(project="p", title="t"), _profile([ChangeType.API_CHANGE])))
    assert "依赖变更文件" not in text
    assert "日志变更文件" not in text
    assert "项目：p" in text


# ===== 检索文本的字符预算（Dify 上限 250）=====
_DIFY_MAX_CHARS = 248


def _big_query():
    """一个「预算必然被吃满」的真实规模 query：多模块 + 各类文件清单 + 大量符号。"""
    prof = ChangeProfile(
        changed_files=40,
        modules=[f"module{i}" for i in range(10)],
        files=[FileChange(path=f"src/main/java/com/x/F{i}.java", status="modified")
               for i in range(20)],
        change_types=[ChangeType.API_CHANGE, ChangeType.DATA_MODEL_CHANGE,
                      ChangeType.CONFIG_CHANGE, ChangeType.DEPENDENCY_CHANGE,
                      ChangeType.LOGGING_CHANGE],
        api_changes=[f"src/main/java/com/x/api/Api{i}.java" for i in range(10)],
        data_changes=[f"src/main/java/com/x/entity/Ent{i}.java" for i in range(10)],
        config_changes=[f"src/main/resources/application-{i}.yml" for i in range(10)],
        dependency_changes=["pom.xml", "build.gradle", "package.json"],
        logging_changes=[f"src/main/java/com/x/log/Log{i}.java" for i in range(10)],
        symbols=[Symbol(name=f"Symbol{i}", kind="class") for i in range(30)],
        keywords=[f"keyword{i}" for i in range(20)],
    )
    return build_kb_query(PRMetadata(
        project="pr-check", title="订单退款链路重构与缓存失效策略调整"), prof)


def test_query_text_respects_char_budget():
    """不变量：给定预算时返回值长度不超过预算（调用方无需再自行截断）。"""
    q = _big_query()
    for budget in (_DIFY_MAX_CHARS, 120, 80):
        text = _canonical_query_text(q, max_chars=budget)
        assert len(text) <= budget, (budget, len(text))


def test_budget_actually_drops_low_priority_parts():
    """长度断言不足以证明预算生效——尾部硬截断也能满足「不超过预算」。

    必须断言**内容**差异：低优先级段落（关键符号 / 关键词 / 后段文件清单）
    在预算内被整段丢弃，而无预算时全部存在。否则关掉预算装配、只留 `[:max_chars]`
    兜底也能让本文件全绿。
    """
    q = _big_query()
    full = _canonical_query_text(q)
    budgeted = _canonical_query_text(q, max_chars=_DIFY_MAX_CHARS)
    assert len(full) > _DIFY_MAX_CHARS * 2, "前提：无预算文本远超预算"
    for label in ("关键符号：", "关键词：", "日志变更文件："):
        assert label in full, f"前提：无预算时应包含 {label}"
        assert label not in budgeted, \
            f"预算内应整段丢弃 {label}，实际仍出现（预算装配未生效？）"


def test_budgeted_text_ends_at_part_boundary():
    """预算内必须是若干**完整**段落，不得出现被腰斩的半截段落。

    这是唯一能区分「按优先级装配 + 整段丢弃」与「拼完再硬截断」的断言——后者会
    在某个段落中间切开，留下「接口变更文件：sr」这类无意义的残片。

    参照物用同一套限额、但不裁剪的渲染（max_chars 极大 → 走预算限额分支而
    不丢任何段落），这样比对的是「丢了哪些段」而不是「限额不同导致的文本差异」。
    """
    q = _big_query()
    reference = _canonical_query_text(q, max_chars=100000)
    lines = reference.split("\n")
    whole_prefixes = {"\n".join(lines[:k]) for k in range(len(lines) + 1)}

    for budget in (_DIFY_MAX_CHARS, 200, 120):
        budgeted = _canonical_query_text(q, max_chars=budget)
        assert budgeted in whole_prefixes, \
            f"预算 {budget} 下的结果不是完整段落的前缀（疑似尾部硬截断）：\n{budgeted}"

    labels = ["项目", "重点查询", "模块", "PR", "变更类型",
              "接口变更文件", "数据模型变更文件", "配置变更文件",
              "依赖变更文件", "日志变更文件", "关键符号", "关键词"]
    budgeted = _canonical_query_text(q, max_chars=_DIFY_MAX_CHARS)
    for label in labels:
        if label in budgeted:
            assert f"{label}：" in budgeted, \
                f"标签「{label}」出现但没有完整内容——说明被硬截断腰斩：\n{budgeted}"


def test_focus_survives_dify_budget():
    """核心回归：focus 曾排在最后，在 Dify 的 248 字符下必然被整段截掉。

    focus 是唯一决定「查哪一类文档」的信号——它被截掉等于按类型聚焦检索从未生效。
    """
    q = _big_query()
    assert len(_build_query_text(q)) > _DIFY_MAX_CHARS   # 前提：无预算时确实超限
    text = _canonical_query_text(q, max_chars=_DIFY_MAX_CHARS)
    assert "重点查询：" in text, text
    for f in q.focus:
        assert f in text, f
    # 项目标识是检索的基本约束，永远保留
    assert text.startswith("项目：pr-check")


def test_focus_precedes_verbose_sections():
    """装配优先级：focus 必须排在模块 / 标题 / 文件清单之前。"""
    text = _canonical_query_text(_big_query(), max_chars=_DIFY_MAX_CHARS)
    assert text.index("重点查询：") < text.index("模块：")
    if "接口变更文件" in text:
        assert text.index("重点查询：") < text.index("接口变更文件")


def test_budgeted_query_truncates_file_lists_not_project():
    """预算内路径清单按条限额并标注总数，而不是让一条长清单吃满预算。"""
    text = _canonical_query_text(_big_query(), max_chars=_DIFY_MAX_CHARS)
    assert "等10个" in text, text
    assert text.count("src/main/java/com/x/api/Api") <= 3
