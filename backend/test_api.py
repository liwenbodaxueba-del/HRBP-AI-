# -*- coding: utf-8 -*-
"""后端全量测试（TestClient 进程内·独立测试库·无需起服务）：python test_api.py"""
import os
import sys

TEST_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hcfb_test.db")
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
os.environ["HCFB_DB"] = TEST_DB

import app  # noqa: E402  （导入即建库）
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app.app)
H = {"X-User": "bonniewbli"}
PASS, FAIL = [], []


def check(name, cond, info=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS" if cond else "FAIL"), name, info)


def j(r):
    try:
        return r.json()
    except Exception:
        return {}


# ========== 基础 ==========
r = client.get("/api/health")
check("health", r.status_code == 200 and j(r)["ok"])
r = client.get("/api/config")
check("config 13项目+账号", len(j(r)["projs"]) == 13 and j(r)["accts"][0]["id"] == "bonniewbli")
r = client.get("/api/board/2026")
b = j(r)
check("空板 lock=6 识空", b["lock"] == 6 and all(v is None for v in b["computed"]["chain"]))

# ========== 校验闸 ==========
check("锁定月拒绝423", client.post("/api/board/2026/cell", json={"metric": "o_bp", "month": 3, "value": 2, "note": "x"}, headers=H).status_code == 423)
check("备注必填422", client.post("/api/board/2026/cell", json={"metric": "o_bp", "month": 9, "value": 5, "note": ""}, headers=H).status_code == 422)
check("系统数拒手填403", client.post("/api/board/2026/cell", json={"metric": "o_sys", "month": 9, "value": 5, "note": "x"}, headers=H).status_code == 403)
check("量级异常422", client.post("/api/board/2026/cell", json={"metric": "o_bp", "month": 9, "value": 999999, "note": "x"}, headers=H).status_code == 422)
check("未配置账号403", client.post("/api/board/2026/cell", json={"metric": "o_bp", "month": 9, "value": 1, "note": "x"}, headers={"X-User": "nobody"}).status_code == 403)

# ========== 计划类豁免锁定（看板2 基线 / 预算） ==========
r = client.post("/api/board/2026/cell", json={"metric": "budget", "month": 1, "value": 544, "note": "预算1月"}, headers=H)
check("budget 拒手填403(走导入/看板2)", r.status_code == 403)
r = client.post("/api/import/2026", files={"file": ("bg.csv", "metric,month,value\nbudget,1,544\n", "text/csv")}, headers=H)
check("budget 导入锁定月直接采纳(计划类豁免)", j(r)["applied"] == 1 and j(r)["diffs"] == 0)
r = client.post("/api/board/2026/cell", json={"metric": "fa_hc", "month": 1, "value": 479, "note": "期初法定HC"}, headers=H)
check("fa_hc(看板2基线) 1月可编辑", r.status_code == 200)

# ========== 有效录入 + 历史 ==========
r = client.post("/api/board/2026/cell", json={"metric": "o_bp", "month": 9, "value": 5, "note": "9月已谈妥5人"}, headers=H)
check("有效录入 outT9=5", j(r)["computed"]["outT"][8] == 5)
client.post("/api/board/2026/cell", json={"metric": "o_bp", "month": 9, "value": 7, "note": "改为7人"}, headers=H)
r = client.get("/api/board/2026/history", params={"metric": "o_bp", "month": 9})
h = j(r)["history"]
check("变更历史2条(5→7可追溯)", len(h) == 2 and h[0]["old_value"] == 5 and h[0]["new_value"] == 7, str([(x["old_value"], x["new_value"]) for x in h]))

# ========== 分支 + 停用还原 ==========
r = client.post("/api/board/2026/branch", json={"sec": "总流出（−）", "name": "组织架构腾挪", "sign": "-"}, headers=H)
bid = j(r)["branches"][0]["id"]
client.post("/api/board/2026/cell", json={"metric": f"branch:{bid}", "month": 9, "value": 2, "note": "冲抵2"}, headers=H)
r = client.get("/api/board/2026")
check("分支生效 outT9=5(7-2)", j(r)["computed"]["outT"][8] == 5)
r = client.post(f"/api/board/2026/branch/{bid}/toggle", headers=H)
check("停用分支后 outT9=7(不计入)", j(r)["computed"]["outT"][8] == 7)
r = client.post(f"/api/board/2026/branch/{bid}/toggle", headers=H)
check("还原分支后 outT9=5(数据保留)", j(r)["computed"]["outT"][8] == 5)

# ========== 导入 + 迟到数据回扫 ==========
bad = "metric,month,value\no_sys,13,4\nxxx,2,1\nactual,1,999999\n"
r = client.post("/api/import/2026", files={"file": ("bad.csv", bad, "text/csv")}, headers=H)
check("坏CSV整批拒绝(3错)", r.status_code == 422 and j(r)["detail"]["total_errors"] == 3)
good = "metric,month,value\n" + "".join(f"actual,{m},{548 - m}\n" for m in range(1, 7))
r = client.post("/api/import/2026", files={"file": ("snap.csv", good, "text/csv")}, headers=H)
check("首导全采纳(6格0差异)", j(r)["applied"] == 6 and j(r)["diffs"] == 0, str(j(r)))
# 二次导入：3月实际变化（锁定月）→ 差异不自动改；7月新值（未锁）→ 自动采纳
late = "metric,month,value\nactual,3,999\nactual,7,540\n"
r = client.post("/api/import/2026", files={"file": ("late.csv", late, "text/csv")}, headers=H)
check("迟到数据: 锁定月留差异+未锁月采纳", j(r)["diffs"] == 1 and j(r)["applied"] == 1, str(j(r)))
r = client.get("/api/board/2026")
check("锁定月3月未被自动改(545)", j(r)["metrics"]["actual"]["vals"][2] == 545)
r = client.get("/api/diffs/2026")
d = j(r)["diffs"]
check("差异清单1条(545 vs 999)", len(d) == 1 and d[0]["cur_value"] == 545 and d[0]["src_value"] == 999)
did = d[0]["id"]
r = client.post(f"/api/diffs/{did}/keep", headers=H)
check("维持口径", r.status_code == 200 and len(j(client.get("/api/diffs/2026"))["diffs"]) == 0)
# 再造一个差异走采纳
client.post("/api/import/2026", files={"file": ("late2.csv", "metric,month,value\nactual,3,999\n", "text/csv")}, headers=H)
did2 = j(client.get("/api/diffs/2026"))["diffs"][0]["id"]
r = client.post(f"/api/diffs/{did2}/accept", headers=H)
check("采纳修正后3月=999", j(r)["metrics"]["actual"]["vals"][2] == 999)
r = client.get("/api/board/2026/history", params={"metric": "actual", "month": 3})
check("采纳写入历史可追溯", any(x["note"] == "采纳修正（迟到数据）" for x in j(r)["history"]))

# ========== 月份确认锁定推进 ==========
r = client.post("/api/years/2026/lock", json={"lock_month": 7}, headers=H)
check("锁定推进到7月", r.status_code == 200)
check("7月现被锁定423", client.post("/api/board/2026/cell", json={"metric": "o_bp", "month": 7, "value": 1, "note": "x"}, headers=H).status_code == 423)
client.post("/api/years/2026/lock", json={"lock_month": 6}, headers=H)  # 还原

# ========== 台账 CRUD ==========
csv_ledger = "部门,中心,业务负责人,招聘岗位,分类,地点,进展,预计到岗时间,offer人选,实际入职时间\n云五,中心A,alice,后台开发,国内,深圳,简历&面试中,2026-08-01,,\n云五,中心B,bob,产品经理,海外,新加坡,已入职,2025-06-01,王五,2025.6.15\n"
r = client.post("/api/ledger/import", files={"file": ("ledger.csv", csv_ledger, "text/csv")}, headers=H)
check("台账CSV按列名识别导入", r.status_code == 200 and j(r)["report"]["rows"] == 2, str(j(r).get("report", j(r))))
r = client.post("/api/ledger/row", json={"fields": {"c": "中心C", "job": "测试岗"}}, headers=H)
rid = j(r)["id"]
check("台账加行", r.status_code == 200 and len(j(r)["rows"]) == 3)
r = client.put(f"/api/ledger/row/{rid}", json={"fields": {"st": "已入职", "join": "26.3.15"}}, headers=H)
row = [x for x in j(r)["rows"] if x["id"] == rid][0]
check("台账改行(两位年归一)", row["st"] == "已入职" and row["join"] == "2026-03-15", row["join"])
check("只读账号拒改台账", client.put(f"/api/ledger/row/{rid}", json={"fields": {"job": "x"}}, headers={"X-User": "nobody"}).status_code == 403)
r = client.delete(f"/api/ledger/row/{rid}", headers=H)
check("台账删行", len(j(r)["rows"]) == 2)

# ========== 导出 ==========
r = client.get("/api/export/2026.csv")
check("导出CSV", r.status_code == 200 and "期末在岗预估" in r.text)
r = client.get("/api/export/2026.xlsx")
check("导出xlsx", r.status_code == 200 and r.headers["content-type"].startswith("application/vnd.openxmlformats") and len(r.content) > 4000)

# ========== 年份/审计 ==========
check("新增年份", client.post("/api/years", json={"year": 2028}, headers=H).status_code == 200)
check("重复年份409", client.post("/api/years", json={"year": 2028}, headers=H).status_code == 409)
r = client.get("/api/audit")
check("审计留痕", len(j(r)) >= 15, f"{len(j(r))}条")

print(f"\n===== {len(PASS)} PASS / {len(FAIL)} FAIL =====")
if FAIL:
    print("FAILED:", FAIL)
sys.exit(1 if FAIL else 0)
