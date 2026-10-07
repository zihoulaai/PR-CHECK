"""detect_change_types 关键词规则测试。

关键词匹配跑在「小写化后的改动行全文」上，无法用词边界（camelCase 小写后粘连），
因此最容易出的问题是英文词误判。这里逐条钉住该命中与不该命中的写法。
"""
from __future__ import annotations

import pytest

from app.domain.enums import ChangeType, HighImpactFeature
from app.parser.base import LineChange, detect_change_types
from app.parser.diff_parser import get_language_parser

_RULE_TYPES = {
    ChangeType.AUTH_CHANGE, ChangeType.CACHE_CHANGE,
    ChangeType.TRANSACTION_CHANGE, ChangeType.SERIALIZATION_CHANGE,
}


def _detect(path: str, text: str, language: str | None = "java"):
    lines = [LineChange(text=text, change="added")]
    return detect_change_types(path, lines, get_language_parser(language), language)


def _hit(path: str, text: str) -> set[ChangeType]:
    ct, _ = _detect(path, text)
    return {c for c in ct if c in _RULE_TYPES}


# ===== 权限相关：不该被 author 家族误判 =====
@pytest.mark.parametrize("text", [
    "private String author;",
    "private String authorName;",
    "private List<String> authors;",
    "// authored by someone",
    "article.setAuthor(user.getName());",
    "private String authorship;",
])
def test_author_family_not_auth_change(text):
    """裸子串 "auth" 会命中 author/authored，把普通 POXO 判成权限变更。"""
    assert _hit("src/Order.java", text) == set(), text


@pytest.mark.parametrize("text", [
    "String authorization = header.get(\"Authorization\");",
    "if (!authService.check(token)) {",
    "boolean auth = login(token);",
    "String url = oauth.getToken();",
    "user.authenticate(password);",
    "@PreAuthorize(\"hasRole('ADMIN')\")",
    "@Secured(\"ROLE_ADMIN\")",
    "assertTrue(checkAuth(token));",
    "if (permissionService.grant(p, r)) {}",
    "需要权限校验",
])
def test_real_permission_signals_detected(text):
    assert ChangeType.AUTH_CHANGE in _hit("src/Api.java", text), text


# ===== 序列化：词干不能是 serial =====
@pytest.mark.parametrize("text", [
    "private static final long serialVersionUID = 1L;",
])
def test_serial_version_uid_not_serialization(text):
    """serialVersionUID 是「类实现了 Serializable」的样板，不是序列化行为变更。"""
    assert _hit("src/Ser.java", text) == set(), text


@pytest.mark.parametrize("text", [
    "byte[] b = serialize(obj);",
    "obj.writeObject(out); // serialize",
    "需要反序列化处理",
    "class Foo implements Serializable {",
])
def test_serialization_signals_detected(text):
    assert ChangeType.SERIALIZATION_CHANGE in _hit("src/Ser.java", text), text


# ===== 缓存 / 事务：camelCase 不能被词边界漏掉 =====
@pytest.mark.parametrize("text", ["cache.put(k, v);", "private Cache<String, Object> cache;"])
def test_cache_detected(text):
    assert ChangeType.CACHE_CHANGE in _hit("src/Cfg.java", text), text


@pytest.mark.parametrize("text", [
    "@Transactional", "transactionManager.commit();", "需要事务回滚",
])
def test_transaction_detected(text):
    assert ChangeType.TRANSACTION_CHANGE in _hit("src/Tx.java", text), text


def test_plain_code_hits_no_keyword_rule():
    assert _hit("src/T.java", "class T { }") == set()


# ===== 高影响特征必须与变更类型同步 =====
def test_high_impact_feature_paired_with_type():
    _, hi = _detect("src/Api.java", "authService.check(token);")
    assert HighImpactFeature.PERMISSION in hi

    _, hi2 = _detect("src/Order.java", "private String author;")
    assert HighImpactFeature.PERMISSION not in hi2


# ===== R5：Kotlin / Scala 复用同一套关键词规则 =====
@pytest.mark.parametrize("language,path", [("kotlin", "src/Tx.kt"), ("scala", "src/Tx.scala")])
def test_jvm_languages_share_keyword_rules(language, path):
    lines = [LineChange(text="@Transactional", change="added"),
             LineChange(text="cache.put(k, v)", change="added")]
    ct, hi = detect_change_types(path, lines, get_language_parser(language), language)
    assert ChangeType.TRANSACTION_CHANGE in ct
    assert ChangeType.CACHE_CHANGE in ct
    assert HighImpactFeature.TRANSACTION in hi


@pytest.mark.parametrize("language,path", [("kotlin", "src/A.kt"), ("scala", "src/A.scala")])
def test_jvm_author_family_still_not_auth(language, path):
    lines = [LineChange(text="private val author: String", change="added")]
    ct, _ = detect_change_types(path, lines, get_language_parser(language), language)
    assert ChangeType.AUTH_CHANGE not in ct


def test_jvm_api_annotation_marks_api_change():
    lines = [LineChange(text="@RestController", change="added")]
    for language, path in (("kotlin", "src/C.kt"), ("scala", "src/C.scala")):
        ct, hi = detect_change_types(path, lines, get_language_parser(language), language)
        assert ChangeType.API_CHANGE in ct
        assert HighImpactFeature.PUBLIC_API in hi
