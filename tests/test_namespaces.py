"""互斥实验命名空间（experiment namespaces）测试。

覆盖：
- 创建 / 查询 / 整体更新 / 删除命名空间及其校验（区间重叠、引用缺失等 422，key 冲突 409）；
- 判定接入：当前版本判定按 namespace_key:user_id 稳定哈希到 0~9999 槽位，
  未命中成员区间返回兜底变体（decision_path="excluded"），命中继续原有判定链；
- 钉住 config_version 的判定不应用命名空间；
- 删除成员开关 / 移除兜底变体（更新、回滚、发布批次提交）返回 409；
- 命名空间更新不改写既有判定快照，同一 request_id 仍重放首次结果；
- 老库幂等建表迁移。
"""

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app import crud
from app import engine as eng


def _flag(client, flag_key, variants=None, rules=None):
    """创建 on/off 两变体开关（可覆盖），返回创建响应体。"""
    payload = {
        "flag_key": flag_key,
        "variants": variants
        or [
            {"key": "on", "weight": 50},
            {"key": "off", "weight": 50},
        ],
        "rules": rules or [],
    }
    resp = client.post("/api/v1/flags", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _member(flag_key, start, end, fallback="off"):
    return {
        "flag_key": flag_key,
        "slot_start": start,
        "slot_end": end,
        "fallback_variant_key": fallback,
    }


def _ns_payload(*, namespace_key="checkout_exp", members=None, description="互斥实验"):
    return {
        "namespace_key": namespace_key,
        "description": description,
        "members": members
        if members is not None
        else [
            _member("exp_a", 0, 5000),
            _member("exp_b", 5000, 10000),
        ],
    }


def _two_flags(client):
    _flag(client, "exp_a")
    _flag(client, "exp_b")


def _create_namespace(client, **kwargs):
    resp = client.post("/api/v1/namespaces", json=_ns_payload(**kwargs))
    assert resp.status_code == 201, resp.text
    return resp.json()


def _user_with_slot(namespace_key, start, end, *, inside=True):
    """确定性构造槽位落在（或不落在）[start, end) 内的 user_id。"""
    for i in range(100000):
        uid = f"slot-user-{i}"
        hit = start <= eng.namespace_slot(namespace_key, uid) < end
        if hit == inside:
            return uid
    raise AssertionError("未找到满足条件的 user_id")


def _evaluate(client, flag_key, **body):
    body.setdefault("user_id", "u1")
    body.setdefault("context", {})
    return client.post(f"/api/v1/flags/{flag_key}/evaluate", json=body)


# ---------------------------------------------------------------------------
# 创建 / 查询 / 更新 / 删除
# ---------------------------------------------------------------------------


def test_create_get_list_namespace(client):
    _two_flags(client)
    body = _create_namespace(client)
    assert body["namespace_key"] == "checkout_exp"
    assert body["description"] == "互斥实验"
    assert body["members"] == [
        {"flag_key": "exp_a", "slot_start": 0, "slot_end": 5000, "fallback_variant_key": "off"},
        {"flag_key": "exp_b", "slot_start": 5000, "slot_end": 10000, "fallback_variant_key": "off"},
    ]
    assert body["created_at"] is not None and body["updated_at"] is not None

    got = client.get("/api/v1/namespaces/checkout_exp")
    assert got.status_code == 200
    assert got.json()["members"] == body["members"]

    listed = client.get("/api/v1/namespaces").json()
    assert [ns["namespace_key"] for ns in listed] == ["checkout_exp"]


def test_create_namespace_duplicate_key_conflict(client):
    _two_flags(client)
    _create_namespace(client)
    resp = client.post("/api/v1/namespaces", json=_ns_payload())
    assert resp.status_code == 409
    assert "checkout_exp" in resp.json()["detail"]


def test_get_update_delete_missing_namespace_404(client):
    assert client.get("/api/v1/namespaces/ghost").status_code == 404
    resp = client.put(
        "/api/v1/namespaces/ghost",
        json={"members": [_member("exp_a", 0, 100)]},
    )
    assert resp.status_code == 404
    assert client.delete("/api/v1/namespaces/ghost").status_code == 404


def test_update_namespace_replaces_members_whole(client):
    _two_flags(client)
    _flag(client, "exp_c")
    _create_namespace(client)

    resp = client.put(
        "/api/v1/namespaces/checkout_exp",
        json={
            "description": "整体更新后",
            "members": [_member("exp_c", 0, 10000, "on")],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["description"] == "整体更新后"
    assert body["members"] == [
        {"flag_key": "exp_c", "slot_start": 0, "slot_end": 10000, "fallback_variant_key": "on"}
    ]
    got = client.get("/api/v1/namespaces/checkout_exp").json()
    assert got["members"] == body["members"]


def test_delete_namespace(client):
    _two_flags(client)
    _create_namespace(client)
    assert client.delete("/api/v1/namespaces/checkout_exp").status_code == 204
    assert client.get("/api/v1/namespaces/checkout_exp").status_code == 404
    assert client.get("/api/v1/namespaces").json() == []


# ---------------------------------------------------------------------------
# 创建 / 更新校验（422）
# ---------------------------------------------------------------------------


def test_members_required_and_range_shape(client):
    _two_flags(client)
    # 空成员列表
    resp = client.post("/api/v1/namespaces", json=_ns_payload(members=[]))
    assert resp.status_code == 422
    # slot_start 必须小于 slot_end
    resp = client.post(
        "/api/v1/namespaces", json=_ns_payload(members=[_member("exp_a", 5000, 5000)])
    )
    assert resp.status_code == 422
    assert "slot_start" in resp.text
    # 越界：end > 10000 / start < 0
    resp = client.post(
        "/api/v1/namespaces", json=_ns_payload(members=[_member("exp_a", 0, 10001)])
    )
    assert resp.status_code == 422
    resp = client.post(
        "/api/v1/namespaces", json=_ns_payload(members=[_member("exp_a", -1, 10)])
    )
    assert resp.status_code == 422


def test_overlapping_ranges_rejected(client):
    _two_flags(client)
    members = [_member("exp_a", 0, 6000), _member("exp_b", 5000, 10000)]
    resp = client.post("/api/v1/namespaces", json=_ns_payload(members=members))
    assert resp.status_code == 422
    assert "重叠" in resp.text

    # 相邻（end == start）不算重叠
    members = [_member("exp_a", 0, 5000), _member("exp_b", 5000, 10000)]
    resp = client.post("/api/v1/namespaces", json=_ns_payload(members=members))
    assert resp.status_code == 201, resp.text


def test_duplicate_flag_key_rejected(client):
    _two_flags(client)
    members = [_member("exp_a", 0, 100), _member("exp_a", 100, 200)]
    resp = client.post("/api/v1/namespaces", json=_ns_payload(members=members))
    assert resp.status_code == 422
    assert "重复" in resp.text


def test_unknown_member_flag_rejected(client):
    members = [_member("ghost_flag", 0, 100)]
    resp = client.post("/api/v1/namespaces", json=_ns_payload(members=members))
    assert resp.status_code == 422
    assert "ghost_flag" in resp.text


def test_unknown_fallback_variant_rejected(client):
    _two_flags(client)
    members = [_member("exp_a", 0, 100, fallback="ghost_variant")]
    resp = client.post("/api/v1/namespaces", json=_ns_payload(members=members))
    assert resp.status_code == 422
    assert "ghost_variant" in resp.text


def test_flag_in_at_most_one_namespace(client):
    _two_flags(client)
    _create_namespace(client, members=[_member("exp_a", 0, 10000)])

    # 新建命名空间引用已入组的开关 -> 422
    resp = client.post(
        "/api/v1/namespaces",
        json=_ns_payload(namespace_key="other_ns", members=[_member("exp_a", 0, 100)]),
    )
    assert resp.status_code == 422
    assert "checkout_exp" in resp.text

    # 更新另一个命名空间把已入组开关拉进来 -> 422
    _create_namespace(client, namespace_key="other_ns", members=[_member("exp_b", 0, 100)])
    resp = client.put(
        "/api/v1/namespaces/other_ns",
        json={"members": [_member("exp_a", 0, 100)]},
    )
    assert resp.status_code == 422
    assert "checkout_exp" in resp.text

    # 更新自身保留同一成员 -> 允许
    resp = client.put(
        "/api/v1/namespaces/checkout_exp",
        json={"members": [_member("exp_a", 0, 5000)]},
    )
    assert resp.status_code == 200, resp.text


def test_update_namespace_validates_members(client):
    _two_flags(client)
    _create_namespace(client)
    resp = client.put(
        "/api/v1/namespaces/checkout_exp",
        json={"members": [_member("exp_a", 0, 6000), _member("exp_b", 5000, 10000)]},
    )
    assert resp.status_code == 422
    # 校验失败不改动原配置
    got = client.get("/api/v1/namespaces/checkout_exp").json()
    assert got["members"][0]["slot_end"] == 5000


# ---------------------------------------------------------------------------
# 判定接入
# ---------------------------------------------------------------------------


def test_excluded_user_gets_fallback_variant(client):
    _two_flags(client)
    _create_namespace(client)
    uid = _user_with_slot("checkout_exp", 0, 5000, inside=False)

    resp = _evaluate(client, "exp_a", user_id=uid, context={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["decision_path"] == "excluded"
    assert body["variant_key"] == "off"  # 兜底变体
    assert body["matched_rule"] is None
    assert body["bucket_value"] is None
    steps = [e["step"] for e in body["explanation"]]
    assert steps == ["namespace_loaded", "namespace_excluded"]
    detail = body["explanation"][-1]["detail"]
    assert detail["namespace_key"] == "checkout_exp"
    assert detail["slot_value"] == eng.namespace_slot("checkout_exp", uid)
    assert detail["slot_range"] == [0, 5000]
    assert detail["fallback_variant_key"] == "off"


def test_included_user_continues_normal_chain(client):
    _flag(
        client,
        "exp_a",
        rules=[
            {
                "priority": 1,
                "attribute": "region",
                "operator": "eq",
                "values": ["cn"],
                "variant_key": "on",
            }
        ],
    )
    _flag(client, "exp_b")
    _create_namespace(client)
    uid = _user_with_slot("checkout_exp", 0, 5000, inside=True)

    # 规则命中
    body = _evaluate(client, "exp_a", user_id=uid, context={"region": "cn"}).json()
    assert body["decision_path"] == "rule"
    assert body["variant_key"] == "on"
    # 无规则上下文走分桶
    body = _evaluate(client, "exp_a", user_id=uid, context={}).json()
    assert body["decision_path"] == "bucket"
    assert body["variant_key"] in ("on", "off")


def test_excluded_skips_override_included_keeps_it(client):
    _two_flags(client)
    _create_namespace(client)
    excluded_uid = _user_with_slot("checkout_exp", 0, 5000, inside=False)
    included_uid = _user_with_slot("checkout_exp", 0, 5000, inside=True)

    for uid in (excluded_uid, included_uid):
        resp = client.put(
            f"/api/v1/flags/exp_a/overrides/{uid}",
            json={"variant_key": "on", "reason": "人工强制"},
        )
        assert resp.status_code == 200

    # 未入组：override 不生效，直接兜底
    body = _evaluate(client, "exp_a", user_id=excluded_uid).json()
    assert body["decision_path"] == "excluded"
    assert body["variant_key"] == "off"
    # 入组：override 正常生效
    body = _evaluate(client, "exp_a", user_id=included_uid).json()
    assert body["decision_path"] == "override"
    assert body["variant_key"] == "on"


def test_slot_assignment_is_stable(client):
    _two_flags(client)
    _create_namespace(client)
    uid = _user_with_slot("checkout_exp", 0, 5000, inside=False)
    first = _evaluate(client, "exp_a", user_id=uid).json()
    second = _evaluate(client, "exp_a", user_id=uid).json()
    assert first["decision_path"] == second["decision_path"] == "excluded"
    assert first["variant_key"] == second["variant_key"] == "off"
    assert first["request_id"] != second["request_id"]


def test_pinned_config_version_bypasses_namespace(client):
    _two_flags(client)
    _create_namespace(client)
    uid = _user_with_slot("checkout_exp", 0, 5000, inside=False)

    # 当前版本：未入组 -> excluded
    body = _evaluate(client, "exp_a", user_id=uid).json()
    assert body["decision_path"] == "excluded"

    # 钉住历史版本：不应用命名空间，走原有分桶
    pinned = _evaluate(client, "exp_a", user_id=uid, config_version=1).json()
    assert pinned["config_version"] == 1
    assert pinned["decision_path"] == "bucket"
    assert pinned["variant_key"] in ("on", "off")


def test_excluded_snapshot_persisted_and_queryable(client):
    _two_flags(client)
    _create_namespace(client)
    uid = _user_with_slot("checkout_exp", 0, 5000, inside=False)
    body = _evaluate(client, "exp_a", user_id=uid, request_id="ns-req-001").json()
    assert body["decision_path"] == "excluded"

    snap = client.get("/api/v1/decisions/ns-req-001").json()
    assert snap["decision_path"] == "excluded"
    assert snap["variant_key"] == "off"
    assert snap["bucket_value"] is None
    assert snap["matched_rule_index"] is None
    steps = [e["step"] for e in snap["explanation"]]
    assert steps == ["namespace_loaded", "namespace_excluded"]

    items = client.get("/api/v1/decisions", params={"flag_key": "exp_a"}).json()["items"]
    hit = next(i for i in items if i["request_id"] == "ns-req-001")
    assert hit["decision_path"] == "excluded"


def test_namespace_update_does_not_rewrite_snapshots_and_replays(client):
    _two_flags(client)
    _create_namespace(client)
    uid = _user_with_slot("checkout_exp", 0, 5000, inside=False)

    first = _evaluate(client, "exp_a", user_id=uid, request_id="ns-req-002").json()
    assert first["decision_path"] == "excluded"

    # 整体更新命名空间：exp_a 区间扩为全量，同一用户现在应入组
    resp = client.put(
        "/api/v1/namespaces/checkout_exp",
        json={"members": [_member("exp_a", 0, 10000)]},
    )
    assert resp.status_code == 200

    # 同一 request_id 仍重放首次结果（不重新判定）
    replay = _evaluate(client, "exp_a", user_id=uid, request_id="ns-req-002").json()
    assert replay["decision_path"] == "excluded"
    assert replay["variant_key"] == "off"
    assert replay["explanation"] == first["explanation"]

    # 既有快照不被改写
    snap = client.get("/api/v1/decisions/ns-req-002").json()
    assert snap["decision_path"] == "excluded"
    assert snap["variant_key"] == "off"

    # 新的 request_id 按更新后的命名空间判定：入组走正常分桶
    fresh = _evaluate(client, "exp_a", user_id=uid).json()
    assert fresh["decision_path"] == "bucket"


def test_delete_namespace_removes_gate(client):
    _two_flags(client)
    _create_namespace(client)
    uid = _user_with_slot("checkout_exp", 0, 5000, inside=False)
    assert _evaluate(client, "exp_a", user_id=uid).json()["decision_path"] == "excluded"

    assert client.delete("/api/v1/namespaces/checkout_exp").status_code == 204
    body = _evaluate(client, "exp_a", user_id=uid).json()
    assert body["decision_path"] == "bucket"


def test_flag_without_namespace_unaffected(client, simple_flag):
    # simple_flag 不属于任何命名空间：原有语义不变
    body = _evaluate(client, "btn_color", user_id="u9", context={}).json()
    assert body["decision_path"] == "bucket"


# ---------------------------------------------------------------------------
# 成员保护：删除开关 / 移除兜底变体 -> 409
# ---------------------------------------------------------------------------


def test_delete_member_flag_conflict(client):
    _two_flags(client)
    _create_namespace(client)
    resp = client.delete("/api/v1/flags/exp_a")
    assert resp.status_code == 409
    assert "checkout_exp" in resp.json()["detail"]
    # 开关未被删除
    assert client.get("/api/v1/flags/exp_a").status_code == 200

    # 非成员开关可正常删除
    _flag(client, "free_flag")
    assert client.delete("/api/v1/flags/free_flag").status_code == 204


def test_update_flag_removing_fallback_variant_conflict(client):
    _two_flags(client)
    _create_namespace(client)
    resp = client.post(
        "/api/v1/flags",
        json={
            "flag_key": "exp_a",
            "variants": [
                {"key": "on", "weight": 50},
                {"key": "standby", "weight": 50},
            ],
            "rules": [],
        },
    )
    assert resp.status_code == 409
    assert "off" in resp.json()["detail"]
    # 配置未被推进
    assert client.get("/api/v1/flags/exp_a").json()["version"] == 1

    # 保留兜底变体的更新正常
    resp = client.post(
        "/api/v1/flags",
        json={
            "flag_key": "exp_a",
            "variants": [
                {"key": "on", "weight": 50},
                {"key": "off", "weight": 50},
            ],
            "rules": [],
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["version"] == 2


def test_rollback_removing_fallback_variant_conflict(client):
    # v1: on/standby（不含 off）
    _flag(
        client,
        "exp_c",
        variants=[
            {"key": "on", "weight": 50},
            {"key": "standby", "weight": 50},
        ],
    )
    # v2: on/off（当前配置含兜底 off）
    resp = client.post(
        "/api/v1/flags",
        json={
            "flag_key": "exp_c",
            "variants": [
                {"key": "on", "weight": 50},
                {"key": "off", "weight": 50},
            ],
            "rules": [],
        },
    )
    assert resp.status_code == 201
    _create_namespace(client, members=[_member("exp_c", 0, 10000)])

    # 回滚到 v1（不含兜底 off）-> 409，当前版本不推进
    resp = client.post(
        "/api/v1/flags/exp_c/rollback",
        json={"target_version": 1, "expected_current_version": 2},
    )
    assert resp.status_code == 409
    assert "off" in resp.json()["detail"]
    assert client.get("/api/v1/flags/exp_c").json()["version"] == 2


def test_release_commit_removing_fallback_variant_conflict(client):
    _two_flags(client)
    _create_namespace(client)
    resp = client.post(
        "/api/v1/releases",
        json={
            "candidates": [
                {
                    "flag_key": "exp_a",
                    "expected_version": 1,
                    "variants": [
                        {"key": "on", "weight": 50},
                        {"key": "standby", "weight": 50},
                    ],
                    "rules": [],
                }
            ],
            "users": [{"user_id": "u1", "context": {}}],
        },
    )
    assert resp.status_code == 201, resp.text
    release_id = resp.json()["release_id"]

    resp = client.post(f"/api/v1/releases/{release_id}/commit")
    assert resp.status_code == 409
    assert "off" in resp.json()["detail"]
    # 整批回滚：版本未推进
    assert client.get("/api/v1/flags/exp_a").json()["version"] == 1
    assert client.get(f"/api/v1/releases/{release_id}").json()["status"] == "pending"


def test_removing_membership_unlocks_flag_changes(client):
    _two_flags(client)
    _create_namespace(client, members=[_member("exp_a", 0, 10000)])

    # 整体更新命名空间移除 exp_a 后，开关可自由更新 / 删除
    resp = client.put(
        "/api/v1/namespaces/checkout_exp",
        json={"members": [_member("exp_b", 0, 10000)]},
    )
    assert resp.status_code == 200
    resp = client.post(
        "/api/v1/flags",
        json={
            "flag_key": "exp_a",
            "variants": [
                {"key": "on", "weight": 50},
                {"key": "standby", "weight": 50},
            ],
            "rules": [],
        },
    )
    assert resp.status_code == 201
    assert client.delete("/api/v1/flags/exp_a").status_code == 204


# ---------------------------------------------------------------------------
# 引擎级单测
# ---------------------------------------------------------------------------


def test_namespace_slot_stable_and_in_range():
    for uid in ("alice", "bob", "user-1001", "张三"):
        slot = eng.namespace_slot("checkout_exp", uid)
        assert 0 <= slot < 10000
        assert slot == eng.namespace_slot("checkout_exp", uid)


def test_namespace_slot_distribution_covers_space():
    slots = {eng.namespace_slot("dist_ns", f"u{i}") for i in range(2000)}
    assert min(slots) < 1000
    assert max(slots) >= 9000


def test_excluded_decision_shape():
    member = eng.NamespaceMember(
        flag_key="exp_a", slot_start=0, slot_end=2500, fallback_variant_key="off"
    )
    decision = eng.excluded_decision("checkout_exp", member, "u1", 7000)
    assert decision.variant_key == "off"
    assert decision.decision_path == "excluded"
    assert decision.bucket_value is None
    assert decision.matched_rule is None
    assert decision.matched_rule_index is None
    steps = [e["step"] for e in decision.explanation]
    assert steps == ["namespace_loaded", "namespace_excluded"]
    detail = decision.explanation[-1]["detail"]
    assert detail["slot_value"] == 7000
    assert detail["slot_range"] == [0, 2500]
    assert detail["hash_input"] == "checkout_exp:u1"


# ---------------------------------------------------------------------------
# 老库幂等迁移
# ---------------------------------------------------------------------------


@pytest.fixture()
def old_schema_db(tmp_path):
    """模拟本功能上线前的老库：没有 experiment_namespaces 表。"""
    db_file = tmp_path / "old.db"
    engine = create_engine(f"sqlite:///{db_file}", future=True)
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE flag_configs ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "flag_key VARCHAR(128) NOT NULL,"
                "description VARCHAR(512) NOT NULL,"
                "rules_json TEXT NOT NULL,"
                "variants_json TEXT NOT NULL,"
                "version INTEGER NOT NULL,"
                "created_at DATETIME,"
                "updated_at DATETIME)"
            )
        )
    Session = sessionmaker(bind=engine, future=True)
    yield Session
    engine.dispose()


def test_ensure_namespaces_table_migrates_old_db(old_schema_db):
    db = old_schema_db()
    try:
        crud.ensure_namespaces_table(db)
        # 幂等：重复执行不报错
        crud.ensure_namespaces_table(db)
        tables = {
            row[0]
            for row in db.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
        assert "experiment_namespaces" in tables
        # 老库迁移后命名空间查询为空而非报错
        assert crud.list_namespaces(db) == []
        assert crud.get_membership_for_flag(db, "legacy") is None
    finally:
        db.close()
