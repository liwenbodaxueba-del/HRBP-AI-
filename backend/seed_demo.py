# -*- coding: utf-8 -*-
"""seed_demo.py — 生成假数库 hcfb_demo.db（结构 = 真库 hcfb.db 的副本）。

规则（Bonnie 定）：
  · 只填【系统取数】项，不填任何【BP 手填】项（o_bp/o_act/soc_bp/i_cbp/i_incr/中心预算当量/自定义分支/fa_hc）——手填留给她自己。
  · 假数落在真实部门/中心：产1~产4 的各中心 + 产5（叶子·直填）。
  · 台账(看板3.2)与看板1 逐行一致：st=已入职→soc_join；st=已offer待入职→soc_sys(+soc_hs)；简历&面试中→纯台账。
  · 真库 hcfb.db 全清（cells/branches/ledger）→ 留空态，等真实 API 写入。

用法：  python seed_demo.py
切库：  默认（无 HCFB_DB 且 hcfb_demo.db 存在）→ 走假数库；HCFB_DB=hcfb.db 强制真库；删 hcfb_demo.db → 回真库。
"""
import os
import sqlite3
import shutil
import random

import store  # 仅取 DEPT_CENTERS 常量（不触 db）

HERE = os.path.dirname(os.path.abspath(__file__))
REAL = os.path.join(HERE, "hcfb.db")
DEMO = os.path.join(HERE, "hcfb_demo.db")
YEAR = 2026
LOCK = 6  # 2026 已确认到 6 月：月 1..6 已发生、7..12 未发生

DEPTS5 = ["云产品一部", "云产品二部", "云产品三部", "云产品四部", "云产品五部"]
JOBS = ["高级研发工程师", "产品经理", "测试开发工程师", "解决方案架构师", "数据工程师", "前端工程师", "运营经理"]
NOW = "2026-08-05T00:00:00"
rng = random.Random(20260805)


def _clear(conn):
    for t in ("cells", "cells_history", "branches", "branch_cells", "ledger_rows", "kb0_adjust", "pending_diffs"):
        try:
            conn.execute(f"DELETE FROM {t}")
        except sqlite3.OperationalError:
            pass


def main():
    # 1) 清空真库 → 真库=空态（靠真实 API 写入）
    rc = sqlite3.connect(REAL)
    _clear(rc)
    rc.commit()
    rc.close()
    print("[real] hcfb.db 已清空（cells/branches/ledger/kb0_adjust 全清；accounts/years/config/perm 保留）")

    # 2) 复制真库(clean) → 假数库
    if os.path.exists(DEMO):
        os.remove(DEMO)
    shutil.copy(REAL, DEMO)
    dc = sqlite3.connect(DEMO)

    def setcell(dept, metric, m, v):
        if v is None or v == "":
            return
        dc.execute(
            "INSERT OR REPLACE INTO cells(year,dept,metric,month,value,note,source,updated_by,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (YEAR, dept, metric, m + 1, float(v), None, "demo", "demo", NOW))

    def addled(dept, center, st, m, job):
        day = "%d-%02d-%02d" % (YEAR, m + 1, rng.randint(5, 25))
        row = dict(
            year=0, batch=-999, dept=dept, center=center, owner="—", src="社招",
            job=job, rmgr="", lvl=rng.choice(["T9", "T10", "T11"]), cls="技术", loc="深圳",
            ask="1", num="1", tgt="", st=st,
            eta=("" if st == "已入职" else day), memo="",
            offer="", olvl="", join_dt=(day if st == "已入职" else ""), jmemo="", who="系统")
        cols = ",".join(row.keys())
        qs = ",".join("?" * len(row))
        dc.execute(f"INSERT INTO ledger_rows({cols}) VALUES({qs})", list(row.values()))

    def seed_unit(cell_dept, dept_for_led, center_for_led):
        """给一个取数单元(叶子中心 或 产5部)灌系统假数。"""
        B = rng.randint(8, 30)
        soc_join = [0] * 12
        soc_sys = [0] * 12
        soc_hs = [0] * 12
        # —— 台账社招行（驱动看板3.2 + 派生 soc_* cells，保证两表逐行一致）——
        for m in range(12):
            if m < LOCK:  # 已发生月：已入职（按 join 归月）
                n = rng.choice([0, 0, 1, 1, 2])
                for _ in range(n):
                    addled(dept_for_led, center_for_led, "已入职", m, rng.choice(JOBS))
                soc_join[m] = n
            if m >= LOCK - 1:  # 未发生月：已offer待入职（按 eta 归月）
                n = rng.choice([0, 1, 1, 2])
                for _ in range(n):
                    addled(dept_for_led, center_for_led, "已offer待入职", m, rng.choice(JOBS))
                hs = 1 if (n >= 2 and rng.random() < 0.4) else 0
                soc_sys[m] = n - hs
                soc_hs[m] = hs
            if LOCK - 1 <= m <= LOCK + 3:  # 简历&面试中（近未来·纯台账）
                for _ in range(rng.choice([0, 1, 1])):
                    addled(dept_for_led, center_for_led, "简历&面试中", m, rng.choice(JOBS))
        for m in range(12):
            if soc_join[m]:
                setcell(cell_dept, "soc_join", m, soc_join[m])
            if soc_sys[m]:
                setcell(cell_dept, "soc_sys", m, soc_sys[m])
            if soc_hs[m]:
                setcell(cell_dept, "soc_hs", m, soc_hs[m])
        # —— 期末在岗快照（已发生月·系统）——
        cur = B
        for m in range(LOCK):
            cur += rng.choice([-1, 0, 0, 1, 1])
            setcell(cell_dept, "actual", m, max(1, cur))
        # —— 实际入职/离职/调出/ER（已发生月·系统）——
        for m in range(LOCK):
            if soc_join[m]:
                setcell(cell_dept, "ai_soc", m, soc_join[m])   # 实际社招入职 ≈ 已入职
            if rng.random() < 0.35:
                setcell(cell_dept, "ai_camp", m, rng.choice([0, 1]))
            if rng.random() < 0.5:
                setcell(cell_dept, "ao_lv", m, rng.choice([0, 0, 1]))
            if rng.random() < 0.3:
                setcell(cell_dept, "ao_tr", m, rng.choice([0, 1]))
            if rng.random() < 0.4:
                setcell(cell_dept, "er_out", m, rng.choice([0, 0, 1]))
        # —— 系统流程中离职（未发生月·系统）——
        for m in range(LOCK, 12):
            if rng.random() < 0.35:
                setcell(cell_dept, "o_sys", m, rng.choice([0, 1]))
        # —— 校招（数仓·6~9 月季节）——
        for m in range(5, 9):
            if rng.random() < 0.5:
                setcell(cell_dept, "camp_off", m, rng.choice([0, 1, 2]))
            if rng.random() < 0.3:
                setcell(cell_dept, "i_yy", m, rng.choice([0, 1]))
            if rng.random() < 0.3:
                setcell(cell_dept, "i_bs", m, rng.choice([0, 1]))
        return B

    # 3) 灌假数：产1~4 各中心 + 产5 直填
    for dept in DEPTS5:
        centers = store.DEPT_CENTERS.get(dept)
        if centers:  # 产1~4：cells 落到各中心；部门级只放 q_init(看板2·系统) + camp_off_tot(数仓)
            base_tot = 0
            for center in centers:
                cname = center.split("/", 1)[1] if "/" in center else center
                base_tot += seed_unit(center, dept, cname)
            for m in range(12):
                setcell(dept, "q_init", m, round(base_tot * (1 + 0.015 * m)))
            setcell(dept, "camp_off_tot", 0, rng.randint(10, 40))
        else:  # 产5：叶子·全部落 dept 本身（台账 center 留空）
            B = seed_unit(dept, dept, "")
            for m in range(12):
                setcell(dept, "q_init", m, round(B * 3 * (1 + 0.015 * m)))
            setcell(dept, "camp_off_tot", 0, rng.randint(8, 20))

    # —— 示例·子PM组组级覆盖（演示「组覆盖 + 卷入部门/集团」）——
    # 组键=PM:部门:组名，与前端 PMG_DEFAULT(云产品一部·bonnie) 对应；仅覆盖「待流出·已明确非系统/主动动作(BP)」预估月，
    # 故意与各成员中心加总(0)不一致 → 组看板标橙 + 卷进云产品一部/集团合计。
    setcell("PM:云产品一部:bonnie", "o_bp", 7, 2)   # 8月·已明确离职（组级）
    setcell("PM:云产品一部:bonnie", "o_act", 8, 3)  # 9月·主动汰换计划（组级）

    dc.commit()
    nc = dc.execute("SELECT COUNT(*) FROM cells").fetchone()[0]
    nl = dc.execute("SELECT COUNT(*) FROM ledger_rows").fetchone()[0]
    nd = dc.execute("SELECT COUNT(DISTINCT dept) FROM cells").fetchone()[0]
    mset = sorted(set(r[0] for r in dc.execute("SELECT DISTINCT metric FROM cells")))
    dc.close()
    print(f"[demo] hcfb_demo.db 建好：cells={nc}（{nd} 个部门/中心）  ledger={nl}")
    print(f"[demo] 指标(仅系统取数)：{mset}")


if __name__ == "__main__":
    main()
