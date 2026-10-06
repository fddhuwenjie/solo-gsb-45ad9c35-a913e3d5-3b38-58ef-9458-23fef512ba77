"""POST /worksheet-migrate 的端到端测试。

覆盖：
- 成功迁移：初始变量改写为新定义下同一单位表达式、同一物理量的精确
  十进制值；步骤原样保留；新工作单引用固定的新定义集标识，可直接交给
  /worksheet 且结果与迁移响应的新侧一致（显示值允许变化）；
- 无初始变量、绝对温度/温差变量、反求步骤、多变量等场景；
- 旧工作单出错（变量无法解析/步骤失败）-> 422 指出变量或步骤；
- 新定义下变量单位无法解析/量纲不兼容/改写值无法精确十进制 -> 422 指出变量；
- 新工作单步骤单位不兼容/执行失败 -> 422 指出步骤；
- 结果含义变化（恒零解的量纲取目标单位）-> 422 指出步骤；
- 别名引用：请求开始解析一次并固定，执行中切换别名不混版；
- 迁移只读：不改写定义集或别名；未知定义集/别名、非法引用等请求级 422。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app import unit_sets

client = TestClient(app, raise_server_exceptions=True)


def var(value, unit):
    return {"value": value, "unit": unit}


def ev_step(name, formula, output_unit):
    return {"name": name, "formula": formula, "output_unit": output_unit}


def solve_step(name, equation, solve_for, output_unit):
    return {
        "name": name,
        "equation": equation,
        "solve_for": solve_for,
        "output_unit": output_unit,
    }


def publish(custom_units):
    resp = client.post("/custom-unit-sets", json={"custom_units": custom_units})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def migrate(steps, old_ref, new_ref, variables=None):
    body: dict = {"steps": steps, "old": old_ref, "new": new_ref}
    if variables is not None:
        body["variables"] = variables
    return client.post("/worksheet-migrate", json=body)


def by_id(set_id):
    return {"custom_unit_set_id": set_id}


def by_alias(name):
    return {"custom_unit_set_alias": name}


SPAN_V1 = [{"symbol": "span", "factor": "2", "base_unit": "m"}]
SPAN_V2 = [{"symbol": "span", "factor": "2.5", "base_unit": "m"}]


# ---------------------------------------------------------------------------
# 成功迁移：变量改写、步骤原样、可直接交给 /worksheet
# ---------------------------------------------------------------------------


def test_migrate_rewrites_variables_and_returns_ready_worksheet():
    # 旧版 1 span = 2 m、新版 1 span = 2.5 m：x = 3 span（6 m）
    # 改写为 2.4 span（仍为 6 m）；步骤 output_unit 中的 span 含义已变，
    # 显示值允许变化（6 span -> 4.8 span），内部精确量不变。
    v1 = publish(SPAN_V1)
    v2 = publish(SPAN_V2)
    body = {
        "variables": {"x": var("3", "span")},
        "steps": [
            ev_step("total", "x*2", "span"),
            ev_step("total_m", "total", "m"),
        ],
        "old": by_id(v1),
        "new": by_id(v2),
    }
    resp = client.post("/worksheet-migrate", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    # 两侧引用在请求开始时固定并回显
    assert data["old"]["resolved_id"] == v1
    assert data["new"]["resolved_id"] == v2

    # 新工作单：变量改写、步骤原样、引用固定的新定义集标识
    worksheet = data["worksheet"]
    assert worksheet["variables"] == {"x": {"value": "2.4", "unit": "span"}}
    assert worksheet["custom_unit_set_id"] == v2
    assert worksheet["steps"] == [
        {"name": "total", "output_unit": "span", "formula": "x*2"},
        {"name": "total_m", "output_unit": "m", "formula": "total"},
    ]

    # 逐步核对明细：内部量一致，显示值允许变化
    row0, row1 = data["steps"]
    assert row0["index"] == 0 and row0["name"] == "total"
    assert row0["old"] == {
        "name": "total", "value": "6", "unit": "span", "dimension": "m"
    }
    assert row0["new"] == {
        "name": "total", "value": "4.8", "unit": "span", "dimension": "m"
    }
    assert row1["old"]["value"] == row1["new"]["value"] == "12"

    # 返回的新工作单可直接交给 /worksheet，结果与迁移响应的新侧一致
    replay = client.post("/worksheet", json=worksheet)
    assert replay.status_code == 200, replay.text
    assert replay.json()["steps"] == [row0["new"], row1["new"]]


def test_migrate_without_variables():
    v1 = publish(SPAN_V1)
    v2 = publish(SPAN_V2)
    resp = migrate(
        [ev_step("a", "2*3", "1"), ev_step("b", "a+1", "1")],
        by_id(v1),
        by_id(v2),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["worksheet"]["variables"] == {}
    assert data["worksheet"]["custom_unit_set_id"] == v2
    assert [row["new"]["value"] for row in data["steps"]] == ["6", "7"]


def test_migrate_multiple_variables_and_chained_custom_units():
    # 链式定义（furlong -> chain -> m）两侧比例不同，多个变量各自改写
    v1 = publish([
        {"symbol": "furlong", "factor": "10", "base_unit": "chain"},
        {"symbol": "chain", "factor": "22", "base_unit": "m"},
    ])
    v2 = publish([
        {"symbol": "furlong", "factor": "20", "base_unit": "chain"},
        {"symbol": "chain", "factor": "22", "base_unit": "m"},
    ])
    body = {
        "variables": {
            "a": var("1", "furlong"),   # 220 m -> 新版 0.5 furlong
            "b": var("11", "chain"),    # 242 m -> 新版 11 chain（定义未变）
            "c": var("0.1", "furlong"), # 22 m -> 新版 0.05 furlong
        },
        "steps": [
            ev_step("s", "a+b+c", "m"),
            ev_step("back", "s", "furlong"),
        ],
        "old": by_id(v1),
        "new": by_id(v2),
    }
    resp = client.post("/worksheet-migrate", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["worksheet"]["variables"] == {
        "a": {"value": "0.5", "unit": "furlong"},
        "b": {"value": "11", "unit": "chain"},
        "c": {"value": "0.05", "unit": "furlong"},
    }
    # 内部量一致（484 m），折回 furlong 的显示值随定义变化
    assert data["steps"][0]["old"]["value"] == data["steps"][0]["new"]["value"] == "484"
    assert data["steps"][1]["old"]["value"] == "2.2"
    assert data["steps"][1]["new"]["value"] == "1.1"


def test_migrate_solve_steps():
    v1 = publish(SPAN_V1)
    v2 = publish(SPAN_V2)
    body = {
        "variables": {"v": var("3", "span")},  # 6 m
        "steps": [
            solve_step("half", "2*t = v", "t", "m"),
            ev_step("back", "half*2", "span"),
        ],
        "old": by_id(v1),
        "new": by_id(v2),
    }
    resp = client.post("/worksheet-migrate", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["worksheet"]["variables"] == {"v": {"value": "2.4", "unit": "span"}}
    assert data["worksheet"]["steps"][0] == {
        "name": "half", "output_unit": "m", "equation": "2*t = v", "solve_for": "t"
    }
    assert data["steps"][0]["old"]["value"] == data["steps"][0]["new"]["value"] == "3"
    # 折回 span：旧 3 / 新 2.4，内部量均为 6 m
    assert data["steps"][1]["old"]["value"] == "3"
    assert data["steps"][1]["new"]["value"] == "2.4"


def test_migrate_absolute_temperature_variables_unchanged():
    # 绝对温度变量（内置仿射温标）两侧解析相同，改写后数值不变
    v1 = publish(SPAN_V1)
    v2 = publish(SPAN_V2)
    body = {
        "variables": {"T0": var("98.6", "degF")},
        "steps": [
            ev_step("Tc", "T0", "degC"),
            ev_step("Tk", "Tc", "K"),
        ],
        "old": by_id(v1),
        "new": by_id(v2),
    }
    resp = client.post("/worksheet-migrate", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["worksheet"]["variables"] == {"T0": {"value": "98.6", "unit": "degF"}}
    assert data["steps"][0]["new"]["value"] == "37"
    assert data["steps"][1]["new"]["value"] == "310.15"


def test_migrate_temperature_difference_custom_units():
    # 温差类自定义单位（线性）：旧 1 mdeg = 2 mK、新 1 mdeg = 4 mK
    v1 = publish([{"symbol": "mdeg", "factor": "2", "base_unit": "mK"}])
    v2 = publish([{"symbol": "mdeg", "factor": "4", "base_unit": "mK"}])
    body = {
        "variables": {"d": var("3", "mdeg")},  # 6 mK -> 新版 1.5 mdeg
        "steps": [
            ev_step("a", "d", "mdeg"),
            ev_step("b", "d", "mK"),
        ],
        "old": by_id(v1),
        "new": by_id(v2),
    }
    resp = client.post("/worksheet-migrate", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["worksheet"]["variables"] == {"d": {"value": "1.5", "unit": "mdeg"}}
    assert data["steps"][0]["old"]["value"] == "3"
    assert data["steps"][0]["new"]["value"] == "1.5"
    assert data["steps"][1]["old"]["value"] == data["steps"][1]["new"]["value"] == "6"


def test_migrate_same_set_on_both_sides_keeps_values():
    set_id = publish(SPAN_V1)
    resp = migrate(
        [ev_step("a", "x", "span"), ev_step("b", "a*2", "m")],
        by_id(set_id),
        by_id(set_id),
        variables={"x": var("3", "span")},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["worksheet"]["variables"] == {"x": {"value": "3", "unit": "span"}}
    assert data["old"]["resolved_id"] == data["new"]["resolved_id"] == set_id
    for row in data["steps"]:
        assert row["old"] == row["new"]


# ---------------------------------------------------------------------------
# 旧工作单出错 -> 422，指出变量或步骤
# ---------------------------------------------------------------------------


def test_old_worksheet_setup_error_is_422_naming_variable():
    # 变量单位 ft 在旧定义集下不存在：指出变量 q
    v1 = publish(SPAN_V1)
    foot = publish([{"symbol": "ft", "factor": "0.3048", "base_unit": "m"}])
    resp = migrate(
        [ev_step("a", "q", "m"), ev_step("b", "q*2", "ft")],
        by_id(v1),
        by_id(foot),
        variables={"q": var("10", "ft")},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "migration_failed"
    assert "'q'" in detail["message"]
    assert "unknown_symbol" in detail["message"]
    assert "worksheet" not in resp.json()


def test_old_worksheet_step_error_is_422_naming_step():
    v1 = publish(SPAN_V1)
    v2 = publish(SPAN_V2)
    resp = migrate(
        [ev_step("a", "x", "m"), ev_step("b", "a+y", "m")],
        by_id(v1),
        by_id(v2),
        variables={"x": var("1", "m"), "y": var("1", "s")},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "migration_failed"
    assert "'b'" in detail["message"]
    assert "dimension_mismatch" in detail["message"]
    assert "worksheet" not in resp.json()


# ---------------------------------------------------------------------------
# 新定义下变量不兼容 / 改写值无法精确表示 -> 422，指出变量
# ---------------------------------------------------------------------------


def test_variable_unit_unknown_under_new_is_422_naming_variable():
    v1 = publish([
        {"symbol": "span", "factor": "2", "base_unit": "m"},
        {"symbol": "ww", "factor": "1", "base_unit": "m"},
    ])
    v2 = publish(SPAN_V2)  # 没有 ww
    resp = migrate(
        [ev_step("a", "x", "m"), ev_step("b", "a", "m")],
        by_id(v1),
        by_id(v2),
        variables={"x": var("1", "ww")},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "migration_failed"
    assert "'x'" in detail["message"]
    assert "unknown_symbol" in detail["message"]


def test_variable_dimension_incompatible_under_new_is_422_naming_variable():
    # 同名符号旧版基于 m、新版基于 s：量纲不兼容
    v1 = publish([{"symbol": "zz", "factor": "2", "base_unit": "m"}])
    v2 = publish([{"symbol": "zz", "factor": "3", "base_unit": "s"}])
    resp = migrate(
        [ev_step("a", "x", "zz"), ev_step("b", "a", "zz")],
        by_id(v1),
        by_id(v2),
        variables={"x": var("1", "zz")},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "migration_failed"
    assert "'x'" in detail["message"]
    assert "量纲不兼容" in detail["message"]


def test_rewrite_value_not_exact_decimal_is_422_naming_variable():
    # 旧版 1 span = 2 m、新版 1 span = 3 m：x = 1 span（2 m）
    # 在新定义下为 2/3 span，不是有限十进制
    v1 = publish(SPAN_V1)
    v2 = publish([{"symbol": "span", "factor": "3", "base_unit": "m"}])
    resp = migrate(
        [ev_step("a", "x", "m"), ev_step("b", "a", "m")],
        by_id(v1),
        by_id(v2),
        variables={"x": var("1", "span")},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "migration_failed"
    assert "'x'" in detail["message"]
    assert "无法精确表示" in detail["message"]
    assert "worksheet" not in resp.json()


# ---------------------------------------------------------------------------
# 新工作单出错 / 结果含义变化 -> 422，指出步骤
# ---------------------------------------------------------------------------


def test_step_output_unit_unknown_under_new_is_422_naming_step():
    # 步骤 output_unit 引用的 ww 只存在于旧定义集
    v1 = publish([
        {"symbol": "span", "factor": "2", "base_unit": "m"},
        {"symbol": "ww", "factor": "1", "base_unit": "m"},
    ])
    v2 = publish(SPAN_V2)
    resp = migrate(
        [ev_step("a", "x", "span"), ev_step("b", "x", "ww")],
        by_id(v1),
        by_id(v2),
        variables={"x": var("3", "span")},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "migration_failed"
    assert "'b'" in detail["message"]
    assert "unknown_symbol" in detail["message"]


def test_step_output_unit_dimension_incompatible_under_new_is_422_naming_step():
    # 步骤 output_unit 的符号在新定义下量纲不同
    v1 = publish([{"symbol": "zz", "factor": "2", "base_unit": "m"}])
    v2 = publish([{"symbol": "zz", "factor": "3", "base_unit": "s"}])
    resp = migrate(
        [ev_step("a", "x", "m"), ev_step("b", "x", "zz")],
        by_id(v1),
        by_id(v2),
        variables={"x": var("1", "m")},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "migration_failed"
    assert "'b'" in detail["message"]
    assert "dimension_mismatch" in detail["message"]


def test_step_display_value_not_exact_decimal_under_new_is_422_naming_step():
    # 内部量 2 m 在新版 span = 3 m 下折回 span 为 2/3：该步十进制化失败
    v1 = publish(SPAN_V1)
    v2 = publish([{"symbol": "span", "factor": "3", "base_unit": "m"}])
    resp = migrate(
        [ev_step("a", "x", "m"), ev_step("b", "a", "span")],
        by_id(v1),
        by_id(v2),
        variables={"x": var("2", "m")},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "migration_failed"
    assert "'b'" in detail["message"]
    assert "precision_overflow" in detail["message"]


def test_meaning_change_is_422_naming_step():
    # 恒零解（2*x = x）的量纲取目标单位：zz 在旧版为 m、新版为 s，
    # 两侧该步内部量纲不同 -> 结果含义变化
    v1 = publish([{"symbol": "zz", "factor": "2", "base_unit": "m"}])
    v2 = publish([{"symbol": "zz", "factor": "3", "base_unit": "s"}])
    resp = migrate(
        [
            ev_step("k", "1", "1"),
            solve_step("z", "2*x = x", "x", "zz"),
        ],
        by_id(v1),
        by_id(v2),
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "migration_failed"
    assert "'z'" in detail["message"]
    assert "含义" in detail["message"]
    assert "worksheet" not in resp.json()


# ---------------------------------------------------------------------------
# 别名引用：请求开始解析一次并固定；迁移只读
# ---------------------------------------------------------------------------


def test_alias_references_resolved_and_echoed():
    v1 = publish(SPAN_V1)
    v2 = publish(SPAN_V2)
    client.put(
        "/custom-unit-set-aliases/lens",
        json={"custom_unit_set_id": v1, "expected_id": None},
    )
    resp = migrate(
        [ev_step("a", "x", "m"), ev_step("b", "x", "span")],
        by_alias("lens"),
        by_id(v2),
        variables={"x": var("3", "span")},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["old"]["reference"] == {"custom_unit_set_alias": "lens"}
    assert data["old"]["resolved_id"] == v1
    assert data["new"]["resolved_id"] == v2
    assert data["worksheet"]["custom_unit_set_id"] == v2


def test_alias_switch_during_request_does_not_mix_versions(monkeypatch):
    v1 = publish(SPAN_V1)
    v2 = publish(SPAN_V2)
    client.put(
        "/custom-unit-set-aliases/lens",
        json={"custom_unit_set_id": v1, "expected_id": None},
    )
    store = unit_sets.get_store()
    orig_build = store.build_scope

    # 旧侧构造 scope 之后、步骤执行之前把别名切到 v2；
    # 旧侧必须完整使用 v1，新侧解析时取到 v2。
    def switched_build(set_id):
        scope = orig_build(set_id)
        if set_id == v1:
            r = client.put(
                "/custom-unit-set-aliases/lens",
                json={"custom_unit_set_id": v2, "expected_id": v1},
            )
            assert r.status_code == 200, r.text
        return scope

    monkeypatch.setattr(store, "build_scope", switched_build)
    resp = migrate(
        [ev_step("a", "x", "m"), ev_step("b", "x", "span")],
        by_alias("lens"),
        by_alias("lens"),
        variables={"x": var("3", "span")},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["old"]["resolved_id"] == v1
    assert data["new"]["resolved_id"] == v2
    # 旧侧自始至终是 v1（3 span = 6 m，折回 span 显示 3）
    assert data["steps"][0]["old"]["value"] == "6"
    assert data["steps"][1]["old"]["value"] == "3"
    # 新工作单引用切换后固定的新标识
    assert data["worksheet"]["custom_unit_set_id"] == v2


def test_migrate_does_not_mutate_sets_or_aliases():
    v1 = publish(SPAN_V1)
    v2 = publish(SPAN_V2)
    client.put(
        "/custom-unit-set-aliases/lens",
        json={"custom_unit_set_id": v1, "expected_id": None},
    )
    before_sets = unit_sets.get_store().count()
    resp = migrate(
        [ev_step("a", "x", "m"), ev_step("b", "x", "span")],
        by_alias("lens"),
        by_id(v2),
        variables={"x": var("3", "span")},
    )
    assert resp.status_code == 200, resp.text
    # 迁移只读：别名指向不变、定义集数量不变
    assert unit_sets.get_store().count() == before_sets
    aliases = client.get("/custom-unit-set-aliases").json()["aliases"]
    assert aliases == [{"alias": "lens", "custom_unit_set_id": v1}]


# ---------------------------------------------------------------------------
# 请求级 422（引用与工作单本体校验，与 /worksheet-diagnose 一致）
# ---------------------------------------------------------------------------


def test_unknown_set_id_is_422():
    v1 = publish(SPAN_V1)
    resp = migrate(
        [ev_step("a", "1", "1"), ev_step("b", "a", "1")],
        by_id("cus_does_not_exist"),
        by_id(v1),
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_request"


def test_unknown_alias_is_422():
    v1 = publish(SPAN_V1)
    resp = migrate(
        [ev_step("a", "1", "1"), ev_step("b", "a", "1")],
        by_alias("nope"),
        by_id(v1),
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_request"


def test_ref_with_both_id_and_alias_is_422():
    v1 = publish(SPAN_V1)
    resp = migrate(
        [ev_step("a", "1", "1"), ev_step("b", "a", "1")],
        {"custom_unit_set_id": v1, "custom_unit_set_alias": "lens"},
        by_id(v1),
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_request"


def test_step_count_limits_still_apply():
    v1 = publish(SPAN_V1)
    v2 = publish(SPAN_V2)
    one = migrate([ev_step("a", "1", "1")], by_id(v1), by_id(v2))
    assert one.status_code == 422
    six = migrate(
        [ev_step(f"s{i}", "1", "1") for i in range(6)], by_id(v1), by_id(v2)
    )
    assert six.status_code == 422


def test_failed_migration_returns_no_worksheet():
    # 任一 422 都不返回迁移工作单
    v1 = publish(SPAN_V1)
    v2 = publish([{"symbol": "span", "factor": "3", "base_unit": "m"}])
    resp = migrate(
        [ev_step("a", "x", "m"), ev_step("b", "a", "m")],
        by_id(v1),
        by_id(v2),
        variables={"x": var("1", "span")},
    )
    assert resp.status_code == 422
    assert "worksheet" not in resp.json()
