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
check("config 12项目+账号(链行已并入实际行)", len(j(r)["projs"]) == 12 and j(r)["accts"][0]["id"] == "bonniewbli")
r = client.get("/api/board/2026")
b = j(r)
check("空板 lock=6 识空", b["lock"] == 6 and all(v is None for v in b["computed"]["chain"]))

# ========== 校验闸 ==========
check("锁定月拒绝423", client.post("/api/board/2026/cell", json={"metric": "o_bp", "month": 3, "value": 2, "note": "x"}, headers=H).status_code == 423)
r = client.post("/api/board/2026/cell", json={"metric": "o_bp", "month": 9, "value": 5, "note": ""}, headers=H)
check("备注可选·空备注直填200(260723 Excel式)", r.status_code == 200 and j(r)["metrics"]["o_bp"]["vals"][8] == 5)
client.post("/api/board/2026/cell", json={"metric": "o_bp", "month": 9, "value": None, "note": ""}, headers=H)  # 清场
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
check("变更历史可追溯(最新5→7)", len(h) >= 2 and h[0]["old_value"] == 5 and h[0]["new_value"] == 7, str([(x["old_value"], x["new_value"]) for x in h]))

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
check("台账加行缺必填422", r.status_code == 422, str(j(r)))
r = client.post("/api/ledger/row", json={"fields": {"dept": "云五", "c": "中心C", "own": "carol", "job": "测试岗", "st": "简历&面试中"}}, headers=H)
rid = j(r)["id"]
check("台账加行(必填齐全)", r.status_code == 200 and len(j(r)["rows"]) == 3)
check("台账必填项清空422", client.put(f"/api/ledger/row/{rid}", json={"fields": {"job": ""}}, headers=H).status_code == 422)
r = client.put(f"/api/ledger/row/{rid}", json={"fields": {"st": "已入职", "join": "26.3.15"}}, headers=H)
row = [x for x in j(r)["rows"] if x["id"] == rid][0]
check("台账改行(两位年归一)", row["st"] == "已入职" and row["join"] == "2026-03-15", row["join"])
check("只读账号拒改台账", client.put(f"/api/ledger/row/{rid}", json={"fields": {"job": "x"}}, headers={"X-User": "nobody"}).status_code == 403)
r = client.delete(f"/api/ledger/row/{rid}", headers=H)
check("台账删行", len(j(r)["rows"]) == 2)

# ========== 分支改名（260723 补接口） ==========
r = client.put(f"/api/board/2026/branch/{bid}", json={"name": "组织架构腾挪·改"}, headers=H)
check("分支改名200", r.status_code == 200 and any(x["name"] == "组织架构腾挪·改" for x in j(r)["branches"]))
client.put(f"/api/board/2026/branch/{bid}", json={"name": "组织架构腾挪"}, headers=H)  # 还原
check("分支改名空名422", client.put(f"/api/board/2026/branch/{bid}", json={"name": " "}, headers=H).status_code == 422)

# ========== 看板2 计划类分支：已发生月豁免锁定（260723） ==========
r = client.post("/api/board/2026/branch", json={"sec": "法定HC·其中", "name": "编制调整", "sign": "+"}, headers=H)
bhc = [x for x in j(r)["branches"] if x["name"] == "编制调整"][0]["id"]
check("法定HC分支已发生月可改(计划类豁免)", client.post("/api/board/2026/cell", json={"metric": f"branch:{bhc}", "month": 2, "value": 3, "note": ""}, headers=H).status_code == 200)
check("流出分支已发生月仍锁423", client.post("/api/board/2026/cell", json={"metric": f"branch:{bid}", "month": 2, "value": 1, "note": ""}, headers=H).status_code == 423)
client.delete(f"/api/board/2026/branch/{bhc}", headers=H)  # 清场

# ========== 台账模板/列性质闸（260723 对齐线下模板v2） ==========
r = client.get("/api/ledger/template.xlsx")
check("模板下载xlsx", r.status_code == 200 and "spreadsheetml" in r.headers["content-type"])
hdr = "部门,中心,业务负责人,招聘岗位,分类（国内/海外）,地点,需求提出时间,招聘数量,进展（当前状态）,预计到岗时间\n"
r = client.post("/api/ledger/import", files={"file": ("t.csv", hdr + "云五,中心A,alice,后台开发,国内,深圳,2026-05-01,2,简历&面试中,2026-08-01\n", "text/csv")}, headers=H)
check("数量≠1整批拒绝(另起一行)", r.status_code == 422 and any("另起一行" in x for x in j(r)["detail"]["errors"]), str(j(r)["detail"]))
r = client.post("/api/ledger/import", files={"file": ("t.csv", hdr + "云五,中心A,alice,后台开发,国内,深圳,2026-05-01,1,在途,2026-08-01\n", "text/csv")}, headers=H)
check("状态非枚举整批拒绝", r.status_code == 422 and any("下拉枚举" in x for x in j(r)["detail"]["errors"]))
r = client.post("/api/ledger/import", files={"file": ("t.csv", hdr + "云五,中心A,alice,后台开发,美国,深圳,2026-05-01,1,已入职,2026-08-01\n", "text/csv")}, headers=H)
check("国内海外枚举闸", r.status_code == 422 and any("国内 或 海外" in x for x in j(r)["detail"]["errors"]))
r = client.post("/api/ledger/import", files={"file": ("t.csv", hdr + "云五,中心A,alice,后台开发,国内,深圳,2026-05-01,1,简历&面试中,1999-08-01\n", "text/csv")}, headers=H)
check("日期年份limit闸", r.status_code == 422 and any("2000-2100" in x for x in j(r)["detail"]["errors"]))
r = client.post("/api/ledger/import", files={"file": ("t.csv", hdr + "云五,中心A,alice,后台开发,国内,深圳,2026-05-01,1,需求确认,2026-08-01\n示例部门,中心A,张三,后台开发,国内,深圳,2026-05-10,1,简历&面试中,2026-08-01\n", "text/csv")}, headers=H)
check("合格导入(需求确认状态·示例行跳过)", r.status_code == 200 and j(r)["report"]["rows"] == 1 and j(r)["report"]["skipped"] >= 1, str(j(r).get("detail", j(r).get("report", ""))))
lrow = j(client.get("/api/ledger"))["rows"][0]
check("列映射(分类/数量)", lrow["cls"] == "国内" and lrow["num"] == "1" and lrow["loc"] == "深圳")
# 日期自动识别：紧凑8位/6位、月00降级、两位年
from kb3_ledger import _norm_date
check("日期归一(20260829→2026-08-29)", _norm_date("20260829") == "2026-08-29")
check("日期归一(202608→2026-08)", _norm_date("202608") == "2026-08")
check("日期归一(2023-00→2023)", _norm_date("2023-00") == "2023")
check("日期归一(2026-05-99→2026-05)", _norm_date("2026-05-99") == "2026-05")
check("日期归一(26.8.29→2026-08-29)", _norm_date("26.8.29") == "2026-08-29")
check("日期归一(乱文保留原文)", _norm_date("待定Q3") == "待定Q3")
lid = lrow["id"]
check("eta变更缺原因422", client.put(f"/api/ledger/row/{lid}", json={"fields": {"eta": "2026-09-01"}}, headers=H).status_code == 422)
r = client.put(f"/api/ledger/row/{lid}", json={"fields": {"eta": "2026-09-01", "memo": "候选人延期"}}, headers=H)
row2 = [x for x in j(r)["rows"] if x["id"] == lid][0]
check("eta变更留旧值到peta", r.status_code == 200 and row2["eta"] == "2026-09-01" and row2["peta"] == "2026-08-01", str(row2["peta"]))
check("行编辑数量≠1拒绝", client.put(f"/api/ledger/row/{lid}", json={"fields": {"num": "3"}}, headers=H).status_code == 422)

# ========== 自然流失预估（近n月ER实际离职均值摊到未发生月·可调参·占总流出%） ==========
# 窗口缺数不派生（识空不编造）：只导 3-6 月
r = client.post("/api/import/2026", files={"file": ("er1.csv", "metric,month,value\ner_out,3,10\ner_out,4,12\ner_out,5,14\ner_out,6,16\n", "text/csv")}, headers=H)
check("er_out部分导入", r.status_code == 200 and j(r)["applied"] == 4)
b = j(client.get("/api/board/2026"))
check("窗口缺数不派生(缺1-2月)", b["nat"]["avg"] is None and sorted(b["nat"]["missing"]) == ["1月", "2月"] and b["metrics"]["o_nat"]["vals"][6] is None, str(b["nat"]))
# 补齐 1-2 月 → 近6月均值 (6+8+10+12+14+16)/6=11 摊到 7-12 月
client.post("/api/import/2026", files={"file": ("er2.csv", "metric,month,value\ner_out,1,6\ner_out,2,8\n", "text/csv")}, headers=H)
b = j(client.get("/api/board/2026"))
check("近6月均值=11摊到未发生月", b["nat"]["avg"] == 11.0 and b["metrics"]["o_nat"]["vals"][6] == 11.0 and b["metrics"]["o_nat"]["vals"][11] == 11.0, str(b["nat"]))
check("占总流出%外显", isinstance(b["nat"]["pct"], (int, float)) and 0 < b["nat"]["pct"] <= 100, str(b["nat"]["pct"]))
check("派生入总流出重算链", b["computed"]["outT"][6] == 11.0)
# 调参 n=3 → 窗口=6/5/4月 (16+14+12)/3=14
check("调参n越界422", client.post("/api/years/2026/natparam", json={"n": 0}, headers=H).status_code == 422)
r = client.post("/api/years/2026/natparam", json={"n": 3}, headers=H)
check("调参n=3均值=14", j(r)["nat"]["n"] == 3 and j(r)["nat"]["avg"] == 14.0, str(j(r)["nat"]))
# 调参 n=8 → 窗口跨年(缺上年11/12月) → 不派生；补上年数后 (6+8+10+12+14+16+20+22)/8=13.5
r = client.post("/api/years/2026/natparam", json={"n": 8}, headers=H)
check("n=8跨年缺数不派生", j(r)["nat"]["avg"] is None and "上年12月" in j(r)["nat"]["missing"], str(j(r)["nat"]))
client.post("/api/import/2025", files={"file": ("er25.csv", "metric,month,value\ner_out,11,20\ner_out,12,22\n", "text/csv")}, headers=H)
b = j(client.get("/api/board/2026"))
check("补上年数后跨年窗口=14(108/8=13.5取整)", b["nat"]["avg"] == 14, str(b["nat"]))
# n=24 窗口越过上一年（前年无数据源）：不回绕取错年份的数，判缺数不派生
r = client.post("/api/years/2026/natparam", json={"n": 24}, headers=H)
check("n=24前年无源不回绕不派生", j(r)["nat"]["avg"] is None and any("前年" in t for t in j(r)["nat"]["missing"]), str(j(r)["nat"]["missing"][-3:]))
# 已发生月：实际自然流失=er_out 当月值直接带出（行内与窗口同源）
b = j(client.get("/api/board/2026"))
check("已发生月o_nat=ER当月实际带出", b["metrics"]["o_nat"]["vals"][0] == 6 and b["metrics"]["o_nat"]["vals"][5] == 16 and b["nat"]["actual_months"] == [1, 2, 3, 4, 5, 6], str(b["metrics"]["o_nat"]["vals"][:6]))
client.post("/api/years/2026/natparam", json={"n": 8}, headers=H)  # 还原，下方存量用例依赖 n=8 派生 13.5
# 存量 o_nat 优先，派生只补空
client.post("/api/import/2026", files={"file": ("nat8.csv", "metric,month,value\no_nat,8,3\n", "text/csv")}, headers=H)
b = j(client.get("/api/board/2026"))
check("存量o_nat优先派生只补空", b["metrics"]["o_nat"]["vals"][7] == 3 and b["metrics"]["o_nat"]["vals"][6] == 14)
# n=24 窗口跨到前年（无数据源）——不得回绕错年取数，必须判缺数不派生
r = client.post("/api/years/2026/natparam", json={"n": 24}, headers=H)
check("n=24前年无源判缺数不派生", j(r)["nat"]["avg"] is None and any("前年" in t for t in j(r)["nat"]["missing"]), str(j(r)["nat"]["missing"][-3:]))
client.post("/api/years/2026/natparam", json={"n": 6}, headers=H)  # 还原

# ========== 外部源直连（月末实际在岗 zhaopin/diy·配置驱动·mock 源） ==========
check("源状态含actual", any(s["metric"] == "actual" for s in j(client.get("/api/sources"))["sources"]))
check("未配置源428", client.post("/api/sources/actual/sync", json={"year": 2026}, headers=H).status_code == 428)
check("不支持指标404", client.post("/api/sources/budget/sync", json={"year": 2026}, headers=H).status_code == 404)
_orig_cfg, _orig_fetch = app.load_sources_cfg, app.fetch_source
app.load_sources_cfg = lambda: {"actual": [{"name": "zhaopin-mock", "url": "mock://kpi", "map": {"month": "m", "value": "v"}}]}
app.fetch_source = lambda cfg, year: {1: 500, 4: 544, 12: 520}
# 当前 actual: 1月=547 2月=546 3月=999(采纳后) 4月=544 5月=543 6月=542；lock=6
# 1月锁定且值不同→差异；4月锁定但值相同→重写采纳；12月未完结→跳过（2026年内跑测试时）
r = client.post("/api/sources/actual/sync", json={"year": 2026}, headers=H)
sy = j(r)
import datetime
exp_skip12 = datetime.date.today() <= datetime.date(2026, 12, 31)
check("源同步:锁定月差异+相同值采纳", r.status_code == 200 and sy["diffs"] == 1 and sy["applied"] == (1 if exp_skip12 else 2), str(sy))
check("源同步:未完结月跳过", (12 in sy["skipped"]) == exp_skip12, str(sy["skipped"]))
check("锁定月1月未被自动改(547)", j(client.get("/api/board/2026"))["metrics"]["actual"]["vals"][0] == 547)
d = [x for x in j(client.get("/api/diffs/2026"))["diffs"] if x["month"] == 1]
check("源同步差异进diffs(547 vs 500)", len(d) == 1 and d[0]["cur_value"] == 547 and d[0]["src_value"] == 500 and d[0]["source"].startswith("sync:"), str(d))
client.post(f"/api/diffs/{d[0]['id']}/keep", headers=H)  # 清场
app.fetch_source = lambda cfg, year: {7: 999999}
check("源同步量级闸422", client.post("/api/sources/actual/sync", json={"year": 2026}, headers=H).status_code == 422)
check("源同步只读403", client.post("/api/sources/actual/sync", json={"year": 2026}, headers={"X-User": "nobody"}).status_code == 403)
app.load_sources_cfg, app.fetch_source = _orig_cfg, _orig_fetch

# ========== 示例数据：demo标签·只填空不覆盖·一键全清 ==========
led_before = len(j(client.get("/api/ledger"))["rows"])
r = client.post("/api/demo/load", json={"year": 2026}, headers=H)
b = j(r)
check("示例加载 demo标志亮", r.status_code == 200 and b["demo"] is True)
check("示例不覆盖真数(budget1月仍544)", b["metrics"]["budget"]["vals"][0] == 544)
check("示例填空格(budget2月=550)", b["metrics"]["budget"]["vals"][1] == 550)
check("已发生月明细项有数(o_bp/i_yy)", b["metrics"]["o_bp"]["vals"][1] == 1 and b["metrics"]["i_yy"]["vals"][1] == 3, str([b["metrics"]["o_bp"]["vals"][1], b["metrics"]["i_yy"]["vals"][1]]))
check("o_nat已发生月=ER当月实际离职(同源自洽)", b["metrics"]["o_nat"]["vals"][0] == 6 and b["metrics"]["o_nat"]["vals"][5] == 16, str(b["metrics"]["o_nat"]["vals"][:6]))
check("示例台账4行入库", len(j(client.get("/api/ledger"))["rows"]) == led_before + 4)
check("示例分支带【示例】标", any("示例" in x["name"] for x in b["branches"]))
check("实际行未发生月=链值合一(260723口径)", b["metrics"]["actual"]["vals"][11] is not None and b["metrics"]["actual"]["vals"][11] == b["computed"]["chain"][11], str(b["metrics"]["actual"]["vals"][6:]))
b25 = j(client.get("/api/board/2025"))
check("历史归档年2025实际月填满", b25["demo"] is True and all(isinstance(v, (int, float)) for v in b25["metrics"]["actual"]["vals"]), str(b25["metrics"]["actual"]["vals"]))
check("2025历史年不加未来调节分支", not any("示例" in x["name"] for x in b25["branches"]))
r = client.post("/api/demo/clear", headers=H)
cr = j(r)
b = j(client.get("/api/board/2026"))
check("删除假数:demo灭+真数保留", b["demo"] is False and b["metrics"]["budget"]["vals"][0] == 544 and b["metrics"]["budget"]["vals"][1] is None, str(cr))
b25 = j(client.get("/api/board/2025"))
check("删除假数:2025历史年同清(真er_out保留)", b25["demo"] is False and b25["metrics"]["actual"]["vals"][0] is None and j(client.get("/api/board/2026"))["nat"] is not None)
check("删除假数:台账示例行清光", len(j(client.get("/api/ledger"))["rows"]) == led_before)
check("删除假数:分支清光", not any("示例" in x["name"] for x in b["branches"]))

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
