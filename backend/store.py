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
            -- 看板0 调节层：PM 速览专用调整值（独立于看板1 源 cells·不影响源；中心调节汇总到部）
            CREATE TABLE IF NOT EXISTS kb0_adjust(
              year INTEGER, dept TEXT, metric TEXT, month INTEGER, value REAL, note TEXT,
              updated_by TEXT, updated_at TEXT,
              PRIMARY KEY(year, dept, metric, month));
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
        # ---- 2608 看板1 部门维度：cells/branches 加 dept（部门空间·各部门独立存一套；现有数据归"集团"）----
        # cells 复合主键要含 dept，SQLite 改主键须重建表（幂等：无 dept 列时才建）
        _cells_cols = [r[1] for r in c.execute("PRAGMA table_info(cells)")]
        if "dept" not in _cells_cols:
            c.executescript(
                """
                ALTER TABLE cells RENAME TO _cells_old;
                CREATE TABLE cells(
                  year INTEGER, dept TEXT NOT NULL DEFAULT '集团', metric TEXT, month INTEGER,
                  value REAL, note TEXT, source TEXT, updated_by TEXT, updated_at TEXT,
                  PRIMARY KEY(year, dept, metric, month));
                INSERT INTO cells(year,dept,metric,month,value,note,source,updated_by,updated_at)
                  SELECT year,'集团',metric,month,value,note,source,updated_by,updated_at FROM _cells_old;
                DROP TABLE _cells_old;
                """
            )
        for _t in ("branches", "cells_history"):  # id 是主键，加列即可
            try:
                c.execute(f"ALTER TABLE {_t} ADD COLUMN dept TEXT NOT NULL DEFAULT '集团'")
            except sqlite3.OperationalError:
                pass  # 列已存在
        # ---- 2608 权限层级 P1：accounts 接组织树（level 职级 / manager_id 上级 / org_path 物化路径）----
        # org_path 用「/」分隔，如 云产品五部/MPaaS/直播产品中心 → 判「上级管下级」= 前缀匹配子树，无需递归爬上级
        for col in ("level", "manager_id", "org_path"):
            try:
                c.execute(f"ALTER TABLE accounts ADD COLUMN {col} TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass  # 列已存在
        # 账号层级 = iOA HR 组织树（非业务线云产品五部；业务线是"数据范围"另说）。org_path 用 iOA 真实路径，「/」分隔。
        _HR_CENTER = "Tencent Group/Tencent/S3 – HR & Management Line/CSIG Human Resources Center"
        _HRBP_TEAM = _HR_CENTER + "/CSIG HRBP Team"
        # 回填内置管理员（幂等·并修正早期误填的业务线路径）：李文博 = CSIG HRBP Team
        c.execute("UPDATE accounts SET org_path=?, level='管理员' WHERE id='bonniewbli' AND (org_path IS NULL OR org_path='' OR org_path='云产品五部')",
                  (_HRBP_TEAM,))
        # demo 层级账号（幂等·标示例）：演示"上级看下级"——负责人在 HR Center，BP 在 HRBP Team 子树
        for did, dname, drole, dlevel, dpath, dmgr in [
            ("demo-hrhead", "（示例）HRBP 负责人", "领导·只读", "总监", _HR_CENTER, ""),
            ("demo-bp1", "（示例）HRBP·张三", "HRBP·可编辑", "专员", _HRBP_TEAM, "demo-hrhead"),
            ("demo-bp2", "（示例）HRBP·李四", "HRBP·可编辑", "专员", _HRBP_TEAM, "demo-hrhead"),
        ]:
            c.execute("INSERT OR IGNORE INTO accounts(id,name,role,dept,kb,on_ok,demo,level,manager_id,org_path) "
                      "VALUES(?,?,?,?,?,1,1,?,?,?)",
                      (did, dname, drole, dpath.split("/")[-1], "[1,1,1,1]", dlevel, dmgr, dpath))
        # ---- 2608 看板1/看板0 部门权限：两套独立可配部门（看板1可见范围 / PM速览可见范围·后台各配）----
        for _col in ("kb1_depts", "kb0_depts"):
            try:
                c.execute(f'ALTER TABLE accounts ADD COLUMN {_col} TEXT DEFAULT \'["集团"]\'')
            except sqlite3.OperationalError:
                pass  # 列已存在
        _ALLD = '["集团","云产品一部","云产品二部","云产品三部","云产品四部","云产品五部"]'
        # 内置管理员两处都看全；demo：hrhead 看板1看全、看板0只看部分（演示"看板1能看·看板0不需要"）
        c.execute("UPDATE accounts SET kb1_depts=?, kb0_depts=? WHERE id='bonniewbli' AND (kb0_depts IS NULL OR kb0_depts='' OR kb0_depts='[\"集团\"]')",
                  (_ALLD, _ALLD))
        c.execute("UPDATE accounts SET kb1_depts=?, kb0_depts=? WHERE id='demo-bp1'", ('["云产品一部"]', '["云产品一部"]'))
        c.execute("UPDATE accounts SET kb1_depts=?, kb0_depts=? WHERE id='demo-bp2'", ('["云产品一部"]', '["云产品一部"]'))
        c.execute("UPDATE accounts SET kb1_depts=?, kb0_depts=? WHERE id='demo-hrhead'", ('["云产品二部"]', '["云产品二部"]'))
        # demo 账号 dept 固定=所属看板1部门（幂等·修正早期把 org_path 末段误写进 dept，如「CSIG HRBP Team」）
        for _aid, _adept in [("demo-bp1", "云产品一部"), ("demo-bp2", "云产品一部"), ("demo-hrhead", "云产品二部")]:
            c.execute("UPDATE accounts SET dept=? WHERE id=? AND demo=1", (_adept, _aid))
        # ---- 2608 账号按看板1部门归属 + 总BP：dept=所属看板1部门；is_head=部门总BP(管本部门其他账号/加人)----
        for _pcol in ("is_head", "is_sysadmin"):  # is_head=部门总BP；is_sysadmin=系统管理员(可配自己·可转移)
            try:
                c.execute(f"ALTER TABLE accounts ADD COLUMN {_pcol} INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass  # 列已存在
        c.execute("UPDATE accounts SET is_sysadmin=1 WHERE id='bonniewbli'")  # 内置系统管理员（幂等）
        try:
            c.execute("ALTER TABLE accounts ADD COLUMN kbperm TEXT DEFAULT ''")  # 4个看板(看板1/2/3/4)编辑查阅权限 [0无/1查阅/2编辑]*4；空=按角色默认
        except sqlite3.OperationalError:
            pass
        try:
            c.execute("ALTER TABLE kb0_adjust ADD COLUMN note TEXT")  # 看板0 调节项备注（现有库补列）
        except sqlite3.OperationalError:
            pass
        if not c.execute("SELECT 1 FROM accounts WHERE is_head=1 LIMIT 1").fetchone():  # 首次
            for aid, adept, ahead in [("bonniewbli", "集团", 1), ("demo-bp1", "云产品一部", 1),
                                      ("demo-bp2", "云产品一部", 0), ("demo-hrhead", "云产品二部", 1)]:
                c.execute("UPDATE accounts SET dept=?, is_head=? WHERE id=?", (adept, ahead, aid))
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


def _write_cell(c, year, metric, month, value, note, source, user, dept="集团"):
    """统一写格：任何变更留 cells_history（口径可复现）。dept=部门空间，各部门独立存储。"""
    old = c.execute("SELECT value FROM cells WHERE year=? AND dept=? AND metric=? AND month=?", (year, dept, metric, month)).fetchone()
    oldv = old["value"] if old else None
    c.execute(
        "INSERT INTO cells(year,dept,metric,month,value,note,source,updated_by,updated_at) VALUES(?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(year,dept,metric,month) DO UPDATE SET value=excluded.value,note=excluded.note,"
        "source=excluded.source,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
        (year, dept, metric, month, value, note, source, user, now()),
    )
    if oldv != value:
        c.execute(
            "INSERT INTO cells_history(year,dept,metric,month,old_value,new_value,source,changed_by,changed_at,note) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (year, dept, metric, month, oldv, value, source, user, now(), note),
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
    """能否管理该账号：全局管理员管全员；部门总BP(is_head)管本部门(dept 相同)的其他账号。不能管自己。"""
    if granter_id == grantee_id:
        return False
    g = get_account(c, granter_id)
    t = get_account(c, grantee_id)
    if not g or not t:
        return False
    if (g.get("role") or "") == "管理员":
        return True  # 全局管理员
    if g.get("is_head") and (g.get("dept") or "") and (g.get("dept") == t.get("dept")):
        return True  # 部门总BP 管同部门
    return False


def manageable_ids(c, granter_id):
    """granter 可管辖的全部账号 id（子树成员）——供后台『上级只看到自己下级』的列表过滤。"""
    return [r["id"] for r in c.execute("SELECT id FROM accounts").fetchall()
            if can_manage(c, granter_id, r["id"])]


init_db()


# ---------------- 识空取数（服务端·与前端同口径） ----------------
# 部门→中心（与前端 index.html/admin.html DEPT_TREE 一致）。部级看板数据 = 其各中心加总（部为只读汇总，数据在中心录入）
_DEPT_CHILDREN = {
    "云产品一部": ["计算产品中心", "轻量云产品中心", "异构计算产品中心", "存储产品中心", "网络产品中心", "高性能网络产品中心", "CBS产品中心", "CLS产品中心", "虚拟化产品中心", "TCE产品中心", "TCS产品中心", "云开发产品中心", "中间件产品中心", "云原生产品中心", "国产数据库产品中心", "云原生数据库产品中心", "NoSQL数据库产品中心", "数据库SaaS产品与技术平台中心", "数据库架构与支持中心", "数据库平台研发中心", "区块链产品中心", "IaaS前沿技术组", "产品架构组", "产品管理支持组", "可用性架构组", "MaaS产品中心", "Agent Runtime产品中心", "TIONE产品中心", "计算加速中心"],
    "云产品二部": ["大数据产品架构与支持中心", "大数据基础产品中心", "TBDS产品中心", "WeData产品中心", "大数据应用产品中心", "产品运营及管理支持组", "数字孪生产品中心"],
    "云产品三部": ["应用产品一中心", "应用产品二中心", "智能体平台产品中心", "交付中心", "经营分析组", "海外运营组"],
    "云产品四部": ["运营产品中心", "客户经营平台产品中心", "计费产品中心", "平台产品中心", "产品支持中心", "腾讯云设计一中心", "腾讯云设计二中心", "服务与产品优化组", "综合业务项目管理组", "平台架构组", "身份产品中心"],
}
DEPT_CENTERS = {p: [p + "/" + ch for ch in kids] for p, kids in _DEPT_CHILDREN.items()}
_BUDGET_BASE = {"q_init", "fa_hc"}  # 看板2 预算基线：以部门维度取数(部门自身)，中心维度BP手填，部门不上卷此两项

# 全部顶层部门（与 DEPT_TREE 一致，去「集团」）。「集团」不是独立部门，而是各部门加总口径（合计）。
ALL_DEPTS = ["云产品一部", "云产品二部", "云产品三部", "云产品四部", "云产品五部", "云产品六部",
             "安全产品一部", "安全产品二部", "安全产品三部", "战略客户部",
             "智慧行业一部", "智慧行业七部", "智慧行业十部",
             "科恩实验室", "玄武实验室", "优图实验室", "星星海实验室",
             "企业中台产品部", "社交协作产品部", "ima产品中心",
             "云产品技术支持部", "云技术运营服务部", "云运营管理部", "云采购供应管理部",
             "港澳台及国际业务部", "CSIG产品管理支持中心"]


def is_agg_dept(dept):
    """该 dept 是否为只读汇总：『含中心的部』(各中心加总) 或『集团』(各部门加总·合计)——不可直接录入"""
    return dept in DEPT_CENTERS or dept == "集团"


def _grid(c, year, dept="集团"):
    """cells → {metric: [v or None]*12}, notes → {metric: {m: note}}（按部门空间 dept）。
    集团→各部门加总(合计·只读)；部级(含中心)→各中心加总(只读汇总)；其余→本 dept 直取。"""
    if dept == "集团":
        agg = {}
        for d in ALL_DEPTS:
            cv, _ = _grid(c, year, d)
            for k, arr in cv.items():
                a = agg.setdefault(k, [None] * 12)
                for m in range(12):
                    if isinstance(arr[m], (int, float)):
                        a[m] = (a[m] if isinstance(a[m], (int, float)) else 0) + arr[m]
        return agg, {}  # 合计不带备注
    if dept in DEPT_CENTERS:
        agg = {}
        for center in DEPT_CENTERS[dept]:
            cv, _ = _grid(c, year, center)  # 中心不在 DEPT_CENTERS，直取
            for k, arr in cv.items():
                if k in _BUDGET_BASE:  # 预算当量基线(q_init/fa_hc)以部门维度取看板2，不从中心加总
                    continue
                a = agg.setdefault(k, [None] * 12)
                for m in range(12):
                    if isinstance(arr[m], (int, float)):
                        a[m] = (a[m] if isinstance(a[m], (int, float)) else 0) + arr[m]
        # 预算当量=部门维度看看板2：q_init/fa_hc 取本部门自身 cells（中心维度由 BP 手填，不上卷；部门只加总其他项）
        for r in c.execute("SELECT metric,month,value FROM cells WHERE year=? AND dept=? AND metric IN ('q_init','fa_hc')", (year, dept)):
            if 1 <= r["month"] <= 12:
                agg.setdefault(r["metric"], [None] * 12)[r["month"] - 1] = r["value"]
        return agg, {}  # 汇总不带备注
    vals, notes = {}, {}
    for r in c.execute("SELECT metric,month,value,note FROM cells WHERE year=? AND dept=?", (year, dept)):
        vals.setdefault(r["metric"], [None] * 12)
        if 1 <= r["month"] <= 12:
            vals[r["metric"]][r["month"] - 1] = r["value"]
            if r["note"]:
                notes.setdefault(r["metric"], {})[r["month"]] = r["note"]
    return vals, notes


def _kb0_adjust(c, year, dept, metric="chain"):
    """调节项 [v or None]*12：集团=各部门加总；部级(含中心)=各中心调节加总；其余=本 dept 直取（识空不补0）"""
    if dept == "集团":
        agg = [None] * 12
        for d in ALL_DEPTS:
            cv = _kb0_adjust(c, year, d, metric)
            for m in range(12):
                if isinstance(cv[m], (int, float)):
                    agg[m] = (agg[m] if isinstance(agg[m], (int, float)) else 0) + cv[m]
        return agg
    if dept in DEPT_CENTERS:
        agg = [None] * 12
        for center in DEPT_CENTERS[dept]:
            cv = _kb0_adjust(c, year, center, metric)
            for m in range(12):
                if isinstance(cv[m], (int, float)):
                    agg[m] = (agg[m] if isinstance(agg[m], (int, float)) else 0) + cv[m]
        return agg
    out = [None] * 12
    for r in c.execute("SELECT month,value FROM kb0_adjust WHERE year=? AND dept=? AND metric=?", (year, dept, metric)):
        if 1 <= r["month"] <= 12:
            out[r["month"] - 1] = r["value"]
    return out


def _read_branches(c, year, dept):
    out = []
    for b in c.execute("SELECT * FROM branches WHERE year=? AND dept=? AND on_ok=1 ORDER BY id", (year, dept)):
        vals = [None] * 12
        bnotes = {}
        for r in c.execute("SELECT month,value,note FROM branch_cells WHERE branch_id=?", (b["id"],)):
            if 1 <= r["month"] <= 12:
                vals[r["month"] - 1] = r["value"]
                if r["note"]:
                    bnotes[r["month"]] = r["note"]
        out.append({"id": b["id"], "sec": b["sec"], "name": b["name"], "sign": b["sign"], "vals": vals, "notes": bnotes})
    return out


def _branches(c, year, dept="集团"):
    if dept == "集团":  # 合计=各部门分支并集
        out = []
        for d in ALL_DEPTS:
            out.extend(_branches(c, year, d))
        return out
    if dept in DEPT_CENTERS:  # 含中心的部：把各中心手动分支汇总上来(标注中心·只读)，计入部门运算(表多出这几项)
        out = []
        for center in DEPT_CENTERS[dept]:
            cn = center.split("/")[-1]
            for b in _read_branches(c, year, center):
                b = dict(b)
                b["name"] = b["name"] + "（" + cn + "）"
                b["center"] = cn
                b["agg"] = True
                out.append(b)
        return out
    return _read_branches(c, year, dept)
