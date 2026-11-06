#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only dashboard for the existing A1 allocation job. Standard library only."""
import argparse
import csv
import datetime as dt
import io
import json
import logging
import re
import sqlite3
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

CST = dt.timezone(dt.timedelta(hours=8))
ATTEMPT = re.compile(r"第\s*(\d+)\s*次请求，可用域\s*(.+?)[。.]?$")
WAIT = re.compile(r"等待\s+(\d+)\s+秒")
ROOT = Path(__file__).resolve().parent


def load_target(path):
    """Read display fields only; never expose the complete task configuration."""
    labels = {"ap-tokyo-1": "东京", "ap-osaka-1": "大阪", "us-ashburn-1": "Ashburn",
              "us-phoenix-1": "Phoenix", "eu-frankfurt-1": "Frankfurt"}
    try:
        config = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        config = {}
    if not isinstance(config, dict):
        config = {}
    region = str(config.get("region") or "未配置区域")
    return {"name": str(config.get("display_name") or "Oracle-A1-1C6G"),
            "region": region, "region_label": labels.get(region, region),
            "shape": "VM.Standard.A1.Flex", "ocpus": 1, "memory_gb": 6, "disk_gb": 50}


def read_state(path):
    # Whitelist fields: never return pending request bodies, SSH keys, or retry tokens.
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        return {"attempts": int(raw.get("attempts", 0)), "instance_id": raw.get("instance_id")}
    except (OSError, ValueError, TypeError):
        return None


def classify(message):
    match = ATTEMPT.search(message)
    if match:
        return "attempt", "发起第 {} 次申请".format(match[1]), {"number": int(match[1]), "ad": match[2].rstrip("。")}
    match = WAIT.search(message)
    if match:
        seconds = int(match[1])
        return "wait", "等待 {} 分 {} 秒后再次尝试".format(seconds // 60, seconds % 60), {"seconds": seconds}
    if "容量不足，稍后重试" in message:
        return "capacity", "目标区域暂时没有空闲容量", {}
    if "临时错误" in message:
        return "transient", "遇到临时网络或服务错误，稍后核查原请求", {}
    if "实例已创建/已存在" in message or "本任务已经成功" in message:
        return "success", "已获得实例，自动申请结束", {}
    if "当前资源检查通过" in message:
        return "check", "当前资源额度检查通过", {}
    if "停止：" in message or "Failed with result" in message or "Main process exited" in message:
        # Raw errors may include private account identifiers or paths. Keep the UI human-readable.
        return "error", "任务已停止，请检查任务状态与申请配置", {}
    if "已达到 max_attempts" in message:
        return "limit", "达到设定尝试次数，任务停止", {}
    if "Started Retry Oracle" in message:
        return "start", "自动申请任务已启动", {}
    if "Stopped Retry Oracle" in message:
        return "stop", "自动申请任务已停止", {}
    return None


class Store:
    def __init__(self, db_path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(db_path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS events (
                cursor TEXT PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL,
                message TEXT NOT NULL, number INTEGER, meta TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS events_time ON events(ts DESC);
            CREATE TABLE IF NOT EXISTS attempts (
                number INTEGER PRIMARY KEY, started_at REAL NOT NULL,
                finished_at REAL, status TEXT NOT NULL, ad TEXT);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        """)
        self.db.commit()

    def cursor(self):
        with self.lock:
            row = self.db.execute("SELECT value FROM settings WHERE key='cursor'").fetchone()
            return row[0] if row else None

    def ingest(self, rows):
        with self.lock:
            latest = self.db.execute("SELECT max(number) FROM attempts").fetchone()[0]
            cursor = None
            for row in rows:
                cursor = row.get("__CURSOR", cursor)
                event = classify(str(row.get("MESSAGE", "")))
                if not event or not row.get("__REALTIME_TIMESTAMP") or not row.get("__CURSOR"):
                    continue
                kind, message, meta = event
                ts = int(row["__REALTIME_TIMESTAMP"]) / 1000000
                if kind == "attempt":
                    latest = meta["number"]
                inserted = self.db.execute(
                    "INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?)",
                    (row["__CURSOR"], ts, kind, message, latest, json.dumps(meta))).rowcount
                if not inserted:
                    continue
                if kind == "attempt":
                    self.db.execute("INSERT OR IGNORE INTO attempts VALUES(?,?,NULL,'running',?)",
                                    (latest, ts, meta["ad"]))
                elif latest and kind in {"capacity", "transient", "success", "error"}:
                    self.db.execute("UPDATE attempts SET finished_at=?, status=? WHERE number=?",
                                    (ts, kind, latest))
            if cursor:
                self.db.execute("INSERT OR REPLACE INTO settings VALUES('cursor',?)", (cursor,))
            self.db.commit()

    def history(self, before=None, status="all", limit=40):
        with self.lock:
            sql, params = "SELECT * FROM attempts WHERE 1=1", []
            if before is not None:
                sql += " AND number < ?"
                params.append(before)
            if status != "all":
                if status == "error":
                    sql += " AND status IN ('error','transient')"
                else:
                    sql += " AND status=?"
                    params.append(status)
            rows = self.db.execute(sql + " ORDER BY number DESC LIMIT ?", params + [limit + 1]).fetchall()
            return {"items": [dict(x) for x in rows[:limit]], "has_more": len(rows) > limit}

    def snapshot(self, state, service, now, collector_error=None, target=None):
        with self.lock:
            latest = self.db.execute("SELECT * FROM attempts ORDER BY number DESC LIMIT 1").fetchone()
            first = self.db.execute("SELECT min(started_at) FROM attempts").fetchone()[0]
            midnight = dt.datetime.fromtimestamp(now, CST).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            today = self.db.execute("SELECT count(*) FROM attempts WHERE started_at>=?", (midnight,)).fetchone()[0]
            counts = {r[0]: r[1] for r in self.db.execute("SELECT status,count(*) FROM attempts GROUP BY status")}
            events = [dict(r) for r in self.db.execute("SELECT ts,kind,message,number FROM events ORDER BY ts DESC LIMIT 14")]
            wait = self.db.execute("SELECT * FROM events WHERE kind='wait' ORDER BY ts DESC LIMIT 1").fetchone()
            success = self.db.execute("SELECT ts FROM events WHERE kind='success' ORDER BY ts DESC LIMIT 1").fetchone()
            total = max((state or {}).get("attempts", 0), latest["number"] if latest else 0)
            instance_id = (state or {}).get("instance_id")
            next_at = wait["ts"] + json.loads(wait["meta"])["seconds"] if wait else None
            wait_seconds = json.loads(wait["meta"])["seconds"] if wait else None
            active = service.get("ActiveState") == "active"
            if instance_id:
                phase = "success"
            elif service.get("ActiveState") == "failed":
                phase = "error"
            elif not active:
                phase = "paused"
            elif latest and latest["status"] == "running" and (not wait or wait["ts"] < latest["started_at"]):
                phase = "requesting"
            elif next_at and next_at > now:
                phase = "waiting"
            else:
                phase = "checking"
            # A one-hour bucket chart, aligned to Beijing time; timestamp labels carry an offset.
            end_hour = int(now // 3600) * 3600
            buckets = []
            for index in range(23, -1, -1):
                start = end_hour - index * 3600
                count = self.db.execute("SELECT count(*) FROM attempts WHERE started_at>=? AND started_at<?",
                                        (start, start + 3600)).fetchone()[0]
                buckets.append({"at": start, "count": count})
            return {"server_time": now, "phase": phase, "service": service.get("ActiveState", "unknown"),
                    "total_attempts": total, "today_attempts": today, "counts": counts,
                    "first_attempt_at": first, "last_attempt": dict(latest) if latest else None,
                    "next_attempt_at": next_at if active and not instance_id else None,
                    "wait_seconds": wait_seconds, "events": events, "hourly": buckets,
                    "instance_id": instance_id, "success_at": success[0] if success else None,
                    "target": target or load_target(None),
                    "data_warning": collector_error or ("暂时无法读取申请计数，正在重新同步" if state is None else None)}


class Collector:
    def __init__(self, store, state_file, journal_file=None, unit="oracle-a1-retry.service", config_file=None):
        self.store, self.state_file, self.journal_file = store, state_file, journal_file
        self.unit = unit
        self.config_file = config_file or Path(state_file).with_name("a1-config.json")
        self.lock = threading.RLock()
        self.data = None
        self.last_success = None
        self.refresh()

    def refresh(self):
        try:
            if self.journal_file:
                text = Path(self.journal_file).read_text(encoding="utf-8")
                service = {"ActiveState": "active"}
            else:
                args = ["journalctl", "--unit", self.unit, "--no-pager", "--output=json"]
                cursor = self.store.cursor()
                args += ["--after-cursor", cursor] if cursor else ["--lines", "20000"]
                result = subprocess.run(args, capture_output=True, text=True, timeout=12)
                if result.returncode and cursor:
                    # Cursor may expire after journal rotation; SQLite deduplicates retained rows.
                    result = subprocess.run(args[:5] + ["--lines", "20000"], capture_output=True, text=True, timeout=12)
                if result.returncode:
                    raise RuntimeError("任务日志暂时不可读取")
                text = result.stdout
                result = subprocess.run(["systemctl", "show", self.unit, "--property=ActiveState,SubState,Result"],
                                        capture_output=True, text=True, check=True, timeout=8)
                service = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
            rows = []
            for line in text.splitlines():
                if line.startswith("{"):
                    rows.append(json.loads(line))
            self.store.ingest(rows)
            now = time.time()
            snapshot = self.store.snapshot(read_state(self.state_file), service, now, target=load_target(self.config_file))
            self.last_success = now
        except Exception:
            logging.exception("Could not refresh allocation status")
            with self.lock:
                snapshot = dict(self.data) if self.data else self.store.snapshot(
                    read_state(self.state_file), {"ActiveState": "unknown"}, time.time(), target=load_target(self.config_file))
            snapshot["data_warning"] = "数据同步暂时中断，显示最近记录，正在自动重试"
        snapshot["collected_at"] = self.last_success
        with self.lock:
            self.data = snapshot

    def loop(self):
        while True:
            time.sleep(5)
            self.refresh()

    def current(self):
        with self.lock:
            return dict(self.data)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_body(self, payload, content_type="application/json; charset=utf-8", status=200, download=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'")
        if download:
            self.send_header("Content-Disposition", 'attachment; filename="{}"'.format(download))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if urlsplit("//" + self.headers.get("Host", "")).hostname not in {"127.0.0.1", "localhost", "::1"}:
            return self.send_body(b'{"error":"Local access only"}', status=403)
        parsed = urlsplit(self.path)
        if parsed.path == "/api/status":
            return self.send_body(json.dumps(self.server.collector.current(), ensure_ascii=False).encode())
        if parsed.path == "/api/attempts":
            params = parse_qs(parsed.query)
            try:
                before = int(params["before"][0]) if "before" in params else None
                limit = min(100, max(1, int(params.get("limit", [40])[0])))
            except (ValueError, TypeError):
                return self.send_body(b'{"error":"Invalid pagination"}', status=400)
            status = params.get("status", ["all"])[0]
            if status not in {"all", "capacity", "success", "error", "running"}:
                return self.send_body(b'{"error":"Invalid filter"}', status=400)
            return self.send_body(json.dumps(self.server.store.history(before, status, limit)).encode())
        if parsed.path == "/api/export.csv":
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(["申请序号", "开始时间（北京时间）", "完成时间（北京时间）", "结果", "可用域"])
            names = {"capacity": "容量不足", "success": "创建成功", "transient": "临时错误", "error": "异常", "running": "申请中"}
            with self.server.store.lock:
                for row in self.server.store.db.execute("SELECT * FROM attempts ORDER BY number DESC"):
                    dates = [dt.datetime.fromtimestamp(row[k], CST).isoformat(timespec="seconds") if row[k] else ""
                             for k in ("started_at", "finished_at")]
                    writer.writerow([row["number"]] + dates + [names.get(row["status"], row["status"]), row["ad"]])
            return self.send_body(("\ufeff" + buffer.getvalue()).encode(), "text/csv; charset=utf-8", download="oracle-a1-attempts.csv")
        files = {"/": ("index.html", "text/html; charset=utf-8"),
                 "/app.js": ("app.js", "application/javascript; charset=utf-8"),
                 "/style.css": ("style.css", "text/css; charset=utf-8")}
        if parsed.path not in files:
            return self.send_body(b'{"error":"Not found"}', status=404)
        name, mime = files[parsed.path]
        return self.send_body((ROOT / "static" / name).read_bytes(), mime)

    def do_POST(self):
        self.send_body(b'{"error":"Read only"}', status=405)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=28181)
    parser.add_argument("--state-file", default="a1-config.state.json")
    parser.add_argument("--config-file", help="申请配置文件，仅读取区域与显示名称")
    parser.add_argument("--unit", default="oracle-a1-retry.service")
    parser.add_argument("--data-dir", default=str(Path.home() / ".local/state/oracle-a1-dashboard"))
    parser.add_argument("--journal-file", help="Replay real journal data for local verification")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.@:-]+\.service", args.unit):
        parser.error("--unit 必须是有效的 systemd .service 名称")
    if not 1 <= args.port <= 65535:
        parser.error("--port 必须在 1–65535 范围内")
    folder = Path(args.data_dir)
    folder.mkdir(parents=True, exist_ok=True)
    store = Store(folder / "history.sqlite3")
    collector = Collector(store, args.state_file, args.journal_file, args.unit, args.config_file)
    threading.Thread(target=collector.loop, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    server.store, server.collector = store, collector
    print("A1 dashboard: http://127.0.0.1:{}/".format(args.port), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
