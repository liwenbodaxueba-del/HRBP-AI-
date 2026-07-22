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
import sqlite3
import time
from contextlib import contextmanager
from typing import Optional

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

DB_PATH = os.path.join(os.path.dirname(__file__), "hcfb.db")
FRONT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 仓库根（index.html/admin.html）

app = FastAPI(title="HC Forecast Board API", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ---------------- 指标口径（结构=配置，非数据） ----------------
CANON_PROJECTS = [
    # key, sec, name, src, src_cls, add_ok, unbind, sys
    ("budget", "预算阶段", "预算当量", "看板2 间接取数", "calc", 0, 0, 1),
    ("actual", "预算阶段", "月末实际在岗", "KPI系统 zhaopin（待接）", "src", 0, 0, 1),
    ("o_sys", "总流出（−）", "已流出/待流出 · 系统明确", "HR数仓（待接）", "src", 0, 1, 1),
    ("o_nat", "总流出（−）", "已流出/待流出 · 自然流失预估", "运算派生", "calc", 0, 1, 1),
    ("o_bp", "总流出（−）", "已流出/待流出 · 已明确非系统（BP）", "BP 手填", "bp", 1, 1, 1),
    ("o_act", "总流出（−）", "已流出/待流出 · 主动动作（BP）（调节项）", "BP 手填", "bp", 1, 1, 1),
    ("i_soc", "总流入（＋）", "已流入/待流入 · 社招", "招聘系统（待接）", "src", 0, 1, 1),
    ("camp", "总流入（＋）", "已流入/待流入 · 校招", "分列求和", "calc", 1, 1, 1),
    ("i_yy", "校招∇ 分列", "校招· 预约入职", "HR数仓（待接）", "src", 0, 1, 1),
    ("i_bs", "校招∇ 分列", "校招· 毕业生转聘", "HR数仓（待接）", "src", 0, 1, 1),
    ("i_cbp", "校招∇ 分列", "校招· 非系统（BP）", "BP 手填", "bp", 1, 1, 1),
    ("i_incr", "总流入（＋）", "已流入/待流入 · 增量需求（BP）（调节项）", "BP 手填", "bp", 1, 1, 1),
    ("chain", "结论", "期末在岗预估", "运算派生（链）", "calc", 0, 0, 1),
]
OUT_KEYS = ["o_sys", "o_nat", "o_bp", "o_act"]
CAMP_KEYS = ["i_yy", "i_bs", "i_cbp"]
IN_DIRECT_KEYS = ["i_soc", "i_incr"]  # + campTot + 流入分支
BP_EDITABLE = {"o_bp", "o_act", "i_cbp", "i_incr"}  # 叶子级 BP 录入位（可被 config.add 关闭）
IMPORTABLE = {"budget", "actual", "o_sys", "o_nat", "i_soc", "i_yy", "i_bs"}  # 上传兜底可写的系统数指标
VALUE_ABS_MAX = 100000  # 量级异常闸


# ---------------- DB ----------------
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS years(
              year INTEGER PRIMARY KEY, status TEXT NOT NULL, lock_month INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS projects(
              key TEXT PRIMARY KEY, sec TEXT, name TEXT, src TEXT, src_cls TEXT,
              add_ok INTEGER, unbind INTEGER, on_ok INTEGER DEFAULT 1, sys INTEGER, pos INTEGER);
            CREATE TABLE IF NOT EXISTS accounts(
              id TEXT PRIMARY KEY, name TEXT, role TEXT, dept TEXT, kb TEXT, on_ok INTEGER DEFAULT 1, demo INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS cells(
              year INTEGER, metric TEXT, month INTEGER, value REAL, note TEXT,
              source TEXT, updated_by TEXT, updated_at TEXT,
              PRIMARY KEY(year, metric, month));
            CREATE TABLE IF NOT EXISTS branches(
              id INTEGER PRIMARY KEY AUTOINCREMENT, year INTEGER, sec TEXT, name TEXT, sign TEXT,
              on_ok INTEGER DEFAULT 1, created_by TEXT, created_at TEXT);
            CREATE TABLE IF NOT EXISTS branch_cells(
              branch_id INTEGER REFERENCES branches(id) ON DELETE CASCADE,
              month INTEGER, value REAL, note TEXT, updated_by TEXT, updated_at TEXT,
              PRIMARY KEY(branch_id, month));
            CREATE TABLE IF NOT EXISTS snapshots(
              id INTEGER PRIMARY KEY AUTOINCREMENT, year INTEGER, filename TEXT, rows_n INTEGER,
              created_by TEXT, created_at TEXT);
            CREATE TABLE IF NOT EXISTS audit(
              id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, user TEXT, action TEXT, detail TEXT);
            """
        )
        if not c.execute("SELECT 1 FROM years LIMIT 1").fetchone():
            c.executemany(
                "INSERT INTO years(year,status,lock_month) VALUES(?,?,?)",
                [(2025, "待接入·历史归档", 12), (2026, "待接入·执行中", 6), (2027, "待接入·待编制", 0)],
            )
        if not c.execute("SELECT 1 FROM projects LIMIT 1").fetchone():
            c.executemany(
                "INSERT INTO projects(key,sec,name,src,src_cls,add_ok,unbind,on_ok,sys,pos) VALUES(?,?,?,?,?,?,?,1,?,?)",
                [(k, s, n, sr, sc, a, u, sy, i) for i, (k, s, n, sr, sc, a, u, sy) in enumerate(CANON_PROJECTS)],
            )
        if not c.execute("SELECT 1 FROM accounts LIMIT 1").fetchone():
            c.execute(
                "INSERT INTO accounts(id,name,role,dept,kb,on_ok,demo) VALUES(?,?,?,?,?,1,0)",
                ("bonniewbli", "李文博", "管理员", "云产品五部", "[1,1,1,1]"),
            )
        _audit(c, "system", "初始化", "建库：年份 2025-2027、项目口径 13 项、账号 bonniewbli；数据表为空（待导入/待录入，不编造）")


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _audit(c, user, action, detail):
    c.execute("INSERT INTO audit(ts,user,action,detail) VALUES(?,?,?,?)", (now(), user, action, detail))


def get_account(c, user_id):
    r = c.execute("SELECT * FROM accounts WHERE id=?", (user_id,)).fetchone()
    return dict(r) if r else None


def require_writer(c, user_id):
    a = get_account(c, user_id)
    if not a:
        raise HTTPException(403, f"账号 {user_id} 未配置（请在管理后台添加）")
    if not a["on_ok"]:
        raise HTTPException(403, f"账号 {user_id} 已停用")
    if a["role"] == "领导·只读":
        raise HTTPException(403, "当前账号为只读权限（领导·只读）")
    return a


def require_admin(c, user_id):
    a = require_writer(c, user_id)
    if a["role"] != "管理员":
        raise HTTPException(403, "仅管理员可执行此操作")
    return a


init_db()


# ---------------- 识空引擎（服务端·与前端同口径） ----------------
def _grid(c, year):
    """cells → {metric: [v or None]*12}, notes → {metric: {m: note}}"""
    vals, notes = {}, {}
    for r in c.execute("SELECT metric,month,value,note FROM cells WHERE year=?", (year,)):
        vals.setdefault(r["metric"], [None] * 12)
        if 1 <= r["month"] <= 12:
            vals[r["metric"]][r["month"] - 1] = r["value"]
            if r["note"]:
                notes.setdefault(r["metric"], {})[r["month"]] = r["note"]
    return vals, notes


def _branches(c, year):
    out = []
    for b in c.execute("SELECT * FROM branches WHERE year=? AND on_ok=1 ORDER BY id", (year,)):
        vals = [None] * 12
        bnotes = {}
        for r in c.execute("SELECT month,value,note FROM branch_cells WHERE branch_id=?", (b["id"],)):
            if 1 <= r["month"] <= 12:
                vals[r["month"] - 1] = r["value"]
                if r["note"]:
                    bnotes[r["month"]] = r["note"]
        out.append({"id": b["id"], "sec": b["sec"], "name": b["name"], "sign": b["sign"], "vals": vals, "notes": bnotes})
    return out


def _agg(parts):
    nums = [p for p in parts if isinstance(p, (int, float))]
    return sum(nums) if nums else None


def compute(vals, branches, lock):
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
                chain.append(prev - o + i)
    def avg(a):
        n = [x for x in a if isinstance(x, (int, float))]
        return round(sum(n) / len(n), 2) if n else None
    return {"campT": campT, "outT": outT, "inT": inT, "chain": chain,
            "chain_avg": avg(chain), "budget_avg": avg(vals.get("budget", [None] * 12))}


# ---------------- 通用 ----------------
@app.get("/api/health")
def health():
    return {"ok": True, "ts": now(), "storage": "sqlite-dev（企业版数据库批后替换）", "version": app.version}


# ---------------- 配置（与前端 localStorage cfg 同构） ----------------
@app.get("/api/config")
def get_config():
    with db() as c:
        projs = [
            {"key": r["key"], "sec": r["sec"], "name": r["name"], "src": r["src"], "srcCls": r["src_cls"],
             "add": bool(r["add_ok"]), "unbind": bool(r["unbind"]), "on": bool(r["on_ok"]), "sys": bool(r["sys"])}
            for r in c.execute("SELECT * FROM projects ORDER BY pos")
        ]
        accts = [
            {"id": r["id"], "name": r["name"], "role": r["role"], "dept": r["dept"],
             "kb": json.loads(r["kb"] or "[1,1,1,1]"), "on": bool(r["on_ok"]), "demo": bool(r["demo"])}
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
                "INSERT INTO projects(key,sec,name,src,src_cls,add_ok,unbind,on_ok,sys,pos) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (p.get("key") or f"x{int(time.time()*1000)}_{i}", p.get("sec", ""), p.get("name", ""),
                 p.get("src", ""), p.get("srcCls", "bp"), int(bool(p.get("add"))), int(bool(p.get("unbind"))),
                 int(p.get("on", True)), int(bool(p.get("sys"))), i),
            )
        c.execute("DELETE FROM accounts")
        for a in doc.accts:
            c.execute(
                "INSERT INTO accounts(id,name,role,dept,kb,on_ok,demo) VALUES(?,?,?,?,?,?,?)",
                (a["id"], a.get("name", ""), a.get("role", "HRBP·可编辑"), a.get("dept", ""),
                 json.dumps(a.get("kb", [1, 1, 1, 1])), int(a.get("on", True)), int(bool(a.get("demo")))),
            )
        _audit(c, x_user, "配置更新", f"项目 {len(doc.projs)} 项 / 账号 {len(doc.accts)} 个（管理后台下发）")
        return {"ok": True}


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
@app.get("/api/board/{year}")
def get_board(year: int):
    with db() as c:
        yr = c.execute("SELECT * FROM years WHERE year=?", (year,)).fetchone()
        if not yr:
            raise HTTPException(404, "年份不存在")
        vals, notes = _grid(c, year)
        brs = _branches(c, year)
        comp = compute(vals, brs, yr["lock_month"])
        metrics = {k: {"vals": vals.get(k, [None] * 12), "notes": notes.get(k, {})} for k, *_ in CANON_PROJECTS}
        return {"year": year, "status": yr["status"], "lock": yr["lock_month"],
                "metrics": metrics, "branches": brs, "computed": comp, "ts": int(time.time() * 1000)}


class CellEdit(BaseModel):
    metric: str
    month: int  # 1-12
    value: Optional[float] = None  # None=清空
    note: str


@app.post("/api/board/{year}/cell")
def edit_cell(year: int, e: CellEdit, x_user: str = Header("bonniewbli")):
    with db() as c:
        require_writer(c, x_user)
        yr = c.execute("SELECT * FROM years WHERE year=?", (year,)).fetchone()
        if not yr:
            raise HTTPException(404, "年份不存在")
        if not (1 <= e.month <= 12):
            raise HTTPException(422, "月份须为 1-12")
        if e.month <= yr["lock_month"]:
            raise HTTPException(423, f"{e.month}月为已发生月（已锁定），改动须走「采纳修正」流程")
        if not e.note.strip():
            raise HTTPException(422, "备注必填（写入审计日志）")
        if e.value is not None and abs(e.value) > VALUE_ABS_MAX:
            raise HTTPException(422, "量级异常，拒绝入库")
        if e.metric.startswith("branch:"):
            bid = int(e.metric.split(":", 1)[1])
            b = c.execute("SELECT * FROM branches WHERE id=? AND year=?", (bid, year)).fetchone()
            if not b:
                raise HTTPException(404, "分支不存在")
            c.execute(
                "INSERT INTO branch_cells(branch_id,month,value,note,updated_by,updated_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(branch_id,month) DO UPDATE SET value=excluded.value,note=excluded.note,"
                "updated_by=excluded.updated_by,updated_at=excluded.updated_at",
                (bid, e.month, e.value, e.note.strip(), x_user, now()),
            )
            _audit(c, x_user, "调节录入", f"{year} 分支「{b['name']}」 {e.month}月 → {e.value}（{e.note.strip()}）")
        else:
            p = c.execute("SELECT * FROM projects WHERE key=?", (e.metric,)).fetchone()
            if not p:
                raise HTTPException(404, "指标不存在")
            if e.metric not in BP_EDITABLE:
                raise HTTPException(403, f"「{p['name']}」为系统数指标，不可手工录入（走数据源/上传兜底）")
            if not p["add_ok"]:
                raise HTTPException(403, f"「{p['name']}」已在管理后台关闭手动录入")
            c.execute(
                "INSERT INTO cells(year,metric,month,value,note,source,updated_by,updated_at) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(year,metric,month) DO UPDATE SET value=excluded.value,note=excluded.note,"
                "source=excluded.source,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
                (year, e.metric, e.month, e.value, e.note.strip(), "bp", x_user, now()),
            )
            _audit(c, x_user, "调节录入", f"{year}「{p['name']}」 {e.month}月 → {e.value}（{e.note.strip()}）")
    return get_board(year)


class BranchNew(BaseModel):
    sec: str
    name: str
    sign: str  # '+' / '-'


@app.post("/api/board/{year}/branch")
def add_branch(year: int, b: BranchNew, x_user: str = Header("bonniewbli")):
    if b.sign not in ("+", "-"):
        raise HTTPException(422, "方向须为 + 或 −")
    if not b.name.strip():
        raise HTTPException(422, "分支名称必填")
    with db() as c:
        require_writer(c, x_user)
        c.execute(
            "INSERT INTO branches(year,sec,name,sign,on_ok,created_by,created_at) VALUES(?,?,?,?,1,?,?)",
            (year, b.sec, b.name.strip(), b.sign, x_user, now()),
        )
        _audit(c, x_user, "新增分支", f"{year} {b.sec} · {b.name.strip()}（{b.sign}）")
    return get_board(year)


@app.delete("/api/board/{year}/branch/{bid}")
def del_branch(year: int, bid: int, x_user: str = Header("bonniewbli")):
    with db() as c:
        require_writer(c, x_user)
        b = c.execute("SELECT * FROM branches WHERE id=? AND year=?", (bid, year)).fetchone()
        if not b:
            raise HTTPException(404, "分支不存在")
        c.execute("DELETE FROM branches WHERE id=?", (bid,))
        _audit(c, x_user, "删除分支", f"{year} {b['sec']} · {b['name']}")
    return get_board(year)


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
        c.execute(
            "INSERT INTO snapshots(year,filename,rows_n,created_by,created_at) VALUES(?,?,?,?,?)",
            (year, file.filename, len(rows), x_user, now()),
        )
        snap_id = c.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
        for metric, month, value in rows:
            c.execute(
                "INSERT INTO cells(year,metric,month,value,note,source,updated_by,updated_at) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(year,metric,month) DO UPDATE SET value=excluded.value,"
                "source=excluded.source,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
                (year, metric, month, value, None, f"import#{snap_id}", x_user, now()),
            )
        _audit(c, x_user, "导入快照", f"{year} 批次#{snap_id}「{file.filename}」{len(rows)} 格，校验闸通过入库")
    return {"ok": True, "snapshot": snap_id, "rows": len(rows)}


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
    for k in ["budget", "actual"]:
        w.writerow([names[k]] + [fmt(v) for v in board["metrics"][k]["vals"]] + [""])
    w.writerow(["总流出（−）"] + [fmt(v) for v in board["computed"]["outT"]] + [""])
    w.writerow(["总流入（＋）"] + [fmt(v) for v in board["computed"]["inT"]] + [""])
    w.writerow([names["chain"]] + [fmt(v) for v in board["computed"]["chain"]] + [board["computed"]["chain_avg"] or ""])
    with db() as c:
        _audit(c, "system", "导出", f"{year} 看板1 导出 CSV")
    return PlainTextResponse("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8")


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
