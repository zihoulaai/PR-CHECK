"""变异检测：验证测试套件本身有牙齿。

逐个注入已知缺陷（破坏一处真实逻辑），跑三层验证，检查是否被抓到：

- pytest 层（强制闸门）：单元 / 集成测试是否失败。
- eval 层（回归报告）：tests/eval_harness.py 是否报 FAIL。
- mypy 层（静态闸门）：`mypy app` 是否报错。

为什么需要 mypy 这一层：有些「正确写法」是**运行期等价**的——例如
``KbDoc.id.in_(x)`` 与 ``col(KbDoc.id).in_(x)`` 行为完全一致，差别只在静态类型
（SQLModel 的字段在类型层就是普通 str）。这类改动 pytest 与 eval 原理上抓不到，
只有静态检查能覆盖。既然 mypy 已是 CI 卡门，把它纳入验证层才不算漏检。

为什么需要它：本项目早期存在过「353 个测试全绿、但 Java 符号抽取实际全错」
的状态——测试覆盖了调用链，却没有任何一条断言真正校验被调用的逻辑。
绿色本身不能证明测试有效，只有「注入缺陷后变红」能。

用法：
    uv run python tests/mutation_check.py            # 全部变异体
    uv run python tests/mutation_check.py --only 3   # 只跑第 3 个

退出码：0 = 全部注入缺陷都被抓到；1 = 有缺陷逃过所有验证层。
"""
from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_PY = sys.executable

# (标签, 文件, 原文, 替换为)
MUTANTS: list[tuple[str, str, str, str]] = [
    (
        "Java 类型声明捕获组退回关键字",
        "app/parser/java.py",
        'r"(?:class|interface|enum|record)\\s+([A-Za-z_]\\w*)"',
        'r"(class|interface|enum|record)\\s+([A-Za-z_]\\w*)"',
    ),
    (
        "路径词级匹配退回裸子串",
        "app/parser/base.py",
        "    words = path_words(path)\n    return any(p in words for p in patterns)",
        "    return path_has_any(path, patterns)",
    ),
    (
        "focus 过滤失效",
        "app/agent/kb_query.py",
        "        focus=sorted(focus),",
        "        focus=[],",
    ),
    (
        "伪造引用不降级（证据规则 3）",
        "app/agent/evidence.py",
        "    return kept, EvidenceLevel.C  # 规则 3：伪造引用，降级保留",
        "    return kept, item.evidence_level",
    ),
    (
        "类型错配不降级（证据规则 4）",
        "app/agent/evidence.py",
        "    if compatible:\n        return compatible, level\n"
        "    return refs, EvidenceLevel.C",
        "    return refs, level",
    ),
    (
        "summary 不清洗强结论词",
        "app/agent/evidence.py",
        '    out = _strip_strong_words(summary or "").strip()',
        '    out = (summary or "").strip()',
    ),
    (
        "N 级不补「无法判断」（证据规则 7）",
        "app/agent/evidence.py",
        "    if level == EvidenceLevel.N and UNKNOWN_MARKER not in out:",
        "    if False:",
    ),
    (
        "人工清单不限量",
        "app/agent/evidence.py",
        "        if len(out) >= _CHECKLIST_MAX_ITEMS:",
        "        if False:",
    ),
    (
        "降级计数恒为 0（meta 不可观测）",
        "app/agent/workflow.py",
        "    cleaned.meta.degraded_count = len(issues)",
        "    cleaned.meta.degraded_count = 0",
    ),
    (
        "过期文档过滤失效",
        "app/agent/workflow.py",
        "    return [h for h in hits if h.id not in stale]",
        "    return list(hits)",
    ),
    (
        "Dify 命中改回 segment id",
        "app/adapters/dify_kb.py",
        '                id=doc.get("id") or seg.get("id", ""),',
        '                id=seg.get("id", ""),',
    ),
    (
        "跨项目隔离失效（不按归属丢弃）",
        "app/adapters/dify_kb.py",
        "                if marked != query.project:",
        "                if False:",
    ),
    (
        "检索文本不守字符预算",
        "app/adapters/query_text.py",
        "        if out and used + cost > max_chars:",
        "        if False:",
    ),
    (
        "CJK 关键词抽取关闭",
        "app/parser/change_profile.py",
        "    for run in _CJK_RUN_RE.findall(text or \"\"):",
        "    for run in []:",
    ),
    (
        "模块排序退回字典序",
        "app/parser/change_profile.py",
        "        modules=_rank_modules(files, impact_by_module),",
        "        modules=sorted({f.module for f in files if f.module}),",
    ),
    (
        "模块排序忽略高影响权重",
        "app/parser/change_profile.py",
        "        key=lambda m: (-impact_by_module.get(m, 0),",
        "        key=lambda m: (0,",
    ),
    (
        "kb delete 护栏失效（无 --force 也放行）",
        "app/cli.py",
        "        if args.doc_id not in known and not forced:",
        "        if False:",
    ),
    (
        "kb delete 先删本地后删向量库（不可重试的中间态）",
        "app/cli.py",
        "        kb.delete(args.doc_id, project=project)\n"
        "        removed = delete_kb_doc(args.doc_id)",
        "        removed = delete_kb_doc(args.doc_id)\n"
        "        kb.delete(args.doc_id, project=project)",
    ),
    (
        "分库路由失效：未命中映射也返回结果（隔离形同虚设）",
        "app/adapters/routing_kb.py",
        "        kb = self._per_project.get(project)\n"
        "        if kb is None:",
        "        kb = (self._per_project.get(project)\n"
        "              or next(iter(self._per_project.values()), None))\n"
        "        if kb is None:",
    ),
    (
        "分库路由：delete 不校验 project（可能删错项目的库）",
        "app/adapters/routing_kb.py",
        "        if not project:\n            raise ValidationError(\n"
        "                \"按项目分库时删除必须指明所属项目",
        "        if False:\n            raise ValidationError(\n"
        "                \"按项目分库时删除必须指明所属项目",
    ),
    (
        "KB_DATASET_MAP 非法时静默回退单库（以为已隔离实际没有）",
        "app/config.py",
        "        try:\n            mapping = parse_dataset_map(self.kb_dataset_map or \"\")\n"
        "        except ValueError as exc:",
        "        try:\n            mapping = parse_dataset_map(self.kb_dataset_map)\n"
        "        except ValueError:\n            self.kb_dataset_map = None\n"
        "            mapping = {}",
    ),
    (
        "no_dataset 被并入 not_configured（运维无法发现漏建库）",
        "app/agent/workflow.py",
        "    if isinstance(container.kb, RoutingKB) and not container.kb.has_dataset(pr.project):",
        "    if False:",
    ),
    # ===== mypy 债务清零后的防退化 =====
    # 这几条守的是「让 mypy 通过的那些改动」：其中多条同时是真实行为
    # （形状校验、隔离、判定降级），去掉类型标注只是让检查变红，行为仍在；
    # 但若有人「顺手简化」掉 col() / json_items / 具名循环，
    # 这些变异体保证测试层也会跟着响，而不是静默退化。
    (
        "list_documents 不校验响应形状（对象被当成空列表）",
        "app/adapters/http_git.py",
        "    return data if isinstance(data, list) else []",
        "    return data or []",
    ),
    (
        "SQLModel 查询绕过 col()（静态认不出列类型）",
        "app/storage/repo.py",
        "        stmt = select(KbDoc).where(col(KbDoc.id).in_(id_set))",
        "        stmt = select(KbDoc).where(KbDoc.id.in_(id_set))",
    ),
    (
        "maas_kb 模块级重复定义同一符号（别名 + 兼容函数）",
        "app/adapters/maas_kb.py",
        "def _to_hits(data, project: str) -> list[KBHit]:  # pragma: no cover",
        "_to_hits = parse_hits\n\n\n"
        "def _to_hits(data, project: str) -> list[KBHit]:  # pragma: no cover",
    ),
    (
        "project_rules 强判定不降 unknown（violation 带着 C 级证据）",
        "app/agent/evidence.py",
        "        if verdict.value == \"violation\" and level not in _STRONG_LEVELS:",
        "        if False:",
    ),
    (
        "tech_debt 强判定不降 possible（闸门按 verdict 求值）",
        "app/agent/evidence.py",
        "        if debt_verdict.value in (\"direct_match\", \"related\") \\\n"
        "                and level not in _STRONG_LEVELS:",
        "        if False:",
    ),
]


def _run(cmd: list[str]) -> tuple[int, str]:
    r = subprocess.run(cmd, cwd=_ROOT, capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def _mypy_failures() -> int:
    """返回 mypy 报出的 error 数（0 = 通过）。"""
    rc, out = _run([sys.executable, "-m", "mypy", "app"])
    if rc == 0:
        return 0
    m = re.search(r"Found (\d+) errors?", out) or re.search(r"Success", out)
    if m and m.group(1).isdigit():
        return int(m.group(1))
    # 解析不到计数（配置错误 / 语法错误导致 mypy 提前退出）也算失败
    return 1 if rc != 0 else 0


def _eval_failures() -> str:
    rc, out = _run([_PY, "tests/eval_harness.py", "-q"])
    m = re.search(r"FAILURES\s+(\S+)", out)
    if m is None:
        return "?" if rc != 0 else "0"
    return m.group(1)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="变异检测：检查测试套件能否抓到注入的缺陷")
    ap.add_argument("--only", type=int, help="只跑指定序号的变异体（1 起）")
    ap.add_argument("--list", action="store_true", help="列出全部变异体后退出")
    args = ap.parse_args(argv)

    if args.list:
        for i, (label, *_rest) in enumerate(MUTANTS, 1):
            print(f"{i:3d}. {label}")
        return 0

    baseline = _mypy_failures()
    if baseline:
        print(f"基线 mypy app 已有 {baseline} 个 error，无法作为验证层——先修干净再跑。",
              file=sys.stderr)
        return 2

    targets = MUTANTS
    if args.only:
        if not 1 <= args.only <= len(MUTANTS):
            print(f"--only 需在 1..{len(MUTANTS)} 之间", file=sys.stderr)
            return 2
        targets = [MUTANTS[args.only - 1]]

    print("注入缺陷 → 检查是否被抓到")
    print(f"{'#':>3} {'注入的缺陷':34s} {'pytest':10s} {'eval':10s} {'mypy':10s}")
    print("-" * 74)

    escaped: list[str] = []
    skipped: list[str] = []
    for idx, (label, rel, old, new) in enumerate(targets, 1):
        path = _ROOT / rel
        original = path.read_text(encoding="utf-8")
        if original.count(old) != 1:
            print(f"{idx:3d} {label:34s} {'锚点不匹配':10s} {'-':10s}")
            skipped.append(label)
            continue
        path.write_text(original.replace(old, new), encoding="utf-8")
        try:
            pytest_caught = _run([_PY, "-m", "pytest", "-q", "-x", "--no-header",
                                  "-p", "no:cacheprovider"])[0] != 0
            fails = _eval_failures()
            eval_caught = fails not in ("0", "?")
            mypy_n = _mypy_failures()
            mypy_caught = mypy_n > 0
        finally:
            path.write_text(original, encoding="utf-8")

        pytest_mark = "抓到" if pytest_caught else "漏"
        eval_mark = f"抓到({fails})" if eval_caught else "漏"
        mypy_mark = f"抓到({mypy_n})" if mypy_caught else "漏"
        print(f"{idx:3d} {label:34s} {pytest_mark:10s} {eval_mark:10s} {mypy_mark:10s}")
        if not pytest_caught and not eval_caught and not mypy_caught:
            escaped.append(label)

    print()
    if skipped:
        print(f"锚点不匹配（代码已变，变异体需更新）：{skipped}")
    if escaped:
        print(f"逃过所有验证层的缺陷（{len(escaped)}）：{escaped}")
        print("→ 这些逻辑当前没有任何验证覆盖，需补断言或补静态标注。")
        return 1
    print("全部注入缺陷均被至少一层抓到。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
