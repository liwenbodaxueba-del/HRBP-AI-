# -*- coding: utf-8 -*-
"""后端 API 冒烟测试（对已启动的 127.0.0.1:8787 跑）"""
import json
import sys
import urllib.error
import urllib.request

B = 'http://127.0.0.1:8787'
PASS, FAIL = [], []


def req(method, path, data=None, headers=None, raw=None):
    h = {'Content-Type': 'application/json', 'X-User': 'bonniewbli'}
    h.update(headers or {})
    body = raw if raw is not None else (json.dumps(data).encode() if data is not None else None)
    r = urllib.request.Request(B + path, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def check(name, cond, info=''):
    (PASS if cond else FAIL).append(name)
    print(('PASS' if cond else 'FAIL'), name, info)


def multipart(content, filename):
    boundary = '----t'
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f'Content-Type: text/csv\r\n\r\n').encode() + content + f'\r\n--{boundary}--\r\n'.encode()
    return body, {'Content-Type': 'multipart/form-data; boundary=' + boundary}


# 1 配置
s, cfg = req('GET', '/api/config')
check('config 读取', s == 200 and len(cfg['projs']) == 13 and cfg['accts'][0]['id'] == 'bonniewbli')

# 2 空板（识空：全空不补零）
s, b = req('GET', '/api/board/2026')
check('空板 lock=6', s == 200 and b['lock'] == 6)
check('空板 chain 全空', all(v is None for v in b['computed']['chain']))
check('空板 budget 全空', all(v is None for v in b['metrics']['budget']['vals']))

# 3 校验闸
s, e = req('POST', '/api/board/2026/cell', {'metric': 'o_bp', 'month': 3, 'value': 2, 'note': 'x'})
check('锁定月拒绝(423)', s == 423)
s, e = req('POST', '/api/board/2026/cell', {'metric': 'o_bp', 'month': 9, 'value': 5, 'note': ''})
check('备注必填拒绝(422)', s == 422)
s, e = req('POST', '/api/board/2026/cell', {'metric': 'o_sys', 'month': 9, 'value': 5, 'note': 'x'})
check('系统数指标拒手填(403)', s == 403)
s, e = req('POST', '/api/board/2026/cell', {'metric': 'o_bp', 'month': 9, 'value': 999999, 'note': 'x'})
check('量级异常拒绝(422)', s == 422)
s, b = req('POST', '/api/board/2026/cell', {'metric': 'o_bp', 'month': 9, 'value': 5, 'note': '9月已谈妥5人'})
check('有效录入→outT9=5', s == 200 and b['computed']['outT'][8] == 5, str(b['computed']['outT'][8]))
check('备注回显', b['metrics']['o_bp']['notes'].get('9') == '9月已谈妥5人')

# 4 分支
s, b = req('POST', '/api/board/2026/branch', {'sec': '总流出（−）', 'name': '组织架构腾挪', 'sign': '-'})
check('建分支', s == 200 and len(b['branches']) == 1)
bid = b['branches'][0]['id']
s, b = req('POST', '/api/board/2026/cell', {'metric': f'branch:{bid}', 'month': 9, 'value': 2, 'note': '并入冲抵2'})
check('分支录入→outT9=3(5-2)', s == 200 and b['computed']['outT'][8] == 3, str(b['computed']['outT'][8]))

# 5 导入：坏文件整批拒绝
bad = 'metric,month,value\no_sys,13,4\nxxx,2,1\nactual,1,999999\n'.encode()
body, hd = multipart(bad, 'bad.csv')
s, e = req('POST', '/api/import/2026', raw=body, headers=hd)
check('坏CSV整批拒绝(422·3错)', s == 422 and e.get('detail', {}).get('total_errors') == 3, str(e.get('detail', {}).get('total_errors')))
s, b = req('GET', '/api/board/2026')
check('坏CSV未污染库', all(v is None for v in b['metrics']['o_sys']['vals']))

# 好文件
good = ('metric,month,value\n'
        + ''.join(f'actual,{m},{548 - m}\n' for m in range(1, 7))
        + ''.join(f'budget,{m},540\n' for m in range(1, 13)))
body, hd = multipart(good.encode(), 'snap.csv')
s, r = req('POST', '/api/import/2026', raw=body, headers=hd)
check('好CSV入库(18格)', s == 200 and r.get('rows') == 18, str(r))
s, b = req('GET', '/api/board/2026')
ch = b['computed']['chain']
# 锚6月实际542；7-12月= prev -0 +0 ... 除9月 outT=3 → 链: 542,542,539,539,539,539
check('链锚实际+调节生效', ch[5] == 542 and ch[6] == 542 and ch[8] == 539 and ch[11] == 539, str(ch))
check('预算年均=540', b['computed']['budget_avg'] == 540)

# 6 权限
s, e = req('POST', '/api/board/2026/cell', {'metric': 'o_bp', 'month': 10, 'value': 1, 'note': 'x'}, headers={'X-User': 'nobody'})
check('未配置账号拒写(403)', s == 403)
# 只读账号
s, cfg = req('GET', '/api/config')
cfg['accts'].append({'id': 'lead1', 'name': '领导', 'role': '领导·只读', 'dept': '云产品五部', 'kb': [1, 1, 1, 1], 'on': True})
s, _ = req('PUT', '/api/config', {'projs': cfg['projs'], 'accts': cfg['accts']})
check('配置下发', s == 200)
s, e = req('POST', '/api/board/2026/cell', {'metric': 'o_bp', 'month': 10, 'value': 1, 'note': 'x'}, headers={'X-User': 'lead1'})
check('只读账号拒写(403)', s == 403)

# 7 新增年份
s, _ = req('POST', '/api/years', {'year': 2028})
check('新增年份', s == 200)
s, e = req('POST', '/api/years', {'year': 2028})
check('重复年份拒绝(409)', s == 409)
s, b = req('GET', '/api/board/2028')
check('新年份空板', s == 200 and all(v is None for v in b['computed']['chain']))

# 8 审计&导出
s, a = req('GET', '/api/audit')
check('审计留痕', s == 200 and len(a) >= 8, f'{len(a)}条')
txt = urllib.request.urlopen(B + '/api/export/2026.csv').read().decode('utf-8')
check('导出CSV', txt.splitlines()[0].startswith('﻿项目') and '期末在岗预估' in txt)

print(f'\n===== {len(PASS)} PASS / {len(FAIL)} FAIL =====')
sys.exit(1 if FAIL else 0)
