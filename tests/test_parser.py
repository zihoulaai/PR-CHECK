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


# ===== Java 符号名回归：捕获组必须是类型名而非 class/interface/enum 关键字 =====
_JAVA_DIFF = (
    "diff --git a/src/main/java/com/x/OrderService.java "
    "b/src/main/java/com/x/OrderService.java\n"
    "--- a/src/main/java/com/x/OrderService.java\n"
    "+++ b/src/main/java/com/x/OrderService.java\n"
    "@@ -1 +1,6 @@\n"
    "+@RestController\n"
    "+public class OrderService implements Serializable {\n"
    "+    private long id;\n"
    "+}\n"
    "+interface RefundPort { }\n"
    "+enum RefundState { PENDING, DONE }\n"
)


def test_java_symbol_names_are_type_names_not_keywords():
    """回归：_CLASS_RE 曾把关键字放在捕获组 1，导致所有 Java 类名都是字面量 "class"。

    extract_symbols 统一取 group(1) 作符号名并按 (kind, name) 去重，
    错误捕获组会让 Java 类名全部塌缩成一条 "class"，且不进入关键符号 / KB key_symbols。
    """
    parsed, profile = _profile_from_diff(_JAVA_DIFF)
    assert parsed[0].language == "java"
    names = {s.name for s in profile.symbols}
    assert {"OrderService", "RefundPort", "RefundState"} <= names
    # 关键字本身绝不能被当成符号名
    assert not names & {"class", "interface", "enum", "record"}
    # 去重按 (kind, name)：三个类型必须各自独立保留，不能被压成一条
    assert len([s for s in profile.symbols if s.kind == "class"]) == 3
    assert ("field", "id") in {(s.kind, s.name) for s in profile.symbols}


def test_java_symbols_reach_kb_key_symbols():
    """Java 类名必须真正进入 KB 查询的 key_symbols（这是符号抽取的唯一消费出口）。"""
    from app.agent.kb_query import build_kb_query

    pr = PRMetadata(project="pr-check", title="订单退款")
    parsed = parse_diff(_JAVA_DIFF)
    profile = build_change_profile(pr, parsed)
    q = build_kb_query(pr, profile)
    assert "OrderService" in q.key_symbols
    assert "RefundPort" in q.key_symbols
    assert "class" not in q.key_symbols


# ===== 方法抽取：同行方法体此前整条丢失 =====
_JAVA_METHODS = (
    "diff --git a/src/main/java/com/x/Refund.java b/src/main/java/com/x/Refund.java\n"
    "--- a/src/main/java/com/x/Refund.java\n"
    "+++ b/src/main/java/com/x/Refund.java\n"
    "@@ -1 +1,4 @@\n"
    "+public class Refund {\n"
    "+    public void settle() {}\n"
    "+    private long id;\n"
    "+}\n"
)

_TS_METHODS = (
    "diff --git a/src/order.ts b/src/order.ts\n"
    "--- a/src/order.ts\n"
    "+++ b/src/order.ts\n"
    "@@ -1 +1,4 @@\n"
    "+export class Order {\n"
    "+  refund(): void {}\n"
    "+  private id: number = 1;\n"
    "+}\n"
)


def test_java_method_with_inline_body_is_extracted():
    """回归：_METHOD_RE 尾部 `\\{?\\s*$` 匹配不了 `{}`，方法符号整条丢失。"""
    _, profile = _profile_from_diff(_JAVA_METHODS)
    names = {(s.kind, s.name) for s in profile.symbols}
    assert ("method", "settle") in names, names


def test_typescript_class_method_is_extracted():
    """TS 此前只有 class/interface/type/function/const-fn，类方法完全不进符号表。"""
    _, profile = _profile_from_diff(_TS_METHODS)
    names = {(s.kind, s.name) for s in profile.symbols}
    assert ("class", "Order") in names, names
    assert ("method", "refund") in names, names


def test_typescript_top_level_call_not_mistaken_for_method():
    """方法正则要求 2+ 缩进且带返回类型标注，裸调用不得被当成方法。"""
    diff = (
        "diff --git a/src/app.ts b/src/app.ts\n"
        "--- a/src/app.ts\n"
        "+++ b/src/app.ts\n"
        "@@ -1 +1,2 @@\n"
        "+const x = compute(1);\n"
        "+doWork();\n"
    )
    _, profile = _profile_from_diff(diff)
    methods = {s.name for s in profile.symbols if s.kind == "method"}
    assert not methods, methods


# ===== 中文关键词抽取 =====
#
# 回归：_TOKEN_RE 只认 ASCII，中文 PR 的标题 / 描述 / 中文路径段此前对关键词
# 的贡献恒为 0——等于把中文团队最强的一路检索信号排除在外。
_CN_DIFF = (
    "diff --git a/src/main/java/com/x/退款/退款控制器.java "
    "b/src/main/java/com/x/退款/退款控制器.java\n"
    "--- a/src/main/java/com/x/退款/退款控制器.java\n"
    "+++ b/src/main/java/com/x/退款/退款控制器.java\n"
    "@@ -1 +1,3 @@\n"
    "+public class 退款控制器 {\n"
    "+    public void refund() {}\n"
    "+}\n"
)


def _profile_with_pr(title: str, description: str, diff: str = _CN_DIFF):
    pr = PRMetadata(project="pr-check", title=title, description=description)
    parsed = parse_diff(diff)
    return pr, parsed, build_change_profile(pr, parsed)


def test_cjk_from_path_segments_extracted():
    """路径天然按 / 与 . 分段，中文路径段是中文项目最可靠的信号。"""
    _, _, profile = _profile_with_pr("", "")
    assert "退款" in profile.keywords
    assert "退款控制器" in profile.keywords


def test_cjk_from_title_and_description_extracted():
    """中文标题 / 描述此前产出 0 关键词。"""
    _, _, profile = _profile_with_pr(
        "订单退款链路重构", "修复重复请求导致的缓存脏读")
    kws = profile.keywords
    assert "订单退款链路重构" in kws
    assert "修复重复请求导致的缓存脏读" in kws
    # 必须真的进入 KB 查询，而不只是停留在 profile 上
    pr = PRMetadata(project="pr-check", title="订单退款链路重构",
                    description="修复重复请求导致的缓存脏读")
    from app.agent.kb_query import build_kb_query

    q = build_kb_query(pr, build_change_profile(pr, parse_diff(_CN_DIFF)))
    assert "订单退款链路重构" in q.keywords


def test_english_keywords_unchanged_by_cjk_support():
    """英文项目的关键词序列不得被 CJK 支持改动（纯 ASCII 输入不应产生任何变化）。"""
    diff = (
        "diff --git a/src/main/java/com/x/RefundService.java "
        "b/src/main/java/com/x/RefundService.java\n"
        "--- a/src/main/java/com/x/RefundService.java\n"
        "+++ b/src/main/java/com/x/RefundService.java\n"
        "@@ -1 +1,2 @@\n"
        "+public class RefundService {\n"
        "+}\n"
    )
    pr = PRMetadata(project="pr-check", title="fix refund cache invalidation",
                    description="handle duplicate request")
    profile = build_change_profile(pr, parse_diff(diff))
    assert profile.keywords == [
        "RefundService", "src", "main", "java", "com",
        "fix", "refund", "cache", "invalidation",
        "handle", "duplicate", "request",
    ]


def test_mixed_chinese_english_keeps_both():
    pr, _, profile = _profile_with_pr("重构 Refund 模块的 cacheKey 策略", "对齐 order 侧口径")
    kws = profile.keywords
    assert "重构" in kws
    assert "cacheKey" in kws and "order" in kws


def test_single_char_cjk_dropped():
    """单字片段信息量过低，不应占用关键词预算。"""
    from app.parser.change_profile import _cjk_tokens

    assert _cjk_tokens("A的B") == []          # 中文单字全部丢弃
    assert _cjk_tokens("退款缓存") == ["退款缓存"]


def test_cjk_tokens_per_source_capped():
    """长描述不得挤占关键词预算（无空格中文会切出大量片段）。"""
    from app.parser.change_profile import _cjk_tokens

    text = "。".join(f"第{i}段中文内容" for i in range(30))
    assert len(_cjk_tokens(text)) <= 6
    assert len(_cjk_tokens(text, limit=3)) == 3


def test_no_bigram_sliding_window():
    """不做 2 字滑窗：中文无空格，滑窗产出的是「与缓」「的缓」这类跨词垃圾。"""
    _, _, profile = _profile_with_pr("订单退款链路重构与缓存失效策略调整", "")
    kws = profile.keywords
    assert "订单退款链路重构与缓存失效策略调整" in kws
    for noise in ("与缓", "的缓", "失效策"):
        assert noise not in kws, noise


# ===== 模块按相关性排序（KBQuery.modules[:10] 会截断）=====
def _mk_diffs(spec: list[tuple[str, int, str]]) -> str:
    """构造 diff：spec = [(路径, 增删行数, 改动内容)]。"""
    out = []
    for path, n, body in spec:
        out.append(f"diff --git a/{path} b/{path}\n"
                   f"--- a/{path}\n+++ b/{path}\n@@ -1 +1,{n + 1} @@\n"
                   + "".join(f"+{body}\n" for _ in range(n)))
    return "".join(out)


def test_modules_ranked_by_relevance_not_alphabet():
    """高影响 + 改动量大的模块必须排在前面。"""
    spec = [(f"src/{name}/File.java", 1, "public class C {}") for name in
            ["aaa", "bbb", "ccc", "ddd"]]
    spec.append(("src/pay/RefundController.java", 40, "    public void refund() {}"))
    pr = PRMetadata(project="pr-check", title="退款链路")
    profile = build_change_profile(pr, parse_diff(_mk_diffs(spec)))
    assert profile.modules[0] == "pay", profile.modules
    # 字典序下 pay 恰好也在前面，故再验证一个字典序靠后的模块名
    spec2 = [(f"src/{name}/File.java", 1, "public class C {}") for name in
             ["aaa", "bbb", "ccc", "ddd", "eee", "fff", "ggg", "hhh", "iii", "jjj"]]
    spec2.append(("src/zpay/RefundController.java", 40, "    public void refund() {}"))
    profile2 = build_change_profile(pr, parse_diff(_mk_diffs(spec2)))
    assert profile2.modules[0] == "zpay", profile2.modules
    assert sorted(profile2.modules)[0] == "aaa"  # 字典序下 zpay 垫底


def test_relevant_module_survives_kb_query_truncation():
    """模块数超过 KBQuery 的 [:10] 截断时，最相关的模块必须活下来。

    这是排序改动的原因：字典序截断会把最有信息量的模块先扔掉。
    """
    spec = [(f"src/{name}/File.java", 1, "public class C {}") for name in
            ["aaa", "bbb", "ccc", "ddd", "eee", "fff", "ggg", "hhh", "iii", "jjj",
             "kkk", "lll"]]
    spec.append(("src/zpayment/RefundController.java", 50,
                 "    public void refund() {}"))
    pr = PRMetadata(project="pr-check", title="退款链路")
    profile = build_change_profile(pr, parse_diff(_mk_diffs(spec)))
    assert len(profile.modules) > 10

    from app.agent.kb_query import build_kb_query

    q = build_kb_query(pr, profile)
    assert len(q.modules) == 10
    assert "zpayment" in q.modules, q.modules
    # 字典序下 zpayment 会排在最后而被截掉
    assert sorted(profile.modules)[-1] == "zpayment"
    assert "zpayment" not in sorted(profile.modules)[:10]


def test_module_ranking_is_stable_across_runs():
    """同权重模块按名称排序，保证多次运行结果一致（报告 / 缓存键稳定性）。"""
    spec = [(f"src/{name}/File.java", 3, "public class C {}")
            for name in ["zeta", "alpha", "mid", "beta"]]
    pr = PRMetadata(project="pr-check", title="t")
    runs = {tuple(build_change_profile(pr, parse_diff(_mk_diffs(spec))).modules)
            for _ in range(3)}
    assert len(runs) == 1, runs
    assert runs.pop() == ("alpha", "beta", "mid", "zeta")


def test_files_without_module_are_skipped_in_ranking():
    """根目录单文件（无目录段）不得产出空模块名。"""
    diff = ("diff --git a/BUILD.bazel b/BUILD.bazel\n"
            "--- a/BUILD.bazel\n+++ b/BUILD.bazel\n@@ -1 +1,2 @@\n+rule\n")
    pr = PRMetadata(project="pr-check", title="t")
    profile = build_change_profile(pr, parse_diff(diff))
    assert all(m for m in profile.modules), profile.modules


def test_module_ranking_prioritises_impact_over_raw_size():
    """排序键优先级：命中高影响特征 > 变更行数 > 文件数。

    改动量小但引入公共 API 的模块，应排在「改动 200 行却无高影响特征」的模块之前
    —— 后者对规范 / 文档核查类检索没有增量信息。
    """
    spec = [
        ("src/utils/Helper.java", 200, "    private int x = 1;"),          # 无高影响特征
        ("src/pay/RefundController.java", 2, "    public void refund() {}"),  # PUBLIC_API
    ]
    pr = PRMetadata(project="pr-check", title="退款")
    profile = build_change_profile(pr, parse_diff(_mk_diffs(spec)))
    assert profile.high_impact_features, "前提：pay 应命中高影响特征"
    assert profile.modules == ["pay", "utils"], profile.modules
    # 字典序下 pay 仍在 utils 之前，故再验一次字典序垫底的场景
    spec2 = [
        ("src/autils/Helper.java", 200, "    private int x = 1;"),
        ("src/zpay/RefundController.java", 2, "    public void refund() {}"),
    ]
    profile2 = build_change_profile(pr, parse_diff(_mk_diffs(spec2)))
    assert profile2.modules == ["zpay", "autils"], profile2.modules
    assert sorted(profile2.modules) == ["autils", "zpay"]


def test_module_ranking_uses_line_count_when_impact_ties():
    """高影响权重持平时，变更行数更多的模块在前。"""
    spec = [
        ("src/aaa/Small.java", 1, "    public class C {}"),
        ("src/zzz/Big.java", 50, "    private int x = 1;"),
    ]
    pr = PRMetadata(project="pr-check", title="t")
    profile = build_change_profile(pr, parse_diff(_mk_diffs(spec)))
    assert profile.modules == ["zzz", "aaa"], profile.modules
