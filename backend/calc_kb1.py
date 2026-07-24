# -*- coding: utf-8 -*-
"""看板1/2 运算引擎：预估链（上月−流出+流入）与自然流失预估（近n月ER均值取整·跨年窗口·缺数不派生）。
纯函数、不碰数据库——运算行不落库，读取时现算（Excel 公式思路）"""

from meta import NAT_N_DEFAULT, OUT_KEYS


def _agg(parts):
    nums = [p for p in parts if isinstance(p, (int, float))]
    return sum(nums) if nums else None


def nat_forecast(vals, lock, prev_er, nat_n):
    """自然流失预估：近 n 个已发生月 ER 实际离职合计 ÷ n，均摊到每个未发生月。
    窗口锚定最近已发生月（lock），可跨年回看上一年 er_out；窗口内任一月缺数则不派生（识空，不编造）。
    返回 (effective_o_nat[12], nat_info)。已有存量 o_nat（导入/源）优先，派生只补空。"""
    er = vals.get("er_out", [None] * 12)
    prev_er = prev_er or [None] * 12
    window, missing, wvals = [], [], []
    for i in range(nat_n):
        m1 = lock - i  # 1-based 月；0~-11 落上一年；再往前无数据源（不回绕取错年，判缺数）
        if m1 >= 1:
            v, tag = er[m1 - 1], f"{m1}月"
        elif m1 + 12 >= 1:
            v, tag = prev_er[m1 + 11], f"上年{m1 + 12}月"
        else:
            v, tag = None, f"前年{m1 + 24}月（无数据源）"
        window.append(tag)
        wvals.append(v)
        if not isinstance(v, (int, float)):
            missing.append(tag)
    # 人数取整（int）：四舍五入（half-up），预估月不出现小数人头
    nat_avg = int(sum(wvals) / nat_n + 0.5) if not missing else None
    eff = list(vals.get("o_nat", [None] * 12))
    derived_months, actual_months = [], []
    for m in range(0, lock):  # 已发生月：实际自然流失=ER报表当月离职数直接带出（与窗口同源，行内自洽）
        if not isinstance(eff[m], (int, float)) and isinstance(er[m], (int, float)):
            eff[m] = er[m]
            actual_months.append(m + 1)
    if nat_avg is not None:
        for m in range(lock, 12):  # 未发生月：均值摊入，且不覆盖存量
            if not isinstance(eff[m], (int, float)):
                eff[m] = nat_avg
                derived_months.append(m + 1)
    info = {"n": nat_n, "avg": nat_avg, "window": window, "missing": missing,
            "derived_months": derived_months, "actual_months": actual_months}
    return eff, info


def compute(vals, branches, lock, prev_er=None, nat_n=NAT_N_DEFAULT):
    nat_eff, nat_info = nat_forecast(vals, lock, prev_er, nat_n)
    vals = dict(vals, o_nat=nat_eff)

    def g(k, m):
        v = vals.get(k, [None] * 12)[m]
        return v if isinstance(v, (int, float)) else None

    def bsum(sec, m):
        s, any_ = 0, False
        for b in branches:
            if b["sec"] == sec and isinstance(b["vals"][m], (int, float)):
                s += (1 if b["sign"] == "+" else -1) * b["vals"][m]
                any_ = True
        return s if any_ else None

    campT, outT, inT, chain = [], [], [], []
    for m in range(12):
        campT.append(_agg([g("i_yy", m), g("i_bs", m), g("i_cbp", m), bsum("校招∇ 分列", m)]))
        outT.append(_agg([g(k, m) for k in OUT_KEYS] + [bsum("总流出（−）", m)]))
        inT.append(_agg([g("i_soc", m), campT[m], g("i_incr", m), bsum("总流入（＋）", m)]))
    for m in range(12):
        if m < lock:
            chain.append(g("actual", m))
        else:
            prev = chain[m - 1] if m > 0 else g("actual", lock - 1) if lock > 0 else None
            if not isinstance(prev, (int, float)):
                chain.append(None)
            else:
                o = outT[m] if isinstance(outT[m], (int, float)) else 0
                i = inT[m] if isinstance(inT[m], (int, float)) else 0
                chain.append(round(prev - o + i, 2))
    # 260724 联动：看板1 预算当量 = 看板2 预算当量父行（期初 q_init + 当月「其中」调整求和）；无看板2数据回退存量 budget
    budget_eff = []
    for m in range(12):
        q = _agg([g("q_init", m), bsum("预算当量·其中", m)])
        budget_eff.append(q if q is not None else g("budget", m))

    def avg(a):
        n = [x for x in a if isinstance(x, (int, float))]
        return round(sum(n) / len(n), 2) if n else None
    nat_nums = [x for x in nat_eff if isinstance(x, (int, float))]
    out_sum = sum(x for x in outT if isinstance(x, (int, float)))
    nat_info["pct"] = round(100 * sum(nat_nums) / out_sum, 1) if nat_nums and out_sum else None
    return {"campT": campT, "outT": outT, "inT": inT, "chain": chain,
            "chain_avg": avg(chain), "budget_avg": avg(budget_eff), "budget_eff": budget_eff,
            "o_nat_eff": nat_eff, "nat": nat_info}
