# -*- coding: utf-8 -*-
"""看板3 台账：xlsx/csv 按列名识别解析（XML 直读·兼容 WPS）、行模型与字段归一（日期两位年/状态同义词）"""
import csv
import io

from fastapi import HTTPException


# ---------------- 看板3 台账：按列名识别解析 xlsx/csv（XML 直读·兼容 WPS） ----------------
import re as _re
import zipfile
import xml.etree.ElementTree as _ET
from datetime import datetime as _dt, timedelta as _td

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_RNS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

# 列名同义词（按包含匹配；靠列名识别，不认列位置/不认 sheet 名）——260723 对齐线下模板 v2（26列）
LEDGER_SYN = [
    ("dept", ["部门"]),
    ("center", ["中心"]),
    ("owner", ["业务负责人", "负责人"]),
    ("src", ["HC来源", "hc来源"]),
    ("job", ["招聘岗位"]),
    ("olvl", ["人选职级"]),          # 须在 lvl 之前：S列「人选职级」勿被「职级」抢占
    ("lvl", ["职级"]),
    ("cls", ["国内/海外", "国内海外"]),  # 须在 fam 之前：H列「国内/海外」；旧模板「分类(国内/海外)」也归此
    ("fam", ["分类"]),               # G列 分类（研发/产品等）
    ("loc", ["地点", "城市"]),
    ("ask", ["需求提出"]),
    ("num", ["招聘数量", "数量"]),
    ("tgt", ["目标到岗"]),
    ("st", ["当前状态", "进展"]),
    ("peta", ["上次预计到岗"]),       # 须在 eta 之前：O列「上次预计到岗(参考)」
    ("eta", ["预计到岗"]),
    ("jmemo", ["入职备注"]),          # 须在 memo 之前
    ("memo", ["变更原因", "卡点", "备注"]),
    ("who", ["面试中人选", "可简述背景"]),   # 须在 offer 之前：「人选姓名（可简述背景）」归 who
    ("offer", ["offer人选", "人选姓名"]),
    ("join", ["实际入职"]),
]
LEDGER_ST = ["需求确认", "已offer待入职", "已入职", "简历&面试中", "简历面试中", "Hold", "取消"]
LEDGER_CLS = ["国内", "海外"]  # H列 下拉枚举
LEDGER_MIN_HITS = 4  # 表头行至少命中的列名数


def _col_idx(ref):
    s = 0
    for ch in ref:
        if ch.isalpha():
            s = s * 26 + (ord(ch.upper()) - 64)
        else:
            break
    return s - 1


def _norm_date(v):
    if isinstance(v, (int, float)) and 20000 < v < 60000:
        return (_dt(1899, 12, 30) + _td(days=int(v))).strftime("%Y-%m-%d")
    s = str(v).strip()
    if not s:
        return ""
    t = s.replace("年", ".").replace("月", ".").replace("日", "").replace("/", ".").replace("-", ".")
    parts = [p for p in _re.split(r"[.\s]+", t) if p.isdigit()]
    if parts and len(parts[0]) == 2 and 20 <= int(parts[0]) <= 39:  # 两位年 25.6.30 → 2025-06-30
        parts[0] = "20" + parts[0]
    if len(parts) >= 3 and len(parts[0]) == 4:
        try:
            return "%s-%02d-%02d" % (parts[0], int(parts[1]), int(parts[2]))
        except ValueError:
            return s
    if len(parts) == 2 and len(parts[0]) == 4:
        return "%s-%02d" % (parts[0], int(parts[1]))
    if len(parts) == 1 and len(parts[0]) == 4:
        return parts[0]
    return s  # 解析不了保留原文（不猜不编）


def _norm_status(v):
    s = str(v).strip()
    low = s.lower()
    for k in LEDGER_ST:
        if k.lower() in low:
            return "简历&面试中" if "面试" in k else k
    return s


def _sheet_cells(z, path, ss):
    """worksheet → [ {col_idx: value} ] 按行；value: float|str"""
    rows = []
    root = _ET.fromstring(z.read(path))
    for row in root.iter(_NS + "row"):
        d = {}
        for c in row.iter(_NS + "c"):
            ref, t = c.get("r") or "", c.get("t")
            v = c.find(_NS + "v")
            val = None
            if t == "inlineStr":
                ie = c.find(_NS + "is")
                if ie is not None:
                    val = "".join(x.text or "" for x in ie.iter(_NS + "t"))
            elif v is not None and v.text is not None:
                if t == "s":
                    try:
                        val = ss[int(v.text)]
                    except (ValueError, IndexError):
                        val = v.text
                elif t == "str":
                    val = v.text
                else:
                    try:
                        val = float(v.text)
                    except ValueError:
                        val = v.text
            if val not in (None, ""):
                d[_col_idx(ref)] = val
        rows.append(d)
    return rows


def _match_header(cells):
    """一行 {col:val} → (colmap{field:col}, hits)；列名按包含匹配，首个命中列生效"""
    colmap = {}
    for col, val in cells.items():
        txt = str(val).replace("\n", "").replace(" ", "")
        for field, syns in LEDGER_SYN:
            if field in colmap:
                continue
            if any(s in txt for s in syns):
                # 「目标到岗」不要抢 eta；「预计到岗」不要抢 tgt——同义词已区分，此处防 memo 抢占具体列
                colmap[field] = col
                break
    return colmap, len(colmap)


def parse_ledger(raw, filename):
    """返回 (rows, report)；识别不到表头 raise HTTPException 422"""
    candidates = []  # (hits, sheet_name, header_idx, colmap, sheet_rows)
    if filename.lower().endswith(".csv"):
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("gbk", errors="replace")
        sheet_rows = []
        for r in csv.reader(io.StringIO(text)):
            sheet_rows.append({i: v for i, v in enumerate(r) if str(v).strip()})
        for i in range(min(6, len(sheet_rows))):
            colmap, hits = _match_header(sheet_rows[i])
            if hits >= LEDGER_MIN_HITS:
                candidates.append((hits, "CSV", i, colmap, sheet_rows))
                break
    else:
        try:
            z = zipfile.ZipFile(io.BytesIO(raw))
            wb = _ET.fromstring(z.read("xl/workbook.xml"))
            rels = dict(_re.findall(r'Id="(rId\d+)"[^>]*Target="([^"]+)"', z.read("xl/_rels/workbook.xml.rels").decode("utf-8")))
            try:
                sroot = _ET.fromstring(z.read("xl/sharedStrings.xml"))
                ss = ["".join(t.text or "" for t in si.iter(_NS + "t")) for si in sroot.findall(_NS + "si")]
            except KeyError:
                ss = []
        except (zipfile.BadZipFile, KeyError) as e:
            raise HTTPException(422, f"无法解析文件（需 .xlsx/.csv）：{e}")
        for sh in wb.iter(_NS + "sheet"):
            name, rid = sh.get("name"), sh.get(_RNS + "id")
            path = rels.get(rid, "")
            if not path.startswith("xl/"):
                path = "xl/" + path
            try:
                sheet_rows = _sheet_cells(z, path, ss)
            except (KeyError, _ET.ParseError):
                continue
            best = None
            for i in range(min(6, len(sheet_rows))):
                colmap, hits = _match_header(sheet_rows[i])
                if hits >= LEDGER_MIN_HITS and (best is None or hits > best[0]):
                    best = (hits, name, i, colmap, sheet_rows)
            if best:
                candidates.append(best)
    if not candidates:
        raise HTTPException(422, {
            "msg": "未识别到台账表头（按列名识别，不认列位置）",
            "hint": "任一 sheet 的前 6 行内，须至少出现 %d 个列名，如：部门/中心/业务负责人/招聘岗位/进展(当前状态)/预计到岗时间/实际入职时间/offer人选…" % LEDGER_MIN_HITS,
        })
    hits, sheet_name, hidx, colmap, sheet_rows = max(candidates, key=lambda x: x[0])

    def g(cells, field):
        col = colmap.get(field)
        v = cells.get(col) if col is not None else None
        return "" if v is None else (str(int(v)) if isinstance(v, float) and v.is_integer() and field not in ("ask", "tgt", "eta", "join") else v)

    rows, skipped = [], 0
    for ai in range(hidx + 1, len(sheet_rows)):
        cells = sheet_rows[ai]
        if not cells:
            continue
        vals = {f: g(cells, f) for f, _ in LEDGER_SYN}
        joined = "".join(str(x) for x in cells.values())
        if "填写说明" in joined or str(vals.get("owner", "")).strip() == "总监/leader" or "下拉" in str(vals.get("cls", "")):
            skipped += 1
            continue
        st = _norm_status(vals.get("st", ""))
        if not str(vals.get("center", "")).strip() and not str(vals.get("job", "")).strip() and not st:
            skipped += 1
            continue
        rows.append({
            "_r": ai + 1,  # 表内 1-based 行号（报错定位用，入库前剥掉）
            "dept": str(vals.get("dept", "")), "center": str(vals.get("center", "")),
            "owner": str(vals.get("owner", "")), "src": str(vals.get("src", "")),
            "job": str(vals.get("job", "")), "lvl": str(vals.get("lvl", "")),
            "fam": str(vals.get("fam", "")), "cls": str(vals.get("cls", "")),
            "loc": str(vals.get("loc", "")),
            "ask": _norm_date(vals.get("ask", "")), "num": str(vals.get("num", "")).strip(),
            "tgt": _norm_date(vals.get("tgt", "")), "st": st,
            "eta": _norm_date(vals.get("eta", "")), "prev_eta": _norm_date(vals.get("peta", "")),
            "memo": str(vals.get("memo", "")),
            "offer": str(vals.get("offer", "")), "olvl": str(vals.get("olvl", "")),
            "join": _norm_date(vals.get("join", "")), "jmemo": str(vals.get("jmemo", "")),
            "who": str(vals.get("who", "")) or str(vals.get("offer", "")),
        })
    report = {
        "sheet": sheet_name, "header_row": hidx + 1, "hits": hits,
        "mapping": {f: chr(65 + c) if c < 26 else "A" + chr(65 + c - 26) for f, c in colmap.items()},
        "rows": len(rows), "skipped": skipped,
    }
    return rows, report


def _ledger_list(c, year):
    out = []
    for r in c.execute("SELECT * FROM ledger_rows WHERE year=? ORDER BY id", (year,)):
        d = dict(r)
        out.append({"id": d["id"], "c": d["center"], "own": d["owner"], "src": d["src"], "job": d["job"],
                    "jlvl": d.get("lvl", ""), "fam": d.get("fam", ""), "cls": d["cls"], "loc": d["loc"],
                    "ask": d["ask"], "num": d["num"], "tgt": d["tgt"],
                    "st": d["st"], "eta": d["eta"], "peta": d.get("prev_eta", ""), "memo": d["memo"],
                    "offer": d["offer"], "lvl": d["olvl"], "join": d["join_dt"], "jmemo": d["jmemo"],
                    "who": d["who"], "dept": d["dept"]})
    return out


# 前端字段名 → DB 列（jlvl=职级F列；lvl=人选职级S列 历史沿用；peta=上次预计到岗·系统维护）
LEDGER_F2DB = {"dept": "dept", "c": "center", "own": "owner", "src": "src", "job": "job", "jlvl": "lvl",
              "fam": "fam", "cls": "cls", "loc": "loc", "ask": "ask", "num": "num", "tgt": "tgt",
              "st": "st", "eta": "eta", "peta": "prev_eta", "memo": "memo",
              "offer": "offer", "lvl": "olvl", "join": "join_dt", "jmemo": "jmemo", "who": "who"}
LEDGER_DATE_F = {"ask", "tgt", "eta", "join", "peta"}
LEDGER_REQUIRED = [("dept", "部门"), ("c", "中心"), ("own", "业务负责人"), ("job", "招聘岗位"), ("st", "进展(当前状态)")]
LEDGER_ST_CANON = ["需求确认", "简历&面试中", "已offer待入职", "已入职", "Hold", "取消"]
_DATE_OK = _re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")


def _ledger_norm(f, v):
    v = "" if v is None else str(v).strip()
    if f in LEDGER_DATE_F:
        return _norm_date(v)
    if f == "st":
        return _norm_status(v)
    return v


def validate_ledger_rows(rows):
    """逐行校验列性质（下拉枚举/日期有效性+年份limit/招聘数量=1）；返回错误清单，非空则整批拒绝。
    招聘数量留空自动按 1 填（每行=一个名额）；填了但≠1 → 报错提醒另起一行。"""
    errors = []
    for r in rows:
        rn = r.get("_r", "?")
        if r["st"] and r["st"] not in LEDGER_ST_CANON:
            errors.append(f"第{rn}行 当前状态「{r['st']}」不在下拉枚举（{'/'.join(LEDGER_ST_CANON)}）")
        if r["cls"] and r["cls"] not in LEDGER_CLS:
            errors.append(f"第{rn}行 国内/海外「{r['cls']}」须为：国内 或 海外")
        num = r.get("num", "")
        if num == "":
            r["num"] = "1"
        else:
            try:
                ok = float(num) == 1
            except ValueError:
                ok = False
            if not ok:
                errors.append(f"第{rn}行 招聘数量「{num}」必须为 1——每行一个名额，多名额请另起一行")
        for f, lab in (("ask", "需求提出时间"), ("tgt", "目标到岗时间"), ("eta", "预计到岗时间"),
                       ("prev_eta", "上次预计到岗"), ("join", "实际入职时间")):
            v = r.get(f, "")
            if not v:
                continue
            if not _DATE_OK.match(str(v)):
                errors.append(f"第{rn}行 {lab}「{v}」无法识别为日期（支持 2026-03-15 / 26.3.15 / 2026年3月15日）")
            elif not (2000 <= int(str(v)[:4]) <= 2100):
                errors.append(f"第{rn}行 {lab}「{v}」年份超出 2000-2100 范围")
    return errors
