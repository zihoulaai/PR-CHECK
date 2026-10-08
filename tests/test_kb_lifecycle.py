"""R1 KB 生命周期闭环：kb suggest / kb verify（只读，不触向量库）。"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from app.cli import main


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def _make_repo(tmp_path: Path) -> Path:
    """建一个含 main 分支的最小仓库（src/pay/RefundService.java）。"""
    repo = tmp_path / "repo"
    (repo / "src" / "pay").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git(repo, "config", "user.email", "a@b.c")
    _git(repo, "config", "user.name", "t")
    (repo / "src" / "pay" / "RefundService.java").write_text(
        "public class RefundService { void refund(){} }\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")
    _git(repo, "branch", "-M", "main")
    return repo


def _upload(tmp_path: Path, name: str, content: str, *, doc_type: str,
            module: str, title: str, capsys) -> None:
    f = tmp_path / name
    f.write_text(content, encoding="utf-8")
    rc = main(["kb", "upload", "--file", str(f), "--project", "p",
               "--doc-type", doc_type, "--module", module, "--title", title])
    assert rc == 0
    capsys.readouterr()


# ===== kb verify =====
def test_kb_verify_flags_stale_and_drift(container, capsys, tmp_path):
    """全无命中 → stale；部分命中 → drift。"""
    repo = _make_repo(tmp_path)
    _upload(tmp_path, "a.md", "`RefundService` `cancelRefund` `LegacyRefundApi`\n",
            doc_type="api_document", module="pay", title="退款接口", capsys=capsys)
    _upload(tmp_path, "b.md", "`ZombieHandler` `GhostApi`\n",
            doc_type="development_rule", module="pay", title="僵尸规范", capsys=capsys)

    assert main(["kb", "verify", "--repo", str(repo), "--project", "p"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["checked"] == 2
    assert {s["title"] for s in out["stale_suspects"]} == {"僵尸规范"}
    assert {d["title"] for d in out["drift_suspects"]} == {"退款接口"}
    # drift 记录给出未命中符号，便于人工定位
    assert "LegacyRefundApi" in out["drift_suspects"][0]["missing"]


def test_kb_verify_ignores_non_target_doc_types(container, capsys, tmp_path):
    """只校验 api_document / development_rule，技术债务类不参与。"""
    repo = _make_repo(tmp_path)
    _upload(tmp_path, "d.md", "`GhostApi`\n", doc_type="technical_debt",
            module="pay", title="债务", capsys=capsys)
    assert main(["kb", "verify", "--repo", str(repo), "--project", "p"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["checked"] == 0


def test_kb_verify_missing_repo_is_invalid_request(container, capsys):
    assert main(["kb", "verify", "--repo", "/no/such/dir/xyz"]) == 2
    body = json.loads(capsys.readouterr().out)
    assert body["error"]["code"] == "INVALID_REQUEST"


# ===== kb suggest =====
def test_kb_suggest_reports_affected_docs_and_gaps(container, capsys, tmp_path):
    repo = _make_repo(tmp_path)
    # feat 分支：改 pay，新增 inventory
    _git(repo, "checkout", "-q", "-b", "feat")
    (repo / "src" / "pay" / "RefundService.java").write_text(
        "public class RefundService { void refund(){} void cancelRefund(){} }\n",
        encoding="utf-8")
    (repo / "src" / "inventory").mkdir(parents=True)
    (repo / "src" / "inventory" / "InventoryService.java").write_text(
        "public class InventoryService {}\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "feat")

    _upload(tmp_path, "a.md", "`RefundService`\n", doc_type="api_document",
            module="pay", title="退款接口", capsys=capsys)

    assert main(["kb", "suggest", "--repo", str(repo), "--base", "main",
                 "--project", "p"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out["changed_modules"]) == {"pay", "inventory"}
    assert any(d["module"] == "pay" for d in out["affected_docs"])
    assert out["coverage_gaps"] == ["inventory"]  # 无对应文档 → 覆盖缺口


def test_kb_suggest_missing_repo_is_invalid_request(container, capsys):
    assert main(["kb", "suggest", "--repo", "/no/such/dir/xyz",
                 "--project", "p"]) == 2


# ===== 只读命令不依赖向量库适配器 =====
def test_kb_readonly_commands_work_without_kb_adapter(container, capsys, tmp_path):
    """未配置知识库（container.kb is None）时，verify/suggest 仍可用（只读本地元数据）。"""
    container.kb = None
    repo = _make_repo(tmp_path)
    assert main(["kb", "verify", "--repo", str(repo), "--project", "p"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["checked"] == 0
    assert out["stale_suspects"] == []
