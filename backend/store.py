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
              dept TEXT, center TEXT, owner TEXT, src TEXT, job TEXT, rmgr TEXT, lvl TEXT, cls TEXT, loc TEXT,
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
            CREATE TABLE IF NOT EXISTS ui_prefs(
              user_id TEXT, k TEXT, v TEXT, updated_at TEXT,
              PRIMARY KEY(user_id, k));
            -- 权限层级 P1：职级默认模板（可配·上级授权叠加在此之上）
            CREATE TABLE IF NOT EXISTS role_templates(
              level TEXT PRIMARY KEY,          -- 职级/模板键（对齐 iOA level）
              label TEXT,                      -- 显示名
              role TEXT,                       -- 映射现有角色：管理员/HRBP·可编辑/领导·只读
              kb TEXT DEFAULT '[1,1,1,1]',     -- 默认看板访问 [kb0..3]
              scope TEXT DEFAULT 'self',       -- 数据范围：self本人部门 / subtree本组织子树 / all全部
              can_manage INTEGER DEFAULT 0,    -- 是否可管下级权限
              edit_items TEXT DEFAULT '[]',    -- 默认可编辑项目 key 数组
              updated_at TEXT);
            -- 权限层级 P1：例外授权（默认不够用时，上级在其管辖子树内加权；叠加层）
            CREATE TABLE IF NOT EXISTS perm_grants(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              grantee_id TEXT,                 -- 被授权人 accounts.id
              resource TEXT,                   -- 对象：看板/项目/部门空间键
              action TEXT,                     -- read / write / manage
              scope TEXT,                      -- 范围（dept / org_path / 看板）
              granted_by TEXT,                 -- 授权人 accounts.id
              granted_at TEXT,
              expires_at TEXT,                 -- 有效期（空=永久）
              reason TEXT);                    -- 授权缘由（进审计）
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
        # ---- 260723 台账对齐线下模板 v2：补「分类(fam)」「上次预计到岗(prev_eta)」；rmgr=招聘经理（招聘岗位后一列）----
        for col in ("fam", "prev_eta", "rmgr"):
            try:
                c.execute(f"ALTER TABLE ledger_rows ADD COLUMN {col} TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass  # 列已存在
        # ---- 260801 项目「可手改」权限：edit='' (无=按前端默认) / 'no' / 'all' / 'future'（后台可配，看板1 据此出🖊+放开录入范围）----
        try:
            c.execute("ALTER TABLE projects ADD COLUMN edit TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass  # 列已存在
        # ---- 2608 权限层级 P1：accounts 接组织树（level 职级 / manager_id 上级 / org_path 物化路径）----
        # org_path 用「/」分隔，如 云产品五部/MPaaS/直播产品中心 → 判「上级管下级」= 前缀匹配子树，无需递归爬上级
        for col in ("level", "manager_id", "org_path"):
            try:
                c.execute(f"ALTER TABLE accounts ADD COLUMN {col} TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass  # 列已存在
        # 回填内置管理员（幂等）：李文博=部门顶层
        c.execute("UPDATE accounts SET org_path='云产品五部', level='管理员' WHERE id='bonniewbli' AND (org_path IS NULL OR org_path='')")
        # 职级→默认模板 seed（首次建库时；后续在后台可改，这里只是起步默认，不是硬编码策略）
        if not c.execute("SELECT 1 FROM role_templates LIMIT 1").fetchone():
            c.executemany(
                "INSERT INTO role_templates(level,label,role,kb,scope,can_manage,edit_items,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                [
                    ("管理员", "管理员", "管理员", "[1,1,1,1]", "all", 1, "[]", now()),
                    ("部长", "部长（部门顶层）", "领导·只读", "[1,1,1,1]", "subtree", 1, "[]", now()),
                    ("AGM", "AGM/产品线负责人", "领导·只读", "[1,1,1,1]", "subtree", 1, "[]", now()),
                    ("总监", "中心负责人（总监）", "领导·只读", "[1,1,1,0]", "subtree", 1, "[]", now()),
                    ("经理", "组长/经理（PM）", "HRBP·可编辑", "[1,1,1,1]", "self", 1, "[]", now()),
                    ("专员", "组员（BP）", "HRBP·可编辑", "[1,1,1,1]", "self", 0, "[]", now()),
                ],
            )
            _audit(c, "system", "权限初始化", "role_templates 起步默认 6 档（管理员/部长/AGM/总监/经理/专员）；职级映射与范围待按 iOA 真实职级校准")
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
        c.execute("UPDATE projects SET name='月末实际在岗/期末在岗预估', src='已发生月=KPI系统 zhaopin（待接）；未发生月=运算链' WHERE key='actual' AND name='月末实际在岗'")
        c.execute("DELETE FROM projects WHERE key='chain'")
        # 260726 口径澄清：实际发生月月末在岗=招聘系统/员工信息宽表 直接po（非运算）
        c.execute("UPDATE projects SET src='已发生月=招聘系统zhaopin/员工信息宽表diy 月末快照直接po（待接·非运算）；未发生月=运算链' WHERE key='actual'")
        # 260726 行名统一为「实际/预估期末在岗」（幂等·强制刷，兼容历史库存的旧名月末实际在岗/期末在岗预估）
        c.execute("UPDATE projects SET name='实际/预估期末在岗' WHERE key='actual'")
        # 260726 去掉行名「已流入/」「已流出/」前缀，只留待流入/待流出（幂等：替换后 LIKE 不再命中）
        c.execute("UPDATE projects SET name=REPLACE(name,'已流入/待流入','待流入') WHERE name LIKE '已流入/待流入%'")
        c.execute("UPDATE projects SET name=REPLACE(name,'已流出/待流出','待流出') WHERE name LIKE '已流出/待流出%'")
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


def can_manage(c, granter_id, grantee_id):
    """上级能否管下级的权限：管理员管全员；否则 grantee.org_path 须为 granter.org_path 的严格子树（前缀匹配），不能管平级/自己。"""
    if granter_id == grantee_id:
        return False
    g = get_account(c, granter_id)
    t = get_account(c, grantee_id)
    if not g or not t:
        return False
    if (g.get("role") or "") == "管理员":
        return True
    gp = (g.get("org_path") or "").strip("/")
    tp = (t.get("org_path") or "").strip("/")
    return bool(gp) and tp.startswith(gp + "/")  # 严格子树=下级；同节点(平级)不算


def manageable_ids(c, granter_id):
    """granter 可管辖的全部账号 id（子树成员）——供后台『上级只看到自己下级』的列表过滤。"""
    return [r["id"] for r in c.execute("SELECT id FROM accounts").fetchall()
            if can_manage(c, granter_id, r["id"])]


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
