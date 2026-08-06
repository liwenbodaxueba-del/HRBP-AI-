# -*- coding: utf-8 -*-
"""存储层：SQLite 连接/建库/审计/统一写格/权限闸/识空取数（企业版数据库批后仅替换本文件）"""
import os
import sqlite3
import time
from contextlib import contextmanager

from fastapi import HTTPException

from meta import CANON_PROJECTS, NAT_N_DEFAULT

# DB 选择（运行时可切·顶栏按钮）：
#   优先级：HCFB_DB 环境变量(forced·锁定不可切) > 持久化模式文件 db_mode > 假数库存在则默认假数库 > 真库 hcfb.db。
#   真库 hcfb.db 靠真实 API 写入(空态·库中无数据留空不编造)；假数库 hcfb_demo.db 由 seed_demo.py 生成(仅系统取数)。
#   db() 每次现取全局 DB_PATH、无连接池 → set_db_mode() 运行时改它即切库，无需重启。
#   is_demo_db() 供前端【示例】横幅（[[feedback_no_fabricated_data]]：假数必须标示例）。
_DB_DIR = os.path.dirname(__file__)
_REAL_DB = os.path.join(_DB_DIR, "hcfb.db")
_DEMO_DB = os.path.join(_DB_DIR, "hcfb_demo.db")
_MODE_FILE = os.path.join(_DB_DIR, "db_mode")  # 记住上次按钮选择，重启后沿用（gitignore）
_ENV_DB = os.environ.get("HCFB_DB")
DB_FORCED_ENV = bool(_ENV_DB)  # 环境变量锁定时按钮不可切


def _resolve_path(mode):
    return _DEMO_DB if mode == "demo" else _REAL_DB


def _initial_db_path():
    if _ENV_DB:
        return _ENV_DB if os.path.isabs(_ENV_DB) else os.path.join(_DB_DIR, _ENV_DB)
    try:
        m = open(_MODE_FILE, encoding="utf-8").read().strip()
        if m in ("demo", "real"):
            return _resolve_path(m)
    except OSError:
        pass
    return _DEMO_DB if os.path.exists(_DEMO_DB) else _REAL_DB


DB_PATH = _initial_db_path()


def is_demo_db():
    return os.path.basename(DB_PATH) == "hcfb_demo.db"


def db_mode_state():
    return {"mode": "demo" if is_demo_db() else "real", "is_demo": is_demo_db(),
            "demo_exists": os.path.exists(_DEMO_DB), "real_exists": os.path.exists(_REAL_DB),
            "forced_env": DB_FORCED_ENV, "db_file": os.path.basename(DB_PATH)}


def set_db_mode(mode):
    """运行时切库（demo=假数库 / real=真库）。db() 无连接池，切换后下一次取数即生效。"""
    global DB_PATH
    if DB_FORCED_ENV:
        raise HTTPException(409, "已通过 HCFB_DB 环境变量锁定数据库，运行时不可切换")
    if mode not in ("demo", "real"):
        raise HTTPException(422, "mode 须为 demo / real")
    if mode == "demo" and not os.path.exists(_DEMO_DB):
        raise HTTPException(404, "假数库 hcfb_demo.db 不存在，请先运行 backend/seed_demo.py 生成")
    DB_PATH = _resolve_path(mode)
    #切库后必须对新库跑一次 init_db（幂等）：init_db 只在启动时对【当时那个库】建表/迁移，
    # 切过去的库可能是旧结构（例如没有 org_depts/org_centers、cells 缺 org_type/org_id），
    # 不初始化就会在取数时报 no such table / no such column。顺带刷新组织缓存到新库的组织。
    init_db()
    try:
        with open(_MODE_FILE, "w", encoding="utf-8") as f:
            f.write(mode)
    except OSError:
        pass
    return db_mode_state()


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
        # ---- 2608 组织架构三层落库（部门/中心/个人）：唯一数据源 = org_depts / org_centers ----
        # 首次建库把内置示例组织(_SEED_*)灌进库作为 demo 数据；等接入真实数据源（企业微信通讯录 /
        # 核心人事 getOrgUnit）后走 org_store.apply_org 增量同步，改名只改 name、调动只改 dept_id，
        # 稳定 id 不变 → 历史数据永不失联。刷新缓存必须在下面用到 ALL_DEPTS 之前完成。
        from org_store import ensure_tables as _org_ensure, seed_org as _org_seed, backfill_cells_org as _org_fill
        _org_ensure(c)
        _org_seed(c, _SEED_ALL_DEPTS, _SEED_DEPT_CHILDREN)
        refresh_org_cache(c)
        _bf = _org_fill(c)  # cells 稳定锚点 org_type/org_id 回填（幂等·只补空值）
        if _bf.get("unresolved"):
            _audit(c, "system", "组织回填告警",
                   f"cells 有 {_bf['unresolved']} 个名字键匹配不到部门/中心，已保持 NULL 未做任何猜测（请人工核对）")
        # bonniewbli（内置系统管理员）= 全部部门：集团 + ALL_DEPTS 动态生成（部门增补自动跟随），无条件覆盖 → 现有库重启即更新，不只新库
        _ALLD = '[' + ','.join('"' + d + '"' for d in (["集团"] + list(ALL_DEPTS))) + ']'
        c.execute("UPDATE accounts SET kb1_depts=?, kb0_depts=? WHERE id='bonniewbli'",
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


# ---------------- 识空取数（服务端·与前端同口径） ----------------
# ============ 内置示例组织（seed 源·只在首次建库时灌入 org_depts/org_centers） ============
# 【注意】这两个 _SEED_* 常量【不再是运行时数据源】，运行时一律读 org_depts/org_centers 两张表
# （见文件末尾 refresh_org_cache）。它们只承担两个职责：
#① 首次建库时把这份内置组织灌进库，作为 demo 数据，让系统立刻可用；
#   ② 等接入真实组织架构数据源（企业微信通讯录 / 核心人事 getOrgUnit）后，
#      真实数据经 org_store.apply_org 增量同步覆盖，这里就只是历史起点、不再参与运算。
# 部门→中心。部级看板数据 = 其各中心加总（部为只读汇总，数据在中心录入）
_SEED_DEPT_CHILDREN = {
    "云产品一部": ["计算产品中心", "轻量云产品中心", "异构计算产品中心", "存储产品中心", "网络产品中心", "高性能网络产品中心", "CBS产品中心", "CLS产品中心", "虚拟化产品中心", "TCE产品中心", "TCS产品中心", "云开发产品中心", "中间件产品中心", "云原生产品中心", "国产数据库产品中心", "云原生数据库产品中心", "NoSQL数据库产品中心", "数据库SaaS产品与技术平台中心", "数据库架构与支持中心", "数据库平台研发中心", "区块链产品中心", "IaaS前沿技术组", "产品架构组", "产品管理支持组", "可用性架构组", "MaaS产品中心", "Agent Runtime产品中心", "TIONE产品中心", "计算加速中心"],
    "云产品二部": ["大数据产品架构与支持中心", "大数据基础产品中心", "TBDS产品中心", "WeData产品中心", "大数据应用产品中心", "产品运营及管理支持组", "数字孪生产品中心"],
    "云产品三部": ["应用产品一中心", "应用产品二中心", "智能体平台产品中心", "交付中心", "经营分析组", "海外运营组"],
    "云产品四部": ["运营产品中心", "客户经营平台产品中心", "计费产品中心", "平台产品中心", "产品支持中心", "腾讯云设计一中心", "腾讯云设计二中心", "服务与产品优化组", "综合业务项目管理组", "平台架构组", "身份产品中心"],
    "云产品五部": ["直播产品中心", "媒体AI产品中心", "WAND算法中心", "MPaaS产品解决方案中心", "边缘平台产品中心", "通信产品中心", "平台研发中心", "终端研发中心", "CPaaS产品售前支持组", "运营商合作中心", "物联与终端应用产品中心", "技术运营组", "音视频及CDN经营组", "产品管理支持中心", "战略规划组", "稳定性专项组"],
    "安全产品一部": ["产品管理组", "售前组", "零信任产品中心", "管家产品中心", "终端安全技术中心"],
    "安全产品二部": ["金融风控产品中心", "流量风控产品中心", "风控平台中心", "产品管理组", "合规技术支持中心", "海外业务组"],
    "安全产品三部": ["网络安全产品中心", "主机安全产品中心", "解决方案中心", "云安全技术中心", "云平台安全中心", "云安全能力中心", "入侵应急组", "产品管理组"],
    "科恩实验室": ["软件安全研究中心", "安全威胁分析运营中心"],
    "玄武实验室": ["生态安全研究组", "基础安全研究组", "产业安全研究组", "移动安全研究组", "天马实验组"],
    "优图实验室": ["盘古研究中心", "轩辕研究中心", "神农研究中心", "数据中心", "微信支付业务联合团队"],
    "企业中台产品部": ["乐享产品中心", "电子签产品中心", "经营平台中心", "微卡产品运营组"],
    "云产品技术支持部": ["平台支持中心", "培训认证中心", "运营组", "国际技术支持中心", "国内技术支持中心", "安灯产品中心", "售后服务管理中心", "云顾问产品中心"],
    "云技术运营服务部": ["技术服务与架构中心", "私有化技术中心", "计算技术中心", "网络技术中心", "服务运营管理中心", "数据库技术中心", "大数据技术中心", "可观测产品中心", "安全技术中心", "业务连续性架构组", "海外技术运营中心"],
    "社交协作产品部": ["基础产品中心", "编辑引擎中心", "Agent产品中心", "企业产品中心", "网盘产品中心", "商业化支持中心", "X1", "X2", "X3", "X4", "基础开发中心", "商业产品中心", "应用开发中心"],
    "ima产品中心": ["产品一组", "产品二组", "产品三组", "产品四组", "产品五组", "研发一组", "研发二组", "研发三组", "研发四组", "算法一组", "算法二组"],
    "智慧行业一部": ["行业拓展一中心", "行业架构一中心", "行业拓展二中心", "行业架构二中心", "行业拓展三中心", "行业架构三中心", "技术方案中心", "行业运营中心", "行业拓展四中心", "行业架构四中心"],
    "智慧行业七部": ["国有大行拓展组", "国有大行架构组", "商业银行拓展中心", "商业银行架构中心", "资管拓展中心", "资管架构中心", "保险拓展中心", "保险架构中心", "技术方案中心", "金融交付中心", "行业运营中心"],
    "智慧行业十部": ["行业拓展一中心", "行业架构一中心", "行业拓展二中心", "行业架构二中心", "行业拓展三中心", "行业架构三中心", "文化传媒拓展一中心", "文化传媒架构一中心", "文化传媒拓展二中心", "文化传媒架构二中心", "文化传媒解决方案中心", "交通拓展一中心", "交通架构一中心", "交通拓展二中心", "交通架构二中心", "交通合作支持中心", "公有云技术方案组", "行业运营中心", "私有化技术方案组"],
    "战略客户部": ["生态运营中心", "华北解决方案架构中心", "华东解决方案架构中心", "华南解决方案架构中心", "华北拓展中心", "华东拓展中心", "华南拓展中心"],
    "星星海实验室": ["通用计算中心", "加速计算与存储中心", "系统软件中心", "服务器运营中心", "技术平台组"],
    "港澳台及国际业务部": ["欧洲业务中心", "亚太区一中心", "亚太区二中心", "港澳台业务中心", "国际产品技术支持中心", "北美业务中心", "业务管理中心", "中东业务中心"],
    "CSIG产品管理支持中心": ["产品运营规范组", "产品生态业务发展组", "投资运营组", "产品出海合规建设组"],
}
_BUDGET_BASE = {"q_init", "fa_hc"}  # 看板2 预算基线：以部门维度取数(部门自身)，中心维度BP手填，部门不上卷此两项
# 部门级【不从中心上卷】、直接取部门自身 cells 的指标（口径已拍板 2608）：
#   · 仅预算当量基线 q_init / fa_hc → 读看板2（部门维度编制）；中心维度 BP 手填，不上卷。
# 【待流入·社招 不在此列】口径已确认：中心级直读该中心的看板3.2；部门级 = 其下各中心求和。
#   所以 soc_join / soc_sys / soc_hs 仍按「部门 = 各中心加总」，不要再加进这个集合。
_DEPT_DIRECT = set(_BUDGET_BASE)
_DEPT_DIRECT_PH = ",".join("?" * len(_DEPT_DIRECT))

# 内置示例组织的全部顶层部门（有序·决定前端展示顺序）。「集团」不是独立部门，而是各部门加总口径（合计）。
_SEED_ALL_DEPTS = ["云产品一部", "云产品二部", "云产品三部", "云产品四部", "云产品五部", "云产品六部",
                   "安全产品一部", "安全产品二部", "安全产品三部", "战略客户部",
                   "智慧行业一部", "智慧行业七部", "智慧行业十部",
                   "科恩实验室", "玄武实验室", "优图实验室", "星星海实验室",
                   "企业中台产品部", "社交协作产品部", "ima产品中心",
                   "云产品技术支持部", "云技术运营服务部", "云运营管理部", "云采购供应管理部",
                   "港澳台及国际业务部", "CSIG产品管理支持中心"]

# ============ 运行时组织缓存（唯一数据源 = org_depts / org_centers 两张表） ============
# 【必须原地更新·不可重新赋值】app.py 用的是 `from store import DEPT_CENTERS`（名字绑定），
# 一旦重新赋值，已import 的引用仍指向旧对象 → 组织变更不会生效。所以只能 clear()+update()。
_DEPT_CHILDREN = {}# {部门名: [中心短名, ...]}，只含【有中心的部】
DEPT_CENTERS = {}     # {部门名: [「部/中心」全路径键, ...]}，即 cells.dept 用的名字键
ALL_DEPTS = []        # 全部顶层部门（有序）


def refresh_org_cache(c):
    """从 org_depts/org_centers 刷新上面三个容器。组织改名/调动/新增后必须调一次。"""
    from org_store import load_org  # 函数级导入避免模块环
    children, all_depts = load_org(c)
    _DEPT_CHILDREN.clear()
    _DEPT_CHILDREN.update(children)
    DEPT_CENTERS.clear()
    DEPT_CENTERS.update({p: [p + "/" + ch for ch in kids] for p, kids in children.items()})
    ALL_DEPTS[:] = all_depts
    return {"depts": len(all_depts), "centers": sum(len(v) for v in children.values())}


# 建库/迁移须在 _SEED_* 与 refresh_org_cache 定义之后（init_db 内要seed 组织并刷新缓存）
init_db()


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
                if k in _DEPT_DIRECT:  # 仅预算基线(q_init/fa_hc)：部门维度直取看板2，不从中心加总
                    continue
                a = agg.setdefault(k, [None] * 12)
                for m in range(12):
                    if isinstance(arr[m], (int, float)):
                        a[m] = (a[m] if isinstance(a[m], (int, float)) else 0) + arr[m]
        # 部门维度直取（仅看板2 预算基线）：取本部门自身 cells；中心维度由 BP 手填、不上卷。
        # 待流入·社招不在此列：中心级直读该中心看板3.2，部门级= 各中心求和（走上面的加总分支）。
        for r in c.execute("SELECT metric,month,value FROM cells WHERE year=? AND dept=? "
                           f"AND metric IN ({_DEPT_DIRECT_PH})", (year, dept, *sorted(_DEPT_DIRECT))):
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
