#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gadgetbridge 健康数据 MCP 服务（纯标准库，无第三方依赖）

数据源：Gadgetbridge 自动导出的 SQLite 数据库
默认：/sdcard/Download/Gadgetbridge（Gadgetbridge 自动导出，可在设置里改路径）

传输：MCP Streamable HTTP，端点 POST /mcp
  - 只返回 application/json（不升级 SSE），符合 Streamable HTTP 规范
  - GET /mcp 返回 405（本服务不主动推送），GET / 返回健康检查

环境变量：
  GB_DB    数据库路径（默认见 DB_PATH）
  HOST     监听地址（默认 0.0.0.0）
  PORT     监听端口（默认 8787）
  TOKEN    可选，设置后要求请求头 Authorization: Bearer <TOKEN>

启动：
  python3 health_mcp.py
"""

import datetime
import json
import os
import sqlite3
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB_PATH = os.environ.get("GB_DB", "/sdcard/Download/Gadgetbridge")
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8787"))
TOKEN = os.environ.get("TOKEN", "").strip()

SERVER_NAME = "gadgetbridge-health"
SERVER_VERSION = "1.0.0"
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL = "2025-06-18"

# 每日汇总里的哨兵值（未测量）
SENTINEL = 100000


# --------------------------------------------------------------------------
# 数据库访问
# --------------------------------------------------------------------------

def _connect():
    """只读打开导出库。文件可能被自动导出覆盖，所以每次调用都重新打开。"""
    if not os.path.exists(DB_PATH):
        raise ToolError(f"数据库不存在：{DB_PATH}")
    uri = "file:" + urllib.parse.quote(DB_PATH) + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=5.0)
    con.row_factory = sqlite3.Row
    return con


def _day_start(d):
    """本地时区当天 00:00 的 epoch 秒。"""
    return int(datetime.datetime.combine(d, datetime.time.min).timestamp())


def _fmt_day(d):
    return d.strftime("%Y-%m-%d")


def _r(v, nd=1):
    return None if v is None else round(float(v), nd)


def _nz(v):
    """把 0 当作「未测量」。"""
    return v if v not in (None, 0) else None


def _calories(v):
    """低于 10 kcal 的日汇总值视为无效（手环未上报时会写 0/1）。"""
    return v if v is not None and 10 <= v < SENTINEL else None


class ToolError(Exception):
    pass


def _parse_days(v, default=7):
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ToolError(f"days 需要整数，收到 {v!r}")
    return max(1, min(n, 730))


def _latest_data_date(con):
    """库中最新一条健康数据的日期（没有数据返回 None）。"""
    cands = []
    row = con.execute("SELECT MAX(TIMESTAMP) FROM XIAOMI_DAILY_SUMMARY_SAMPLE").fetchone()
    if row and row[0]:
        cands.append(datetime.datetime.fromtimestamp(row[0] / 1000.0).date())
    row = con.execute("SELECT MAX(TIMESTAMP) FROM XIAOMI_ACTIVITY_SAMPLE").fetchone()
    if row and row[0]:
        cands.append(datetime.datetime.fromtimestamp(row[0]).date())
    return max(cands) if cands else None


def _latest_meaningful_date(con):
    """最新一天「有实际活动」的日期（跳过只有零星几条记录的残日）。"""
    cands = []
    row = con.execute(
        "SELECT MAX(TIMESTAMP) FROM XIAOMI_DAILY_SUMMARY_SAMPLE WHERE STEPS > 0").fetchone()
    if row and row[0]:
        cands.append(datetime.datetime.fromtimestamp(row[0] / 1000.0).date())
    row = con.execute(
        "SELECT MAX(TIMESTAMP) FROM XIAOMI_ACTIVITY_SAMPLE WHERE STEPS > 0").fetchone()
    if row and row[0]:
        cands.append(datetime.datetime.fromtimestamp(row[0]).date())
    return max(cands) if cands else None


def _summary_row(con, d):
    s = _day_start(d)
    return con.execute(
        "SELECT * FROM XIAOMI_DAILY_SUMMARY_SAMPLE "
        "WHERE TIMESTAMP >= ? AND TIMESTAMP < ? ORDER BY TIMESTAMP LIMIT 1",
        (s * 1000, (s + 86400) * 1000),
    ).fetchone()


def _activity_agg(con, d):
    s = _day_start(d)
    return con.execute(
        """
        SELECT
          COALESCE(SUM(STEPS), 0)                          AS steps,
          COALESCE(SUM(DISTANCE_CM), 0)                    AS distance_cm,
          COALESCE(SUM(CASE WHEN HEART_RATE > 0 THEN 1 ELSE 0 END), 0) AS hr_n,
          MIN(CASE WHEN HEART_RATE > 0 THEN HEART_RATE END) AS hr_min,
          MAX(CASE WHEN HEART_RATE > 0 THEN HEART_RATE END) AS hr_max,
          AVG(CASE WHEN HEART_RATE > 0 THEN HEART_RATE END) AS hr_avg,
          COALESCE(SUM(CASE WHEN SPO2 > 0 THEN 1 ELSE 0 END), 0) AS spo2_n,
          MIN(CASE WHEN SPO2 > 0 THEN SPO2 END)             AS spo2_min,
          MAX(CASE WHEN SPO2 > 0 THEN SPO2 END)             AS spo2_max,
          AVG(CASE WHEN SPO2 > 0 THEN SPO2 END)             AS spo2_avg,
          COALESCE(SUM(CASE WHEN SPO2 > 0 AND SPO2 < 90 THEN 1 ELSE 0 END), 0) AS spo2_low_n,
          AVG(CASE WHEN STRESS > 0 THEN STRESS END)         AS stress_avg,
          COUNT(*)                                          AS samples
        FROM XIAOMI_ACTIVITY_SAMPLE
        WHERE TIMESTAMP >= ? AND TIMESTAMP < ?
        """,
        (s, s + 86400),
    ).fetchone()


def _sessions(con, d1, d2):
    """按醒来日期取睡眠会话（d1 <= 醒来日期 <= d2）。"""
    s = _day_start(d1)
    e = _day_start(d2) + 86400
    return con.execute(
        """
        SELECT TIMESTAMP AS start_ms, WAKEUP_TIME AS end_ms, TOTAL_DURATION,
               DEEP_SLEEP_DURATION, LIGHT_SLEEP_DURATION, REM_SLEEP_DURATION,
               AWAKE_DURATION
        FROM XIAOMI_SLEEP_TIME_SAMPLE
        WHERE WAKEUP_TIME >= ? AND WAKEUP_TIME < ?
        ORDER BY WAKEUP_TIME
        """,
        (s * 1000, e * 1000),
    ).fetchall()


def _sleep_dict(row):
    total = row["TOTAL_DURATION"] or 0
    wake_ms = row["end_ms"]
    return {
        "date": _fmt_day(datetime.datetime.fromtimestamp(wake_ms / 1000.0).date()),
        "start": datetime.datetime.fromtimestamp(row["start_ms"] / 1000.0).strftime("%Y-%m-%d %H:%M"),
        "end": datetime.datetime.fromtimestamp(wake_ms / 1000.0).strftime("%Y-%m-%d %H:%M"),
        "total_min": total,
        "total_hours": round(total / 60.0, 1),
        "deep_min": row["DEEP_SLEEP_DURATION"] or 0,
        "light_min": row["LIGHT_SLEEP_DURATION"] or 0,
        "rem_min": row["REM_SLEEP_DURATION"] or 0,
        "awake_min": row["AWAKE_DURATION"] or 0,
    }


def _anchor(con, days):
    """返回 (截止日期, 起始日期)。优先以今天为截止，若导出落后则用库里最新一天。"""
    latest = _latest_data_date(con)
    today = datetime.date.today()
    if latest and latest >= today:
        end = today
    else:
        end = (_latest_meaningful_date(con) or latest or today)
    return end, end - datetime.timedelta(days=days - 1)


# --------------------------------------------------------------------------
# 工具实现
# --------------------------------------------------------------------------

def get_today_summary(days: int = 1):
    con = _connect()
    try:
        latest = _latest_data_date(con)
        if latest is None:
            return {"error": "库里没有任何健康数据", "data_through": None}
        today = datetime.date.today()
        # 今天有数据就用今天；导出落后时退到最近一个「有实际活动」的日子
        d = today if latest >= today else (_latest_meaningful_date(con) or latest)
        is_today = d == today

        summ = _summary_row(con, d)
        agg = _activity_agg(con, d)

        steps = (summ["STEPS"] if summ and summ["STEPS"] is not None else agg["steps"]) or 0
        calories = _calories(summ["CALORIES"] if summ else None)
        distance_km = round((agg["distance_cm"] or 0) / 100000.0, 2)

        hr_avg = _nz(summ["HR_AVG"] if summ else None)
        hr_min = _nz(summ["HR_MIN"] if summ else None)
        hr_max = _nz(summ["HR_MAX"] if summ else None)
        hr_resting = _nz(summ["HR_RESTING"] if summ else None)
        if not hr_avg and agg["hr_n"]:
            hr_avg, hr_min, hr_max = agg["hr_avg"], agg["hr_min"], agg["hr_max"]

        spo2_avg = _nz(summ["SPO2_AVG"] if summ else None)
        spo2_min = _nz(summ["SPO2_MIN"] if summ else None)
        spo2_max = _nz(summ["SPO2_MAX"] if summ else None)
        if not spo2_avg and agg["spo2_n"]:
            spo2_avg, spo2_min, spo2_max = agg["spo2_avg"], agg["spo2_min"], agg["spo2_max"]

        sleep_rows = _sessions(con, d - datetime.timedelta(days=1), d)
        sleep = None
        if sleep_rows:
            todays = [r for r in sleep_rows
                      if datetime.datetime.fromtimestamp(r["end_ms"] / 1000.0).date() == d]
            pool = todays or sleep_rows
            main = max(pool, key=lambda r: r["TOTAL_DURATION"] or 0)
            sleep = _sleep_dict(main)
            sleep["naps"] = len([r for r in pool if r is not main])

        return {
            "date": _fmt_day(d),
            "is_today": is_today,
            "data_through": _fmt_day(latest),
            "steps": steps,
            "distance_km": distance_km,
            "calories_kcal": calories,
            "heart_rate": {
                "avg": _r(hr_avg, 0),
                "min": hr_min,
                "max": hr_max,
                "resting": hr_resting,
            },
            "spo2": {
                "avg": _r(spo2_avg, 0),
                "min": spo2_min,
                "max": spo2_max,
            },
            "stress_avg": _r(_nz(summ["STRESS_AVG"] if summ else agg["stress_avg"]), 0),
            "sleep": sleep,
            "note": None if is_today else
                    f"导出数据最新只到 {_fmt_day(latest)}，以下为 {_fmt_day(d)} 的数据",
        }
    finally:
        con.close()


def get_steps(days: int = 7):
    days = _parse_days(days)
    con = _connect()
    try:
        end, start = _anchor(con, days)
        out = []
        d = start
        while d <= end:
            summ = _summary_row(con, d)
            agg = _activity_agg(con, d)
            steps = (summ["STEPS"] if summ and summ["STEPS"] is not None else agg["steps"]) or 0
            calories = _calories(summ["CALORIES"] if summ else None)
            out.append({
                "date": _fmt_day(d),
                "steps": steps,
                "distance_km": round((agg["distance_cm"] or 0) / 100000.0, 2),
                "calories_kcal": calories,
            })
            d += datetime.timedelta(days=1)

        vals = [x["steps"] for x in out]
        return {
            "days": days,
            "from": _fmt_day(start),
            "to": _fmt_day(end),
            "data_through": _fmt_day(end),
            "total_steps": sum(vals),
            "avg_steps": round(sum(vals) / len(vals)) if vals else 0,
            "daily": out,
        }
    finally:
        con.close()


def get_heart_rate(days: int = 7):
    days = _parse_days(days)
    con = _connect()
    try:
        end, start = _anchor(con, days)
        out = []
        d = start
        while d <= end:
            summ = _summary_row(con, d)
            agg = _activity_agg(con, d)
            avg = summ["HR_AVG"] if summ else None
            hmin = summ["HR_MIN"] if summ else None
            hmax = summ["HR_MAX"] if summ else None
            if not avg and agg["hr_n"]:
                avg, hmin, hmax = agg["hr_avg"], agg["hr_min"], agg["hr_max"]
            out.append({
                "date": _fmt_day(d),
                "avg": _r(avg, 0),
                "min": hmin,
                "max": hmax,
                "resting": summ["HR_RESTING"] if summ else None,
                "samples": agg["hr_n"],
            })
            d += datetime.timedelta(days=1)

        # 最后一天逐小时心率（数据量可控，方便模型看日内波动）
        s = _day_start(end)
        hourly_rows = con.execute(
            """
            SELECT CAST((TIMESTAMP - ?) / 3600 AS INTEGER) AS hour,
                   AVG(HEART_RATE) AS avg, MIN(HEART_RATE) AS min,
                   MAX(HEART_RATE) AS max, COUNT(*) AS n
            FROM XIAOMI_ACTIVITY_SAMPLE
            WHERE TIMESTAMP >= ? AND TIMESTAMP < ? AND HEART_RATE > 0
            GROUP BY hour ORDER BY hour
            """,
            (s, s, s + 86400),
        ).fetchall()
        hourly = [
            {"hour": f"{r['hour']:02d}:00", "avg": _r(r["avg"], 0),
             "min": r["min"], "max": r["max"], "samples": r["n"]}
            for r in hourly_rows
        ]

        vals = [x["avg"] for x in out if x["avg"]]
        return {
            "days": days,
            "from": _fmt_day(start),
            "to": _fmt_day(end),
            "avg_of_avg": _r(sum(vals) / len(vals), 0) if vals else None,
            "daily": out,
            "hourly_last_day": {"date": _fmt_day(end), "hours": hourly},
        }
    finally:
        con.close()


def get_sleep(days: int = 7):
    days = _parse_days(days)
    con = _connect()
    try:
        end, start = _anchor(con, days)
        rows = _sessions(con, start, end)
        by_date = {}
        for r in rows:
            item = _sleep_dict(r)
            by_date.setdefault(item["date"], []).append(item)

        nights = []
        for date in sorted(by_date):
            sessions = sorted(by_date[date], key=lambda x: x["total_min"], reverse=True)
            main = sessions[0]
            main["is_nap"] = False
            naps = sessions[1:]
            for n in naps:
                n["is_nap"] = True
            nights.append({"date": date, "main": main, "naps": naps})

        totals = [n["main"]["total_hours"] for n in nights]
        avg = round(sum(totals) / len(totals), 1) if totals else None
        return {
            "days": days,
            "from": _fmt_day(start),
            "to": _fmt_day(end),
            "nights": len(nights),
            "avg_hours": avg,
            "avg_deep_min": round(sum(n["main"]["deep_min"] for n in nights) / len(nights)) if nights else None,
            "avg_rem_min": round(sum(n["main"]["rem_min"] for n in nights) / len(nights)) if nights else None,
            "daily": nights,
            "note": "以醒来日期归属主睡眠；naps 为同一天的短时段记录；时长单位分钟",
        }
    finally:
        con.close()


def get_spo2(days: int = 7):
    days = _parse_days(days)
    con = _connect()
    try:
        end, start = _anchor(con, days)
        out = []
        d = start
        while d <= end:
            summ = _summary_row(con, d)
            agg = _activity_agg(con, d)
            avg = summ["SPO2_AVG"] if summ else None
            smin = summ["SPO2_MIN"] if summ else None
            smax = summ["SPO2_MAX"] if summ else None
            if not avg and agg["spo2_n"]:
                avg, smin, smax = agg["spo2_avg"], agg["spo2_min"], agg["spo2_max"]
            out.append({
                "date": _fmt_day(d),
                "avg": _r(avg, 0),
                "min": smin,
                "max": smax,
                "samples": agg["spo2_n"],
                "below_90": agg["spo2_low_n"],
            })
            d += datetime.timedelta(days=1)

        vals = [x["avg"] for x in out if x["avg"]]
        return {
            "days": days,
            "from": _fmt_day(start),
            "to": _fmt_day(end),
            "avg": _r(sum(vals) / len(vals), 0) if vals else None,
            "daily": out,
        }
    finally:
        con.close()


# --------------------------------------------------------------------------
# 工具定义（MCP tools/list）
# --------------------------------------------------------------------------

def _days_schema(default=7, desc=None):
    return {
        "type": "object",
        "properties": {
            "days": {
                "type": "integer",
                "minimum": 1,
                "maximum": 730,
                "default": default,
                "description": desc or "How many days back from the latest available date (default 7).",
            }
        },
        "additionalProperties": False,
    }


TOOLS = [
    {
        "name": "get_today_summary",
        "description": (
            "Overview of one day: steps, distance, calories, heart rate (avg/min/max/resting), "
            "SpO2, stress and last night's sleep. Uses today if data exists, otherwise the most "
            "recent day in the export (see is_today / note)."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": get_today_summary,
    },
    {
        "name": "get_steps",
        "description": (
            "Daily step totals (plus distance and calories) for the most recent N days, "
            "oldest to newest, with total and average."
        ),
        "inputSchema": _days_schema(7, "How many days to return (default 7)."),
        "handler": get_steps,
    },
    {
        "name": "get_heart_rate",
        "description": (
            "Daily heart rate avg/min/max and resting rate for the last N days, plus hourly "
            "breakdown for the most recent day."
        ),
        "inputSchema": _days_schema(7, "How many days to return (default 7)."),
        "handler": get_heart_rate,
    },
    {
        "name": "get_sleep",
        "description": (
            "Sleep sessions for the last N days: start/end, total hours, and minutes in "
            "deep / light / REM / awake. A night is attributed to its wake-up date."
        ),
        "inputSchema": _days_schema(7, "How many days to return (default 7)."),
        "handler": get_sleep,
    },
    {
        "name": "get_spo2",
        "description": (
            "Blood oxygen (SpO2) daily average/min/max and count of readings below 90%, "
            "for the last N days."
        ),
        "inputSchema": _days_schema(7, "How many days to return (default 7)."),
        "handler": get_spo2,
    },
]

TOOL_MAP = {t["name"]: t for t in TOOLS}


# --------------------------------------------------------------------------
# JSON-RPC / MCP 处理
# --------------------------------------------------------------------------

def _ok(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def handle_rpc(msg):
    """处理一条 JSON-RPC 消息，返回响应 dict；通知类返回 None。"""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _err(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid Request")

    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}

    # 通知 / 客户端应答：不需要回包
    if req_id is None and method:
        return None

    if method == "initialize":
        asked = params.get("protocolVersion")
        version = asked if asked in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL
        return _ok(req_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "Health data from a Xiaomi smart band, exported periodically by Gadgetbridge. "
                "Data lags behind real time: check the data_through field. "
                "Dates are local (Asia/Shanghai)."
            ),
        })

    if method == "ping":
        return _ok(req_id, {})

    if method in ("notifications/initialized", "notifications/cancelled", "logging/setLevel"):
        return _ok(req_id, {}) if req_id is not None else None

    if method == "tools/list":
        return _ok(req_id, {
            "tools": [
                {"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]}
                for t in TOOLS
            ]
        })

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        tool = TOOL_MAP.get(name)
        if tool is None:
            return _err(req_id, -32602, f"Unknown tool: {name}")
        try:
            if not isinstance(args, dict):
                raise ToolError("arguments 必须是对象")
            result = tool["handler"](**args)
            text = json.dumps(result, ensure_ascii=False, indent=2)
            return _ok(req_id, {
                "content": [{"type": "text", "text": text}],
                "isError": False,
            })
        except TypeError as e:
            return _ok(req_id, {
                "content": [{"type": "text", "text": f"参数错误：{e}"}],
                "isError": True,
            })
        except Exception as e:  # 工具内部错误回给模型，不中断连接
            return _ok(req_id, {
                "content": [{"type": "text", "text": f"读取健康数据失败：{e}"}],
                "isError": True,
            })

    return _err(req_id, -32601, f"Method not found: {method}")


class Handler(BaseHTTPRequestHandler):
    server_version = f"{SERVER_NAME}/{SERVER_VERSION}"
    protocol_version = "HTTP/1.1"

    # ---- 基础工具 ----
    def _send(self, code, payload=None):
        body = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        if payload is not None:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _authorized(self):
        if not TOKEN:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {TOKEN}"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s - %s\n" % (
            datetime.datetime.now().strftime("%H:%M:%S"),
            self.address_string(), fmt % args))

    # ---- 路由 ----
    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path.rstrip("/") not in ("/mcp", ""):
            self._send(404, {"error": "not found, use POST /mcp"})
            return
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 4 * 1024 * 1024:
            self._send(400, {"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700, "message": "empty or oversized body"}})
            return

        raw = self.rfile.read(length)
        try:
            msg = json.loads(raw.decode("utf-8"))
        except Exception as e:
            self._send(400, {"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700, "message": f"parse error: {e}"}})
            return

        if isinstance(msg, list):
            if not msg:
                self._send(400, {"jsonrpc": "2.0", "id": None,
                                 "error": {"code": -32600, "message": "empty batch"}})
                return
            out = [r for r in (handle_rpc(m) for m in msg) if r is not None]
            if not out:
                self._send(202)
            else:
                self._send(200, out)
            return

        resp = handle_rpc(msg)
        if resp is None:
            self._send(202)
        else:
            self._send(200, resp)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path.rstrip("/") == "/mcp":
            # 本服务不提供 server->client 的 SSE 流，按规范回 405
            self.send_response(405)
            self.send_header("Allow", "POST, DELETE")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._send(200, {
            "server": SERVER_NAME,
            "version": SERVER_VERSION,
            "endpoint": "/mcp",
            "db": DB_PATH,
            "tools": [t["name"] for t in TOOLS],
        })

    def do_DELETE(self):
        self._send(200, {})


def main():
    if not os.path.exists(DB_PATH):
        sys.stderr.write(f"警告：数据库不存在 {DB_PATH}\n")
    else:
        try:
            con = _connect()
            latest = _latest_data_date(con)
            con.close()
            sys.stderr.write(f"数据源：{DB_PATH}（最新数据：{latest}）\n")
        except Exception as e:
            sys.stderr.write(f"警告：数据库暂时无法读取：{e}\n")

    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    httpd.daemon_threads = True
    sys.stderr.write(f"健康数据 MCP 已启动：http://{HOST}:{PORT}/mcp\n")
    sys.stderr.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
