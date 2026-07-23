# -*- coding: utf-8 -*-
"""外部源直连（KPI系统 zhaopin / diy 员工宽表 / HR数仓 er_out…）：配置驱动，凭据在 sources_config.json（gitignore）"""
import json
import os

from fastapi import HTTPException


# ---------------- 外部源直连（KPI系统 zhaopin / diy 员工宽表 / HR数仓…） ----------------
# 高压线：凭据与接口信息放 backend/sources_config.json（已 .gitignore，绝不进仓库）；
# 未配置/接口未通 → 428 明确报错，单元格保持空，绝不编造。
# 配置由「浏览器 F12 抓包 Copy as cURL」或平台开放 API 文档填入（url/method/headers/params/map），改配置不改代码。
SOURCES_CFG_PATH = os.path.join(os.path.dirname(__file__), "sources_config.json")
SOURCE_METRICS = {  # 可直连的系统数指标（均在 IMPORTABLE 白名单内）
    "actual": "月末实际在岗（月末快照·集团本部含青云）",
    "er_out": "ER报表·月实际离职数（自然流失预估运算源）",
    "o_sys": "已流出/待流出·系统明确",
    "i_soc": "已流入/待流入·社招",
    "i_yy": "校招·预约入职",
    "i_bs": "校招·毕业生转聘",
}


def load_sources_cfg():
    if not os.path.exists(SOURCES_CFG_PATH):
        return {}
    with open(SOURCES_CFG_PATH, encoding="utf-8") as f:
        return json.load(f)


def _dig(obj, path):
    for part in path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
    return obj


def fetch_source(cfg, year):
    """按配置调外部源，返回 {month(1-12): value}。month 字段兼容 1-12 / '202601' / '2026-01'。"""
    import httpx
    def sub(o):
        if isinstance(o, str):
            return o.replace("{year}", str(year))
        if isinstance(o, dict):
            return {k: sub(v) for k, v in o.items()}
        if isinstance(o, list):
            return [sub(v) for v in o]
        return o
    r = httpx.request(cfg.get("method", "GET"), cfg["url"], params=sub(cfg.get("params")),
                      json=sub(cfg.get("body")), headers=cfg.get("headers") or {},
                      timeout=30, verify=cfg.get("verify", True))
    if r.status_code in (401, 403):
        raise HTTPException(502, f"源「{cfg.get('name', '?')}」鉴权失败（{r.status_code}）：headers 里的 Cookie/token 失效，请重新抓包更新")
    r.raise_for_status()
    data = r.json()
    mp = cfg["map"]
    rows = _dig(data, mp["list"]) if mp.get("list") else data
    if not isinstance(rows, list):
        raise HTTPException(502, f"源响应里找不到列表路径「{mp.get('list')}」——用返回 JSON 核对 map.list 配置")
    out = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        m_raw, v_raw = row.get(mp["month"]), row.get(mp["value"])
        try:
            digits = "".join(ch for ch in str(m_raw) if ch.isdigit())
            m = int(digits[-2:]) if len(digits) > 2 else int(digits)
            v = float(v_raw)
        except (TypeError, ValueError):
            continue
        if 1 <= m <= 12:
            out[m] = v
    if not out:
        raise HTTPException(502, "源返回 0 个有效月份值——核对 map.month / map.value 字段名与筛选条件")
    return out


def _month_completed(year, month):
    """月末快照仅当月最后一天已过才成立（当月不取期中值冒充月末）"""
    import calendar
    import datetime
    last = datetime.date(year, month, calendar.monthrange(year, month)[1])
    return last < datetime.date.today()
