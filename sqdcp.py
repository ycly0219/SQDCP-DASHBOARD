#!/usr/bin/env python3
"""SQDCP 单一入口：从飞书多维表格拉数据，生成看板 HTML，或启动本地服务。"""
import argparse
import datetime
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

# ===== 飞书凭证与应用标识 =====
# 凭证可通过环境变量覆盖；缺省值为本项目的示例 App，便于直接跑通。
APP_ID = os.environ.get("FEISHU_APP_ID", "cli_aadc4c86b6791cee")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "g98NQlSXeF6iTuCOb9JACgQQnjGiFGnI")
APP_TOKEN = "SokabZYA1a4cnMsmahzcD5UKnqe"
BASE = "https://open.feishu.cn/open-apis"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE_DIR, "dashboard.html")
TEMPLATE = os.path.join(BASE_DIR, "sqdcp.html")
ISSUE_TABLE_NAME = "问题跟踪表"

# ===== 本地服务配置 =====
PORT = 7700
CACHE_TTL = 300  # 接口结果缓存 5 分钟，避免前端刷新/重开时频繁打飞书 API
_cache = {"data": None, "ts": 0}

# 进程启动时一次性读入页面模板，供 build_html 注入与 serve 直接返回复用
HTML_TEMPLATE = open(TEMPLATE, encoding="utf-8").read()


def get_token():
    """用 App ID/Secret 换取飞书 tenant_access_token，供后续接口调用鉴权。"""
    r = requests.post(f"{BASE}/auth/v3/tenant_access_token/internal",
                      json={"app_id": APP_ID, "app_secret": APP_SECRET})
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError(f"get_token failed: {d}")
    return d["tenant_access_token"]


def auth(tk):
    """组装带 Bearer 鉴权的请求头，供所有飞书数据接口复用。"""
    return {"Authorization": f"Bearer {tk}", "Content-Type": "application/json"}


def list_tables(tk):
    """列出多维表格下的全部数据表，返回 {表名: table_id} 字典。"""
    r = requests.get(f"{BASE}/bitable/v1/apps/{APP_TOKEN}/tables", headers=auth(tk))
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError(f"list_tables failed: {d}")
    return {it["name"]: it["table_id"] for it in d["data"]["items"]}


def list_all_records(tk, tid):
    """分页拉取某张表的全部记录，自动翻页直到 has_more 为假，返回记录列表。"""
    out = []
    page_token = None
    while True:
        params = {"page_size": 500}
        if page_token:
            params["page_token"] = page_token
        r = requests.get(f"{BASE}/bitable/v1/apps/{APP_TOKEN}/tables/{tid}/records",
                         headers=auth(tk), params=params)
        d = r.json()
        if d.get("code") != 0:
            raise RuntimeError(f"list_records failed: {d}")
        out.extend(d["data"].get("items", []))
        if not d["data"].get("has_more"):
            break
        page_token = d["data"].get("page_token")
    return out


def get_text(v):
    """从飞书富文本字段里抽出展示用纯文本；兼容 list / dict / 标量三种形态。"""
    if v is None:
        return None
    if isinstance(v, list):
        for x in v:
            if isinstance(x, dict) and x.get("text"):
                return x["text"]
            if isinstance(x, str):
                return x
        return None
    if isinstance(v, dict):
        return v.get("text") or v.get("name")
    return v


def get_link_id(v):
    """从飞书关联记录字段里取出被关联记录的 record_id，把每日数据表行关联回指标配置表。"""
    if isinstance(v, list) and v:
        first = v[0]
        if isinstance(first, dict):
            ids = first.get("record_ids") or [first.get("record_id")]
            return ids[0] if ids else None
        return first
    if isinstance(v, dict):
        ids = v.get("record_ids") or [v.get("record_id")]
        return ids[0] if ids else None
    return v


def to_date_str(v):
    """把飞书日期字段（毫秒时间戳）转成 YYYY-MM-DD；非时间戳返回 None。"""
    if not isinstance(v, (int, float)):
        return None
    return datetime.datetime.fromtimestamp(v / 1000, datetime.timezone.utc).date().strftime("%Y-%m-%d")


def issue_table_fields():
    """问题跟踪表的建表字段模板，供 init-issue-table 幂等创建使用。"""
    return [
        {"field_name": "问题日期", "type": 5},
        {"field_name": "指标编号", "type": 1},
        {"field_name": "问题描述", "type": 1},
        {"field_name": "行动", "type": 1},
        {"field_name": "状态", "type": 3,
         "property": {"options": [{"name": "未开始"}, {"name": "进行中"}, {"name": "已关闭"}]}},
        {"field_name": "负责人", "type": 1},
        {"field_name": "计划关闭日期", "type": 5},
        {"field_name": "实际关闭日期", "type": 5},
    ]


def create_issue_table(tk):
    """调用飞书 bitable API 创建「问题跟踪表」。"""
    payload = {"table": {"name": ISSUE_TABLE_NAME, "fields": issue_table_fields()}}
    r = requests.post(f"{BASE}/bitable/v1/apps/{APP_TOKEN}/tables",
                      headers=auth(tk), json=payload)
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError(f"create issue table failed: {d}")


def list_table_fields(tk, tid):
    """列出某张数据表的字段，返回字段列表供幂等迁移检查。"""
    r = requests.get(f"{BASE}/bitable/v1/apps/{APP_TOKEN}/tables/{tid}/fields",
                     headers=auth(tk))
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError(f"list issue table fields failed: {d}")
    return d["data"]["items"]


def create_issue_field(tk, tid, field_name):
    """在现有「问题跟踪表」中补建缺失字段，不清空已有记录。"""
    r = requests.post(f"{BASE}/bitable/v1/apps/{APP_TOKEN}/tables/{tid}/fields",
                      headers=auth(tk), json={"field_name": field_name, "type": 1})
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError(f"create issue field failed: {d}")


def fetch_data(year=None):
    """从飞书多维表格拉取指标配置、每日数据与问题跟踪表。

    year: 仅保留该年份的数据；为 None 时返回全部。
    返回 {metrics:[...], daily:[...], issues:[...], months:[...]}，供前端/接口复用。
    """
    print("[1] get token ...", file=sys.stderr)
    token = get_token()
    tables = list_tables(token)
    tid_metric = tables["指标配置表"]
    tid_daily = tables["每日数据表"]
    if ISSUE_TABLE_NAME not in tables:
        raise RuntimeError(
            f"飞书多维表格缺少「{ISSUE_TABLE_NAME}」，请先运行: python sqdcp.py init-issue-table")
    tid_issue = tables[ISSUE_TABLE_NAME]

    print("[2] read metric config ...", file=sys.stderr)
    mrecs = list_all_records(token, tid_metric)
    rid2metric = {}
    code2metric = {}
    metrics = []
    for r in mrecs:
        f = r["fields"]
        code = f.get("指标编号")
        if not code:
            continue
        m = {"code": code, "name": get_text(f.get("指标名称")) or code,
             "category": get_text(f.get("SQDCP分类")) or "Other",
             "target": f.get("目标值"),
             "targetDir": get_text(f.get("目标方向")) or "",
             "unit": get_text(f.get("单位")) or ""}
        rid2metric[r["record_id"]] = m
        code2metric[str(code)] = m
        metrics.append(m)

    print("[3] read daily ...", file=sys.stderr)
    drecs = list_all_records(token, tid_daily)
    daily = []
    months = set()
    for r in drecs:
        f = r["fields"]
        rid = get_link_id(f.get("指标"))
        m = rid2metric.get(rid)
        if not m:
            continue
        date_ms = f.get("日期")
        if not isinstance(date_ms, (int, float)):
            continue
        dt = datetime.datetime.fromtimestamp(date_ms / 1000, datetime.timezone.utc).date()
        if year is not None and dt.year != year:
            continue
        # 数值字段允许是字符串，尝试转 float；失败则记为 None 视作无效值
        val = f.get("数值")
        if isinstance(val, str):
            try:
                val = float(val)
            except Exception:
                val = None
        daily.append({"date": dt.strftime("%Y-%m-%d"), "year": dt.year,
                      "month": dt.month, "day": dt.day, "code": m["code"],
                      "dim": get_text(f.get("维度")) or "", "value": val})
        # 收集出现过的月份，供前端月份下拉与渲染使用
        months.add(f"{dt.year}-{dt.month:02d}")

    print("[4] read issues ...", file=sys.stderr)
    irecs = list_all_records(token, tid_issue)
    issues = []
    for r in irecs:
        f = r["fields"]
        date_str = to_date_str(f.get("问题日期"))
        if date_str is None:
            continue
        dt = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
        if year is not None and dt.year != year:
            continue
        code = (get_text(f.get("指标编号")) or "").strip()
        m = code2metric.get(code)
        desc = (get_text(f.get("问题描述")) or "").strip() or "未填写问题描述"
        status = get_text(f.get("状态")) or "未开始"
        issues.append({
            "id": r.get("record_id", ""),
            "date": date_str,
            "year": dt.year,
            "month": dt.month,
            "day": dt.day,
            "code": code,
            "name": m["name"] if m else (code or "未填写指标编号"),
            "known": bool(m),
            "desc": desc,
            "action": (get_text(f.get("行动")) or "").strip(),
            "status": status,
            "owner": (get_text(f.get("负责人")) or "").strip(),
            "plannedClose": to_date_str(f.get("计划关闭日期")),
            "actualClose": to_date_str(f.get("实际关闭日期")),
        })
        months.add(f"{dt.year}-{dt.month:02d}")

    return {"metrics": metrics, "daily": daily, "issues": issues, "months": sorted(months)}


def build_html(data):
    """把最新数据注入 sqdcp.html 模板，返回可写盘的完整 HTML。"""
    data_json = json.dumps(data, ensure_ascii=False, default=str)
    # 避免 JSON 里出现 </script>，破坏内嵌数据脚本
    data_json = data_json.replace("</", "<\\/")
    # 占位结构与 sqdcp.html 内 <script id="sqdcp-data">__SQDCP_DATA__</script> 对齐
    placeholder = ">__SQDCP_DATA__</script>"
    if placeholder not in HTML_TEMPLATE:
        raise RuntimeError("sqdcp.html 缺少 __SQDCP_DATA__ 注入点")
    return HTML_TEMPLATE.replace(placeholder, ">" + data_json + "</script>")


def get_data():
    """serve 路由用：命中缓存直接返回，否则拉取当前年份数据并写入缓存。"""
    now = time.time()
    if _cache["data"] is not None and now - _cache["ts"] < CACHE_TTL:
        return _cache["data"]
    # 只取"当前年份"的数据（与本地服务原始行为一致）
    data = fetch_data(year=datetime.date.today().year)
    _cache["data"] = data
    _cache["ts"] = now
    return data


class Handler(BaseHTTPRequestHandler):
    """本地 HTTP 服务：对外提供页面与数据接口，均允许跨域、禁缓存。"""
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # 路由一：首页直接返回页面模板（serve 模式同源轮询）
        if self.path in ("/", "/index.html"):
            try:
                with open(TEMPLATE, "r", encoding="utf-8") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))
            return
        # 路由二：数据接口，返回 JSON（经 get_data 命中缓存）
        if self.path.startswith("/api/data"):
            try:
                self._send(200, json.dumps(get_data(), ensure_ascii=False, default=str))
            except Exception as e:
                self._send(502, json.dumps({"error": str(e)}, ensure_ascii=False))
            return
        # 路由三：其余路径统一 404
        self._send(404, json.dumps({"error": "not found"}))

    def log_message(self, *a):
        pass


def cmd_build(year):
    """build 子命令：拉数据 → 注入模板 → 写出 dashboard.html。"""
    data = fetch_data(year)
    html = build_html(data)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[4] dashboard.html -> {OUT}")


def cmd_data(year):
    """data 子命令：拉取数据并把 JSON 打到 stdout，便于管线调试。"""
    data = fetch_data(year)
    sys.stdout.write(json.dumps(data, ensure_ascii=False, default=str) + "\n")


def cmd_init_issue_table():
    """init-issue-table 子命令：幂等创建飞书「问题跟踪表」。"""
    print("[1] get token ...", file=sys.stderr)
    tk = get_token()
    tables = list_tables(tk)
    if ISSUE_TABLE_NAME in tables:
        tid = tables[ISSUE_TABLE_NAME]
        fields = list_table_fields(tk, tid)
        names = {it.get("field_name") for it in fields}
        if "行动" in names:
            print(f"「{ISSUE_TABLE_NAME}」已存在且已包含「行动」，跳过创建")
            return
        create_issue_field(tk, tid, "行动")
        print(f"已为「{ISSUE_TABLE_NAME}」补建「行动」字段")
        return
    create_issue_table(tk)
    print(f"已创建「{ISSUE_TABLE_NAME}」")


def cmd_serve():
    """serve 子命令：起 HTTP 服务对外提供页面与数据接口。"""
    print(f"SQDCP dashboard server  ->  http://localhost:{PORT}")
    print("前端打开 http://localhost:7700/ 即可（同源，轮询无 CORS 问题）")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="sqdcp.py",
        description="SQDCP 看板：拉取飞书数据、生成 HTML、启动本地服务。",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="启动本地服务（默认命令，端口 7700）")
    build = sub.add_parser("build", help="从飞书拉取数据并生成 dashboard.html")
    build.add_argument("year", nargs="?", type=int,
                       help="仅保留该年份；省略则包含全部年份")
    data = sub.add_parser("data", help="从飞书拉取数据并输出 JSON")
    data.add_argument("year", nargs="?", type=int,
                      help="仅保留该年份；省略则包含全部年份")
    sub.add_parser("init-issue-table", help="幂等创建飞书「问题跟踪表」")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    year = getattr(args, "year", None)
    if args.command == "build":
        cmd_build(year)
    elif args.command == "data":
        cmd_data(year)
    elif args.command == "init-issue-table":
        cmd_init_issue_table()
    else:
        cmd_serve()


if __name__ == "__main__":
    main()
