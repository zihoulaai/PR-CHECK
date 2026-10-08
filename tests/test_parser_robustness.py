"""Diff Parser 健壮性回归测试（对齐真实 git 输出）。

这些用例都是曾经的静默错误：解析不抛异常，但 path / 行数 / 文件数是错的。
"""
from __future__ import annotations

import pytest

from app.domain.schemas import PRMetadata
from app.parser.base import derive_module
from app.parser.change_profile import _classify_mode, select_focused_diff
from app.parser.diff_parser import parse_diff
from app.config import get_settings


def _block(path: str, body: str) -> str:
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -1,2 +1,3 @@\n x=1\n{body}")


# ===== 路径前缀剥离：不得吃掉路径中间的 a/ 与 b/ =====
@pytest.mark.parametrize("path", [
    "src/main/java/com/x/Foo.java",  # java/
    "data/config.json",               # data/
    "beta/service.ts",                # beta/
    "lib/foo.py",
    "app/adapters/base.py",
    "web/app.js",
    "a/b.py",
])
def test_strip_prefix_only_at_start(path):
    parsed = parse_diff(_block(path, "+x\n"))
    assert len(parsed) == 1
    assert parsed[0].path == path


def test_path_drives_module_and_language():
    parsed = parse_diff(_block("src/main/java/com/x/Foo.java", "+x\n"))
    assert parsed[0].module == "com"  # 跳过 src/main/java 布局前缀
    assert parsed[0].language == "java"


def test_path_with_spaces():
    d = ("diff --git a/my dir/my file.txt b/my dir/my file.txt\n"
         "--- a/my dir/my file.txt\n+++ b/my dir/my file.txt\n"
         "@@ -1 +1 @@\n-x\n+y\n")
    assert parse_diff(d)[0].path == "my dir/my file.txt"


# ===== hunk 内的 --- / +++ 不是文件头 =====
def test_hunk_content_lines_do_not_corrupt_path():
    """删除一行以 `--` 开头的 SQL 注释：diff 中呈现为 `--- 注释`。"""
    d = ("diff --git a/db.sql b/db.sql\n--- a/db.sql\n+++ b/db.sql\n"
         "@@ -1,3 +1,2 @@\n CREATE TABLE t(id INT);\n"
         "--- 说明\n SELECT 1;\n")
    pf = parse_diff(d)[0]
    assert pf.path == "db.sql"
    assert pf.additions == 0
    assert pf.deletions == 1  # 修复前被 `---` 前缀过滤掉，误判为 0


def test_hunk_added_line_starting_with_plus_plus():
    d = ("diff --git a/A.java b/A.java\n--- a/A.java\n+++ b/A.java\n"
         "@@ -1,2 +1,3 @@\n int x=1;\n+++ not a path\n")
    pf = parse_diff(d)[0]
    assert pf.path == "A.java"
    assert pf.additions == 1
    assert pf.deletions == 0


def test_multiple_hunks_counted():
    d = ("diff --git a/A.java b/A.java\n--- a/A.java\n+++ b/A.java\n"
         "@@ -1,2 +1,3 @@\n a\n+b\n c\n@@ -10,2 +11,3 @@\n x\n+y\n z\n")
    pf = parse_diff(d)[0]
    assert (pf.additions, pf.deletions) == (2, 0)


# ===== 无 hunk 的块不得被丢弃 =====
def test_binary_patch_block_parsed():
    """`GIT binary patch` 块没有 ---/+++ 也没有 hunk，修复前整块被丢弃。"""
    d = ("diff --git a/img.bin b/img.bin\n"
         "index ebc7d..b47c1 100644\nGIT binary patch\nliteral 13\n"
         "Ucmd<&WME`s{{N4Qmw\n")
    parsed = parse_diff(d)
    assert len(parsed) == 1
    assert parsed[0].path == "img.bin"
    assert parsed[0].status == "modified"
    assert parsed[0].additions == parsed[0].deletions == 0


def test_mode_only_block_parsed():
    d = ("diff --git a/script.sh b/script.sh\n"
         "old mode 100644\nnew mode 100755\n")
    parsed = parse_diff(d)
    assert len(parsed) == 1
    assert parsed[0].path == "script.sh"
    assert parsed[0].status == "modified"


def test_pure_rename_uses_new_path():
    d = ("diff --git a/keep.txt b/renamed.txt\n"
         "similarity index 100%\nrename from keep.txt\nrename to renamed.txt\n")
    pf = parse_diff(d)[0]
    assert pf.path == "renamed.txt"  # 修复前取的是旧路径
    assert pf.status == "renamed"


def test_rename_with_edit_uses_new_path():
    d = ("diff --git a/old/A.java b/new/B.java\n"
         "similarity index 85%\nrename from old/A.java\nrename to new/B.java\n"
         "--- a/old/A.java\n+++ b/new/B.java\n@@ -1 +1 @@\n-x\n+y\n")
    pf = parse_diff(d)[0]
    assert pf.path == "new/B.java"
    assert pf.status == "renamed"
    assert pf.additions == 1 and pf.deletions == 1


def test_all_real_git_block_shapes_parsed():
    """一次覆盖 git 会产生的全部块形态。"""
    d = (
        "diff --git a/a.txt b/a.txt\nindex f0f..7c3 100644\n--- a/a.txt\n+++ b/a.txt\n"
        "@@ -1,3 +1,3 @@\n l1\n-l2\n+CHANGED\n l3\n"
        "diff --git a/new.txt b/new.txt\nnew file mode 100644\nindex 000..abc\n"
        "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+hello\n"
        "diff --git a/gone.txt b/gone.txt\ndeleted file mode 100644\nindex abc..000\n"
        "--- a/gone.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-bye\n"
        "diff --git a/keep.txt b/renamed.txt\nsimilarity index 100%\n"
        "rename from keep.txt\nrename to renamed.txt\n"
        "diff --git a/img.bin b/img.bin\nindex a..b 100644\nGIT binary patch\nliteral 4\n"
        "diff --git a/s.sh b/s.sh\nold mode 100644\nnew mode 100755\n"
    )
    parsed = parse_diff(d)
    assert len(parsed) == 6
    by_path = {p.path: p for p in parsed}
    assert by_path["a.txt"].status == "modified"
    assert by_path["new.txt"].status == "added"
    assert by_path["gone.txt"].status == "deleted"
    assert by_path["renamed.txt"].status == "renamed"
    assert "img.bin" in by_path and "s.sh" in by_path


# ===== CRLF =====
def test_crlf_diff():
    d = ("diff --git a/a.py b/a.py\r\n--- a/a.py\r\n+++ b/a.py\r\n"
         "@@ -1 +1,2 @@\r\n+x=1\r\n")
    pf = parse_diff(d)[0]
    assert pf.path == "a.py"
    assert pf.additions == 1


# ===== 无可分析内容不应进 LLM =====
@pytest.mark.parametrize("n_files,n_lines", [
    (0, 0),   # 空 diff
    (2, 0),   # 仅二进制 / 权限变更
    (1, 0),   # 纯重命名
])
def test_no_analyzable_change_is_summary_only(n_files, n_lines):
    """修复前 0 文件 0 行被判为 full，会拿空输入调 LLM。"""
    assert _classify_mode(n_files, n_lines, get_settings()).value == "summary_only"


@pytest.mark.parametrize("n_files,n_lines,expected", [
    (1, 1, "full"),
    (20, 800, "full"),
    (21, 801, "focused"),
    (80, 3000, "focused"),
    (81, 3001, "summary_only"),
])
def test_mode_thresholds_unchanged(n_files, n_lines, expected):
    assert _classify_mode(n_files, n_lines, get_settings()).value == expected


def test_empty_diff_profile():
    prof = _profile("")
    assert prof.changed_files == 0
    assert prof.analysis_mode.value == "summary_only"


# ===== focused 模式必须真的裁剪 =====
def _medium_diff(high_impact_body: str, high_impact_path: str) -> str:
    parts = [_block(high_impact_path, high_impact_body)]
    for i in range(23):
        parts.append(_block(f"src/plain/P{i}.java", f"+int v{i} = {i};\n"))
    return "".join(parts)


@pytest.mark.parametrize("body,path", [
    ("+payload = serialize(obj);\n", "src/ser/Ser.java"),          # SERIALIZATION
    ("+cache.put(k, v);\n", "src/cache/C.java"),                   # CACHE
    ("+@GetMapping(\"/x\")\n", "src/api/Ctl.java"),                 # PUBLIC_API
    ("+@Transactional\n", "src/tx/T.java"),                         # TRANSACTION
])
def test_focused_trims_context(body, path):
    """任一高影响特征都不得让 focused 退化为把整个 diff 送进 LLM。"""
    d = _medium_diff(body, path)
    parsed = parse_diff(d)
    prof = _profile(d, parsed)
    assert prof.analysis_mode.value == "focused"
    focused = select_focused_diff(parsed, prof)
    assert focused.strip() != d.strip()
    assert path in focused
    assert len(focused) < len(d) * 0.5


def test_focus_features_cover_every_high_impact_feature():
    """_FOCUS_FEATURES 必须覆盖 HighImpactFeature 全部取值。

    漏掉任何一个，新增该特征时 focused 模式会静默退化为把整个 diff 送进 LLM。
    """
    from app.domain.enums import HighImpactFeature
    from app.parser.change_profile import _FOCUS_FEATURES

    assert set(HighImpactFeature) - _FOCUS_FEATURES == set()


def test_no_high_impact_keeps_all():
    """无任何高影响特征时保留全部是有意设计（避免空 context），不是缺陷。"""
    parts = [_block(f"src/plain/P{i}.java", f"+int v{i} = {i};\n")
             for i in range(23)]
    d = "".join(parts)
    parsed = parse_diff(d)
    prof = _profile(d, parsed)
    assert prof.high_impact_features == []
    assert select_focused_diff(parsed, prof).strip() == d.strip()


def _profile(diff_text: str, parsed=None):
    parsed = parse_diff(diff_text) if parsed is None else parsed
    from app.parser.change_profile import build_change_profile
    return build_change_profile(PRMetadata(project="p"), parsed)


def test_derive_module_skips_layout_prefixes():
    """src/ 布局的仓库 module 恒为 src 会让 KB modules 过滤与报告「涉及模块」失效。"""
    assert derive_module("src/main/java/com/x/Foo.java") == "com"
    assert derive_module("src/refund/RefundController.java") == "refund"
    assert derive_module("app/adapters/base.py") == "adapters"
    assert derive_module("packages/core/src/index.ts") == "core"
    assert derive_module("src/test/java/com/x/FooTest.java") == "com"


def test_derive_module_prefers_first_meaningful_segment():
    """core / common / server 等是真实模块名，不能被当成布局前缀跳过。"""
    # com 是 groupId 根，先于 core 被取到 —— 这是「取第一个有语义段」的直接结果
    assert derive_module("src/main/java/com/core/Order.java") == "com"
    assert derive_module("src/common/util.py") == "common"
    assert derive_module("src/server/Handler.java") == "server"


def test_derive_module_fallbacks():
    # 整条路径都是布局段：回退到第一级目录
    assert derive_module("lib/foo.py") == "lib"
    # 只有文件名：回退到文件名
    assert derive_module("README.md") == "README.md"
    assert derive_module("db.sql") == "db.sql"
    assert derive_module("") == ""
