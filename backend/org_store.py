# -*- coding: utf-8 -*-
"""组织架构数据层：部门 / 中心 / 个人 三层落库（线级预留 line_name，本轮不建 org_lines 表）。

【为什么要有这一层】
原先部门/中心是三份彼此独立的硬编码（index.html DEPT_TREE、admin.html DEPT_TREE、
store.py _DEPT_CHILDREN），靠注释约束一致，且中心一改名/调部门，看板历史数据就会失联。
本模块把它们收敛为唯一数据源（org_depts / org_centers），并给出稳定 ID + 改名/调动留痕，
等接入真实组织架构数据源（企业微信通讯录/ 核心人事 getOrgUnit）后只需换fetch 实现。

【核心不变量·已与业务拍板】
1)稳定 ID 优先：上下级引用一律用自增 id，name 只是可变属性 → 改名不影响任何历史数据。
2) 中心可跨部门调动，且「数据跟着中心走」：调动后该中心【全部历史数据】归入新部门汇总，
   不按生效日期分段（口径已确认）。实现方式 = 改org_centers.dept_id + rekey 名字键。
3) 组织只软删（active=0），绝不物理删除、绝不删cells 历史数据。
4) 一切改名/调动写 org_renames / org_moves 留痕，可追溯。

【与 cells 的关系（关键）】
cells 主键是 (year, dept, metric, month)，dept 存的是【名字键】：
    '集团'（各部门加总·agg） / '云产品五部'（部级） / '云产品五部/直播产品中心'（中心级全路径）
改主键风险过大，因此这里的做法是：
  · cells 增加 org_type + org_id 两列作为【权威锚点】（名字键即使被外部改乱也能靠它找回）；
  · 改名/调动时用 rekey_org() 把各表里的名字键批量迁移到新键，读写逻辑完全不变（零风险）。
所以「中心调部门 → 键从 A部/X变成 B部/X → 汇总自动归到 B 部」正是拍板要的效果。

【循环导入】本模块所有函数都接受调用方传入的连接 c，不 import store 的 db()。
store.py 侧用函数级import 调用（沿用 store.py 既有的 `from kb3_ledger import ...` 范式）。
"""
import json
import os
import sqlite3
import time

ENTITY_DEPT = "dept"
ENTITY_CENTER = "center"
ORG_AGG = "agg"          # '集团'：各部门加总的伪节点，不是真部门
SEED_SOURCE = "builtin-demo"   # 首次灌入的内置示例组织（等接真实数据源后 ext_source 会被覆盖为 wecom/hrcore）

_CFG = os.path.join(os.path.dirname(__file__), "org_config.json")

# 名字键存放 dept 列的表（改名/调动时需要同步迁移，否则历史数据失联）
_KEYED_TABLES = ("cells", "cells_history", "branches", "kb0_adjust")


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ---------------- 建表（幂等） ----------------
_DDL = """
CREATE TABLE IF NOT EXISTS org_depts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  line_name TEXT DEFAULT '',
  ext_id TEXT DEFAULT '',
  ext_source TEXT DEFAULT '',
  order_no INTEGER DEFAULT 0,
  active INTEGER DEFAULT 1,
  synced_at TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS ix_org_depts_name ON org_depts(name);

CREATE TABLE IF NOT EXISTS org_centers(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  dept_id INTEGER NOT NULL,
  ext_id TEXT DEFAULT '',
  ext_source TEXT DEFAULT '',
  order_no INTEGER DEFAULT 0,
  active INTEGER DEFAULT 1,
  synced_at TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS ix_org_centers_dept_name ON org_centers(dept_id, name);

CREATE TABLE IF NOT EXISTS org_renames(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_type TEXT, entity_id INTEGER,
  old_name TEXT, new_name TEXT, changed_at TEXT, changed_by TEXT);

CREATE TABLE IF NOT EXISTS org_moves(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_type TEXT, entity_id INTEGER,
  old_parent_id INTEGER, new_parent_id INTEGER, changed_at TEXT, changed_by TEXT);
"""


def _add_col(c, table, col, decl):
    try:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    except sqlite3.OperationalError:
        pass  # 列已存在


def ensure_tables(c):
    """建组织表 + 给 accounts / cells 幂等补列。可反复调用。"""
    c.executescript(_DDL)
    # 个人层挂到中心/部门（保留原 dept 名字列做兼容；org_path 是 HR 组织树维度，两者正交，不得互相覆盖）
    _add_col(c, "accounts", "dept_id", "INTEGER")
    _add_col(c, "accounts", "center_id", "INTEGER")
    # cells 稳定锚点：org_type ∈ agg|dept|center，org_id 指向 org_depts.id / org_centers.id
    _add_col(c, "cells", "org_type", "TEXT")
    _add_col(c, "cells", "org_id", "INTEGER")


# ---------------- 名字键 ----------------
def center_key(dept_name, center_name):
    return dept_name + "/" + center_name


def parse_key(key):
    """名字键 → (org_type, dept_name, center_name)"""
    if key == "集团":
        return ORG_AGG, "", ""
    if "/" in key:
        d, _, cn = key.partition("/")
        return ENTITY_CENTER, d, cn
    return ENTITY_DEPT, key, ""


# ---------------- seed（把现有硬编码灌成 demo 组织数据） ----------------
def seed_org(c, seed_all_depts, seed_children, source=SEED_SOURCE):
    """首次灌入内置示例组织。幂等：已存在的部门/中心不覆盖、不重复插入。

    seed_all_depts: 全部顶层部门（有序，决定前端展示顺序；不含'集团'）
    seed_children:  {部门名: [中心名, ...]}（只有含中心的部才有键）
    """
    ts = now()
    added_d = added_c = 0
    for i, dname in enumerate(seed_all_depts):
        row = c.execute("SELECT id FROM org_depts WHERE name=?", (dname,)).fetchone()
        if not row:
            c.execute("INSERT INTO org_depts(name,line_name,ext_id,ext_source,order_no,active,synced_at) "
                      "VALUES(?,'','',?,?,1,?)", (dname, source, i, ts))
            added_d += 1
            did = c.execute("SELECT id FROM org_depts WHERE name=?", (dname,)).fetchone()["id"]
        else:
            did = row["id"]
        for j, cname in enumerate(seed_children.get(dname, [])):
            ex = c.execute("SELECT id FROM org_centers WHERE dept_id=? AND name=?", (did, cname)).fetchone()
            if not ex:
                c.execute("INSERT INTO org_centers(name,dept_id,ext_id,ext_source,order_no,active,synced_at) "
                          "VALUES(?,?,'',?,?,1,?)", (cname, did, source, j, ts))
                added_c += 1
    return {"depts_added": added_d, "centers_added": added_c}


# ---------------- 读取 ----------------
def load_org(c):
    """→ (children, all_depts)。children 只含【有中心的部】，与原_DEPT_CHILDREN 语义一致。"""
    children, all_depts = {}, []
    for d in c.execute("SELECT id,name FROM org_depts WHERE active=1 ORDER BY order_no,id").fetchall():
        all_depts.append(d["name"])
        cs = [r["name"] for r in c.execute(
            "SELECT name FROM org_centers WHERE dept_id=? AND active=1 ORDER BY order_no,id", (d["id"],)).fetchall()]
        if cs:
            children[d["name"]] = cs
    return children, all_depts


def tree(c):
    """给 GET /api/departments 用：带稳定 id 的部门树。"""
    out = []
    for d in c.execute("SELECT id,name,line_name,ext_id,ext_source,order_no,synced_at FROM org_depts "
                       "WHERE active=1 ORDER BY order_no,id").fetchall():
        centers = [{"id": r["id"], "name": r["name"], "ext_id": r["ext_id"] or ""}
                   for r in c.execute("SELECT id,name,ext_id FROM org_centers WHERE dept_id=? AND active=1 "
                                      "ORDER BY order_no,id", (d["id"],)).fetchall()]
        out.append({"id": d["id"], "name": d["name"], "line_name": d["line_name"] or "",
                    "ext_id": d["ext_id"] or "", "centers": centers})
    return out


def dept_id_of(c, name):
    r = c.execute("SELECT id FROM org_depts WHERE name=?", (name,)).fetchone()
    return r["id"] if r else None


def center_id_of(c, dept_name, center_name):
    r = c.execute("SELECT ct.id FROM org_centers ct JOIN org_depts d ON d.id=ct.dept_id "
                  "WHERE d.name=? AND ct.name=?", (dept_name, center_name)).fetchone()
    return r["id"] if r else None


def key_of(c, org_type, org_id):
    """(org_type, org_id) → 当前名字键。中心改名/调动后此函数返回的是【新键】。"""
    if org_type == ORG_AGG:
        return "集团"
    if org_type == ENTITY_DEPT:
        r = c.execute("SELECT name FROM org_depts WHERE id=?", (org_id,)).fetchone()
        return r["name"] if r else None
    r = c.execute("SELECT ct.name AS cn, d.name AS dn FROM org_centers ct JOIN org_depts d ON d.id=ct.dept_id "
                  "WHERE ct.id=?", (org_id,)).fetchone()
    return center_key(r["dn"], r["cn"]) if r else None


# ---------------- cells 稳定锚点回填（幂等） ----------------
def backfill_cells_org(c):
    """给 cells 的 org_type/org_id 回填。幂等：只补 org_id IS NULL 的行。

    不认识的历史脏键（对应部门/中心已不存在）保持 NULL 并计数上报，绝不猜测、绝不删除。
    """
    stat = {"agg": 0, "dept": 0, "center": 0, "unresolved": 0}
    dept_ids, center_ids = {}, {}
    for r in c.execute("SELECT id,name FROM org_depts").fetchall():
        dept_ids[r["name"]] = r["id"]
    for r in c.execute("SELECT ct.id AS cid, ct.name AS cn, d.name AS dn FROM org_centers ct "
                       "JOIN org_depts d ON d.id=ct.dept_id").fetchall():
        center_ids[center_key(r["dn"], r["cn"])] = r["cid"]
    keys = [r["dept"] for r in c.execute(
        "SELECT DISTINCT dept FROM cells WHERE org_id IS NULL OR org_type IS NULL").fetchall()]
    for k in keys:
        if k == "集团":
            c.execute("UPDATE cells SET org_type=?, org_id=0 WHERE dept=?", (ORG_AGG, k))
            stat["agg"] += 1
            continue
        if k in center_ids:
            c.execute("UPDATE cells SET org_type=?, org_id=? WHERE dept=?", (ENTITY_CENTER, center_ids[k], k))
            stat["center"] += 1
            continue
        if k in dept_ids:
            c.execute("UPDATE cells SET org_type=?, org_id=? WHERE dept=?", (ENTITY_DEPT, dept_ids[k], k))
            stat["dept"] += 1
            continue
        stat["unresolved"] += 1
    return stat


# ---------------- 名字键迁移（改名/调动时保证历史数据不失联） ----------------
def rekey_org(c, old_key, new_key):
    """把各表里的名字键 old_key 迁移到 new_key。返回各表迁移行数。

    UPDATE 可能与目标键已存在的行撞主键，因此先合并已存在的目标行（保留目标值，
    源行改名后若冲突则丢弃源行——正常业务下目标键不应已有数据，这里只是防御）。
    """
    moved = {}
    for t in _KEYED_TABLES:
        cols = [r[1] for r in c.execute(f"PRAGMA table_info({t})")]
        if "dept" not in cols:
            continue
        try:
            n = c.execute(f"UPDATE {t} SET dept=? WHERE dept=?", (new_key, old_key)).rowcount
        except sqlite3.IntegrityError:
            # 目标键已有同主键行：只迁移不冲突的部分，冲突行保留目标、丢弃源（并留痕计数）
            n = 0
            if t == "cells":
                n = c.execute(
                    "UPDATE cells SET dept=? WHERE dept=? AND NOT EXISTS("
                    "  SELECT 1 FROM cells t WHERE t.dept=? AND t.year=cells.year "
                    "  AND t.metric=cells.metric AND t.month=cells.month)",
                    (new_key, old_key, new_key)).rowcount
            elif t == "kb0_adjust":
                n = c.execute(
                    "UPDATE kb0_adjust SET dept=? WHERE dept=? AND NOT EXISTS("
                    "  SELECT 1 FROM kb0_adjust t WHERE t.dept=? AND t.year=kb0_adjust.year "
                    "  AND t.metric=kb0_adjust.metric AND t.month=kb0_adjust.month)",
                    (new_key, old_key, new_key)).rowcount
        moved[t] = n
    # 台账ledger_rows：dept / center 是分开两列存短名，单独处理
    ot, od, oc = parse_key(old_key)
    nt, nd, nc = parse_key(new_key)
    if ot == ENTITY_CENTER and nt == ENTITY_CENTER:
        moved["ledger_rows"] = c.execute("UPDATE ledger_rows SET dept=?, center=? WHERE dept=? AND center=?",
                                         (nd, nc, od, oc)).rowcount
    elif ot == ENTITY_DEPT and nt == ENTITY_DEPT:
        moved["ledger_rows"] = c.execute("UPDATE ledger_rows SET dept=? WHERE dept=?", (nd, od)).rowcount
    return moved


# ---------------- 改名 / 调动 / 软删（全部留痕） ----------------
def _rename(c, entity_type, entity_id, old_name, new_name, user):
    c.execute("INSERT INTO org_renames(entity_type,entity_id,old_name,new_name,changed_at,changed_by) "
              "VALUES(?,?,?,?,?,?)", (entity_type, entity_id, old_name, new_name, now(), user))


def rename_dept(c, dept_id, new_name, user="system"):
    r = c.execute("SELECT name FROM org_depts WHERE id=?", (dept_id,)).fetchone()
    if not r or r["name"] == new_name:
        return {"changed": False}
    old = r["name"]
    # 该部下每个中心的名字键都要跟着迁移（键含部门名前缀）
    centers = c.execute("SELECT name FROM org_centers WHERE dept_id=?", (dept_id,)).fetchall()
    c.execute("UPDATE org_depts SET name=?, synced_at=? WHERE id=?", (new_name, now(), dept_id))
    _rename(c, ENTITY_DEPT, dept_id, old, new_name, user)
    rekey_org(c, old, new_name)
    for ct in centers:
        rekey_org(c, center_key(old, ct["name"]), center_key(new_name, ct["name"]))
    c.execute("UPDATE accounts SET dept=? WHERE dept=?", (new_name, old))
    return {"changed": True, "old": old, "new": new_name, "centers_rekeyed": len(centers)}


def rename_center(c, center_id, new_name, user="system"):
    r = c.execute("SELECT ct.name AS cn, d.name AS dn FROM org_centers ct JOIN org_depts d ON d.id=ct.dept_id "
                  "WHERE ct.id=?", (center_id,)).fetchone()
    if not r or r["cn"] == new_name:
        return {"changed": False}
    old_key, new_key = center_key(r["dn"], r["cn"]), center_key(r["dn"], new_name)
    c.execute("UPDATE org_centers SET name=?, synced_at=? WHERE id=?", (new_name, now(), center_id))
    _rename(c, ENTITY_CENTER, center_id, r["cn"], new_name, user)
    moved = rekey_org(c, old_key, new_key)
    return {"changed": True, "old": old_key, "new": new_key, "moved": moved}


def move_center(c, center_id, new_dept_id, user="system"):
    """中心跨部门调动。口径已拍板：数据跟着中心走 → 全部历史数据归入新部门汇总。"""
    r = c.execute("SELECT ct.name AS cn, ct.dept_id AS did, d.name AS dn FROM org_centers ct "
                  "JOIN org_depts d ON d.id=ct.dept_id WHERE ct.id=?", (center_id,)).fetchone()
    if not r or r["did"] == new_dept_id:
        return {"changed": False}
    nd = c.execute("SELECT name FROM org_depts WHERE id=?", (new_dept_id,)).fetchone()
    if not nd:
        return {"changed": False, "err": "目标部门不存在"}
    old_key, new_key = center_key(r["dn"], r["cn"]), center_key(nd["name"], r["cn"])
    c.execute("UPDATE org_centers SET dept_id=?, synced_at=? WHERE id=?", (new_dept_id, now(), center_id))
    c.execute("INSERT INTO org_moves(entity_type,entity_id,old_parent_id,new_parent_id,changed_at,changed_by) "
              "VALUES(?,?,?,?,?,?)", (ENTITY_CENTER, center_id, r["did"], new_dept_id, now(), user))
    moved = rekey_org(c, old_key, new_key)
    c.execute("UPDATE accounts SET dept=? WHERE center_id=?", (nd["name"], center_id))
    return {"changed": True, "old": old_key, "new": new_key, "moved": moved}


def disable_dept(c, dept_id, user="system"):
    """软删部门：只置 active=0，不动任何历史数据。"""
    c.execute("UPDATE org_depts SET active=0, synced_at=? WHERE id=?", (now(), dept_id))
    return {"changed": True}


def disable_center(c, center_id, user="system"):
    c.execute("UPDATE org_centers SET active=0, synced_at=? WHERE id=?", (now(), center_id))
    return {"changed": True}


# ---------------- 外部数据源（待接权限；现在只有mock /无源两档） ----------------
def load_cfg():
    """org_config.json（已gitignore）。无配置 → None，调用方须回落库内数据、不得清空。"""
    try:
        with open(_CFG, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def fetch_org():
    """拉取外部组织架构 → {'source':..., 'depts':[{name,ext_id,line_name,centers:[{name,ext_id}]}]}

    三档兜底（与 sources.py / fetch_ioa_profile 的配置驱动范式一致）：
      1) 无org_config.json → 返回 None（系统行为与改造前完全一致，绝不编造、绝不清空）
      2) 配了 mock → 用 mock 数据（供本地演练改名/调动）
      3) 配了真实 endpoint → 待接入权限；当前明确抛 428，不静默假装成功
    """
    cfg = load_cfg()
    if not cfg:
        return None
    if cfg.get("mock"):
        return {"source": cfg.get("source") or "mock", "depts": cfg["mock"].get("depts", [])}
    if cfg.get("endpoint"):
        from fastapi import HTTPException
        raise HTTPException(428, "组织架构数据源已配置 endpoint，但真实接口对接尚未开通权限（"
                                 "企业微信通讯录 Secret / 核心人事 getOrgUnit 任一），请先完成权限申请")
    return None


# ---------------- 差异比对 / 落库 ----------------
def diff_org(c, src):
    """与库内现状比对 → {added, renamed, moved, disabled, bound}

    实体识别顺序（顺序很关键）：
      1) 先按 ext_id（外部稳定 ID）匹配——这是区分「改名」与「新增+删除」的唯一可靠依据；
      2) ext_id 匹配不到时【回退按名字匹配】，命中则认定为同一实体，并记入 bound（首次绑定 ext_id）。
    第2 步是首次接入真实数据源的命门：库内demo 组织的 ext_id 全为空，若不回退按名字匹配，
    首次同步会把 26 个部门/211 个中心全判成「新增」并把旧的全判「停用」→ 组织被整体重建、
    稳定 id 全变、看板历史数据挂到错误实体上。所以这里必须是「认领并绑定」而不是「新增」。
    """
    res = {"added": [], "renamed": [], "moved": [], "disabled": [], "bound": []}
    db_depts = {r["id"]: dict(r) for r in c.execute("SELECT * FROM org_depts WHERE active=1").fetchall()}
    db_centers = {r["id"]: dict(r) for r in c.execute("SELECT * FROM org_centers WHERE active=1").fetchall()}
    d_by_ext = {r["ext_id"]: r for r in db_depts.values() if r["ext_id"]}
    d_by_name = {r["name"]: r for r in db_depts.values()}
    c_by_ext = {r["ext_id"]: r for r in db_centers.values() if r["ext_id"]}
    c_by_dn = {(r["dept_id"], r["name"]): r for r in db_centers.values()}
    seen_d, seen_c = set(), set()

    for sd in src.get("depts", []):
        ext, nm = sd.get("ext_id") or "", sd.get("name") or ""
        cur = d_by_ext.get(ext) if ext else None
        if not cur:
            cur = d_by_name.get(nm)  # 回退按名字：首次接入时认领库内同名部门，而不是当成新增
        if not cur:
            res["added"].append({"type": ENTITY_DEPT, "name": nm, "ext_id": ext})
            did = None
        else:
            did = cur["id"]
            seen_d.add(did)
            if ext and not cur["ext_id"]:
                res["bound"].append({"type": ENTITY_DEPT, "id": did, "name": cur["name"], "ext_id": ext})
            if cur["name"] != nm:
                res["renamed"].append({"type": ENTITY_DEPT, "id": did, "old": cur["name"], "new": nm})
        for sc in sd.get("centers", []):
            cext, cnm = sc.get("ext_id") or "", sc.get("name") or ""
            ccur = c_by_ext.get(cext) if cext else None
            if not ccur and did:
                ccur = c_by_dn.get((did, cnm))  # 回退按 (部门, 中心名) 认领
            if not ccur:
                res["added"].append({"type": ENTITY_CENTER, "name": cnm, "ext_id": cext, "dept": nm})
                continue
            seen_c.add(ccur["id"])
            if cext and not ccur["ext_id"]:
                res["bound"].append({"type": ENTITY_CENTER, "id": ccur["id"], "name": ccur["name"], "ext_id": cext})
            if ccur["name"] != cnm:
                res["renamed"].append({"type": ENTITY_CENTER, "id": ccur["id"], "old": ccur["name"], "new": cnm,
                                       "dept": nm})
            if did and ccur["dept_id"] != did:
                res["moved"].append({"type": ENTITY_CENTER, "id": ccur["id"], "name": cnm,
                                     "old_dept": db_depts.get(ccur["dept_id"], {}).get("name", "?"), "new_dept": nm})
    for did, r in db_depts.items():
        if did not in seen_d:
            res["disabled"].append({"type": ENTITY_DEPT, "id": did, "name": r["name"]})
    for cid, r in db_centers.items():
        if cid not in seen_c:
            res["disabled"].append({"type": ENTITY_CENTER, "id": cid, "name": r["name"],
                                    "dept": db_depts.get(r["dept_id"], {}).get("name", "?")})
    res["suspect_renames"] = _suspect_renames(res)
    return res


def _suspect_renames(res, ratio=0.5, limit=50):
    """挑出「疑似改名」供人工确认：同一部门下同时出现一个 added 和一个 disabled 且名字高度相似。

    为什么需要：首次接入时库内组织 ext_id 全为空，若外部数据同时改了名，就没有任何线索能把
    两者认定为同一实体，diff 只能表现为「新增一个 + 停用一个」。若照此落库，该中心的历史数据
    会留在被停用的旧实体上、新实体从零开始——等于数据断档。所以这里主动配对提示，
    让人工在预览阶段就发现「这其实是改名」，先走改名接口再同步即可。
    """
    import difflib
    adds = [x for x in res["added"] if x["type"] == ENTITY_CENTER and x.get("dept")]
    diss = [x for x in res["disabled"] if x["type"] == ENTITY_CENTER and x.get("dept")]
    out = []
    for a in adds:
        for d in diss:
            if d["dept"] != a["dept"]:
                continue
            sim = difflib.SequenceMatcher(None, a["name"], d["name"]).ratio()
            if sim >= ratio:
                out.append({"dept": a["dept"], "maybe_old": d["name"], "maybe_old_id": d["id"],
                            "maybe_new": a["name"], "new_ext_id": a.get("ext_id", ""), "similarity": round(sim, 2),
                            "hint": "若确为改名，请先调 POST /api/departments/rename 改名（历史数据会跟着走），再同步"})
            if len(out) >= limit:
                return out
    return out


DISABLE_GUARD_RATIO = 0.3  # 一次同步要停用的组织超过此比例 → 拒绝执行（防数据源配错造成大面积误停用）


def apply_org(c, src, user="system", dry_run=True, force=False):
    """按 diff 结果落库。dry_run=True 只返回预览、不写任何数据。

    落库顺序有讲究：先 bound（回填 ext_id 认领实体）→ 再 renamed / moved（此时实体已绑定，
    改名与调动才会落到正确实体上）→ 然后 added → 最后 disabled。

    落库规则（防丢数据第一优先）：改名→UPDATE name + 留痕 + rekey；调动→UPDATE dept_id + 留痕 + rekey；
    消失→只 active=0 软删；新增→INSERT。任何情况都不物理删除、不动 cells 历史数据。

    安全阀：若本次要停用的组织占现有组织比例超过 DISABLE_GUARD_RATIO，判定为数据源异常
    （最常见原因是 root_dept_id 配错、只拉到一个子树），直接拒绝并要求人工确认 force=True。
    """
    d = diff_org(c, src)
    alive = (c.execute("SELECT COUNT(*) n FROM org_depts WHERE active=1").fetchone()["n"]
             + c.execute("SELECT COUNT(*) n FROM org_centers WHERE active=1").fetchone()["n"])
    risky = alive and len(d["disabled"]) / alive > DISABLE_GUARD_RATIO
    guard = {"blocked": bool(risky and not dry_run and not force),
             "disable_n": len(d["disabled"]), "alive_n": alive,
             "ratio": round(len(d["disabled"]) / alive, 3) if alive else 0}
    if dry_run:
        return {"dry_run": True, "diff": d, "guard": guard}
    if guard["blocked"]:
        from fastapi import HTTPException
        raise HTTPException(409, {"msg": "本次同步将停用过多组织，已拦截（疑似数据源范围配错，如root 组织 ID 不对）",
                                  "disable_n": guard["disable_n"], "alive_n": alive, "ratio": guard["ratio"],
                                  "howto": "核对数据源范围；确认无误再带 force=true 重试"})
    source = src.get("source") or "external"
    ts = now()
    for it in d["bound"]:  # 首次绑定外部稳定 ID（认领库内既有实体，不新建）
        t = "org_depts" if it["type"] == ENTITY_DEPT else "org_centers"
        c.execute(f"UPDATE {t} SET ext_id=?, ext_source=?, synced_at=? WHERE id=?",
                  (it["ext_id"], source, ts, it["id"]))
    for it in d["renamed"]:
        if it["type"] == ENTITY_DEPT:
            rename_dept(c, it["id"], it["new"], user)
        else:
            rename_center(c, it["id"], it["new"], user)
    for it in d["moved"]:
        nid = dept_id_of(c, it["new_dept"])
        if nid:
            move_center(c, it["id"], nid, user)
    for it in d["added"]:
        if it["type"] == ENTITY_DEPT:
            mx = c.execute("SELECT COALESCE(MAX(order_no),0)+1 AS n FROM org_depts").fetchone()["n"]
            c.execute("INSERT OR IGNORE INTO org_depts(name,line_name,ext_id,ext_source,order_no,active,synced_at) "
                      "VALUES(?,'',?,?,?,1,?)", (it["name"], it.get("ext_id") or "", source, mx, ts))
        else:
            did = dept_id_of(c, it.get("dept") or "")
            if did:
                mx = c.execute("SELECT COALESCE(MAX(order_no),0)+1 AS n FROM org_centers WHERE dept_id=?",
                               (did,)).fetchone()["n"]
                c.execute("INSERT OR IGNORE INTO org_centers(name,dept_id,ext_id,ext_source,order_no,active,synced_at)"
                          " VALUES(?,?,?,?,?,1,?)", (it["name"], did, it.get("ext_id") or "", source, mx, ts))
    for it in d["disabled"]:
        if it["type"] == ENTITY_DEPT:
            disable_dept(c, it["id"], user)
        else:
            disable_center(c, it["id"], user)
    return {"dry_run": False, "diff": d, "guard": guard}
