# -*- coding: utf-8 -*-
"""存储层：SQLite 连接/建库/审计/统一写格/权限闸/识空取数（企业版数据库批后仅替换本文件）"""
import os
import sqlite3
import time
from contextlib import contextmanager

from fastapi import HTTPException

from meta import CANON_PROJECTS, NAT_N_DEFAULT

DB_PATH = os.environ.get("HCFB_DB") or os.path.join(os.path.dirname(__file__), "hcfb.db")


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
            CREATE TABLE IF NOT EXISTS ledger_rows(
              id INTEGER PRIMARY KEY AUTOINCREMENT, year INTEGER, batch INTEGER,
              dept TEXT, center TEXT, owner TEXT, src TEXT, job TEXT, lvl TEXT, cls TEXT, loc TEXT,
              ask TEXT, num TEXT, tgt TEXT, st TEXT, eta TEXT, memo TEXT,
              offer TEXT, olvl TEXT, join_dt TEXT, jmemo TEXT, who TEXT);
            CREATE TABLE IF NOT EXISTS cells_history(
              id INTEGER PRIMARY KEY AUTOINCREMENT, year INTEGER, metric TEXT, month INTEGER,
              old_value REAL, new_value REAL, source TEXT, changed_by TEXT, changed_at TEXT, note TEXT);
            CREATE TABLE IF NOT EXISTS pending_diffs(
              id INTEGER PRIMARY KEY AUTOINCREMENT, year INTEGER, metric TEXT, month INTEGER,
              cur_value REAL, src_value REAL, source TEXT, status TEXT DEFAULT 'open',
              created_at TEXT, resolved_by TEXT, resolved_at TEXT);
            CREATE TABLE IF NOT EXISTS ledger_snapshots(
              id INTEGER PRIMARY KEY AUTOINCREMENT, year INTEGER, filename TEXT, sheet TEXT,
              rows_n INTEGER, created_by TEXT, created_at TEXT);
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
        try:
            c.execute(f"ALTER TABLE years ADD COLUMN nat_n INTEGER NOT NULL DEFAULT {NAT_N_DEFAULT}")
        except sqlite3.OperationalError:
            pass  # 列已存在
        # ---- 260723 台账对齐线下模板 v2：补「分类(fam)」「上次预计到岗(prev_eta)」两列 ----
        for col in ("fam", "prev_eta"):
            try:
                c.execute(f"ALTER TABLE ledger_rows ADD COLUMN {col} TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass  # 列已存在
        # ---- 260724 台账行序（Excel式任意位置插行）：sort_pos 排序列，缺省=id ----
        try:
            c.execute("ALTER TABLE ledger_rows ADD COLUMN sort_pos REAL")
        except sqlite3.OperationalError:
            pass
        c.execute("UPDATE ledger_rows SET sort_pos=id WHERE sort_pos IS NULL")
        # ---- 260723 台账日期列自动归一（历史脏数据一次性清洗，幂等：归一函数对已归一值不变）----
        from kb3_ledger import _norm_date as _nd  # 函数级导入避免模块环
        for r in c.execute("SELECT id,ask,tgt,eta,prev_eta,join_dt FROM ledger_rows").fetchall():
            upd = {}
            for colname in ("ask", "tgt", "eta", "prev_eta", "join_dt"):
                ov = r[colname] or ""
                nv = _nd(ov) if ov else ""
                if nv != ov:
                    upd[colname] = nv
            if upd:
                c.execute("UPDATE ledger_rows SET " + ",".join(f"{k}=?" for k in upd) + " WHERE id=?",
                          (*upd.values(), r["id"]))
        # ---- 260723 口径迁移（幂等）----
        # ① 链行并入实际行：项目表改名 actual、删除 chain 行（computed.chain 仍在响应里）
        c.execute("UPDATE projects SET name='实际/预估期末在岗', src='已发生月=KPI系统 zhaopin（待接）；未发生月=运算链' WHERE key='actual' AND name IN ('月末实际在岗','月末实际在岗/期末在岗预估')")
        c.execute("DELETE FROM projects WHERE key='chain'")
        # ② 分支不再分加减向：历史 '−' 分支等价转换为 '+'（数值取反，语义分毫不变）
        for b in c.execute("SELECT id FROM branches WHERE sign='-'").fetchall():
            c.execute("UPDATE branch_cells SET value=-value WHERE branch_id=? AND value IS NOT NULL", (b["id"],))
            c.execute("UPDATE branches SET sign='+' WHERE id=?", (b["id"],))


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _audit(c, user, action, detail):
    c.execute("INSERT INTO audit(ts,user,action,detail) VALUES(?,?,?,?)", (now(), user, action, detail))


def _write_cell(c, year, metric, month, value, note, source, user):
    """统一写格：任何变更留 cells_history（口径可复现——上周汇报的数字这周还能查到）"""
    old = c.execute("SELECT value FROM cells WHERE year=? AND metric=? AND month=?", (year, metric, month)).fetchone()
    oldv = old["value"] if old else None
    c.execute(
        "INSERT INTO cells(year,metric,month,value,note,source,updated_by,updated_at) VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(year,metric,month) DO UPDATE SET value=excluded.value,note=excluded.note,"
        "source=excluded.source,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
        (year, metric, month, value, note, source, user, now()),
    )
    if oldv != value:
        c.execute(
            "INSERT INTO cells_history(year,metric,month,old_value,new_value,source,changed_by,changed_at,note) VALUES(?,?,?,?,?,?,?,?,?)",
            (year, metric, month, oldv, value, source, user, now(), note),
        )


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


# ---------------- 识空取数（服务端·与前端同口径） ----------------
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
