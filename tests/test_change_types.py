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


# ===== 路径词级匹配：短 pattern 不得跨词命中 =====
#
# _MODEL_PATTERNS 含 2 字符 vo / dao，_TEST_PATTERNS 含 test / spec，
# 走裸子串匹配会确定性误判（evo-lution 含 vo、a-void 含 vo、la-test 含 test）。
# 误判沿 change_types -> KB focus -> 报告「涉及模块/文档核查」整条链路放大。
_PATH_FALSE_POSITIVE_PATHS = [
    "src/evolution/Service.java",      # evolution 内含 vo
    "src/avoid/x.java",                # avoid 内含 vo
    "docs/latest.md",                  # latest 内含 test
    "app/latest_service.py",           # latest 内含 test
    "src/manifest/Build.java",         # manifest 内含 test
    "src/genesis/Init.go",             # genesis 内含 ?（回归防护）
    "src/devtools/Profiler.java",      # dev + tools，无 data-model 词
    "src/main/java/com/x/service/RefundService.java",  # 普通业务文件
]


@pytest.mark.parametrize("path", _PATH_FALSE_POSITIVE_PATHS)
def test_path_words_no_cross_word_false_positive(path):
    """路径不应被判成数据模型/测试变更。"""
    language = "java" if path.endswith(".java") else (
        "go" if path.endswith(".go") else ("python" if path.endswith(".py") else None))
    ct, _ = _detect(path, "line one", language=language)
    assert ChangeType.DATA_MODEL_CHANGE not in ct, path
    assert ChangeType.TEST_CHANGE not in ct, path


@pytest.mark.parametrize("path", [
    # 数据模型：目录段命中 + 驼峰复合文件名命中 + 复数目录命中
    "src/main/java/com/x/vo/UserVO.java",
    "src/main/java/com/x/dto/ReqVO.java",
    "src/main/java/com/x/models/Order.java",
    "src/main/java/com/x/entity/User.java",
    "app/models/order.py",
    "app/domain/Order.java",
])
def test_data_model_path_positives_still_hit(path):
    """词级匹配不得漏掉真实的数据模型路径（含驼峰复合名与复数目录）。"""
    language = "java" if path.endswith(".java") else "python"
    ct, _ = _detect(path, "line one", language=language)
    assert ChangeType.DATA_MODEL_CHANGE in ct, path


@pytest.mark.parametrize("path", [
    "src/main/java/com/x/controller/OrderController.java",  # controller 目录段
    "app/controllers/OrderController.java",               # 复数目录 + 驼峰复合名
    "api/handlers/refund.go",
    "app/api/router.ts",                                 # /api/ 路径形态
    "pkg/handlers/worker.go",
])
def test_api_path_positives_still_hit(path):
    """controller/router/handler/endpoint 改词级后不得漏判。"""
    language = {"java": "java", "go": "go", "ts": "typescript"}[path.rsplit(".", 1)[-1]]
    ct, hi = _detect(path, "line one", language=language)
    assert ChangeType.API_CHANGE in ct, path
    assert HighImpactFeature.PUBLIC_API in hi, path


@pytest.mark.parametrize("path", [
    "src/test/java/com/x/FooTest.java",
    "tests/test_parser.py",
    "docs/spec/api.md",
    "app/__tests__/order.ts",
])
def test_test_path_positives_still_hit(path):
    """test/tests/spec/__test__ 目录与文件命中不被词级匹配破坏。"""
    ct, _ = _detect(path, "line one", language="java")
    assert ChangeType.TEST_CHANGE in ct, path


def test_path_words_handles_plural_and_camel():
    """词切分的两个关键形态：复数归一 + 驼峰切分。"""
    from app.parser.base import path_words

    assert "controller" in path_words("app/controllers/OrderController.java")
    assert "test" in path_words("src/test/java/FooTest.java")
    assert "vo" in path_words("src/vo/UserVO.java")
    assert "model" in path_words("app/models/order.py")
    # 跨词不得产生词
    assert "vo" not in path_words("src/evolution/Service.java")
    assert "test" not in path_words("docs/latest.md")


# ===== 并发特征：HighImpactFeature.CONCURRENCY 的可达性 =====
#
# 该取值此前定义在枚举里、也映射了 checklist 项「并发安全（竞态、死锁）」，
# 但没有任何规则产出它——这条最该在并发改动时出现的提醒永远不可能出现。
@pytest.mark.parametrize("text,language", [
    ("public synchronized void run() {}", "java"),
    ("AtomicInteger count = new AtomicInteger(0);", "java"),
    ("private final ReentrantLock lock = new ReentrantLock();", "java"),
    ("Map<Long, Long> m = new ConcurrentHashMap<>();", "java"),
    ("var wg = new sync.WaitGroup();", "go"),
    ("lock := sync.Mutex{}", "go"),
    ("async def handle(self):", "python"),
    ("await client.fetch(url)", "python"),
    ("export async function load() {}", "typescript"),
    ("with threading.Lock():", "python"),
    ("// 修复并发导致的竞态问题", "java"),
    ("// 存在死锁风险", "java"),
    ("// 改为乐观锁更新", "java"),
])
def test_concurrency_feature_is_reachable(text, language):
    """并发改动必须产出 CONCURRENCY 高影响特征。"""
    lines = [LineChange(text=text, change="added")]
    ct, hi = detect_change_types(f"src/X.{language}", lines,
                                get_language_parser(language), language)
    assert HighImpactFeature.CONCURRENCY in hi, text


@pytest.mark.parametrize("text", [
    "// update blockchain block number",   # blockchain 含 block
    "// this handles concurrent users gracefully",   # 英文散文里的 concurrent
    "// 事务回滚后重试",                   # 非并发
])
def test_concurrency_not_fired_by_ordinary_text(text):
    lines = [LineChange(text=text, change="added")]
    _, hi = detect_change_types("src/X.java", lines, get_language_parser("java"), "java")
    assert HighImpactFeature.CONCURRENCY not in hi, text


def test_concurrency_reaches_manual_checklist():
    """CONCURRENCY 必须真的出现在人工清单里——否则只是枚举里的死值。"""
    from app.agent.workflow import build_checklist
    from app.domain.schemas import ChangeProfile

    profile = ChangeProfile(changed_files=1, high_impact_features=[
        HighImpactFeature.CONCURRENCY])
    checklist = build_checklist(profile)
    assert any("并发" in item for item in checklist), checklist
