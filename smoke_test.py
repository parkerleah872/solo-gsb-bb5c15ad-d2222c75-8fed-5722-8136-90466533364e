"""端到端冒烟测试：不依赖网络，直接用 FastAPI TestClient 跑全流程。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".pylibs"))

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

with TestClient(app) as client:
    # --- 健康检查 ---
    assert client.get("/health").json() == {"status": "ok"}

    # --- 1. 循环依赖被拒绝 ---
    r = client.post("/api/releases", json={
        "version": "bad",
        "nodes": [{"name": "x"}, {"name": "y"}],
        "dependencies": [
            {"from_node": "x", "to_node": "y"},
            {"from_node": "y", "to_node": "x"},
        ],
    })
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "cycle_detected"
    print("[OK] 循环依赖 -> 422 cycle_detected:", r.json()["error"].get("cycle_nodes"))

    # --- 2. 依赖边引用不存在节点 ---
    r = client.post("/api/releases", json={
        "version": "bad2",
        "nodes": [{"name": "a"}],
        "dependencies": [{"from_node": "a", "to_node": "ghost"}],
    })
    assert r.status_code == 422 and r.json()["error"]["code"] == "unknown_node"
    print("[OK] 未知节点引用 -> 422 unknown_node")

    # --- 3. 重复节点名 ---
    r = client.post("/api/releases", json={
        "version": "bad3",
        "nodes": [{"name": "a"}, {"name": "a"}],
        "dependencies": [],
    })
    assert r.status_code == 422 and r.json()["error"]["code"] == "duplicate_node"
    print("[OK] 重复节点 -> 422 duplicate_node")

    # --- 4. 正常创建批次，验证拓扑分层 ---
    r = client.post("/api/releases", json={
        "version": "v1",
        "nodes": [
            {"name": "a", "node_type": "service"},
            {"name": "b"}, {"name": "c"}, {"name": "d"},
        ],
        "dependencies": [
            {"from_node": "a", "to_node": "c"},
            {"from_node": "b", "to_node": "c"},
            {"from_node": "c", "to_node": "d"},
        ],
    })
    assert r.status_code == 201, r.text
    body = r.json()
    rid = body["id"]
    assert body["status"] == "pending"
    assert body["levels"] == [["a", "b"], ["c"], ["d"]], body["levels"]
    assert body["next_nodes"] == ["a", "b"]
    print("[OK] 创建批次", rid, "分层:", body["levels"])

    # 计划接口
    plan = client.get(f"/api/releases/{rid}/plan").json()
    assert plan["total_levels"] == 3
    assert plan["plan"] == [
        {"level": 0, "parallel_nodes": ["a", "b"]},
        {"level": 1, "parallel_nodes": ["c"]},
        {"level": 2, "parallel_nodes": ["d"]},
    ]
    # 未填 estimated_seconds：旧批次语义，工期全部按 0
    s0 = plan["schedule"]
    assert s0["shortest_duration_seconds"] == 0
    assert s0["deadline_seconds"] is None and s0["feasible"] is True
    assert s0["gap_seconds"] is None
    assert s0["critical_nodes"] == ["a", "b", "c", "d"]
    assert s0["critical_path"] == ["a", "c", "d"]  # 多条零长路径中字典序最小
    assert all(n["estimated_seconds"] == 0 and n["total_slack"] == 0 for n in plan["nodes"])
    print("[OK] 执行计划接口（缺省工期按 0，CPM 零工期分析）")

    # --- 4b. 带工期的 CPM 分析：前向 / 后向 DP、零余量关键节点 ---
    r = client.post("/api/releases", json={
        "version": "v-cpm",
        "nodes": [
            {"name": "a", "estimated_seconds": 2},
            {"name": "b", "estimated_seconds": 5},
            {"name": "c", "estimated_seconds": 3},
            {"name": "d", "estimated_seconds": 1},
        ],
        "dependencies": [
            {"from_node": "a", "to_node": "c"},
            {"from_node": "b", "to_node": "c"},
            {"from_node": "c", "to_node": "d"},
            {"from_node": "b", "to_node": "d"},
        ],
    })
    cid = r.json()["id"]
    cp = client.get(f"/api/releases/{cid}/plan").json()
    sch = cp["schedule"]
    tt = {n["name"]: n for n in cp["nodes"]}
    # 前向：a/b 从 0 开始；c 等 b（ES=5）；d 等 c（ES=8）；最短总工期 9
    assert tt["a"]["earliest_start"] == 0 and tt["a"]["latest_start"] == 3
    assert tt["b"]["earliest_start"] == 0 and tt["b"]["latest_start"] == 0
    assert tt["c"]["earliest_start"] == 5 and tt["c"]["latest_start"] == 5
    assert tt["d"]["earliest_start"] == 8 and tt["d"]["latest_start"] == 8
    assert tt["a"]["total_slack"] == 3
    assert {n for n in ("b", "c", "d") if tt[n]["total_slack"] == 0} == {"b", "c", "d"}
    assert sch["shortest_duration_seconds"] == 9
    assert sch["critical_nodes"] == ["b", "c", "d"]
    assert sch["critical_path"] == ["b", "c", "d"]  # b->d 仅 6 秒，非最长
    print("[OK] CPM：最短工期", sch["shortest_duration_seconds"],
          "关键节点", sch["critical_nodes"], "路径", sch["critical_path"])

    # --- 4c. 并列最长路径取节点名序列字典序最小 ---
    r = client.post("/api/releases", json={
        "version": "v-tie",
        "nodes": [
            {"name": "s", "estimated_seconds": 1},
            {"name": "m", "estimated_seconds": 2},
            {"name": "n", "estimated_seconds": 2},
            {"name": "e", "estimated_seconds": 1},
        ],
        "dependencies": [
            {"from_node": "s", "to_node": "m"},
            {"from_node": "s", "to_node": "n"},
            {"from_node": "m", "to_node": "e"},
            {"from_node": "n", "to_node": "e"},
        ],
    })
    tid = r.json()["id"]
    tsch = client.get(f"/api/releases/{tid}/plan").json()["schedule"]
    assert tsch["shortest_duration_seconds"] == 4
    assert tsch["critical_nodes"] == ["s", "m", "n", "e"]
    assert tsch["critical_path"] == ["s", "m", "e"], tsch["critical_path"]
    print("[OK] 并列最长路径 -> 字典序最小:", tsch["critical_path"])

    # --- 4d. deadline_seconds：可行 / 恰好 / 不可行（完整分析 + 缺口）---
    bad = client.get(f"/api/releases/{tid}/plan?deadline_seconds=3").json()["schedule"]
    assert bad["feasible"] is False and bad["gap_seconds"] == 1
    assert bad["deadline_seconds"] == 3
    assert bad["shortest_duration_seconds"] == 4  # 仍返回完整分析
    assert bad["critical_path"] == ["s", "m", "e"] and bad["critical_nodes"] == ["s", "m", "n", "e"]
    exact = client.get(f"/api/releases/{tid}/plan?deadline_seconds=4").json()["schedule"]
    assert exact["feasible"] is True and exact["gap_seconds"] == 0
    loose = client.get(f"/api/releases/{tid}/plan?deadline_seconds=10").json()["schedule"]
    assert loose["feasible"] is True and loose["gap_seconds"] == 0
    # 查询不得改变批次状态
    assert client.get(f"/api/releases/{tid}").json()["status"] == "pending"
    # deadline 非非负整数 -> 422（请求校验，不进入业务逻辑）
    assert client.get(f"/api/releases/{tid}/plan?deadline_seconds=-1").status_code == 422
    print("[OK] deadline 可行性分析：infeasible gap=1，且查询不改变批次状态")

    # --- 4e. estimated_seconds 必须为正整数 ---
    assert client.post("/api/releases", json={
        "version": "bad-dur", "nodes": [{"name": "a", "estimated_seconds": 0}]}).status_code == 422
    assert client.post("/api/releases", json={
        "version": "bad-dur", "nodes": [{"name": "a", "estimated_seconds": -5}]}).status_code == 422
    r = client.post("/api/releases", json={
        "version": "ok-dur", "nodes": [{"name": "a", "estimated_seconds": 7}]})
    assert r.status_code == 201 and r.json()["nodes"][0]["estimated_seconds"] == 7
    rid_dur = r.json()["id"]
    print("[OK] estimated_seconds 正整数校验；不传按 0")

    # --- 5. 乱序回报：a 成功后直接回报 c（b 还没成功）---
    r = client.post(f"/api/releases/{rid}/report",
                    json={"node": "a", "status": "success", "version": "v1"})
    assert r.status_code == 200 and r.json()["release"]["status"] == "running"

    r = client.post(f"/api/releases/{rid}/report",
                    json={"node": "c", "status": "success", "version": "v1"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "out_of_order"
    assert r.json()["error"]["blockers"] == [{"node": "b", "status": "pending"}]
    print("[OK] 乱序回报 -> 409 out_of_order, blockers:", r.json()["error"]["blockers"])

    # --- 6. 跨版本回报 ---
    r = client.post(f"/api/releases/{rid}/report",
                    json={"node": "b", "status": "success", "version": "v9-old"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "version_mismatch"
    print("[OK] 跨版本回报 -> 409 version_mismatch")

    # --- 7. 重复回报 ---
    r = client.post(f"/api/releases/{rid}/report",
                    json={"node": "a", "status": "success", "version": "v1"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "already_reported"
    print("[OK] 重复回报 -> 409 already_reported")

    # --- 8. b 成功 -> c 失败 -> 仅回滚 a、b（逆序）---
    r = client.post(f"/api/releases/{rid}/report",
                    json={"node": "b", "status": "success", "version": "v1"})
    assert r.status_code == 200

    r = client.post(f"/api/releases/{rid}/report", json={
        "node": "c", "status": "failed", "version": "v1",
        "error_code": "DEPLOY_TIMEOUT", "error_message": "健康检查超时",
    })
    assert r.status_code == 200, r.text
    fb = r.json()
    assert fb["result"] == "failed_and_rolled_back"
    assert fb["rollback_scope"] == "current_release_only"
    # 同层按 reported_at 倒序：b 后成功先回滚，a 随后
    assert [x["node"] for x in fb["rolled_back_nodes"]] == ["b", "a"], fb["rolled_back_nodes"]
    assert fb["resumable"] is True
    cp = fb["checkpoint"]
    assert cp["retry_endpoint"] == f"/api/releases/{rid}/retry"
    assert cp["restart_nodes"] == ["a", "b"] and cp["restart_level"] == 0
    print("[OK] c 失败 -> 回滚", [x["node"] for x in fb["rolled_back_nodes"]], "断点:", cp)

    st = fb["release"]
    assert st["status"] == "partially_rolled_back"
    assert st["failed_node"] == "c"
    node_status = {n["name"]: n["status"] for n in st["nodes"]}
    assert node_status == {"a": "rolled_back", "b": "rolled_back",
                           "c": "failed", "d": "pending"}, node_status

    # 回滚后不能直接继续回报
    r = client.post(f"/api/releases/{rid}/report",
                    json={"node": "a", "status": "success", "version": "v1"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "release_aborted"
    print("[OK] 回滚后继续回报 -> 409 release_aborted")

    # --- 9. 断点重试：failed/rolled_back 重置为 pending ---
    r = client.post(f"/api/releases/{rid}/retry")
    assert r.status_code == 200, r.text
    rb = r.json()
    assert sorted(rb["reset_nodes"]) == ["a", "b", "c"]
    assert rb["checkpoint"]["next_nodes"] == ["a", "b"]
    assert rb["release"]["status"] == "running"
    print("[OK] 断点重试 -> 重置", sorted(rb["reset_nodes"]), "next:", rb["checkpoint"]["next_nodes"])

    # 非失败状态不能重试
    r = client.post(f"/api/releases/{rid}/retry")
    assert r.status_code == 409 and r.json()["error"]["code"] == "retry_not_allowed"
    print("[OK] 非失败状态重试 -> 409 retry_not_allowed")

    # --- 10. 重试后走完全部节点 -> succeeded ---
    for node in ("a", "b", "c", "d"):
        r = client.post(f"/api/releases/{rid}/report",
                        json={"node": node, "status": "success", "version": "v1"})
    assert r.status_code == 200
    assert r.json()["release"]["status"] == "succeeded"
    assert r.json()["release"]["progress"] == {"succeeded": 4, "total": 4, "completed": True}
    print("[OK] 重试后全部成功 -> succeeded")

    # 已完成批次不再接受回报
    r = client.post(f"/api/releases/{rid}/report",
                    json={"node": "a", "status": "success", "version": "v1"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "release_finished"
    print("[OK] 完成后回报 -> 409 release_finished")

    # --- 11. 第二批失败且此前无成功节点 -> failed（而非 partially_rolled_back），只影响本批次 ---
    r = client.post("/api/releases", json={
        "version": "v2",
        "nodes": [{"name": "solo"}],
        "dependencies": [],
    })
    rid2 = r.json()["id"]
    r = client.post(f"/api/releases/{rid2}/report",
                    json={"node": "solo", "status": "failed", "version": "v2"})
    assert r.json()["release"]["status"] == "failed"
    assert r.json()["rolled_back_nodes"] == []
    # 第一批仍然 succeeded —— 回滚严格限定本批次
    assert client.get(f"/api/releases/{rid}").json()["status"] == "succeeded"
    print("[OK] 无成功节点失败 -> failed；历史批次不受影响")

    # --- 12. 部分回滚：只回滚成功的 a，无关的 pending 节点 b 不受影响 ---
    r = client.post("/api/releases", json={
        "version": "v3",
        "nodes": [{"name": "a"}, {"name": "b"}, {"name": "c"}],
        "dependencies": [{"from_node": "a", "to_node": "c"}],  # level0=[a,b], level1=[c]
    })
    rid3 = r.json()["id"]
    assert client.post(f"/api/releases/{rid3}/report",
                       json={"node": "a", "status": "success", "version": "v3"}).status_code == 200
    r = client.post(f"/api/releases/{rid3}/report",
                    json={"node": "c", "status": "failed", "version": "v3"})
    fb3 = r.json()
    assert [x["node"] for x in fb3["rolled_back_nodes"]] == ["a"]
    assert fb3["checkpoint"]["restart_nodes"] == ["a"]  # b 仍 pending，不在重置列表
    ns3 = {n["name"]: n["status"] for n in fb3["release"]["nodes"]}
    assert ns3 == {"a": "rolled_back", "b": "pending", "c": "failed"}, ns3
    rt = client.post(f"/api/releases/{rid3}/retry").json()
    # 重试后 a 重新可执行，b 本来就 pending，二者同为 level0 断点
    assert rt["checkpoint"]["next_nodes"] == ["a", "b"]
    assert sorted(rt["reset_nodes"]) == ["a", "c"]
    print("[OK] 部分回滚 -> 仅回滚 [a]；重试断点 next=[a,b]，重置=[a,c]")

    # --- 12b. targets 裁剪：单目标 + 全部传递前置，无关分支排除，共享前置去重 ---
    # 图：a,b -> c -> d；e（独立）；f 是 d 的额外前置。targets=["c"] -> 仅 {a,b,c}
    r = client.post("/api/releases", json={
        "version": "v-tgt",
        "nodes": [{"name": "a"}, {"name": "b"}, {"name": "c"},
                  {"name": "d"}, {"name": "e"}, {"name": "f"}],
        "dependencies": [
            {"from_node": "a", "to_node": "c"},
            {"from_node": "b", "to_node": "c"},
            {"from_node": "c", "to_node": "d"},
            {"from_node": "f", "to_node": "d"},
        ],
        "targets": ["c"],
    })
    assert r.status_code == 201, r.text
    tb = r.json()
    ridt = tb["id"]
    assert tb["targets"] == ["c"] and tb["excluded_node_count"] == 3  # d/e/f 被排除
    assert tb["levels"] == [["a", "b"], ["c"]], tb["levels"]
    assert tb["next_nodes"] == ["a", "b"]
    tnames = {n["name"]: n for n in tb["nodes"]}
    assert set(tnames) == {"a", "b", "c"}
    assert tnames["c"]["is_target"] is True
    assert tnames["a"]["is_target"] is False and tnames["b"]["is_target"] is False
    assert {(e["from"], e["to"]) for e in tb["dependencies"]} == {("a", "c"), ("b", "c")}
    # 进度 total 也只统计保留节点
    assert tb["progress"] == {"succeeded": 0, "total": 3, "completed": False}
    print("[OK] targets 裁剪 -> 仅保留目标 c 及传递前置 a/b，excluded_node_count=3")

    # plan / CPM 统一基于诱导子图：不出现 d/e/f
    tp = client.get(f"/api/releases/{ridt}/plan").json()
    assert tp["targets"] == ["c"] and tp["excluded_node_count"] == 3
    assert tp["total_levels"] == 2
    assert {n["name"] for n in tp["nodes"]} == {"a", "b", "c"}
    assert {n["name"] for n in tp["nodes"] if n["is_target"]} == {"c"}
    assert {(e["from"], e["to"]) for e in tp["dependencies"]} == {("a", "c"), ("b", "c")}
    assert "d" not in tp["schedule"]["critical_nodes"]
    assert tp["schedule"]["critical_path"][-1] == "c"
    print("[OK] 裁剪批次 plan/CPM 仅含诱导子图节点，is_target 同步标出")

    # 多目标：共享前置只保留一次；目标自身也可在同一 level
    r = client.post("/api/releases", json={
        "version": "v-tgt2",
        "nodes": [{"name": "a"}, {"name": "b"}, {"name": "c"}, {"name": "x"}],
        "dependencies": [
            {"from_node": "a", "to_node": "b"},
            {"from_node": "a", "to_node": "c"},
            {"from_node": "x", "to_node": "c"},
        ],
        "targets": ["b", "c"],
    })
    assert r.status_code == 201, r.text
    tb2 = r.json()
    ridt2 = tb2["id"]
    assert tb2["excluded_node_count"] == 0  # a/b/c/x 全部是目标或目标的前置
    assert {n["name"] for n in tb2["nodes"]} == {"a", "b", "c", "x"}
    assert {n["name"] for n in tb2["nodes"] if n["is_target"]} == {"b", "c"}
    assert tb2["levels"] == [["a", "x"], ["b", "c"]], tb2["levels"]
    # 依赖边全保留（诱导子图内部边）
    assert len(tb2["dependencies"]) == 3
    print("[OK] 多目标共享前置 a 只保留一次，x 作为 c 的前置保留，levels 连续重编号")

    # --- 12c. 裁剪批次可正常执行：回报、next_nodes、回滚、重试均不出现排除节点 ---
    # ridt: level0=[a,b], level1=[c]
    r = client.post(f"/api/releases/{ridt}/report",
                    json={"node": "a", "status": "success", "version": "v-tgt"})
    assert r.status_code == 200 and r.json()["release"]["next_nodes"] == ["b"]
    # 排除节点无法回报（节点行不存在 -> node_not_found，且不会误放行）
    r = client.post(f"/api/releases/{ridt}/report",
                    json={"node": "d", "status": "success", "version": "v-tgt"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "node_not_found"
    # c 前置不齐仍拒绝乱序
    r = client.post(f"/api/releases/{ridt}/report",
                    json={"node": "c", "status": "success", "version": "v-tgt"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "out_of_order"
    r = client.post(f"/api/releases/{ridt}/report",
                    json={"node": "b", "status": "success", "version": "v-tgt"})
    assert r.status_code == 200 and r.json()["release"]["next_nodes"] == ["c"]
    # 目标 c 失败：仅逆序回滚子图内已成功的 b、a；checkpoint 也只有子图节点
    r = client.post(f"/api/releases/{ridt}/report",
                    json={"node": "c", "status": "failed", "version": "v-tgt"})
    assert r.status_code == 200, r.text
    fb = r.json()
    assert [x["node"] for x in fb["rolled_back_nodes"]] == ["b", "a"]
    assert fb["checkpoint"]["restart_nodes"] == ["a", "b"]
    assert fb["checkpoint"]["restart_level"] == 0
    assert all(n["name"] in {"a", "b", "c"} for n in fb["release"]["nodes"])
    assert fb["release"]["excluded_node_count"] == 3
    # 重试后续发，走完全程 -> succeeded（total=3）
    assert client.post(f"/api/releases/{ridt}/retry").status_code == 200
    for node in ("a", "b", "c"):
        r = client.post(f"/api/releases/{ridt}/report",
                        json={"node": node, "status": "success", "version": "v-tgt"})
    assert r.json()["release"]["status"] == "succeeded"
    assert r.json()["release"]["progress"] == {"succeeded": 3, "total": 3, "completed": True}
    print("[OK] 裁剪批次执行/乱序拒绝/排除节点 404/回滚/重试全程仅含子图节点")

    # --- 12d. 裁剪改变关键路径：排除的无关长分支不影响 CPM ---
    # a(10)->b(1)；x(1)->y(100)。全量时最长工期 101；targets=["b"] 后仅剩 a->b，工期 11
    r = client.post("/api/releases", json={
        "version": "v-tgt-cpm",
        "nodes": [
            {"name": "a", "estimated_seconds": 10},
            {"name": "b", "estimated_seconds": 1},
            {"name": "x", "estimated_seconds": 1},
            {"name": "y", "estimated_seconds": 100},
        ],
        "dependencies": [
            {"from_node": "a", "to_node": "b"},
            {"from_node": "x", "to_node": "y"},
        ],
        "targets": ["b"],
    })
    ridt3 = r.json()["id"]
    sch = client.get(f"/api/releases/{ridt3}/plan").json()["schedule"]
    assert sch["shortest_duration_seconds"] == 11, sch
    assert sch["critical_nodes"] == ["a", "b"] and sch["critical_path"] == ["a", "b"]
    print("[OK] CPM 基于裁剪子图：排除百秒无关分支后最短工期 11、关键路径 a->b")

    # --- 12e. targets 校验：空 / 重复 / 空白 / 不存在（零写入）/ 整图先校验 ---
    assert client.post("/api/releases", json={
        "version": "bad-empty", "nodes": [{"name": "a"}], "targets": []
    }).json()["error"]["code"] == "empty_targets"
    r = client.post("/api/releases", json={
        "version": "bad-dup", "nodes": [{"name": "a"}, {"name": "b"}], "targets": ["a", "a"]
    })
    assert r.status_code == 422 and r.json()["error"]["code"] == "duplicate_target"
    assert r.json()["error"]["targets"] == ["a"]
    # 空白目标名 -> 422（请求模型校验）
    assert client.post("/api/releases", json={
        "version": "bad-blank", "nodes": [{"name": "a"}], "targets": ["  "]
    }).status_code == 422
    # 非字符串 / 空字符串元素 -> 422（请求模型校验）
    assert client.post("/api/releases", json={
        "version": "bad-type", "nodes": [{"name": "a"}], "targets": [123]
    }).status_code == 422
    assert client.post("/api/releases", json={
        "version": "bad-type", "nodes": [{"name": "a"}], "targets": [""]
    }).status_code == 422
    # 目标不存在 -> 422 unknown_target，且整批零写入
    before = client.get("/api/releases?limit=200").json()["items"]
    r = client.post("/api/releases", json={
        "version": "bad-ghost",
        "nodes": [{"name": "a"}, {"name": "b"}],
        "dependencies": [{"from_node": "a", "to_node": "b"}],
        "targets": ["b", "ghost"],
    })
    assert r.status_code == 422 and r.json()["error"]["code"] == "unknown_target"
    assert r.json()["error"]["targets"] == ["ghost"]
    after = client.get("/api/releases?limit=200").json()["items"]
    assert len(after) == len(before), "unknown_target 必须整批零写入"
    assert all(i["version"] != "bad-ghost" for i in after)
    print("[OK] empty/duplicate/blank targets 与 unknown_target(422, 零写入) 校验")

    # 即使环在与目标无关的分支上，也先按整图规则拒绝（先校验完整图再裁剪）
    r = client.post("/api/releases", json={
        "version": "bad-tgt-cycle",
        "nodes": [{"name": "a"}, {"name": "x"}, {"name": "y"}],
        "dependencies": [
            {"from_node": "x", "to_node": "y"},
            {"from_node": "y", "to_node": "x"},
        ],
        "targets": ["a"],
    })
    assert r.status_code == 422 and r.json()["error"]["code"] == "cycle_detected"
    # 悬空边同样先于裁剪被拒绝
    r = client.post("/api/releases", json={
        "version": "bad-tgt-edge",
        "nodes": [{"name": "a"}, {"name": "b"}],
        "dependencies": [{"from_node": "a", "to_node": "zzz"}],
        "targets": ["a"],
    })
    assert r.status_code == 422 and r.json()["error"]["code"] == "unknown_node"
    print("[OK] targets 裁剪前先校验完整图：无关分支环 / 悬空边均 422")

    # --- 12f. 显式传 null 与不传等价：全量发布 ---
    r = client.post("/api/releases", json={
        "version": "v-tgt-null",
        "nodes": [{"name": "a"}, {"name": "b"}],
        "dependencies": [{"from_node": "a", "to_node": "b"}],
        "targets": None,
    })
    assert r.status_code == 201, r.text
    tb4 = r.json()
    ridt4 = tb4["id"]
    assert tb4["targets"] is None and tb4["excluded_node_count"] == 0
    assert {n["name"] for n in tb4["nodes"]} == {"a", "b"}
    assert all(n["is_target"] is False for n in tb4["nodes"])
    print("[OK] targets=null 与不传一致：全量发布、targets=null、is_target 全 false")

    # --- 13. 列表 & 404 ---
    items = client.get("/api/releases").json()["items"]
    assert {i["id"] for i in items} == {rid, rid2, rid3, cid, tid, rid_dur,
                                        ridt, ridt2, ridt3, ridt4}
    assert client.get("/api/releases/nope").status_code == 404
    assert client.post("/api/releases/nope/report",
                       json={"node": "a", "status": "success", "version": "v"}).status_code == 404
    print("[OK] 列表接口 / 404")

# --- 14. 进程重启场景：对同一 SQLite 文件重新 init_db，数据仍在且表结构幂等 ---
from app import database  # noqa: E402

database.init_db(os.environ["DB_PATH"])
conn = database.get_conn()
count = conn.execute("SELECT COUNT(*) AS c FROM releases").fetchone()["c"]
assert count == 10, count
with database.lock():
    v1 = conn.execute("SELECT status, version FROM releases WHERE version='v1'").fetchone()
assert dict(v1) == {"status": "succeeded", "version": "v1"}
print("[OK] SQLite 重新初始化幂等，历史数据保留（10 个批次）")

# --- 15. 旧版 DB 文件（nodes 无 estimated_seconds 列）启动自动迁移，旧工期按 0 ---
import sqlite3  # noqa: E402

old_db = os.path.join(_tmp, "legacy.db")
_legacy = sqlite3.connect(old_db)
_legacy.executescript("""
CREATE TABLE releases (id TEXT PRIMARY KEY, version TEXT NOT NULL, status TEXT NOT NULL,
  levels_json TEXT NOT NULL DEFAULT '[]', error_code TEXT, error_message TEXT,
  failed_node TEXT, failed_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE nodes (id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT NOT NULL,
  name TEXT NOT NULL, node_type TEXT NOT NULL DEFAULT 'service', level INTEGER NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'pending',
  reported_version TEXT, error_code TEXT, error_message TEXT, reported_at TEXT,
  updated_at TEXT NOT NULL, UNIQUE(release_id, name));
CREATE TABLE edges (id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT NOT NULL,
  from_node TEXT, to_node TEXT, UNIQUE(release_id, from_node, to_node));
INSERT INTO releases VALUES ('leg1','ver','pending','[["x"],["y"]]',NULL,NULL,NULL,NULL,'t','t');
INSERT INTO nodes (release_id,name,node_type,level,metadata_json,status,updated_at)
  VALUES ('leg1','x','service',0,'{}','pending','t'),
         ('leg1','y','service',1,'{}','pending','t');
INSERT INTO edges (release_id,from_node,to_node) VALUES ('leg1','x','y');
""")
_legacy.commit()
_legacy.close()

from app import config  # noqa: E402

config.DB_PATH = old_db
database.init_db(old_db)
cols = {row["name"] for row in database.get_conn().execute("PRAGMA table_info(nodes)")}
assert "estimated_seconds" in cols, cols
assert "is_target" in cols, cols  # 旧库幂等补列
rel_cols = {row["name"] for row in database.get_conn().execute("PRAGMA table_info(releases)")}
assert {"targets_json", "excluded_node_count"} <= rel_cols, rel_cols
with TestClient(app) as legacy_client:
    lg0 = legacy_client.get("/api/releases/leg1").json()
    # 旧批次：无 targets 概念，按全量发布兼容
    assert lg0["targets"] is None and lg0["excluded_node_count"] == 0
    assert all(n["is_target"] is False for n in lg0["nodes"])
    lp = legacy_client.get("/api/releases/leg1/plan").json()
    assert lp["targets"] is None and lp["excluded_node_count"] == 0
    assert all(n["is_target"] is False for n in lp["nodes"])
    assert lp["schedule"]["shortest_duration_seconds"] == 0
    assert lp["schedule"]["critical_nodes"] == ["x", "y"]
    assert lp["schedule"]["critical_path"] == ["x", "y"]
    assert [n["estimated_seconds"] for n in lp["nodes"]] == [0, 0]
    zero_deadline = legacy_client.get(
        "/api/releases/leg1/plan?deadline_seconds=0"
    ).json()["schedule"]
    assert zero_deadline["feasible"] is True and zero_deadline["gap_seconds"] == 0
print("[OK] 旧库自动迁移 estimated_seconds/is_target/targets 列，旧批次按全量发布兼容且 CPM 完整")

# --- 16. 有限自动重试：max_attempts / retry_delay_seconds 校验与持久化 ---
# 第 15 步把全局连接切到了 legacy.db，先切回主测试库
config.DB_PATH = os.environ["DB_PATH"]
database.init_db(os.environ["DB_PATH"])
conn = database.get_conn()

assert client.post("/api/releases", json={
    "version": "bad-ma", "nodes": [{"name": "a", "max_attempts": 0}]}).status_code == 422
assert client.post("/api/releases", json={
    "version": "bad-ma", "nodes": [{"name": "a", "max_attempts": 11}]}).status_code == 422
assert client.post("/api/releases", json={
    "version": "bad-rd", "nodes": [{"name": "a", "retry_delay_seconds": -1}]}).status_code == 422
assert client.post("/api/releases", json={
    "version": "bad-rd", "nodes": [{"name": "a", "retry_delay_seconds": 3601}]}).status_code == 422

r = client.post("/api/releases", json={
    "version": "v-retry",
    "nodes": [
        {"name": "a", "max_attempts": 3, "retry_delay_seconds": 3600},
        {"name": "b"}],
    "dependencies": [{"from_node": "a", "to_node": "b"}],
})
assert r.status_code == 201, r.text
ridr = r.json()["id"]
na = next(n for n in r.json()["nodes"] if n["name"] == "a")
assert na["max_attempts"] == 3 and na["retry_delay_seconds"] == 3600
assert na["attempt_count"] == 0 and na["retry_scheduled"] is False
assert na["next_retry_at"] is None
# 未传时默认 max_attempts=1 / retry_delay_seconds=0
nb = next(n for n in r.json()["nodes"] if n["name"] == "b")
assert nb["max_attempts"] == 1 and nb["retry_delay_seconds"] == 0
print("[OK] max_attempts(1~10) / retry_delay_seconds(0~3600) 校验与默认值持久化")

# --- 17. 首次失败（未达上限）：不回滚、恢复 pending、排除出 next_nodes、到期前 409 ---
r = client.post(f"/api/releases/{ridr}/report",
                json={"node": "a", "status": "failed", "version": "v-retry",
                      "error_code": "BOOM", "error_message": "第一次失败"})
assert r.status_code == 200, r.text
sched = r.json()
assert sched["result"] == "retry_scheduled"
assert sched["rollback_scope"] == "none" and sched["rolled_back_nodes"] == []
assert sched["attempt_count"] == 1 and sched["max_attempts"] == 3
assert sched["next_retry_at"] is not None
assert sched["release"]["status"] == "running"  # 未触发批次失败 / 回滚
na = next(n for n in sched["release"]["nodes"] if n["name"] == "a")
assert na["status"] == "pending"
assert na["attempt_count"] == 1 and na["retry_scheduled"] is True
assert na["next_retry_at"] == sched["next_retry_at"]
assert na["error_code"] == "BOOM"  # 最近一次失败信息保留在等待中的节点上
# 未到期：a 不进入 next_nodes（其前置为空，但仍被排除）
assert sched["release"]["next_nodes"] == []

# 到期前回报（无论成功失败）-> 409 retry_not_due，状态不变
r = client.post(f"/api/releases/{ridr}/report",
                json={"node": "a", "status": "success", "version": "v-retry"})
assert r.status_code == 409 and r.json()["error"]["code"] == "retry_not_due"
assert r.json()["error"]["next_retry_at"] == sched["next_retry_at"]
assert r.json()["error"]["attempt_count"] == 1
r = client.post(f"/api/releases/{ridr}/report",
                json={"node": "a", "status": "failed", "version": "v-retry"})
assert r.status_code == 409 and r.json()["error"]["code"] == "retry_not_due"
# 409 不改变状态：attempt_count 仍为 1，批次仍 running，a 仍被排除
st = client.get(f"/api/releases/{ridr}").json()
na = next(n for n in st["nodes"] if n["name"] == "a")
assert na["attempt_count"] == 1 and na["retry_scheduled"] is True
assert st["status"] == "running" and st["next_nodes"] == []
print("[OK] 首次失败 -> retry_scheduled（不回滚/pending/排除 next_nodes），到期前 409 retry_not_due 且状态不变")

# --- 18. 到期后节点重新出现，继续失败到上限 -> 现有失败回滚语义 ---
conn.execute("UPDATE nodes SET next_retry_at = ? WHERE release_id = ? AND name = 'a'",
             ("2000-01-01T00:00:00+00:00", ridr))
conn.commit()
st = client.get(f"/api/releases/{ridr}").json()
assert st["next_nodes"] == ["a"]  # 到期且无前置 -> 重新出现

r = client.post(f"/api/releases/{ridr}/report",
                json={"node": "a", "status": "failed", "version": "v-retry"})
assert r.json()["result"] == "retry_scheduled"
assert r.json()["attempt_count"] == 2  # 原子递增
conn.execute("UPDATE nodes SET next_retry_at = ? WHERE release_id = ? AND name = 'a'",
             ("2000-01-01T00:00:00+00:00", ridr))
conn.commit()

# 第三次失败：attempt_count(3) 达到 max_attempts(3) -> 终态回滚
r = client.post(f"/api/releases/{ridr}/report",
                json={"node": "a", "status": "failed", "version": "v-retry",
                      "error_code": "FATAL"})
assert r.status_code == 200, r.text
fb = r.json()
assert fb["result"] == "failed_and_rolled_back"
assert fb["release"]["status"] == "failed"  # 此前无成功节点
na = next(n for n in fb["release"]["nodes"] if n["name"] == "a")
assert na["status"] == "failed" and na["attempt_count"] == 3
assert na["retry_scheduled"] is False and na["next_retry_at"] is None
assert na["error_code"] == "FATAL"
# 终态后回报仍是 release_aborted（未被重试逻辑旁路）
r = client.post(f"/api/releases/{ridr}/report",
                json={"node": "a", "status": "success", "version": "v-retry"})
assert r.status_code == 409 and r.json()["error"]["code"] == "release_aborted"
print("[OK] 到期重新出现 -> 第 2 次仍调度；第 3 次达上限走失败终态与回滚 checkpoint 语义")

# --- 19. 手动 /retry 续发后 attempt_count 清零，重新获得完整自动重试额度 ---
r = client.post(f"/api/releases/{ridr}/retry")
assert r.status_code == 200
na = next(n for n in r.json()["release"]["nodes"] if n["name"] == "a")
assert na["status"] == "pending" and na["attempt_count"] == 0
assert na["retry_scheduled"] is False and na["next_retry_at"] is None
r = client.post(f"/api/releases/{ridr}/report",
                json={"node": "a", "status": "failed", "version": "v-retry"})
assert r.json()["result"] == "retry_scheduled"
assert r.json()["attempt_count"] == 1  # 重新从 1 计数
print("[OK] /retry 续发重置 attempt_count/retry_scheduled/next_retry_at，重试额度刷新")

# --- 20. retry_delay_seconds=0：失败立即可再次回报（next_retry_at 已到期）---
r = client.post("/api/releases", json={
    "version": "v-zero",
    "nodes": [{"name": "z", "max_attempts": 2, "retry_delay_seconds": 0}],
})
ridz = r.json()["id"]
r = client.post(f"/api/releases/{ridz}/report",
                json={"node": "z", "status": "failed", "version": "v-zero"})
assert r.json()["result"] == "retry_scheduled"
assert r.json()["release"]["next_nodes"] == ["z"]  # 延迟 0：到期立即可见
r = client.post(f"/api/releases/{ridz}/report",
                json={"node": "z", "status": "success", "version": "v-zero"})
assert r.status_code == 200 and r.json()["release"]["status"] == "succeeded"
nz = next(n for n in r.json()["release"]["nodes"] if n["name"] == "z")
assert nz["attempt_count"] == 1 and nz["retry_scheduled"] is False  # 成功后清除等待标记
print("[OK] retry_delay_seconds=0：失败后立即重回 next_nodes；成功清除重试标记并完成批次")

# --- 21. 旧批次兼容：迁移后的库 max_attempts 缺省为 1，首次失败即终态回滚 ---
# 切回第 15 步建立的 legacy.db
config.DB_PATH = old_db
database.init_db(old_db)
with TestClient(app) as legacy_client:
    # leg1 在第 15 步建立：x(level0)->y(level1)，迁移后重试字段取默认值
    lg = legacy_client.get("/api/releases/leg1").json()
    lx = next(n for n in lg["nodes"] if n["name"] == "x")
    assert lx["max_attempts"] == 1 and lx["attempt_count"] == 0
    assert lx["retry_delay_seconds"] == 0 and lx["retry_scheduled"] is False
    assert lx["next_retry_at"] is None
    r = legacy_client.post("/api/releases/leg1/report",
                           json={"node": "x", "status": "failed", "version": "ver"})
    assert r.status_code == 200, r.text
    assert r.json()["result"] == "failed_and_rolled_back"
    assert r.json()["release"]["status"] == "failed"
print("[OK] 旧批次 max_attempts 默认 1：首次失败即终态回滚，详情返回全部重试字段")

# --- 22. 发布批次结构差异与影响面：GET /api/releases/{id}/diff ---
# 切回主测试库（第 21 步把连接切到了 legacy.db）
config.DB_PATH = os.environ["DB_PATH"]
database.init_db(os.environ["DB_PATH"])

# 基线批次：a,b -> c -> d；e 独立
r = client.post("/api/releases", json={
    "version": "v-diff-base",
    "nodes": [
        {"name": "a", "node_type": "service", "estimated_seconds": 2,
         "max_attempts": 1, "retry_delay_seconds": 0, "metadata": {"team": "infra"}},
        {"name": "b"}, {"name": "c"}, {"name": "d"}, {"name": "e"},
    ],
    "dependencies": [
        {"from_node": "a", "to_node": "c"},
        {"from_node": "b", "to_node": "c"},
        {"from_node": "c", "to_node": "d"},
    ],
})
dbid = r.json()["id"]

# 目标批次：a 四个标量字段 + 元数据变化；c 前置集合变化（b 移除、n1 新增）；
# d 新增前置 n2；b/e 被移除；n1/n2 新增；d 自身不变但在 c 的下游
r = client.post("/api/releases", json={
    "version": "v-diff-target",
    "nodes": [
        {"name": "a", "node_type": "job", "estimated_seconds": 5,
         "max_attempts": 3, "retry_delay_seconds": 30,
         "metadata": {"team": "infra", "region": "cn"}},
        {"name": "c"}, {"name": "d"}, {"name": "n1"}, {"name": "n2"},
    ],
    "dependencies": [
        {"from_node": "a", "to_node": "c"},
        {"from_node": "n1", "to_node": "c"},
        {"from_node": "c", "to_node": "d"},
        {"from_node": "n2", "to_node": "d"},
    ],
})
dtid = r.json()["id"]

d = client.get(f"/api/releases/{dtid}/diff?base_release_id={dbid}").json()
assert d["base_release_id"] == dbid and d["target_release_id"] == dtid
assert d["base_version"] == "v-diff-base" and d["target_version"] == "v-diff-target"
assert d["added"] == ["n1", "n2"]
assert d["removed"] == ["b", "e"]
changed_map = {c_["node"]: c_["reasons"] for c_ in d["changed"]}
assert sorted(changed_map) == ["a", "c", "d"]
assert [x["field"] for x in changed_map["a"]] == [
    "node_type", "estimated_seconds", "max_attempts",
    "retry_delay_seconds", "metadata",
]
type_reason = next(x for x in changed_map["a"] if x["field"] == "node_type")
assert type_reason["base"] == "service" and type_reason["target"] == "job"
dep_reason = next(x for x in changed_map["c"] if x["field"] == "dependencies")
assert dep_reason["base"] == ["a", "b"] and dep_reason["target"] == ["a", "n1"]
dep_reason_d = next(x for x in changed_map["d"] if x["field"] == "dependencies")
assert dep_reason_d["base"] == ["c"] and dep_reason_d["target"] == ["c", "n2"]
# 影响面：seeds = added{n1,n2} ∪ changed{a,c,d}；沿目标图下游传播（含起点）
# a -> c -> d；n1 -> c -> d；n2 -> d；并集 {a,c,d,n1,n2}（b/e 为 removed，不参与）
assert d["impacted_nodes"] == ["a", "c", "d", "n1", "n2"], d["impacted_nodes"]
assert d["counts"] == {"added": 2, "removed": 2, "changed": 3, "impacted_nodes": 5}
print("[OK] diff：added/removed/changed 字段级 reasons、下游传递 impacted_nodes 与计数")

# 仅比较实际持久化的裁剪后图：目标按 targets=['c'] 裁剪，d 虽在提交图中但未入库 -> removed
r = client.post("/api/releases", json={
    "version": "v-diff-pruned",
    "nodes": [
        {"name": "a", "node_type": "service", "estimated_seconds": 2,
         "max_attempts": 1, "retry_delay_seconds": 0, "metadata": {"team": "infra"}},
        {"name": "b"}, {"name": "c"}, {"name": "d"},
    ],
    "dependencies": [
        {"from_node": "a", "to_node": "c"},
        {"from_node": "b", "to_node": "c"},
        {"from_node": "c", "to_node": "d"},
    ],
    "targets": ["c"],
})
dpid = r.json()["id"]
d2 = client.get(f"/api/releases/{dpid}/diff?base_release_id={dbid}").json()
assert d2["added"] == [] and d2["removed"] == ["d", "e"], d2
assert d2["changed"] == [] and d2["impacted_nodes"] == []
assert d2["counts"] == {"added": 0, "removed": 2, "changed": 0, "impacted_nodes": 0}
print("[OK] diff 仅比较持久化的裁剪后图：未入库的 d 计为 removed，无 added/changed 时影响面为空")

# 完全相同的两个批次（含 targets） -> 全部为空
r = client.post("/api/releases", json={
    "version": "v-diff-same",
    "nodes": [
        {"name": "a", "node_type": "service", "estimated_seconds": 2,
         "metadata": {"team": "infra"}},
        {"name": "b"}, {"name": "c"}, {"name": "d"}, {"name": "e"},
    ],
    "dependencies": [
        {"from_node": "a", "to_node": "c"},
        {"from_node": "b", "to_node": "c"},
        {"from_node": "c", "to_node": "d"},
    ],
})
sameid = r.json()["id"]
d3 = client.get(f"/api/releases/{sameid}/diff?base_release_id={dbid}").json()
assert d3["added"] == [] and d3["removed"] == [] and d3["changed"] == []
assert d3["impacted_nodes"] == []
# 自身与自身比较
dself = client.get(f"/api/releases/{dbid}/diff?base_release_id={dbid}").json()
assert dself["added"] == dself["removed"] == dself["changed"] == []
assert dself["impacted_nodes"] == []
print("[OK] 相同批次 / 自身比较：差异与影响面均为空")

# removed 不映射到目标图中不存在的节点：基线 a->c->d，目标只有 a
r = client.post("/api/releases", json={
    "version": "v-diff-shrink",
    "nodes": [
        {"name": "a", "node_type": "service", "estimated_seconds": 2,
         "max_attempts": 1, "retry_delay_seconds": 0, "metadata": {"team": "infra"}},
    ],
})
shid = r.json()["id"]
d4 = client.get(f"/api/releases/{shid}/diff?base_release_id={dbid}").json()
assert d4["added"] == [] and d4["removed"] == ["b", "c", "d", "e"]
assert d4["changed"] == [] and d4["impacted_nodes"] == []
print("[OK] removed 单独报告且不参与影响面传播")

# 只读：批次状态 / 节点状态在 diff 前后不变
assert client.get(f"/api/releases/{dbid}").json()["status"] == "pending"
assert client.get(f"/api/releases/{dtid}").json()["status"] == "pending"

# 缺失批次沿用 404；缺少 query 参数 -> 422
assert client.get("/api/releases/ghost/diff",
                  params={"base_release_id": dbid}).status_code == 404
r = client.get(f"/api/releases/{dtid}/diff?base_release_id=ghost")
assert r.status_code == 404 and r.json()["error"]["code"] == "release_not_found"
assert client.get(f"/api/releases/{dtid}/diff").status_code == 422
assert client.get(f"/api/releases/{dtid}/diff",
                  params={"base_release_id": " "}).status_code == 404  # 非空但不存在 -> 404
print("[OK] diff 只读不改状态；缺失批次 404、缺少 base_release_id 422、空白值按不存在 404")

# --- 23. 安全取消：pending 批次整批取消 ---
r = client.post("/api/releases", json={
    "version": "v-cancel-pending",
    "nodes": [{"name": "p1"}, {"name": "p2"}],
    "dependencies": [{"from_node": "p1", "to_node": "p2"}],
})
rcp = r.json()["id"]
assert r.json()["revision"] == 1  # 新建批次修订号从 1 开始
r = client.post(f"/api/releases/{rcp}/cancel",
                json={"reason": "需求撤回", "expected_revision": 1})
assert r.status_code == 200, r.text
cb = r.json()
assert cb["result"] == "cancelled" and cb["revision"] == 2
assert cb["reason"] == "需求撤回" and cb["cancelled_at"] is not None
assert [x["node"] for x in cb["cancelled_nodes"]] == ["p1", "p2"]
assert [x["level"] for x in cb["cancelled_nodes"]] == [0, 1]
assert cb["rolled_back_nodes"] == []  # 尚未执行任何节点
st = client.get(f"/api/releases/{rcp}").json()
assert st["status"] == "cancelled" and st["revision"] == 2
assert st["cancel_reason"] == "需求撤回" and st["cancelled_at"] == cb["cancelled_at"]
assert {n["name"]: n["status"] for n in st["nodes"]} == {"p1": "cancelled", "p2": "cancelled"}
assert st["next_nodes"] == []
assert st["progress"] == {"succeeded": 0, "total": 2, "completed": False}
# 列表接口同样展示修订号与取消信息
item = next(i for i in client.get("/api/releases").json()["items"] if i["id"] == rcp)
assert item["revision"] == 2 and item["cancel_reason"] == "需求撤回" and item["cancelled_at"]
print("[OK] pending 批次取消：未执行节点标 cancelled、revision 1->2、状态查询含原因/时间")

# 取消后的批次拒绝 report / retry（409 release_cancelled）
r = client.post(f"/api/releases/{rcp}/report",
                json={"node": "p1", "status": "success", "version": "v-cancel-pending"})
assert r.status_code == 409 and r.json()["error"]["code"] == "release_cancelled"
assert r.json()["error"]["cancelled_at"] == cb["cancelled_at"]
r = client.post(f"/api/releases/{rcp}/retry")
assert r.status_code == 409 and r.json()["error"]["code"] == "release_cancelled"
# 取消后的状态不变（409 无任何写入）
st2 = client.get(f"/api/releases/{rcp}").json()
assert st2["revision"] == 2 and st2["status"] == "cancelled"
print("[OK] 取消后 report / retry -> 409 release_cancelled，状态不变")

# 重复取消（带最新修订号）-> 409 release_cancelled；取消原因等状态不变
r = client.post(f"/api/releases/{rcp}/cancel",
                json={"reason": "再次取消", "expected_revision": 2})
assert r.status_code == 409 and r.json()["error"]["code"] == "release_cancelled"
assert client.get(f"/api/releases/{rcp}").json()["cancel_reason"] == "需求撤回"

# 修订号过期（即便批次也已处于取消终态）-> 先命中 409 state_conflict，状态不变
r = client.post(f"/api/releases/{rcp}/cancel",
                json={"reason": "旧修订号", "expected_revision": 99})
assert r.status_code == 409 and r.json()["error"]["code"] == "state_conflict"
assert r.json()["error"]["current_revision"] == 2
assert client.get(f"/api/releases/{rcp}").json()["revision"] == 2
print("[OK] 已取消批次：匹配修订号重复取消 -> 409 release_cancelled；过期修订号 -> 409 state_conflict")

# --- 24. running 批次取消：未执行标 cancelled，已成功节点逆拓扑回滚 ---
# 图：a,b(level0) -> c(level1) -> d(level2)；a、b 已成功，c、d 未执行
r = client.post("/api/releases", json={
    "version": "v-cancel-run",
    "nodes": [{"name": n} for n in ("a", "b", "c", "d")],
    "dependencies": [
        {"from_node": "a", "to_node": "c"},
        {"from_node": "b", "to_node": "c"},
        {"from_node": "c", "to_node": "d"},
    ],
})
rcr = r.json()["id"]
assert client.post(f"/api/releases/{rcr}/report",
                   json={"node": "a", "status": "success",
                         "version": "v-cancel-run"}).status_code == 200
assert client.post(f"/api/releases/{rcr}/report",
                   json={"node": "b", "status": "success",
                         "version": "v-cancel-run"}).status_code == 200
st = client.get(f"/api/releases/{rcr}").json()
assert st["status"] == "running" and st["revision"] == 3
# 过期修订号 -> 409 state_conflict，状态不变
r = client.post(f"/api/releases/{rcr}/cancel",
                json={"reason": "线上故障中止", "expected_revision": 1})
assert r.status_code == 409, r.text
err = r.json()["error"]
assert err["code"] == "state_conflict"
assert err["expected_revision"] == 1 and err["current_revision"] == 3
st_after_stale = client.get(f"/api/releases/{rcr}").json()
assert st_after_stale["status"] == "running" and st_after_stale["revision"] == 3
assert {n["name"]: n["status"] for n in st_after_stale["nodes"]} == {
    "a": "success", "b": "success", "c": "pending", "d": "pending"}
print("[OK] 过期修订号 -> 409 state_conflict（携带两侧修订号），批次/节点状态不变")

# 正确修订号取消：c/d 标 cancelled，b、a 逆拓扑回滚（同层按成功时间倒序）
r = client.post(f"/api/releases/{rcr}/cancel",
                json={"reason": "线上故障中止", "expected_revision": 3})
assert r.status_code == 200, r.text
cb = r.json()
assert cb["revision"] == 4
assert [x["node"] for x in cb["cancelled_nodes"]] == ["c", "d"]
assert [x["level"] for x in cb["cancelled_nodes"]] == [1, 2]
assert [x["node"] for x in cb["rolled_back_nodes"]] == ["b", "a"]
assert [x["level"] for x in cb["rolled_back_nodes"]] == [0, 0]
st = client.get(f"/api/releases/{rcr}").json()
assert st["status"] == "cancelled" and st["revision"] == 4
assert st["cancel_reason"] == "线上故障中止" and st["cancelled_at"]
assert {n["name"]: n["status"] for n in st["nodes"]} == {
    "a": "rolled_back", "b": "rolled_back", "c": "cancelled", "d": "cancelled"}
print("[OK] running 批次取消：未执行 cancelled、已成功逆拓扑回滚（b 先于 a），revision=4")

# --- 25. 终态批次不可取消；请求校验；404 ---
r = client.post("/api/releases", json={
    "version": "v-cancel-done", "nodes": [{"name": "z1"}]})
rzd = r.json()["id"]
assert client.post(f"/api/releases/{rzd}/report",
                   json={"node": "z1", "status": "success",
                         "version": "v-cancel-done"}).status_code == 200
st = client.get(f"/api/releases/{rzd}").json()
r = client.post(f"/api/releases/{rzd}/cancel",
                json={"reason": "x", "expected_revision": st["revision"]})
assert r.status_code == 409 and r.json()["error"]["code"] == "cancel_not_allowed"
assert r.json()["error"]["status"] == "succeeded"
assert client.get(f"/api/releases/{rzd}").json()["status"] == "succeeded"  # 状态不变
# failed / partially_rolled_back 也不可取消（只能 /retry）
r = client.post("/api/releases", json={
    "version": "v-cancel-fail", "nodes": [{"name": "f1"}]})
rcf = r.json()["id"]
assert client.post(f"/api/releases/{rcf}/report",
                   json={"node": "f1", "status": "failed",
                         "version": "v-cancel-fail"}).status_code == 200
r = client.post(f"/api/releases/{rcf}/cancel",
                json={"reason": "x", "expected_revision": 2})
assert r.status_code == 409 and r.json()["error"]["code"] == "cancel_not_allowed"
assert client.get(f"/api/releases/{rcf}").json()["status"] == "failed"
# 404 / 请求体校验
assert client.post("/api/releases/ghost/cancel",
                   json={"reason": "x", "expected_revision": 1}).status_code == 404
assert client.post(f"/api/releases/{rzd}/cancel", json={}).status_code == 422
assert client.post(f"/api/releases/{rzd}/cancel",
                   json={"reason": "x", "expected_revision": 0}).status_code == 422
assert client.post(f"/api/releases/{rzd}/cancel",
                   json={"reason": "x", "expected_revision": "abc"}).status_code == 422
# reason 非空字符串（沿用 version/node 的 min_length=1 约定，不做 strip 拦截），
# 但批次已 succeeded -> 走到状态校验返回 409
assert client.post(f"/api/releases/{rzd}/cancel",
                   json={"reason": "   ", "expected_revision": 1}).status_code == 409
print("[OK] succeeded/failed 批次取消 -> 409 cancel_not_allowed；404 / 请求体 422 校验")

# --- 26. 取消与节点回报并发：条件状态迁移保证只有一方成功 ---
# 写事务经 service 层同一把 RLock 串行；直接从两个线程并发调用 service 函数制造竞争
from app import service as _service  # noqa: E402
from app.models import CancelReleaseRequest, ReportRequest  # noqa: E402
import threading  # noqa: E402

# cancel(revision=1) vs cancel(revision=1)：恰一个成功，落败方 state_conflict
for i in range(10):
    rid_i = client.post("/api/releases", json={
        "version": f"v-race-cc-{i}", "nodes": [{"name": "n0"}]}).json()["id"]
    results_i: dict = {}
    barrier_i = threading.Barrier(2)

    def _cancel(tag: str) -> None:
        barrier_i.wait()
        try:
            _service.cancel_release(
                rid_i, CancelReleaseRequest(reason="race", expected_revision=1))
            results_i[tag] = "ok"
        except Exception as exc:  # noqa: BLE001
            results_i[tag] = exc.code

    t1 = threading.Thread(target=_cancel, args=("c1",))
    t2 = threading.Thread(target=_cancel, args=("c2",))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert sorted(results_i.values()) == ["ok", "state_conflict"], results_i
    st_i = client.get(f"/api/releases/{rid_i}").json()
    assert st_i["status"] == "cancelled" and st_i["revision"] == 2, st_i

# cancel(revision=1) vs report(failed)：恰好一次终态迁移，revision 恰好 +1
for i in range(10):
    rid_i = client.post("/api/releases", json={
        "version": f"v-race-cf-{i}", "nodes": [{"name": "n0"}]}).json()["id"]
    results_i = {}
    barrier_i = threading.Barrier(2)

    def _cancel_i() -> None:
        barrier_i.wait()
        try:
            _service.cancel_release(
                rid_i, CancelReleaseRequest(reason="race", expected_revision=1))
            results_i["cancel"] = "ok"
        except Exception as exc:  # noqa: BLE001
            results_i["cancel"] = exc.code

    def _fail_i() -> None:
        barrier_i.wait()
        try:
            _service.report_node(
                rid_i, ReportRequest(node="n0", status="failed",
                                     version=f"v-race-cf-{i}"))
            results_i["fail"] = "ok"
        except Exception as exc:  # noqa: BLE001
            results_i["fail"] = exc.code

    t1 = threading.Thread(target=_cancel_i)
    t2 = threading.Thread(target=_fail_i)
    t1.start(); t2.start(); t1.join(); t2.join()
    st_i = client.get(f"/api/releases/{rid_i}").json()
    assert st_i["revision"] == 2, st_i  # 只有一方迁移成功
    if st_i["status"] == "cancelled":
        assert results_i == {"cancel": "ok", "fail": "release_cancelled"}, results_i
        assert st_i["nodes"][0]["status"] == "cancelled"
    else:
        assert st_i["status"] == "failed", st_i
        assert results_i == {"cancel": "state_conflict", "fail": "ok"}, results_i
        assert st_i["nodes"][0]["status"] == "failed"
print("[OK] 取消 vs 取消 / 取消 vs 失败回报并发：仅一个状态迁移成功，revision 恰好 +1")

# --- 27. 自动重试等待中的节点取消：随未执行节点标 cancelled，清除等待标记 ---
r = client.post("/api/releases", json={
    "version": "v-cancel-retry",
    "nodes": [{"name": "w1", "max_attempts": 3, "retry_delay_seconds": 3600},
              {"name": "w2"}],
})
ridw = r.json()["id"]
assert client.post(f"/api/releases/{ridw}/report",
                   json={"node": "w1", "status": "failed",
                         "version": "v-cancel-retry"}).status_code == 200
st = client.get(f"/api/releases/{ridw}").json()
assert st["revision"] == 2 and st["status"] == "running"
nw1 = next(n for n in st["nodes"] if n["name"] == "w1")
assert nw1["retry_scheduled"] is True and nw1["next_retry_at"]
r = client.post(f"/api/releases/{ridw}/cancel",
                json={"reason": "重试也不要了", "expected_revision": 2})
assert r.status_code == 200, r.text
cb = r.json()
assert sorted(x["node"] for x in cb["cancelled_nodes"]) == ["w1", "w2"]
st = client.get(f"/api/releases/{ridw}").json()
nw1 = next(n for n in st["nodes"] if n["name"] == "w1")
assert nw1["status"] == "cancelled" and nw1["retry_scheduled"] is False
assert nw1["next_retry_at"] is None and nw1["attempt_count"] == 1  # 历史保留
# 取消后该节点无法再借到期回报
r = client.post(f"/api/releases/{ridw}/report",
                json={"node": "w1", "status": "success", "version": "v-cancel-retry"})
assert r.status_code == 409 and r.json()["error"]["code"] == "release_cancelled"
print("[OK] 自动重试等待期节点随取消标 cancelled（清等待标记、留 attempt_count 历史）")

print("\n全部测试通过 ✔")
