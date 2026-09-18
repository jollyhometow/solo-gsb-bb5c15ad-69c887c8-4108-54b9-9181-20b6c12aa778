"""POST /api/v1/exposures 与 GET /api/v1/flags/{flag_key}/exposure-summary 测试。"""

from datetime import datetime, timedelta

from app.database import SessionLocal
from app.models import DecisionSnapshot

BASE = datetime(2026, 9, 17, 8, 0, 0)


def _evaluate(client, flag_key="btn_color", user_id="u1", request_id=None, context=None):
    body = {"user_id": user_id, "context": context or {}}
    if request_id:
        body["request_id"] = request_id
    resp = client.post(f"/api/v1/flags/{flag_key}/evaluate", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _expose(client, exposure_id, request_id):
    return client.post(
        "/api/v1/exposures",
        json={"exposure_id": exposure_id, "request_id": request_id},
    )


def _insert_snapshot(
    *,
    request_id: str,
    flag_key: str,
    user_id: str = "u1",
    variant_key: str = "red",
    decided_at: datetime,
    config_version: int = 1,
):
    """直接落一条判定快照（不依赖开关存在），用于控制 decided_at / 版本。"""
    db = SessionLocal()
    try:
        db.add(
            DecisionSnapshot(
                request_id=request_id,
                flag_key=flag_key,
                user_id=user_id,
                request_context_json="{}",
                config_version=config_version,
                bucket_value=None,
                matched_rule_index=None,
                variant_key=variant_key,
                explanation_json="[]",
                decided_at=decided_at,
            )
        )
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# POST /api/v1/exposures
# ---------------------------------------------------------------------------


def test_create_exposure_freezes_snapshot_fields(client, simple_flag):
    dec = _evaluate(client, user_id="u1", request_id="req-1", context={"region": "cn"})
    assert dec["variant_key"] == "red"  # region=cn 命中规则

    resp = _expose(client, "exp-1", "req-1")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["exposure_id"] == "exp-1"
    assert body["request_id"] == "req-1"
    assert body["flag_key"] == "btn_color"
    assert body["user_id"] == "u1"
    assert body["config_version"] == dec["config_version"] == 1
    assert body["variant_key"] == "red"
    # 曝光时间字段带时区输出
    assert body["decided_at"].endswith("Z")
    assert body["created_at"].endswith("Z")


def test_create_exposure_keeps_original_decided_at(client):
    _insert_snapshot(request_id="exp-old-1", flag_key="time_flag", decided_at=BASE)
    resp = _expose(client, "exp-old-1", "exp-old-1")
    assert resp.status_code == 201
    body = resp.json()
    # 固化的是原判定时间，而非曝光上报时间
    assert body["decided_at"].startswith("2026-09-17T08:00:00")


def test_create_exposure_snapshot_missing_returns_404(client):
    resp = _expose(client, "exp-x", "no-such-request")
    assert resp.status_code == 404
    assert "no-such-request" in resp.text


def test_exposure_id_retry_returns_first_result(client, simple_flag):
    _evaluate(client, user_id="u1", request_id="req-1")
    first = _expose(client, "exp-1", "req-1")
    assert first.status_code == 201

    retry = _expose(client, "exp-1", "req-1")
    assert retry.status_code == 200
    assert retry.json() == first.json()

    # 重试不产生新记录：汇总中仍只有 1 次曝光
    summary = client.get("/api/v1/flags/btn_color/exposure-summary").json()
    assert sum(r["exposures"] for r in summary["rows"]) == 1


def test_exposure_id_reused_for_other_snapshot_returns_409(client, simple_flag):
    _evaluate(client, user_id="u1", request_id="req-1")
    _evaluate(client, user_id="u2", request_id="req-2")
    assert _expose(client, "exp-1", "req-1").status_code == 201

    resp = _expose(client, "exp-1", "req-2")
    assert resp.status_code == 409
    assert "exp-1" in resp.text


def test_create_exposure_validation_422(client):
    assert client.post("/api/v1/exposures", json={}).status_code == 422
    assert client.post("/api/v1/exposures", json={"exposure_id": "e1"}).status_code == 422
    assert (
        client.post(
            "/api/v1/exposures", json={"exposure_id": "", "request_id": "r1"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/exposures", json={"exposure_id": "e1", "request_id": ""}
        ).status_code
        == 422
    )


def test_exposure_freezes_version_and_variant_after_flag_update(client, simple_flag):
    dec = _evaluate(client, user_id="u1", request_id="req-1", context={"region": "cn"})
    assert dec["config_version"] == 1

    # 判定后更新开关（版本推进到 v2，规则改为全部 blue）再曝光
    resp = client.post(
        "/api/v1/flags",
        json={
            "flag_key": "btn_color",
            "variants": [
                {"key": "red", "weight": 0},
                {"key": "blue", "weight": 100},
            ],
            "rules": [],
        },
    )
    assert resp.status_code == 201

    body = _expose(client, "exp-1", "req-1").json()
    # 固化的仍是判定时的版本与变体，不受后续配置更新影响
    assert body["config_version"] == 1
    assert body["variant_key"] == "red"


def test_delete_flag_keeps_exposures(client, simple_flag):
    _evaluate(client, user_id="u1", request_id="req-1")
    assert _expose(client, "exp-1", "req-1").status_code == 201

    assert client.delete("/api/v1/flags/btn_color").status_code == 204

    # 删除开关后历史曝光保留，汇总仍可查询
    summary = client.get("/api/v1/flags/btn_color/exposure-summary").json()
    assert summary["flag_key"] == "btn_color"
    assert len(summary["rows"]) == 1
    assert summary["rows"][0]["exposures"] == 1
    assert summary["rows"][0]["unique_users"] == 1


def test_expose_after_flag_deleted_still_works(client, simple_flag):
    _evaluate(client, user_id="u1", request_id="req-1")
    assert client.delete("/api/v1/flags/btn_color").status_code == 204

    # 判定快照不随开关删除，曝光归因仍可基于快照固化
    resp = _expose(client, "exp-1", "req-1")
    assert resp.status_code == 201
    assert resp.json()["flag_key"] == "btn_color"


# ---------------------------------------------------------------------------
# GET /api/v1/flags/{flag_key}/exposure-summary
# ---------------------------------------------------------------------------


def test_summary_groups_by_version_and_variant(client):
    fk = "sum_flag"
    # 三个用户、两个版本、两个变体；u1 在 v1/red 上曝光两次
    _insert_snapshot(request_id="exp-r1", flag_key=fk, user_id="u1", variant_key="red", config_version=1, decided_at=BASE)
    _insert_snapshot(request_id="exp-r2", flag_key=fk, user_id="u1", variant_key="red", config_version=1, decided_at=BASE)
    _insert_snapshot(request_id="exp-r3", flag_key=fk, user_id="u2", variant_key="blue", config_version=1, decided_at=BASE)
    _insert_snapshot(request_id="exp-r4", flag_key=fk, user_id="u3", variant_key="red", config_version=2, decided_at=BASE)
    for rid in ("exp-r1", "exp-r2", "exp-r3", "exp-r4"):
        assert _expose(client, f"exp-{rid}-x", rid).status_code == 201

    body = client.get(f"/api/v1/flags/{fk}/exposure-summary").json()
    assert body["flag_key"] == fk
    assert body["decided_from"] is None and body["decided_to"] is None
    # 按 (config_version, variant_key) 升序；u1 两次曝光去重为 1 个用户
    assert body["rows"] == [
        {"config_version": 1, "variant_key": "blue", "unique_users": 1, "exposures": 1},
        {"config_version": 1, "variant_key": "red", "unique_users": 1, "exposures": 2},
        {"config_version": 2, "variant_key": "red", "unique_users": 1, "exposures": 1},
    ]


def test_summary_scoped_to_flag(client):
    _insert_snapshot(request_id="exp-a1", flag_key="sum_scope_a", decided_at=BASE)
    _insert_snapshot(request_id="exp-b1", flag_key="sum_scope_b", decided_at=BASE)
    assert _expose(client, "exp-a1", "exp-a1").status_code == 201
    assert _expose(client, "exp-b1", "exp-b1").status_code == 201

    body = client.get("/api/v1/flags/sum_scope_a/exposure-summary").json()
    assert len(body["rows"]) == 1
    assert body["rows"][0]["exposures"] == 1


def test_summary_time_window_inclusive_and_tz_offset(client):
    fk = "sum_window_flag"
    _insert_snapshot(request_id="exp-t0", flag_key=fk, decided_at=BASE)
    _insert_snapshot(request_id="exp-t1", flag_key=fk, decided_at=BASE + timedelta(hours=1))
    _insert_snapshot(request_id="exp-t2", flag_key=fk, decided_at=BASE + timedelta(hours=2))
    for rid in ("exp-t0", "exp-t1", "exp-t2"):
        assert _expose(client, f"exp-{rid}", rid).status_code == 201

    # 2026-09-17T16:00:00+08:00 == 2026-09-17T08:00:00Z；边界含 t0 与 t1
    body = client.get(
        f"/api/v1/flags/{fk}/exposure-summary",
        params={
            "decided_from": "2026-09-17T16:00:00+08:00",
            "decided_to": "2026-09-17T09:00:00Z",
        },
    ).json()
    assert sum(r["exposures"] for r in body["rows"]) == 2
    # 回显规范化后的生效窗口（UTC）
    assert body["decided_from"].startswith("2026-09-17T08:00:00")
    assert body["decided_to"].startswith("2026-09-17T09:00:00")

    # 窗口外无数据
    empty = client.get(
        f"/api/v1/flags/{fk}/exposure-summary",
        params={"decided_to": "2026-09-17T07:00:00Z"},
    ).json()
    assert empty["rows"] == []


def test_summary_naive_datetime_returns_422(client):
    resp = client.get(
        "/api/v1/flags/any_flag/exposure-summary",
        params={"decided_from": "2026-09-17T08:00:00"},
    )
    assert resp.status_code == 422
    assert "时区" in resp.text


def test_summary_bad_datetime_returns_422(client):
    resp = client.get(
        "/api/v1/flags/any_flag/exposure-summary",
        params={"decided_to": "not-a-date"},
    )
    assert resp.status_code == 422


def test_summary_inverted_range_returns_422(client):
    resp = client.get(
        "/api/v1/flags/any_flag/exposure-summary",
        params={
            "decided_from": "2026-09-18T00:00:00Z",
            "decided_to": "2026-09-17T00:00:00Z",
        },
    )
    assert resp.status_code == 422
    assert "decided_from" in resp.text


def test_summary_empty_for_flag_without_exposures(client):
    body = client.get("/api/v1/flags/never_exposed/exposure-summary").json()
    assert body == {
        "flag_key": "never_exposed",
        "decided_from": None,
        "decided_to": None,
        "rows": [],
    }
