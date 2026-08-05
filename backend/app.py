# -*- coding: utf-8 -*-
"""
HC Forecast Board · V1 后端底座
- FastAPI + SQLite（本地开发库；企业版数据库批下后仅替换本存储层）
- 高压线：所有数字须来自真实数据源/人工录入；库中无数据一律返回空（不编造、不补零）
- 校验闸：已发生月锁定、备注必填、只读账号拒写、导入整批校验不合格拒绝入库
- 登录为 iOA 占位（X-User 头，默认 bonniewbli）；正式版接 iOA 统一登录
启动：uvicorn app:app --port 8787（工作目录 backend/）
"""
import csv
import io
import json
import os
import random
import sqlite3
import time
from contextlib import contextmanager
from typing import Optional

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

DB_PATH = os.environ.get("HCFB_DB") or os.path.join(os.path.dirname(__file__), "hcfb.db")
FRONT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 仓库根（index.html/admin.html）

app = FastAPI(title="HC Forecast Board API", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ---------------- 模块装配（按看板分工拆分；行为与单文件版完全一致） ----------------
# meta=指标口径 · store=存储/权限/审计 · calc_kb1=看板1/2运算引擎 · sources=外部源直连 · kb3_ledger=看板3台账解析
from meta import (CANON_PROJECTS, OUT_KEYS, CAMP_KEYS, IN_DIRECT_KEYS, BP_EDITABLE,
                  IMPORTABLE, VALUE_ABS_MAX, PLAN_METRICS, PLAN_BRANCH_SECS, EXTRA_METRICS, NAT_N_DEFAULT)
from store import (DB_PATH, db, init_db, now, _audit, _write_cell, get_account,
                   require_writer, require_admin, can_manage, manageable_ids, is_agg_dept, DEPT_CENTERS, _kb0_adjust, _grid, _branches)
from calc_kb1 import compute
from sources import SOURCE_METRICS, load_sources_cfg, fetch_source, _month_completed
from kb3_ledger import (LEDGER_CLS, LEDGER_DATE_F, LEDGER_F2DB, LEDGER_REQUIRED, LEDGER_ST_CANON,
                        parse_ledger, validate_ledger_rows, _ledger_list, _ledger_norm, _norm_date, _norm_status)

init_db()


# ---------------- 通用 ----------------
@app.get("/api/health")
def health():
    return {"ok": True, "ts": now(), "storage": "sqlite-dev（企业版数据库批后替换）", "version": app.version}


# 多端实时同步：审计表 id 单调自增，任何数据改动都留痕 → 拿 MAX(id) 当"数据版本号"，
# 各端轮询此接口，版本变了就重新拉数据。极轻量、无长连接、公网/代理下都稳。
@app.get("/api/version")
def get_version():
    with db() as c:
        row = c.execute("SELECT COALESCE(MAX(id),0) AS v FROM audit").fetchone()
        return {"v": int(row["v"]), "ts": int(time.time() * 1000)}


# ---------------- 配置（与前端 localStorage cfg 同构） ----------------
@app.get("/api/config")
def get_config():
    with db() as c:
        projs = [
            {"key": r["key"], "sec": r["sec"], "name": r["name"], "src": r["src"], "srcCls": r["src_cls"],
             "add": bool(r["add_ok"]), "unbind": bool(r["unbind"]), "on": bool(r["on_ok"]), "sys": bool(r["sys"]),
             "edit": (r["edit"] if "edit" in r.keys() else "") or ""}
            for r in c.execute("SELECT * FROM projects ORDER BY pos")
        ]
        accts = [
            {"id": r["id"], "name": r["name"], "role": r["role"], "dept": r["dept"],
             "is_head": bool(r["is_head"] if "is_head" in r.keys() else 0),
             "is_sysadmin": bool(r["is_sysadmin"] if "is_sysadmin" in r.keys() else 0),
             "kb": json.loads(r["kb"] or "[1,1,1,1]"), "on": bool(r["on_ok"]), "demo": bool(r["demo"]),
             "level": (r["level"] if "level" in r.keys() else "") or "",
             "manager_id": (r["manager_id"] if "manager_id" in r.keys() else "") or "",
             "org_path": (r["org_path"] if "org_path" in r.keys() else "") or "",
             "kb1_depts": json.loads((r["kb1_depts"] if "kb1_depts" in r.keys() else "") or '["集团"]'),
             "kb0_depts": json.loads((r["kb0_depts"] if "kb0_depts" in r.keys() else "") or '["集团"]'),
             "kbperm": json.loads((r["kbperm"] if "kbperm" in r.keys() else "") or "[]")}
            for r in c.execute("SELECT * FROM accounts")
        ]
        return {"projs": projs, "accts": accts, "ts": int(time.time() * 1000)}


class ConfigDoc(BaseModel):
    projs: list
    accts: list


@app.put("/api/config")
def put_config(doc: ConfigDoc, x_user: str = Header("bonniewbli")):
    with db() as c:
        require_admin(c, x_user)
        c.execute("DELETE FROM projects")
        for i, p in enumerate(doc.projs):
            c.execute(
                "INSERT INTO projects(key,sec,name,src,src_cls,add_ok,unbind,on_ok,sys,pos,edit) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (p.get("key") or f"x{int(time.time()*1000)}_{i}", p.get("sec", ""), p.get("name", ""),
                 p.get("src", ""), p.get("srcCls", "bp"), int(bool(p.get("add"))), int(bool(p.get("unbind"))),
                 int(p.get("on", True)), int(bool(p.get("sys"))), i, (p.get("edit") or "")),
            )
        me_old = get_account(c, x_user)  # 本人现有账号：权限字段防自改（系统管理员除外）
        me_sys = bool(me_old["is_sysadmin"]) if me_old else False
        sys_map = {r["id"]: (r["is_sysadmin"] or 0) for r in c.execute("SELECT id,is_sysadmin FROM accounts")}  # is_sysadmin 保原值·不经配置篡改
        c.execute("DELETE FROM accounts")
        ids = {a["id"] for a in doc.accts}
        if x_user not in ids:
            raise HTTPException(400, "不可移除当前登录账号（本人账号必须保留）")
        for a in doc.accts:
            keep_sys = int(sys_map.get(a["id"], 0) or 0)
            if a["id"] == x_user and me_old and not me_sys:
                # 非系统管理员的本人：权限字段一律用旧值（不能自己配自己），仅姓名可改
                c.execute(
                    "INSERT INTO accounts(id,name,role,dept,kb,on_ok,demo,level,manager_id,org_path,kb1_depts,kb0_depts,is_head,is_sysadmin,kbperm) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (x_user, a.get("name", me_old["name"]), me_old["role"], me_old["dept"],
                     me_old["kb"], 1, me_old["demo"],
                     me_old["level"] or "", me_old["manager_id"] or "", me_old["org_path"] or "",
                     me_old["kb1_depts"] or '["集团"]', me_old["kb0_depts"] or '["集团"]',
                     int(me_old["is_head"] or 0), keep_sys, (me_old.get("kbperm") or "")),
                )
                continue
            # 其他账号 / 系统管理员本人：用下发值（本人 on 强制启用防自锁；is_sysadmin 保原值）
            c.execute(
                "INSERT INTO accounts(id,name,role,dept,kb,on_ok,demo,level,manager_id,org_path,kb1_depts,kb0_depts,is_head,is_sysadmin,kbperm) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (a["id"], a.get("name", ""), a.get("role", "HRBP·可编辑"), a.get("dept", ""),
                 json.dumps(a.get("kb", [1, 1, 1, 1])), (1 if a["id"] == x_user else int(a.get("on", True))), int(bool(a.get("demo"))),
                 a.get("level", ""), a.get("manager_id", ""), a.get("org_path", ""),
                 json.dumps(a.get("kb1_depts", ["集团"]), ensure_ascii=False),
                 json.dumps(a.get("kb0_depts", ["集团"]), ensure_ascii=False),
                 int(bool(a.get("is_head"))), keep_sys,
                 json.dumps(a.get("kbperm") or [], ensure_ascii=False)),
            )
        _audit(c, x_user, "配置更新", f"项目 {len(doc.projs)} 项 / 账号 {len(doc.accts)} 个（管理后台下发）")
        return {"ok": True}


# ---------------- 转移管理员职位（现任管理员移交给他人，自己降为可编辑） ----------------
class TransferAdmin(BaseModel):
    new_admin: str


@app.post("/api/accounts/transfer-admin")
def transfer_admin(t: TransferAdmin, x_user: str = Header("bonniewbli")):
    with db() as c:
        require_admin(c, x_user)  # 仅现任管理员可转移
        if t.new_admin == x_user:
            raise HTTPException(400, "不能转移给自己")
        target = get_account(c, t.new_admin)
        if not target:
            raise HTTPException(404, "目标账号不存在")
        me = get_account(c, x_user)
        sys = int(me["is_sysadmin"] or 0)  # 若现任是系统管理员，一并移交
        c.execute("UPDATE accounts SET role='管理员', is_sysadmin=? WHERE id=?", (sys, t.new_admin))  # 目标接管
        c.execute("UPDATE accounts SET role='HRBP·可编辑', is_sysadmin=0 WHERE id=?", (x_user,))       # 现任交出
        _audit(c, x_user, "转移管理员", f"{'系统' if sys else ''}管理员职位：{x_user} → {t.new_admin}（{target['name']}）")
        return {"ok": True, "new_admin": t.new_admin}


# ---------------- 当前登录账号自身权限（前端据此过滤看板1可见部门等） ----------------
@app.get("/api/me")
def get_me(x_user: str = Header("bonniewbli")):
    with db() as c:
        a = get_account(c, x_user)
        if not a:
            raise HTTPException(403, f"账号 {x_user} 未配置")
        return {"id": a["id"], "name": a["name"], "role": a["role"], "dept": a.get("dept", "") or "",
                "is_head": bool(a.get("is_head", 0)), "is_sysadmin": bool(a.get("is_sysadmin", 0)),
                "kb1_depts": json.loads((a.get("kb1_depts") or "") or '["集团"]'),
                "kb0_depts": json.loads((a.get("kb0_depts") or "") or '["集团"]'),
                "kbperm": json.loads((a.get("kbperm") or "") or "[]")}


# ---------------- 账号层级树（上级只看到自己管辖子树；管理员看全员） ----------------
@app.get("/api/accounts/tree")
def accounts_tree(x_user: str = Header("bonniewbli")):
    """返回调用者可管辖的账号（含自己），带 org_path/level/manager_id，供后台渲染可展开层级列表。
    非管理员只回其 org_path 子树成员；管理员回全员。前端按 org_path 建树。"""
    with db() as c:
        me = get_account(c, x_user)
        if not me:
            raise HTTPException(403, f"账号 {x_user} 未配置")
        ids = set(manageable_ids(c, x_user)) | {x_user}  # 管辖子树 + 自己
        rows = []
        for r in c.execute("SELECT * FROM accounts"):
            if r["id"] not in ids:
                continue
            rows.append({
                "id": r["id"], "name": r["name"], "role": r["role"],
                "level": (r["level"] if "level" in r.keys() else "") or "",
                "org_path": (r["org_path"] if "org_path" in r.keys() else "") or "",
                "manager_id": (r["manager_id"] if "manager_id" in r.keys() else "") or "",
                "dept": r["dept"], "on": bool(r["on_ok"]), "demo": bool(r["demo"]),
                "is_head": bool(r["is_head"] if "is_head" in r.keys() else 0),
                "is_sysadmin": bool(r["is_sysadmin"] if "is_sysadmin" in r.keys() else 0),
                "kb": json.loads(r["kb"] or "[1,1,1,1]"),
                "kb1_depts": json.loads((r["kb1_depts"] if "kb1_depts" in r.keys() else "") or '["集团"]'),
                "kb0_depts": json.loads((r["kb0_depts"] if "kb0_depts" in r.keys() else "") or '["集团"]'),
                "kbperm": json.loads((r["kbperm"] if "kbperm" in r.keys() else "") or "[]"),
                "can_manage": bool(can_manage(c, x_user, r["id"])),  # 我能否管这个人（自己=False）
            })
        return {"me": {"id": me["id"], "role": me["role"],
                       "is_sysadmin": bool(me.get("is_sysadmin", 0)),  # 仅系统管理员可改他人看板权限
                       "org_path": (me["org_path"] if "org_path" in me.keys() else "") or ""},
                "accounts": rows}


# ---------------- iOA 组织同步（预留接口：正式版接 iOA OpenAPI；未接入→占位，绝不编造） ----------------
@app.post("/api/accounts/{acct_id}/ioa-sync")
def ioa_sync(acct_id: str, x_user: str = Header("bonniewbli")):
    """按 iOA 账号拉取组织信息（姓名/部门/org_path/职级level/上级manager_id）并回填账号。
    现为预留桩：iOA 未接入 → 428，不自动建号、不编造。正式版在 sources 层配置 iOA OpenAPI 后打通。"""
    with db() as c:
        require_admin(c, x_user)
        prof = fetch_ioa_profile(acct_id)  # 未接入返回 None
        if not prof:
            raise HTTPException(428, {"msg": "iOA 组织接口未接入", "acct": acct_id,
                                      "expect": ["name", "dept", "org_path", "level", "manager_id"]})
        c.execute("UPDATE accounts SET name=?, dept=?, org_path=?, level=?, manager_id=? WHERE id=?",
                  (prof["name"], prof["dept"], prof["org_path"], prof["level"], prof["manager_id"], acct_id))
        _audit(c, x_user, "iOA同步", f"{acct_id}：{prof['org_path']} · {prof['level']}")
        return {"ok": True, "profile": prof}


IOA_CFG_PATH = os.path.join(os.path.dirname(__file__), "ioa_config.json")


def load_ioa_cfg():
    """iOA 接入配置（backend/ioa_config.json·已 gitignore）。不存在→{}（未接入）。"""
    if not os.path.exists(IOA_CFG_PATH):
        return {}
    try:
        with open(IOA_CFG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        return {}


def fetch_ioa_profile(acct_id):
    """iOA 组织/职级拉取（配置驱动·接口即插即用，不改代码只改 ioa_config.json）：
      · 无配置 → None（前端提示"待接 iOA"，不建号不编造）
      · 配 mock（{"mock":{"acct":{...}}}）→ 直接返回，供联调不依赖真 iOA
      · 配 url/headers/field_map → 调 iOA OpenAPI（urllib·无第三方依赖），按 field_map 映射字段
    返回 {name,dept,org_path,level,manager_id} 或 None。org_path 用组织全路径「/」拼接。"""
    cfg = load_ioa_cfg()
    if not cfg:
        return None
    if cfg.get("mock"):
        return cfg["mock"].get(acct_id)
    url = cfg.get("url")
    if not url:
        return None
    import urllib.request
    req = urllib.request.Request(url.replace("{acct}", acct_id), headers=cfg.get("headers", {}))
    try:
        with urllib.request.urlopen(req, timeout=cfg.get("timeout", 8)) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None  # 接口异常→判空，绝不编造
    fm = cfg.get("field_map", {})

    def _dig(obj, path):
        for p in (path or "").split("."):
            obj = obj.get(p) if isinstance(obj, dict) else None
        return obj

    seg_join = cfg.get("org_path_join", "/")
    prof = {}
    for k in ("name", "dept", "org_path", "level", "manager_id"):
        v = _dig(data, fm.get(k)) if fm.get(k) else None
        if isinstance(v, list):  # org_path 若为数组则拼接
            v = seg_join.join(str(x) for x in v)
        prof[k] = v or ""
    return prof if prof.get("org_path") else None


def fetch_ioa_bp_roster():
    """iOA 读取 BP 关系链（BP名单→所属部门→上级/权限链）。配置驱动·接口即插即用：
      · 无配置 → None（前端提示待接入，不建号不编造）
      · 配 mock（ioa_config.json 里 {"bp_roster":[{id,name,dept,org_path,manager_id,role},...]}）→ 直接返回，供联调
      · 配 bp_url/headers → 调 iOA OpenAPI 拉全量 BP 关系（urllib·无第三方依赖）"""
    cfg = load_ioa_cfg()
    if not cfg:
        return None
    if cfg.get("bp_roster") is not None:
        return cfg["bp_roster"]
    url = cfg.get("bp_url")
    if not url:
        return None
    import urllib.request
    try:
        req = urllib.request.Request(url, headers=cfg.get("headers", {}))
        with urllib.request.urlopen(req, timeout=cfg.get("timeout", 8)) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data if isinstance(data, list) else data.get("roster")
    except Exception:
        return None  # 接口异常→判空，绝不编造


@app.get("/api/bp-relations")
def bp_relations(x_user: str = Header("bonniewbli")):
    """读取 BP 关系链（BP名单 · 所属部门 · 部门权限BP关系）——iOA 配置驱动的预留窗口。
    未接入 → 428（不编造）；接入后返回 [{id,name,dept,org_path,manager_id,role}]，供后台按链回填/识别部门权限。"""
    with db() as c:
        require_admin(c, x_user)
    roster = fetch_ioa_bp_roster()
    if roster is None:
        raise HTTPException(428, {"msg": "iOA BP 关系链未接入（预留窗口·等待接入）",
                                  "expect": ["id", "name", "dept", "org_path", "manager_id", "role"],
                                  "hint": "在 backend/ioa_config.json 配 bp_roster(mock) 或 bp_url 即打通；接入后由后台读取识别 BP→所属部门→部门权限关系链"})
    return {"ok": True, "count": len(roster), "roster": roster}


# ---------------- 年份 ----------------
@app.get("/api/years")
def list_years():
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM years ORDER BY year")]


class YearNew(BaseModel):
    year: int


@app.post("/api/years")
def add_year(y: YearNew, x_user: str = Header("bonniewbli")):
    if y.year < 2000 or y.year > 2100:
        raise HTTPException(422, "年份无效")
    with db() as c:
        require_writer(c, x_user)
        if c.execute("SELECT 1 FROM years WHERE year=?", (y.year,)).fetchone():
            raise HTTPException(409, "年份已存在")
        c.execute("INSERT INTO years(year,status,lock_month) VALUES(?,?,0)", (y.year, "待接入·待编制"))
        _audit(c, x_user, "新增年份", f"{y.year}：生成空模板（看板0-3 · 独立数据空间）")
        _audit(c, "system", "读取系统数", f"{y.year}：各数据源 API 未接入 → 单元格留空待取数（不编造）")
        return {"ok": True}


# ---------------- 看板读写 ----------------
def _prev_dec_ending(c, year, dept="集团", _depth=0):
    """上一年 12 月期末在岗（跨年链首种子·同部门空间）：优先取上年 12 月实际值；
    上年也是整年预估（lock=0）时，递推其运算链的 12 月值（最多回溯一层，再往前判无源→None，不编造）。"""
    py = year - 1
    pyr = c.execute("SELECT * FROM years WHERE year=?", (py,)).fetchone()
    if not pyr:
        return None
    pv = _grid(c, py, dept)[0]
    dec = (pv.get("actual") or [None] * 12)[11]
    if isinstance(dec, (int, float)):
        return dec  # 上年 12 月实际在岗直接作种子
    if _depth >= 1:
        return None  # 只回溯一层，避免深链；再往前无实际则判缺数
    pnat = pyr["nat_n"] if "nat_n" in pyr.keys() else NAT_N_DEFAULT
    pseed = _prev_dec_ending(c, py, dept, _depth + 1) if pyr["lock_month"] == 0 else None
    pcomp = compute(pv, _branches(c, py, dept), pyr["lock_month"],
                    prev_er=_grid(c, py - 1, dept)[0].get("er_out"), nat_n=pnat, seed=pseed)
    return pcomp["chain"][11]


@app.get("/api/board/{year}")
def get_board(year: int, dept: str = "集团"):
    with db() as c:
        yr = c.execute("SELECT * FROM years WHERE year=?", (year,)).fetchone()
        if not yr:
            raise HTTPException(404, "年份不存在")
        vals, notes = _grid(c, year, dept)
        brs = _branches(c, year, dept)
        prev_er = _grid(c, year - 1, dept)[0].get("er_out")
        nat_n = yr["nat_n"] if "nat_n" in yr.keys() else NAT_N_DEFAULT
        seed = _prev_dec_ending(c, year, dept) if yr["lock_month"] == 0 else None
        comp = compute(vals, brs, yr["lock_month"], prev_er=prev_er, nat_n=nat_n, seed=seed)
        metrics = {k: {"vals": vals.get(k, [None] * 12), "notes": notes.get(k, {})} for k, *_ in CANON_PROJECTS}
        for k in EXTRA_METRICS:  # 看板2 期初基线（fa_hc/q_init）一并下发
            metrics[k] = {"vals": vals.get(k, [None] * 12), "notes": notes.get(k, {})}
        metrics["o_nat"]["vals"] = comp["o_nat_eff"]  # 存量优先，派生只补未发生月空格
        metrics["budget"]["vals"] = comp["budget_eff"]  # 预算当量 = 看板2 期初+其中（前后端/导出同一口径）
        # 260723 口径：链行并入实际行——未发生月空格由预估链补（月末实际在岗/期末在岗预估）
        av = list(metrics["actual"]["vals"])
        for m in range(yr["lock_month"], 12):
            if av[m] is None and comp["chain"][m] is not None:
                av[m] = comp["chain"][m]
        metrics["actual"]["vals"] = av
        demo = bool(c.execute("SELECT 1 FROM cells WHERE year=? AND dept=? AND source='demo' LIMIT 1", (year, dept)).fetchone()
                    or c.execute("SELECT 1 FROM branches WHERE year=? AND dept=? AND created_by='demo' LIMIT 1", (year, dept)).fetchone()
                    or (dept == "集团" and c.execute("SELECT 1 FROM ledger_rows WHERE batch=-999 LIMIT 1").fetchone()))
    # 含中心的部：各中心预算当量之和（供与系统取数=看板2部门维度对比·纯参考·不上卷不影响）
    budget_centers_sum = None
    if dept in DEPT_CENTERS:
        budget_centers_sum = [None] * 12
        for center in DEPT_CENTERS[dept]:
            cb = get_board(year, center)["metrics"]["budget"]["vals"]
            for m in range(12):
                if isinstance(cb[m], (int, float)):
                    budget_centers_sum[m] = (budget_centers_sum[m] if isinstance(budget_centers_sum[m], (int, float)) else 0) + cb[m]
    return {"year": year, "dept": dept, "status": yr["status"], "lock": yr["lock_month"], "seed": seed,
            "metrics": metrics, "branches": brs, "computed": comp, "nat": comp["nat"],
            "budget_centers_sum": budget_centers_sum, "demo": demo, "ts": int(time.time() * 1000)}


DEPTS_ALL = ["集团", "云产品一部", "云产品二部", "云产品三部", "云产品四部", "云产品五部"]


def _user_depts(c, user_id, field="kb0_depts"):
    """账号可见部门列表：field='kb0_depts'（PM速览·默认）或 'kb1_depts'（看板1）。
    空则管理员看全部、其余看集团。"""
    a = get_account(c, user_id)
    if not a:
        return []
    try:
        depts = json.loads((a.get(field) or "").strip() or "[]")
    except Exception:
        depts = []
    if depts:
        return depts  # 直接用配置的部门（可含「部/中心」中心路径）
    return DEPTS_ALL if a.get("role") == "管理员" else ["集团"]



class Kb0Adjust(BaseModel):
    dept: str
    month: int
    value: Optional[float] = None
    note: str = ""
    metric: str = "chain"


@app.post("/api/kb0/{year}/adjust")
def kb0_adjust_write(year: int, e: Kb0Adjust, x_user: str = Header("bonniewbli")):
    """看板0 调节项写入：仅中心/叶子部门（部级为汇总·只读）；独立存储不碰看板1 源数据。"""
    with db() as c:
        require_writer(c, x_user)
        if not (1 <= e.month <= 12):
            raise HTTPException(422, "月份须为 1-12")
        if is_agg_dept(e.dept):
            raise HTTPException(403, f"「{e.dept}」为各中心汇总（只读），请在具体中心填调节项")
        if e.value is not None and abs(e.value) > VALUE_ABS_MAX:
            raise HTTPException(422, "量级异常，拒绝入库")
        c.execute(
            "INSERT INTO kb0_adjust(year,dept,metric,month,value,note,updated_by,updated_at) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(year,dept,metric,month) DO UPDATE SET value=excluded.value,note=excluded.note,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
            (year, e.dept, e.metric, e.month, e.value, (e.note or "").strip(), x_user, now()),
        )
        _audit(c, x_user, "看板0调节", f"[{e.dept}] {year} {e.month}月 调节项 → {e.value}（{(e.note or '').strip()}）")
    return {"ok": True}  # 看板0已删；调节项写入端点保留给子PM组/线级复用


class Kb0AdjustBatch(BaseModel):
    dept: str
    cells: list  # [{month, value, note}]
    metric: str = "chain"


@app.post("/api/kb0/{year}/adjust-batch")
def kb0_adjust_batch(year: int, b: Kb0AdjustBatch, x_user: str = Header("bonniewbli")):
    """看板0 调节项整行批量写入（🖊 编辑后保存）：仅中心/叶子部门；不碰看板1 源。"""
    with db() as c:
        require_writer(c, x_user)
        if is_agg_dept(b.dept):
            raise HTTPException(403, f"「{b.dept}」为各中心汇总（只读），请在具体中心填调节项")
        for cell in b.cells:
            m = int(cell.get("month", 0))
            if not (1 <= m <= 12):
                continue
            v = cell.get("value")
            if v is not None and abs(float(v)) > VALUE_ABS_MAX:
                raise HTTPException(422, "量级异常，拒绝入库")
            c.execute(
                "INSERT INTO kb0_adjust(year,dept,metric,month,value,note,updated_by,updated_at) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(year,dept,metric,month) DO UPDATE SET value=excluded.value,note=excluded.note,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
                (year, b.dept, b.metric, m, v, (cell.get("note") or "").strip(), x_user, now()),
            )
        _audit(c, x_user, "看板0调节", f"[{b.dept}] {year} 调节项整行保存（{len(b.cells)} 格）")
    return {"ok": True}  # 看板0已删；调节项写入端点保留给子PM组/线级复用


@app.get("/api/pmgroup/{year}")
def get_pmgroup(year: int, centers: str = "", key: str = "", x_user: str = Header("bonniewbli")):
    """子PM组求和看板：对所选中心求和(预算当量/期末在岗)，叠加该组独立调节项(存 kb0_adjust，dept=key)。
    调节项写入复用 POST /api/kb0/{year}/adjust（dept 传 key；key 不在 DEPT_CENTERS→非汇总→可写）。"""
    cl = [x.strip() for x in centers.split(",") if x.strip()]
    with db() as c:
        if not c.execute("SELECT 1 FROM years WHERE year=?", (year,)).fetchone():
            raise HTTPException(404, "年份不存在")
    budget = [None] * 12
    chain = [None] * 12
    lock = 0
    demo = False
    members = []  # 各成员(部门/中心)明细：供线级/组页展开查看
    for ct in cl:
        b = get_board(year, ct)  # 复用看板1 运算求各成员 预算当量/期末在岗
        lock = b["lock"]
        demo = demo or b["demo"]
        bv = b["metrics"]["budget"]["vals"]
        cv = b["metrics"]["actual"]["vals"]
        for m in range(12):
            if isinstance(bv[m], (int, float)):
                budget[m] = (budget[m] if isinstance(budget[m], (int, float)) else 0) + bv[m]
            if isinstance(cv[m], (int, float)):
                chain[m] = (chain[m] if isinstance(chain[m], (int, float)) else 0) + cv[m]
        members.append({"name": ct, "budget": bv, "chain": cv,
                        "budgetAvg": b["computed"].get("budget_avg"), "chainAvg": b["computed"].get("chain_avg")})
    with db() as c:
        adj = _kb0_adjust(c, year, key) if key else [None] * 12
        anote = {}
        if key:
            for r in c.execute("SELECT month,note FROM kb0_adjust WHERE year=? AND dept=? AND metric='chain' AND note IS NOT NULL AND note!=''", (year, key)):
                anote[str(r["month"])] = r["note"]
    chain_adj = [((chain[m] if isinstance(chain[m], (int, float)) else 0) + adj[m])
                 if isinstance(adj[m], (int, float)) else chain[m] for m in range(12)]

    def _avg(a):
        nums = [x for x in a if isinstance(x, (int, float))]
        return round(sum(nums) / len(nums), 2) if nums else None
    return {"year": year, "key": key, "centers": cl, "lock": lock, "demo": demo,
            "budget": budget, "chain": chain, "adjust": adj, "adjustNote": anote, "chainAdj": chain_adj,
            "budgetAvg": _avg(budget), "chainAvg": _avg(chain), "chainAdjAvg": _avg(chain_adj),
            "members": members}


class CellEdit(BaseModel):
    metric: str
    month: int  # 1-12
    value: Optional[float] = None  # None=清空
    note: str = ""  # 260723 起可选：Excel 式直填不强制备注，右键可补


@app.post("/api/board/{year}/cell")
def edit_cell(year: int, e: CellEdit, dept: str = "集团", x_user: str = Header("bonniewbli")):
    with db() as c:
        require_writer(c, x_user)
        if is_agg_dept(dept):
            raise HTTPException(403, f"「{dept}」为各中心汇总（只读），请在具体中心录入")
        yr = c.execute("SELECT * FROM years WHERE year=?", (year,)).fetchone()
        if not yr:
            raise HTTPException(404, "年份不存在")
        if not (1 <= e.month <= 12):
            raise HTTPException(422, "月份须为 1-12")
        base_metric = e.metric.split(":", 1)[0] if e.metric.startswith("branch") else e.metric
        # 项目「可手改」权限：'all'=全年可改（覆盖已发生月锁定）/'future'=仅未发生月/'no'=禁/''=未配置(沿用 add_ok)
        proj = None
        if not e.metric.startswith("branch:") and e.metric not in EXTRA_METRICS:
            proj = c.execute("SELECT * FROM projects WHERE key=?", (e.metric,)).fetchone()
        proj_edit = (proj["edit"] if proj is not None and "edit" in proj.keys() else "") or ""
        locked = base_metric not in PLAN_METRICS and e.month <= yr["lock_month"] and proj_edit != "all"
        if locked and e.metric.startswith("branch:"):
            pb = c.execute("SELECT sec FROM branches WHERE id=?", (int(e.metric.split(":", 1)[1]),)).fetchone()
            if pb and pb["sec"] in PLAN_BRANCH_SECS:
                locked = False  # 看板2 计划类分支（法定HC/预算当量·其中）：规划数据，已发生月可改
        if locked:
            raise HTTPException(423, f"{e.month}月为已发生月（已锁定），改动须走「采纳修正」流程")
        # 260723 交互调整：备注改为可选（Excel 式直填；右键单元格可补备注）——改动本身仍全量审计留痕
        if e.value is not None and abs(e.value) > VALUE_ABS_MAX:
            raise HTTPException(422, "量级异常，拒绝入库")
        if e.metric.startswith("branch:"):
            bid = int(e.metric.split(":", 1)[1])
            b = c.execute("SELECT * FROM branches WHERE id=? AND year=? AND dept=?", (bid, year, dept)).fetchone()
            if not b:
                raise HTTPException(404, "分支不存在")
            c.execute(
                "INSERT INTO branch_cells(branch_id,month,value,note,updated_by,updated_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(branch_id,month) DO UPDATE SET value=excluded.value,note=excluded.note,"
                "updated_by=excluded.updated_by,updated_at=excluded.updated_at",
                (bid, e.month, e.value, e.note.strip(), x_user, now()),
            )
            _audit(c, x_user, "调节录入", f"{year} 分支「{b['name']}」 {e.month}月 → {e.value}（{e.note.strip()}）")
        elif e.metric in EXTRA_METRICS:
            _write_cell(c, year, e.metric, e.month, e.value, e.note.strip(), "bp", x_user, dept)
            _audit(c, x_user, "调节录入", f"[{dept}] {year}「{EXTRA_METRICS[e.metric]}」 {e.month}月 → {e.value}（{e.note.strip()}）")
        else:
            if proj is None:
                raise HTTPException(404, "指标不存在")
            if e.metric not in BP_EDITABLE:
                raise HTTPException(403, f"「{proj['name']}」为系统数指标，不可手工录入（走数据源/上传兜底）")
            if proj_edit == "no":
                raise HTTPException(403, f"「{proj['name']}」已在管理后台关闭手改")
            if proj_edit == "" and not proj["add_ok"]:
                raise HTTPException(403, f"「{proj['name']}」已在管理后台关闭手动录入")
            _write_cell(c, year, e.metric, e.month, e.value, e.note.strip(), "bp", x_user, dept)
            _audit(c, x_user, "调节录入", f"[{dept}] {year}「{proj['name']}」 {e.month}月 → {e.value}（{e.note.strip()}）")
    return get_board(year, dept)


class BranchNew(BaseModel):
    sec: str
    name: str
    sign: str  # '+' / '-'


@app.post("/api/board/{year}/branch")
def add_branch(year: int, b: BranchNew, dept: str = "集团", x_user: str = Header("bonniewbli")):
    if b.sign not in ("+", "-"):
        raise HTTPException(422, "方向须为 + 或 −")
    if not b.name.strip():
        raise HTTPException(422, "分支名称必填")
    with db() as c:
        require_writer(c, x_user)
        c.execute(
            "INSERT INTO branches(year,dept,sec,name,sign,on_ok,created_by,created_at) VALUES(?,?,?,?,?,1,?,?)",
            (year, dept, b.sec, b.name.strip(), b.sign, x_user, now()),
        )
        _audit(c, x_user, "新增分支", f"[{dept}] {year} {b.sec} · {b.name.strip()}（{b.sign}）")
    return get_board(year, dept)


class BranchRename(BaseModel):
    name: str


@app.put("/api/board/{year}/branch/{bid}")
def rename_branch(year: int, bid: int, b: BranchRename, dept: str = "集团", x_user: str = Header("bonniewbli")):
    if not b.name.strip():
        raise HTTPException(422, "分支名称必填")
    with db() as c:
        require_writer(c, x_user)
        row = c.execute("SELECT * FROM branches WHERE id=? AND year=?", (bid, year)).fetchone()
        if not row:
            raise HTTPException(404, "分支不存在")
        c.execute("UPDATE branches SET name=? WHERE id=?", (b.name.strip(), bid))
        _audit(c, x_user, "改分支定义", f"{year} {row['sec']} · 「{row['name']}」→「{b.name.strip()}」")
    return get_board(year, dept)


@app.delete("/api/board/{year}/branch/{bid}")
def del_branch(year: int, bid: int, dept: str = "集团", x_user: str = Header("bonniewbli")):
    with db() as c:
        require_writer(c, x_user)
        b = c.execute("SELECT * FROM branches WHERE id=? AND year=?", (bid, year)).fetchone()
        if not b:
            raise HTTPException(404, "分支不存在")
        c.execute("DELETE FROM branches WHERE id=?", (bid,))
        _audit(c, x_user, "删除分支", f"{year} {b['sec']} · {b['name']}")
    return get_board(year, dept)


# ---------------- 上传兜底：CSV 导入 → 校验闸 → 快照入库 ----------------
@app.post("/api/import/{year}")
async def import_csv(year: int, file: UploadFile = File(...), x_user: str = Header("bonniewbli")):
    with db() as c:
        require_writer(c, x_user)
        if not c.execute("SELECT 1 FROM years WHERE year=?", (year,)).fetchone():
            raise HTTPException(404, "年份不存在")
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("gbk", errors="replace")
    reader = csv.reader(io.StringIO(text))
    rows, errors = [], []
    header_skipped = False
    for ln, row in enumerate(reader, start=1):
        if not row or all(not x.strip() for x in row):
            continue
        if len(row) < 3:
            errors.append(f"第{ln}行：缺字段（需 metric,month,value）")
            continue
        metric, month_s, value_s = row[0].strip(), row[1].strip(), row[2].strip()
        if not header_skipped and metric.lower() in ("metric", "指标", "key"):
            header_skipped = True
            continue
        if metric not in IMPORTABLE:
            errors.append(f"第{ln}行：未知/不可导入指标「{metric}」（可导入：{','.join(sorted(IMPORTABLE))}）")
            continue
        if not month_s.isdigit() or not (1 <= int(month_s) <= 12):
            errors.append(f"第{ln}行：期间错（month 须 1-12，收到「{month_s}」）")
            continue
        try:
            value = float(value_s)
        except ValueError:
            errors.append(f"第{ln}行：数值无效「{value_s}」")
            continue
        if abs(value) > VALUE_ABS_MAX:
            errors.append(f"第{ln}行：量级异常（|{value}|>{VALUE_ABS_MAX}）")
            continue
        rows.append((metric, int(month_s), value))
    if errors:
        raise HTTPException(422, {"msg": "数据不合格，整批拒绝入库", "errors": errors[:50], "total_errors": len(errors)})
    if not rows:
        raise HTTPException(422, "文件无有效数据行")
    with db() as c:
        require_writer(c, x_user)
        yr = c.execute("SELECT * FROM years WHERE year=?", (year,)).fetchone()
        lock = yr["lock_month"]
        c.execute(
            "INSERT INTO snapshots(year,filename,rows_n,created_by,created_at) VALUES(?,?,?,?,?)",
            (year, file.filename, len(rows), x_user, now()),
        )
        snap_id = c.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
        applied, diffs = 0, 0
        for metric, month, value in rows:
            cur = c.execute("SELECT value FROM cells WHERE year=? AND metric=? AND month=?", (year, metric, month)).fetchone()
            curv = cur["value"] if cur else None
            confirmed = metric not in PLAN_METRICS and month <= lock and curv is not None
            if confirmed and curv != value:
                # 迟到数据：已确认月不自动改——留差异，待「采纳修正/维持口径」
                ex = c.execute(
                    "SELECT id FROM pending_diffs WHERE year=? AND metric=? AND month=? AND status='open'",
                    (year, metric, month)).fetchone()
                if ex:
                    c.execute("UPDATE pending_diffs SET src_value=?, source=?, created_at=? WHERE id=?",
                              (value, f"import#{snap_id}", now(), ex["id"]))
                else:
                    c.execute(
                        "INSERT INTO pending_diffs(year,metric,month,cur_value,src_value,source,status,created_at) "
                        "VALUES(?,?,?,?,?,?,'open',?)",
                        (year, metric, month, curv, value, f"import#{snap_id}", now()))
                diffs += 1
            else:
                _write_cell(c, year, metric, month, value, None, f"import#{snap_id}", x_user)
                applied += 1
        _audit(c, x_user, "导入快照",
               f"{year} 批次#{snap_id}「{file.filename}」采纳 {applied} 格" +
               (f"；已确认月差异 {diffs} 格待处理（源数据已变化）" if diffs else "，校验闸通过入库"))
    return {"ok": True, "snapshot": snap_id, "rows": len(rows), "applied": applied, "diffs": diffs}


@app.get("/api/sources")
def sources_status():
    cfg = load_sources_cfg()
    out = []
    for k, name in SOURCE_METRICS.items():
        entries = cfg.get(k) or []
        if isinstance(entries, dict):
            entries = [entries]
        out.append({"metric": k, "name": name,
                    "configured": bool(entries and entries[0].get("url")),
                    "sources": [{"name": e.get("name", "?"), "url": e.get("url", ""), "note": e.get("note", "")} for e in entries]})
    return {"sources": out, "cfg_path": "backend/sources_config.json（gitignore·凭据不进仓库）"}


class SyncReq(BaseModel):
    year: int
    source: Optional[str] = None  # 多来源时指定 name（如 diy 第二来源核对），缺省用第一个


@app.post("/api/sources/{metric}/sync")
def source_sync(metric: str, q: SyncReq, x_user: str = Header("bonniewbli")):
    if metric not in SOURCE_METRICS or metric not in IMPORTABLE:
        raise HTTPException(404, f"指标「{metric}」不支持外部源直连")
    entries = load_sources_cfg().get(metric) or []
    if isinstance(entries, dict):
        entries = [entries]
    cfg = next((e for e in entries if not q.source or e.get("name") == q.source), None)
    if not cfg or not cfg.get("url"):
        raise HTTPException(428, {"msg": f"「{SOURCE_METRICS[metric]}」外部源未配置",
                                  "need": "backend/sources_config.json 填入该指标的 url/method/headers/params/map（参照 sources_config.example.json；来源=F12抓包 Copy as cURL 或平台开放API文档）"})
    with db() as c:
        require_writer(c, x_user)
        yr = c.execute("SELECT * FROM years WHERE year=?", (q.year,)).fetchone()
        if not yr:
            raise HTTPException(404, "年份不存在")
    months_vals = fetch_source(cfg, q.year)
    bad = [f"{m}月={v}" for m, v in months_vals.items() if abs(v) > VALUE_ABS_MAX]
    if bad:
        raise HTTPException(422, {"msg": "量级异常，整批拒绝入库", "errors": bad})
    with db() as c:
        require_writer(c, x_user)
        lock = yr["lock_month"]
        src_tag = f"sync:{cfg.get('name', metric)}"
        applied, diffs, skipped = 0, 0, []
        for m in sorted(months_vals):
            v = months_vals[m]
            if not _month_completed(q.year, m):
                skipped.append(m)  # 未完结月：月末快照尚不存在
                continue
            cur = c.execute("SELECT value FROM cells WHERE year=? AND metric=? AND month=?", (q.year, metric, m)).fetchone()
            curv = cur["value"] if cur else None
            confirmed = metric not in PLAN_METRICS and m <= lock and curv is not None
            if confirmed and curv != v:
                ex = c.execute("SELECT id FROM pending_diffs WHERE year=? AND metric=? AND month=? AND status='open'",
                               (q.year, metric, m)).fetchone()
                if ex:
                    c.execute("UPDATE pending_diffs SET src_value=?, source=?, created_at=? WHERE id=?",
                              (v, src_tag, now(), ex["id"]))
                else:
                    c.execute("INSERT INTO pending_diffs(year,metric,month,cur_value,src_value,source,status,created_at) "
                              "VALUES(?,?,?,?,?,?,'open',?)", (q.year, metric, m, curv, v, src_tag, now()))
                diffs += 1
            else:
                _write_cell(c, q.year, metric, m, v, None, src_tag, x_user)
                applied += 1
        _audit(c, x_user, "源同步",
               f"{q.year}「{SOURCE_METRICS[metric]}」← {cfg.get('name', '?')}：采纳 {applied} 格" +
               (f"；已确认月差异 {diffs} 格待处理" if diffs else "") +
               (f"；跳过未完结月 {','.join(map(str, skipped))}（月末快照未成立）" if skipped else ""))
    return {"ok": True, "metric": metric, "source": cfg.get("name", "?"),
            "applied": applied, "diffs": diffs, "skipped": skipped}


# ---------------- 示例（演示假数）数据：source='demo' 全程打标，一键彻底清除 ----------------
# 高压线兜底：示例数据只填当前为空的格（绝不覆盖真实数据）；页面挂【示例】横幅；
# 清除 = 按 demo 标签删 cells/branch/台账/历史，真实数据分毫不动。
DEMO_LEDGER_BATCH = -999


@app.post("/api/demo/load")
def demo_load(y: YearNew, x_user: str = Header("bonniewbli")):
    """一次填所有年份页签（含历史归档年）：按各年 lock 填历史实际月，只填空格不覆盖真数"""
    with db() as c:
        require_writer(c, x_user)
        if not c.execute("SELECT 1 FROM years WHERE year=?", (y.year,)).fetchone():
            raise HTTPException(404, "年份不存在")
        filled, skipped = 0, 0
        for yr in c.execute("SELECT * FROM years ORDER BY year").fetchall():
            yy, lock = yr["year"], yr["lock_month"]
            base = 548 + (2026 - yy) * 6  # 历史年在岗基数略高，逐年递减更像真实走势

            def put(metric, month, value, note=None):
                nonlocal filled, skipped
                if not (1 <= month <= 12):
                    return
                if c.execute("SELECT 1 FROM cells WHERE year=? AND metric=? AND month=?", (yy, metric, month)).fetchone():
                    skipped += 1  # 已有数据（真实或旧示例）不覆盖
                    return
                c.execute("INSERT INTO cells(year,metric,month,value,note,source,updated_by,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                          (yy, metric, month, value, note, "demo", x_user, now()))
                filled += 1

            for m in range(1, 13):
                # 预算当量口径统一(budget=q_init)：若该月已有真数(非示例)则镜像真数，示例绝不遮蔽真实预算
                real = c.execute("SELECT value FROM cells WHERE year=? AND metric IN('budget','q_init') "
                                 "AND month=? AND source!='demo' AND value IS NOT NULL", (yy, m)).fetchone()
                bval = real["value"] if real else 550 + (2026 - yy) * 5
                put("budget", m, bval)
                put("fa_hc", m, 479)
                put("q_init", m, bval)
            # 历史实际月：只随机填「源」单元格（Excel 思路——运算行 o_nat/总流出/总流入/链 由引擎公式现算，不落库）
            rng = random.Random(yy * 97 + 7)  # 每年固定种子：重复导入结果一致（幂等）
            # 每个单元格都有数、且合理（示例·便人工核对运算打通）：社招三子行(系统源)全12月；
            # 已发生月「实际社招入职」≈「待流入·社招」三子行合计（预估准 → 实际≈预测），流出/调节全12月
            # 一律 ≥1（0=空）：不留 0 值空格
            for m in range(1, 13):
                sj = rng.randint(1, 2)   # 社招·已入职（系统·HR数仓）
                ss = rng.randint(1, 3)   # 社招·待入职（社招系统）
                sh = rng.randint(1, 2)   # 活水·已offer（活水系统）
                put("soc_join", m, sj)   # 无备注（避免只读格挂备注）
                put("soc_sys", m, ss)
                put("soc_hs", m, sh)
                put("soc_bp", m, rng.randint(1, 2))
                put("o_sys", m, rng.randint(1, 3))
                put("o_bp", m, rng.randint(1, 2))
                put("o_act", m, rng.randint(1, 2))
                put("i_incr", m, rng.choice([-2, -1, 1, 2]))  # 调节项：可正可负，非0（0=空）
                if m <= lock:  # 已发生月·实际口径
                    put("actual", m, base - m + rng.randint(-2, 2))  # 月末快照
                    put("er_out", m, rng.randint(4, 9))  # ER实际离职（o_nat 源）
                    # 实际社招入职 ≈ 待流入·社招三子行合计（sj+ss+sh + 简历面试中≈1），小噪声
                    put("ai_soc", m, max(1, sj + ss + sh + 1 + rng.randint(-1, 1)), "【示例】实际社招入职（≈待流入·社招·预估准）")
                    put("ai_camp", m, rng.randint(1, 3), "【示例】当月实际校招入职（系统读）")
                    put("ao_lv", m, rng.randint(2, 6), "【示例】当月实际离职·主动+被动（系统读）")
                    put("ao_tr", m, rng.randint(1, 2), "【示例】当月实际调出（系统读）")
            for m in range(lock + 1, 13):  # 校招 BP 按月分配：仅未发生月
                put("camp_off", m, rng.randint(1, 4), "【示例】校招BP·按月分配")
            put("camp_off_tot", 1, 24, "【示例】数仓·校招已offer待入职总数")
            if lock >= 2:
                put("i_yy", 2, 3, "【示例】春季批次到岗")
            if lock >= 3:
                put("i_bs", 3, 2, "【示例】毕业生转聘2人")
            if lock >= 5:
                put("i_cbp", 5, 1, "【示例】BP补录1人")
            if lock >= 7:
                put("i_yy", 7, 22, "【示例】历史校招批次")
            if lock < 12:  # 执行中/规划年：未发生月的 BP 调节与分支
                put("o_sys", lock + 1, 2)
                put("o_bp", lock + 3, 3, "【示例】某中心已明确离职3人")
                put("o_act", lock + 4, 2, "【示例】计划优化2人")
                put("soc_sys", lock + 1, 3)
                put("soc_hs", lock + 1, 1)
                put("soc_sys", lock + 2, 3)
                put("soc_bp", lock + 3, 2, "【示例】社招台账待入职（PlanB·BP录）")
                put("i_yy", lock + 1, 25, "【示例】校招批次到岗")
                put("i_bs", lock + 2, 5)
                put("i_cbp", lock + 3, 1, "【示例】BP补录1人")
                brow = c.execute("SELECT id FROM branches WHERE year=? AND created_by='demo'", (yy,)).fetchone()
                if brow:
                    bid = brow["id"]
                else:
                    c.execute("INSERT INTO branches(year,sec,name,sign,on_ok,created_by,created_at) VALUES(?,?,?,?,1,'demo',?)",
                              (yy, "总流出（−）", "【示例】组织架构腾挪", "+", now()))
                    bid = c.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
                # 260723 起分支纯计人头：负数=调减（示范系统数冲抵写法）
                for bm, bv, bnote in [(lock + 3, -2, "【示例】腾挪冲抵-2（负数=调减）"), (max(lock - 2, 1), 1, "【示例】历史月腾挪1")]:
                    if 1 <= bm <= 12:
                        c.execute("INSERT OR IGNORE INTO branch_cells(branch_id,month,value,note,updated_by,updated_at) VALUES(?,?,?,?,'demo',?)",
                                  (bid, bm, bv, bnote, now()))
        if not c.execute("SELECT 1 FROM ledger_rows WHERE batch=?", (DEMO_LEDGER_BATCH,)).fetchone():
            mm = lambda off: f"{y.year}-{min(lock + off, 12):02d}-15"
            demo_rows = [
                ("已入职", "", mm(0), "【示例】张三", "后台开发", mm(0)),
                ("已offer待入职", mm(1), "", "【示例】李四", "产品经理", ""),
                ("简历&面试中", mm(2), "", "【示例】王五", "前端开发", ""),
                ("Hold", "", "", "【示例】赵六", "测试开发", ""),
            ]
            for st, eta, join_dt, who, job, jd in demo_rows:
                c.execute("INSERT INTO ledger_rows(year,batch,dept,center,owner,src,job,lvl,cls,loc,ask,num,tgt,st,eta,memo,offer,olvl,join_dt,jmemo,who) "
                          "VALUES(0,?,?,?,?,?,?,'','','深圳',?,'1','',?,?,?,?,'',?,'',?)",
                          (DEMO_LEDGER_BATCH, "【示例】云产品五部", "【示例】某中心", "【示例】负责人", "【示例】演示行",
                           job, f"{y.year}-{max(lock - 1, 1):02d}-01", st, eta, "【示例】", who, join_dt or jd, who))
            # 每月放一条「简历&面试中」示例台账，使「社招·简历面试中」（读3.1台账）每月都有一点
            for mo in range(1, 13):
                c.execute("INSERT INTO ledger_rows(year,batch,dept,center,owner,src,job,lvl,cls,loc,ask,num,tgt,st,eta,memo,offer,olvl,join_dt,jmemo,who) "
                          "VALUES(0,?,?,?,?,?,?,'','','深圳',?,'1','','简历&面试中',?,?,'','',?,'',?)",
                          (DEMO_LEDGER_BATCH, "【示例】云产品五部", "【示例】某中心", "【示例】负责人", "【示例】面试演示",
                           f"面试岗{mo}", f"{y.year}-{max(mo - 1, 1):02d}-01", f"{y.year}-{mo:02d}-20", "【示例】", "", f"【示例】面试人{mo}"))
        _audit(c, x_user, "导入示例数据",
               f"全部年份页签（含历史归档年填满实际月）：示例(demo标签)填充 {filled} 格（跳过已有数据 {skipped} 格·不覆盖真实数）+ 示例分支/台账4行；页面挂【示例】横幅，说「删除假数」一键全清")
    return get_board(y.year)


@app.post("/api/demo/clear")
def demo_clear(x_user: str = Header("bonniewbli")):
    with db() as c:
        require_writer(c, x_user)
        n_cells = c.execute("SELECT COUNT(*) AS n FROM cells WHERE source='demo'").fetchone()["n"]
        c.execute("DELETE FROM cells WHERE source='demo'")
        c.execute("DELETE FROM cells_history WHERE source='demo'")
        bids = [r["id"] for r in c.execute("SELECT id FROM branches WHERE created_by='demo'")]
        for bid in bids:
            c.execute("DELETE FROM branch_cells WHERE branch_id=?", (bid,))
            c.execute("DELETE FROM branches WHERE id=?", (bid,))
        n_led = c.execute("SELECT COUNT(*) AS n FROM ledger_rows WHERE batch=?", (DEMO_LEDGER_BATCH,)).fetchone()["n"]
        c.execute("DELETE FROM ledger_rows WHERE batch=?", (DEMO_LEDGER_BATCH,))
        c.execute("DELETE FROM pending_diffs WHERE source='demo'")
        _audit(c, x_user, "清除示例数据",
               f"按 demo 标签彻底清除：{n_cells} 格 + 分支 {len(bids)} 条 + 台账 {n_led} 行（真实数据不动，审计留痕）")
    return {"ok": True, "cells": n_cells, "branches": len(bids), "ledger": n_led}


# ---------------- 审计 / 导出 ----------------
@app.get("/api/audit")
def get_audit(limit: int = 200):
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (min(limit, 1000),))]


@app.get("/api/export/{year}.csv")
def export_csv(year: int):
    board = get_board(year)
    names = {k: n for k, _s, n, *_ in CANON_PROJECTS}
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["项目"] + [f"{m}月" for m in range(1, 13)] + ["年均"])
    def fmt(v):
        return "" if v is None else (int(v) if float(v).is_integer() else v)
    w.writerow([names["budget"]] + [fmt(v) for v in board["metrics"]["budget"]["vals"]] + [""])
    w.writerow(["总流出（−）"] + [fmt(v) for v in board["computed"]["outT"]] + [""])
    w.writerow(["总流入（＋）"] + [fmt(v) for v in board["computed"]["inT"]] + [""])
    # 260723 口径：实际与预估合一行（已发生月=实际，未发生月=链）
    w.writerow([names["actual"]] + [fmt(v) for v in board["metrics"]["actual"]["vals"]] + [board["computed"]["chain_avg"] or ""])
    with db() as c:
        _audit(c, "system", "导出", f"{year} 看板1 导出 CSV")
    return PlainTextResponse("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8")


# ---------------- 迟到数据差异：采纳修正 / 维持口径 ----------------
def _metric_name(c, key):
    if key == "er_out":
        return "ER报表·月实际离职数（HR数仓·URL待求）"
    if key in EXTRA_METRICS:
        return EXTRA_METRICS[key]
    p = c.execute("SELECT name FROM projects WHERE key=?", (key,)).fetchone()
    return p["name"] if p else key


@app.get("/api/diffs/{year}")
def get_diffs(year: int):
    with db() as c:
        out = []
        for r in c.execute("SELECT * FROM pending_diffs WHERE year=? AND status='open' ORDER BY month,metric", (year,)):
            d = dict(r)
            d["metric_name"] = _metric_name(c, d["metric"])
            out.append(d)
        return {"year": year, "diffs": out}


@app.post("/api/diffs/{did}/accept")
def diff_accept(did: int, x_user: str = Header("bonniewbli")):
    with db() as c:
        require_writer(c, x_user)
        d = c.execute("SELECT * FROM pending_diffs WHERE id=? AND status='open'", (did,)).fetchone()
        if not d:
            raise HTTPException(404, "差异不存在或已处理")
        _write_cell(c, d["year"], d["metric"], d["month"], d["src_value"], "采纳修正（迟到数据）", d["source"], x_user)
        c.execute("UPDATE pending_diffs SET status='accepted', resolved_by=?, resolved_at=? WHERE id=?", (x_user, now(), did))
        _audit(c, x_user, "采纳修正",
               f"{d['year']}「{_metric_name(c, d['metric'])}」{d['month']}月：{d['cur_value']} → {d['src_value']}（预估链重锚重算·校验重跑）")
    return get_board(d["year"])


@app.post("/api/diffs/{did}/keep")
def diff_keep(did: int, x_user: str = Header("bonniewbli")):
    with db() as c:
        require_writer(c, x_user)
        d = c.execute("SELECT * FROM pending_diffs WHERE id=? AND status='open'", (did,)).fetchone()
        if not d:
            raise HTTPException(404, "差异不存在或已处理")
        c.execute("UPDATE pending_diffs SET status='kept', resolved_by=?, resolved_at=? WHERE id=?", (x_user, now(), did))
        _audit(c, x_user, "维持口径",
               f"{d['year']}「{_metric_name(c, d['metric'])}」{d['month']}月差异（{d['cur_value']} vs 源 {d['src_value']}）留案，随下周期滚入")
        return {"ok": True}


# ---------------- 月份确认：锁定推进 ----------------
class LockSet(BaseModel):
    lock_month: int


@app.post("/api/years/{year}/lock")
def set_lock(year: int, s: LockSet, x_user: str = Header("bonniewbli")):
    if not (0 <= s.lock_month <= 12):
        raise HTTPException(422, "锁定月须为 0-12")
    with db() as c:
        require_writer(c, x_user)
        yr = c.execute("SELECT * FROM years WHERE year=?", (year,)).fetchone()
        if not yr:
            raise HTTPException(404, "年份不存在")
        c.execute("UPDATE years SET lock_month=? WHERE year=?", (s.lock_month, year))
        _audit(c, x_user, "月份确认", f"{year} 锁定推进：1-{yr['lock_month']}月 → 1-{s.lock_month}月（已确认月改动须走采纳修正）")
    return {"ok": True, "year": year, "lock_month": s.lock_month}


# ---------------- 自然流失预估调参（参考前 n 个月平均） ----------------
class NatParam(BaseModel):
    n: int


@app.post("/api/years/{year}/natparam")
def set_natparam(year: int, p: NatParam, x_user: str = Header("bonniewbli")):
    if not (1 <= p.n <= 24):
        raise HTTPException(422, "回看月数 n 须为 1-24")
    with db() as c:
        require_writer(c, x_user)
        yr = c.execute("SELECT * FROM years WHERE year=?", (year,)).fetchone()
        if not yr:
            raise HTTPException(404, "年份不存在")
        old_n = yr["nat_n"] if "nat_n" in yr.keys() else NAT_N_DEFAULT
        c.execute("UPDATE years SET nat_n=? WHERE year=?", (p.n, year))
        _audit(c, x_user, "自然流失调参", f"{year} 回看月数 n：{old_n} → {p.n}（近{p.n}个月实际离职均值摊到未发生月，重算）")
    return get_board(year)


# ---------------- 分支停用 / 还原（不删数据） ----------------
@app.post("/api/board/{year}/branch/{bid}/toggle")
def branch_toggle(year: int, bid: int, dept: str = "集团", x_user: str = Header("bonniewbli")):
    with db() as c:
        require_writer(c, x_user)
        b = c.execute("SELECT * FROM branches WHERE id=? AND year=?", (bid, year)).fetchone()
        if not b:
            raise HTTPException(404, "分支不存在")
        newv = 0 if b["on_ok"] else 1
        c.execute("UPDATE branches SET on_ok=? WHERE id=?", (newv, bid))
        _audit(c, x_user, "启用分支" if newv else "停用分支",
               f"{year} {b['sec']} · {b['name']}（数据保留" + ("，已恢复计入合计）" if newv else "，可随时还原）"))
    return get_board(year, dept)


# ---------------- 单元格变更历史（口径可复现） ----------------
@app.get("/api/board/{year}/history")
def cell_history(year: int, metric: str, month: int):
    with db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM cells_history WHERE year=? AND metric=? AND month=? ORDER BY id DESC LIMIT 50",
            (year, metric, month))]
        return {"year": year, "metric": metric, "metric_name": _metric_name(c, metric), "month": month, "history": rows}


# ---------------- 导出 xlsx ----------------
@app.get("/api/export/{year}.xlsx")
def export_xlsx(year: int):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from fastapi.responses import Response
    board = get_board(year)
    names = {k: n for k, _s, n, *_ in CANON_PROJECTS}
    wb = Workbook()
    ws = wb.active
    ws.title = f"看板1-{year}"
    header = ["项目"] + [f"{m}月" for m in range(1, 13)] + ["年均"]
    ws.append(header)
    for cell in ws[1]:
        cell.font = Font(name="微软雅黑", bold=True)
        cell.fill = PatternFill("solid", fgColor="F3F8FF")
    def row(name, vals, avg_=None):
        ws.append([name] + [("" if v is None else v) for v in vals] + [avg_ if avg_ is not None else ""])
    row(names["budget"], board["metrics"]["budget"]["vals"], board["computed"]["budget_avg"])
    row("总流出（−）", board["computed"]["outT"])
    row("总流入（＋）", board["computed"]["inT"])
    # 260723 口径：实际与预估合一行
    row(names["actual"], board["metrics"]["actual"]["vals"], board["computed"]["chain_avg"])
    ws.column_dimensions["A"].width = 22
    buf = io.BytesIO()
    wb.save(buf)
    with db() as c:
        _audit(c, "system", "导出", f"{year} 看板1 导出 xlsx")
    return Response(buf.getvalue(),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename=board1-{year}.xlsx"})


class LedgerEdit(BaseModel):
    fields: dict  # 前端字段名 → 新值（仅人工列；派生列不存库）


@app.post("/api/ledger/row")
def ledger_add_row(e: LedgerEdit, x_user: str = Header("bonniewbli")):
    with db() as c:
        require_writer(c, x_user)
        vals = {db_f: "" for db_f in LEDGER_F2DB.values()}
        for f, v in (e.fields or {}).items():
            if f in LEDGER_F2DB:
                vals[LEDGER_F2DB[f]] = _ledger_norm(f, v)
        missing = [lab for f, lab in LEDGER_REQUIRED if not vals[LEDGER_F2DB[f]]]
        if missing:
            raise HTTPException(422, "必填项未填：" + "、".join(missing))
        if not vals["num"]:
            vals["num"] = "1"  # 每行=一个名额
        try:
            num_ok = float(vals["num"]) == 1
        except ValueError:
            num_ok = False
        if not num_ok:
            raise HTTPException(422, f"招聘数量「{vals['num']}」必须为 1——每行一个名额，多名额请另起一行")
        if vals["st"] and vals["st"] not in LEDGER_ST_CANON:
            raise HTTPException(422, f"当前状态「{vals['st']}」不在下拉枚举（{'/'.join(LEDGER_ST_CANON)}）")
        if vals["cls"] and vals["cls"] not in LEDGER_CLS:
            raise HTTPException(422, "国内/海外 须为：国内 或 海外")
        c.execute(
            "INSERT INTO ledger_rows(year,batch,dept,center,owner,src,job,rmgr,lvl,fam,cls,loc,ask,num,tgt,st,eta,prev_eta,memo,offer,olvl,join_dt,jmemo,who) "
            "VALUES(0,0,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (vals["dept"], vals["center"], vals["owner"], vals["src"], vals["job"], vals["rmgr"], vals["lvl"], vals["fam"], vals["cls"], vals["loc"],
             vals["ask"], vals["num"], vals["tgt"], vals["st"], vals["eta"], vals["prev_eta"], vals["memo"],
             vals["offer"], vals["olvl"], vals["join_dt"], vals["jmemo"], vals["who"]),
        )
        rid = c.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
        _audit(c, x_user, "台账新增行", f"行#{rid}" + (f"（{vals['center']} · {vals['job']}）" if vals["center"] or vals["job"] else "（空行·待填）"))
    with db() as c:
        return {"ok": True, "id": rid, "rows": _ledger_list(c, 0)}


@app.put("/api/ledger/row/{rid}")
def ledger_edit_row(rid: int, e: LedgerEdit, x_user: str = Header("bonniewbli")):
    with db() as c:
        require_writer(c, x_user)
        row = c.execute("SELECT * FROM ledger_rows WHERE id=? AND year=0", (rid,)).fetchone()
        if not row:
            raise HTTPException(404, "台账行不存在")
        changes = []
        req = dict(LEDGER_REQUIRED)
        fields = e.fields or {}
        # 列性质闸：数量=1 / 状态枚举 / 国内海外枚举
        if "num" in fields:
            nv_num = _ledger_norm("num", fields["num"]) or "1"
            try:
                if float(nv_num) != 1:
                    raise HTTPException(422, f"招聘数量「{nv_num}」必须为 1——每行一个名额，多名额请另起一行")
            except ValueError:
                raise HTTPException(422, f"招聘数量「{nv_num}」必须为 1——每行一个名额，多名额请另起一行")
        if "st" in fields:
            nv_st = _ledger_norm("st", fields["st"])
            if nv_st and nv_st not in LEDGER_ST_CANON:
                raise HTTPException(422, f"当前状态「{nv_st}」不在下拉枚举（{'/'.join(LEDGER_ST_CANON)}）")
        if "cls" in fields:
            nv_cls = _ledger_norm("cls", fields["cls"])
            if nv_cls and nv_cls not in LEDGER_CLS:
                raise HTTPException(422, "国内/海外 须为：国内 或 海外")
        # 预计到岗变更闸：须填变更原因（memo），旧值自动写入「上次预计到岗(参考)」
        if "eta" in fields:
            nv_eta = _ledger_norm("eta", fields["eta"])
            old_eta = row["eta"] or ""
            if nv_eta != old_eta:
                new_memo = _ledger_norm("memo", fields.get("memo", ""))
                if not new_memo:
                    raise HTTPException(422, "预计到岗时间变更须填写「变更原因/卡点备注」（模板 P 列规则）")
                if old_eta:
                    c.execute("UPDATE ledger_rows SET prev_eta=? WHERE id=?", (old_eta, rid))
                    changes.append(f"peta:自动记录上次预计到岗「{old_eta}」")
        for f, v in fields.items():
            if f not in LEDGER_F2DB:
                continue
            db_f = LEDGER_F2DB[f]
            nv = _ledger_norm(f, v)
            if f in req and not nv:
                raise HTTPException(422, f"必填项「{req[f]}」不能清空")
            ov = row[db_f] or ""
            if nv != ov:
                c.execute(f"UPDATE ledger_rows SET {db_f}=? WHERE id=?", (nv, rid))
                changes.append(f"{f}:「{ov}」→「{nv}」")
        if changes:
            _audit(c, x_user, "台账改行", f"行#{rid} " + "；".join(changes)[:300])
    with db() as c:
        return {"ok": True, "rows": _ledger_list(c, 0)}


@app.delete("/api/ledger/row/{rid}")
def ledger_del_row(rid: int, x_user: str = Header("bonniewbli")):
    with db() as c:
        require_writer(c, x_user)
        row = c.execute("SELECT * FROM ledger_rows WHERE id=? AND year=0", (rid,)).fetchone()
        if not row:
            raise HTTPException(404, "台账行不存在")
        c.execute("DELETE FROM ledger_rows WHERE id=?", (rid,))
        _audit(c, x_user, "台账删行", f"行#{rid}（{row['center']} · {row['job']} · {row['st']}）")
    with db() as c:
        return {"ok": True, "rows": _ledger_list(c, 0)}


@app.get("/api/ledger")
def get_ledger():
    with db() as c:
        return {"rows": _ledger_list(c, 0)}


@app.post("/api/ledger/import")
async def import_ledger(file: UploadFile = File(...), x_user: str = Header("bonniewbli")):
    """台账全局一份（跨年滚动）；3.2 矩阵由前端按归月年份分发到各年份页签"""
    year = 0
    with db() as c:
        require_writer(c, x_user)
    raw = await file.read()
    rows, report = parse_ledger(raw, file.filename or "upload")
    if not rows:
        raise HTTPException(422, "识别到表头但无有效数据行")
    errors, bad_rns = validate_ledger_rows(rows, partial=True)  # 260724 部分导入：坏行跳过，好行照入
    good_rows = [r for r in rows if r.get("_r", "?") not in bad_rns]
    if not good_rows:
        raise HTTPException(422, {"msg": f"台账数据不合格 {len(errors)} 处，且无一行可入库",
                                  "errors": errors[:50], "total_errors": len(errors)})
    with db() as c:
        require_writer(c, x_user)
        c.execute(
            "INSERT INTO ledger_snapshots(year,filename,sheet,rows_n,created_by,created_at) VALUES(?,?,?,?,?,?)",
            (year, file.filename, report["sheet"], len(good_rows), x_user, now()),
        )
        batch = c.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
        c.execute("DELETE FROM ledger_rows WHERE year=?", (year,))
        for r in good_rows:
            c.execute(
                "INSERT INTO ledger_rows(year,batch,dept,center,owner,src,job,lvl,fam,cls,loc,ask,num,tgt,st,eta,prev_eta,memo,offer,olvl,join_dt,jmemo,who) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (year, batch, r["dept"], r["center"], r["owner"], r["src"], r["job"], r["lvl"], r["fam"], r["cls"], r["loc"],
                 r["ask"], r["num"], r["tgt"], r["st"], r["eta"], r["prev_eta"], r["memo"], r["offer"], r["olvl"], r["join"], r["jmemo"], r["who"]),
            )
        skipped_rns = sorted(bad_rns, key=lambda x: (isinstance(x, str), x))
        _audit(c, x_user, "导入台账",
               f"全局台账 批次#{batch}「{file.filename}」sheet「{report['sheet']}」表头第{report['header_row']}行 · "
               f"列名命中{report['hits']}项 · 入库{len(good_rows)}行/校验跳过{len(bad_rns)}行"
               + (f"（跳过行号：{skipped_rns}）" if bad_rns else "") + f"/空行跳过{report['skipped']}行（整年替换）")
    with db() as c:
        return {"ok": True, "report": report, "rows": _ledger_list(c, year),
                "imported": len(good_rows), "skipped_bad": len(bad_rns),
                "skipped_rows": skipped_rns,
                "errors": errors[:50], "total_errors": len(errors)}


# ---------------- 台账模板下载（与线下模板 v2 同构：分组表头/下拉/冻结/说明行） ----------------
@app.get("/api/ledger/template.xlsx")
def ledger_template():
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.worksheet.datavalidation import DataValidation
    from fastapi.responses import Response
    wb = Workbook()
    ws = wb.active
    ws.title = "台账"
    groups = [("需求信息", 9), ("进展阶段", 3), ("入职阶段", 4)]  # 系统派生列不进模板（网页自动算）
    headers = ["部门", "中心", "业务负责人", "HC来源备注", "招聘岗位", "分类（国内/海外）", "地点",
               "需求提出时间", "招聘数量",
               "进展（当前状态）", "预计到岗时间", "备注（变更原因/招聘卡点）",
               "offer人选", "人选职级", "实际入职时间", "备注"]
    hints = ["", "", "总监/leader", "离职补录/新增投入/转岗等", "", "下拉", "", "", "必须为1(多名额另起一行)",
             "下拉;变更即留痕(网页版)", "改动须填变更原因", "到岗变化原因/招聘卡点",
             "", "", "入职后填,状态改已入职", "如:活水/base城市"]
    demo_rows = [
        ["示例部门", "中心A", "张三", "离职补录", "后台开发工程师", "国内", "深圳", "2026-05-10", 1,
         "简历&面试中", "2026-08-01", "候选人初筛中", "", "", "", ""],
        ["示例部门", "中心B", "李四", "新增投入", "产品经理(海外增长)", "海外", "新加坡", "2026-04-20", 1,
         "已offer待入职", "2026-08-15", "签证办理中,到岗顺延", "陈某", "P11", "", ""],
        ["示例部门", "中心A", "张三", "已报备HC", "SRE工程师", "国内", "深圳", "2026-03-01", 1,
         "已入职", "2026-06-01", "", "刘某", "T8", "2026-06-03", "活水调入"],
        ["示例部门", "中心C", "王五", "暂缓岗位", "算法工程师", "国内", "上海", "2026-02-15", 1,
         "Hold", "2026-09-01", "业务方向调整,暂缓", "", "", "", ""],
        ["示例部门", "中心B", "李四", "离职补录", "解决方案架构师", "海外", "北美", "2026-01-20", 1,
         "简历&面试中", "2026-06-30", "区域重新定位", "", "", "", ""],
    ]
    col = 1
    for gname, span in groups:
        ws.cell(1, col, gname)
        ws.merge_cells(start_row=1, start_column=col, end_row=1, end_column=col + span - 1)
        col += span
    ws.append(headers)
    ws.append(hints)
    for dr in demo_rows:  # 示例行（部门=「示例部门」，导入时自动跳过不入库）
        ws.append(dr)
    for cell in ws[1]:
        cell.font = Font(name="微软雅黑", bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="0A2E6E")
        cell.alignment = Alignment(horizontal="center")
    for cell in ws[2]:
        cell.font = Font(name="微软雅黑", bold=True)
        cell.fill = PatternFill("solid", fgColor="DBEAFE")
    for cell in ws[3]:
        cell.font = Font(name="微软雅黑", size=9, color="8194AE")
    dv_cls = DataValidation(type="list", formula1='"%s"' % ",".join(LEDGER_CLS), allow_blank=True,
                            errorTitle="无效值", error="须为：国内 或 海外", showErrorMessage=True)
    dv_st = DataValidation(type="list", formula1='"%s"' % ",".join(LEDGER_ST_CANON), allow_blank=True,
                           errorTitle="无效值", error="须从下拉选择", showErrorMessage=True)
    dv_num = DataValidation(type="whole", operator="equal", formula1="1",
                            errorTitle="招聘数量必须为1", error="每行一个名额，多名额请另起一行", showErrorMessage=True)
    ws.add_data_validation(dv_cls)
    ws.add_data_validation(dv_st)
    ws.add_data_validation(dv_num)
    dv_cls.add("F4:F500")   # 分类（国内/海外）
    dv_st.add("J4:J500")    # 进展（当前状态）
    dv_num.add("I4:I500")   # 招聘数量=1
    widths = [10, 10, 12, 16, 18, 13, 9, 13, 10, 15, 13, 20, 12, 9, 13, 14]
    for i, w in enumerate(widths):
        ws.column_dimensions[chr(65 + i)].width = w
    ws.freeze_panes = "F4"  # 冻结：部门~招聘岗位（A-E）+ 三行表头
    buf = io.BytesIO()
    wb.save(buf)
    return Response(buf.getvalue(),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": "attachment; filename=ledger-template.xlsx"})


# ---------------- 看板视图偏好（列宽/固定列/隐藏列/隐藏行）：按用户存后端，换设备也在 ----------------
UI_PREF_KEYS = {"hcfb_colw", "hcfb_kb3pin", "hcfb_kb3colhide", "hcfb_kb3hide"}


@app.get("/api/prefs")
def get_prefs(x_user: str = Header("bonniewbli")):
    with db() as c:
        out = {}
        for r in c.execute("SELECT k,v FROM ui_prefs WHERE user_id=?", (x_user,)):
            try:
                out[r["k"]] = json.loads(r["v"])
            except (ValueError, TypeError):
                pass
        return out


class PrefSet(BaseModel):
    value: object = None  # 任意 JSON（列宽对象/固定列数组/隐藏映射）


@app.put("/api/prefs/{key}")
def put_pref(key: str, p: PrefSet, x_user: str = Header("bonniewbli")):
    if key not in UI_PREF_KEYS:
        raise HTTPException(422, f"未知偏好键「{key}」（可存：{','.join(sorted(UI_PREF_KEYS))}）")
    with db() as c:
        require_writer(c, x_user)
        c.execute(
            "INSERT INTO ui_prefs(user_id,k,v,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id,k) DO UPDATE SET v=excluded.v,updated_at=excluded.updated_at",
            (x_user, key, json.dumps(p.value, ensure_ascii=False), now()),
        )
    return {"ok": True}


# ---------------- 静态前端（同源托管 index.html / admin.html） ----------------
@app.get("/")
def root():
    return FileResponse(os.path.join(FRONT_DIR, "index.html"))


@app.get("/{page}.html")
def page(page: str):
    fp = os.path.join(FRONT_DIR, f"{page}.html")
    if page in ("index", "admin") and os.path.exists(fp):
        return FileResponse(fp)
    raise HTTPException(404)
