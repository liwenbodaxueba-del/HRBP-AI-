# -*- coding: utf-8 -*-
"""用 AX44 真数验证 calc_kb1 运算引擎是否打通（对照 Excel 表『期末在岗人数_预估』行34）。
运行：python _validate_ax44.py"""
import io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
from calc_kb1 import compute, nat_forecast

PASS = "✓"; FAIL = "✗"
results = []
def check(name, got, want, tol=1e-6):
    ok = (got is None and want is None) or (
        isinstance(got, (int, float)) and isinstance(want, (int, float)) and abs(got - want) <= tol)
    results.append(ok)
    print(f"  {PASS if ok else FAIL} {name}: got={got}  want={want}")
    return ok

N = lambda: [None] * 12
def put(a, idx0, v):  # idx0 0-based
    a[idx0] = v

print("=" * 70)
print("测试1｜预估链递推  chain = 上月 − 总流出 + 总流入   (对照 Excel 行34, 2026)")
print("=" * 70)
# 2026 各月流项（0-based: idx0=2601 … idx11=2612），取自 AX44 表行35-46
o_nat = [3,3,4,3,3,3,3,3,3,3,3,3]          # c2 自然流失预估(行36)
o_act = [0,0,6,6,6,3,0,0,0,6,0,3]          # c3 主动计划(行37)
i_soc = [4,0,0,0,0,0,0,0,0,0,0,0]          # c4+c5 社招/存量(行38+39)
i_yy  = [0,0,0,0,3,11,36,0,0,0,0,0]        # c6 校招入职(行40)
i_incr= [0,0,0,1,1,2,2,1,3,0,0,0]          # c7 增量招聘需求(行42)
# 青云头部补贴结束(行46) —— 属 BP 手动调节项，用 ⊕ 分支承载（源表仅 AN/AQ 计入公式）
qingyun = N(); put(qingyun, 3, 1); put(qingyun, 6, 2)   # 2604=1, 2607=2

vals = {"actual": [537] + [None]*11,        # lock=1：2601=537 作为链首实际(=536−3+4，手算见下)
        "o_nat": o_nat, "o_act": o_act, "i_soc": i_soc, "i_yy": i_yy, "i_incr": i_incr,
        "budget": N()}
branches = [{"id":1, "sec":"总流入（＋）", "name":"青云头部补贴结束", "sign":"+", "vals":qingyun}]
comp = compute(vals, branches, lock=1)

print(f"  链首 2601 手算: 536(2512基数) − 3(自然) + 4(存量) = {536-3+4}  (Excel行34 AK=537)")
excel_row34 = {2:534, 3:524, 4:517, 5:512, 6:519, 7:556, 8:554, 9:554, 10:545, 11:542, 12:536}
for mth, want in excel_row34.items():
    check(f"2026 {mth:>2}月 期末预估", comp["chain"][mth-1], want)

print("\n" + "=" * 70)
print("测试2｜自然流失预估  近n月ER均值取整·均摊·识空   (PRD 4.1 待流出·自然流失)")
print("=" * 70)
# 取 Excel 行36 2025实际离职做 ER 源：P36..(1,0,2,0,2,2)=6个月 → 均值
er = [None,None,None,None,None,1,0,2,0,2,2,None]   # 6-11月(2506-2511)有数, 12月空
v2 = {"er_out": er, "o_nat": N()}
eff, info = nat_forecast(v2, lock=11, prev_er=None, nat_n=6)  # 锚定11月, 回看6个月(6-11月)
print(f"  窗口={info['window']}  值合计={1+0+2+0+2+2}  均值取整={info['avg']}")
check("近6月均值(round half-up) (1+0+2+0+2+2)/6=1.17→1", info["avg"], 1)
check("已发生月(6月)带出ER实际=1", eff[5], 1)
check("未发生月(12月)填入均值=1", eff[11], 1)

# 识空：窗口内缺一个月 → 整体不派生
er2 = [None,None,None,None,None,1,0,None,0,2,2,None]  # 8月(2508)缺
eff2, info2 = nat_forecast({"er_out":er2,"o_nat":N()}, lock=11, prev_er=None, nat_n=6)
check("识空：窗口缺数则不派生(avg=None)", info2["avg"], None)
check("识空：未发生月留空(不编造)", eff2[11], None)

print("\n" + "=" * 70)
print("测试3｜汇总与红灯  总流出/总流入汇总 + 年均vs预算超标判断")
print("=" * 70)
# 总流出 2603 = o_nat4 + o_act6 = 10 ; 总流入 2607 = i_yy36 + i_incr2 + 青云2 = 40
check("总流出 2603 = 4+6", comp["outT"][2], 10)
check("总流入 2607 = 36+2+2(青云)", comp["inT"][6], 40)
# 红灯：年均预估 > 预算年均 判超标
vals_b = dict(vals, budget=[520]*12)
comp_b = compute(vals_b, branches, lock=1)
over = comp_b["chain_avg"] > comp_b["budget_avg"]
print(f"  链年均={comp_b['chain_avg']}  预算年均={comp_b['budget_avg']}  超标={over}")
check("年均预估>预算 → 应亮红灯(over=True)", over, True)

print("\n" + "=" * 70)
print("测试4｜跨年种子  lock=0 整年纯预估时，链首=上年12月期末(seed)")
print("=" * 70)
# 无 seed：lock=0 时链无种子（现状边界）
vals_s = {"actual": N(), "o_nat": [3]*12, "i_soc": [4]+[0]*11, "budget": N(),
          "o_act": N(), "i_yy": N(), "i_incr": N()}
c_noseed = compute(vals_s, [], lock=0)
check("无seed·lock=0 → 链首留空(不派生)", c_noseed["chain"][0], None)
# 有 seed=536(上年12月)：1月 = 536 − 3(自然) + 4(存量) = 537，逐月递推
c_seed = compute(vals_s, [], lock=0, seed=536)
check("有seed=536 → 1月 = 536−3+4", c_seed["chain"][0], 537)
check("有seed → 2月 = 537−3", c_seed["chain"][1], 534)
check("有seed → 12月递推", c_seed["chain"][11], 537 - 3*11)  # 1月537后每月−3

print("\n" + "=" * 70)
ok = sum(results); tot = len(results)
print(f"结果：{ok}/{tot} 通过" + ("  ✅ 运算逻辑打通" if ok == tot else f"  ❌ {tot-ok} 项未通过"))
print("=" * 70)
sys.exit(0 if ok == tot else 1)
