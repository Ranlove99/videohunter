#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视频猎手 —— 入口服务。

统一网关【不剥前缀】，/app/videohunter/xxx 会原样打到这里，所以先剥前缀。
优先监听 Unix Socket 供网关转发；绑定失败回退 TCP，便于本机调试。
"""

import json
import os
import signal
import socket
import socketserver
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "vendor"))
sys.path.insert(0, HERE)

from core import settings, tasks  # noqa: E402

PREFIX = settings.PREFIX
UI_DIR = os.path.join(HERE, "ui")
MAX_BODY = 2 * 1024 * 1024
VERSION = os.environ.get("TRIM_APPVER") or "1.4.0"

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


def log(msg):
    sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    sys.stderr.flush()


class Handler(BaseHTTPRequestHandler):
    server_version = "VideoHunter/" + VERSION
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------ 基础输出

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, val in (extra or {}).items():
            self.send_header(key, val)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, data, code=200):
        self._send(code, json.dumps(data, ensure_ascii=False))

    def _error(self, msg, code=400):
        self._json({"ok": False, "error": msg}, code)

    def _drain(self):
        """把请求体读干净，返回原始字节（每个请求只读一次）。

        HTTP/1.1 长连接下必须做这件事：不读 POST 的 body，字节就留在 socket 里，
        下一个请求会被解析成 "{}GET /api/tasks"，服务器直接回 501 并关连接。
        表现就是「清理已完成点了没反应」「暂停后进度消失」——请求看着 200，
        紧接着的轮询全挂。所以不管这个接口用不用 body，都先读掉。
        """
        cached = getattr(self, "_body", None)
        if cached is not None:
            return cached
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            self._body = b""
            return self._body
        if length <= MAX_BODY:
            try:
                self._body = self.rfile.read(length) or b""
            except (OSError, ValueError):
                self._body = b""
            return self._body
        # 超大 body：照样读掉丢弃，别让它污染连接
        left = length
        while left > 0:
            try:
                chunk = self.rfile.read(min(65536, left))
            except (OSError, ValueError):
                break
            if not chunk:
                break
            left -= len(chunk)
        self._body = b""
        return self._body

    def _read_json(self):
        raw = self._drain()
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def log_message(self, fmt, *args):
        addr = self.client_address
        if isinstance(addr, tuple) and addr:
            addr = addr[0]
        log("%s - %s" % (addr or "-", fmt % args))

    # ------------------------------------------------------------ 路由

    def handle_one_request(self):
        self._body = None   # 同一条连接会复用这个 handler，每个请求都要重置
        BaseHTTPRequestHandler.handle_one_request(self)

    def _route(self, method):
        self._drain()   # 任何请求都先读掉 body，避免长连接被污染
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        # 网关前缀
        if path == PREFIX:
            self._send(302, b"", "text/plain", {"Location": PREFIX + "/"})
            return
        if path.startswith(PREFIX + "/"):
            path = path[len(PREFIX):]

        if path.startswith("/api/"):
            self._api(method, path[len("/api/"):], query)
            return
        if method in ("GET", "HEAD"):
            self._static(path)
        else:
            self._error("不支持的请求", 405)

    def do_GET(self):
        self._route("GET")

    def do_HEAD(self):
        self._route("HEAD")

    def do_POST(self):
        self._route("POST")

    # ------------------------------------------------------------ API

    def _api(self, method, route, query):
        m = tasks.MANAGER

        if route == "health" and method == "GET":
            outdir = settings.resolve_download_dir()
            rep = settings.report_snapshot()
            rt = settings.render_status()
            ff = settings.ffmpeg_report()
            imp = settings.impersonate_report()
            self._json({
                "ok": True,
                "version": VERSION,
                "prefix": PREFIX,
                "browser": rep["ok"],
                "browser_state": rep["state"],
                "browser_version": rep["version"],
                "browser_reason": rep["reason"],
                "browser_missing": rep["missing"],
                "browser_render_error": rt["last_error"],
                "ffmpeg": ff["ok"],
                "ffmpeg_version": ff["version"],
                "ffmpeg_reason": ff["reason"],
                "impersonate": imp["ok"],
                "impersonate_reason": imp["reason"],
                "download_dir": outdir,
                "disk_free": settings.disk_free_text(outdir),
                "pid": os.getpid(),
            })
            return

        if route == "browser/check" and method == "POST":
            queued = settings.start_probe_async(force=True)
            self._json({"ok": True, "queued": queued,
                        "state": settings.report_snapshot()["state"]})
            return

        if route == "settings":
            if method == "GET":
                cfg = settings.load()
                cfg["_resolved_dir"] = settings.resolve_download_dir(cfg)
                cfg["_browser"] = settings.browser_available()
                cfg["_share_dirs"] = settings.share_candidates()
                self._json({"ok": True, "settings": cfg})
                return
            if method == "POST":
                body = self._read_json()
                current = settings.load()
                current.update({k: v for k, v in body.items() if not k.startswith("_")})
                saved = settings.save(current)
                saved["_resolved_dir"] = settings.resolve_download_dir(saved)
                saved["_browser"] = settings.browser_available()
                self._json({"ok": True, "settings": saved})
                return

        if route == "scan" and method == "POST":
            body = self._read_json()
            url = (body.get("url") or "").strip()
            if not url:
                self._error("请填写网址")
                return
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            job_id = m.start_scan(url)
            self._json({"ok": True, "job_id": job_id})
            return

        if route.startswith("scan/"):
            parts = route.split("/")
            job_id = parts[1] if len(parts) > 1 else ""
            if len(parts) == 3 and parts[2] in ("cancel", "pause", "resume") \
                    and method == "POST":
                action = parts[2]
                if action == "cancel":
                    ok = m.cancel_scan(job_id)
                else:
                    ok = m.pause_scan(job_id, action == "pause")
                self._json({"ok": ok})
                return
            if method == "GET":
                snap = m.get_scan(job_id)
                if not snap:
                    self._error("任务不存在", 404)
                    return
                self._json({"ok": True, "job": snap})
                return

        if route == "download" and method == "POST":
            body = self._read_json()
            items = body.get("urls") or []
            if not isinstance(items, list):
                self._error("参数格式不正确")
                return
            ids = m.enqueue(items)
            if not ids:
                self._error("没有有效的地址")
                return
            uniq = list(dict.fromkeys(ids))
            self._json({"ok": True, "ids": uniq, "count": len(uniq)})
            return

        if route == "tasks" and method == "GET":
            self._json({"ok": True, "tasks": m.list_tasks()})
            return

        if route == "tasks/clear" and method == "POST":
            m.clear_finished()
            self._json({"ok": True})
            return

        if route.startswith("tasks/"):
            parts = route.split("/")
            task_id = parts[1] if len(parts) > 1 else ""
            action = parts[2] if len(parts) > 2 else ""
            if action in ("cancel", "retry") and method == "POST":
                fn = m.cancel_task if action == "cancel" else m.retry_task
                self._json({"ok": fn(task_id)})
                return

        if route == "logs" and method == "GET":
            try:
                lines = int((query.get("lines") or ["120"])[0] or 120)
            except ValueError:
                lines = 120
            lines = max(10, min(1000, lines))
            self._json({"ok": True, "lines": _tail_log(lines)})
            return

        self._error("接口不存在", 404)

    # ------------------------------------------------------------ 静态文件

    def _static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        rel = urllib.parse.unquote(path).lstrip("/")
        target = os.path.normpath(os.path.join(UI_DIR, rel))

        # 穿越防护：规范化后必须仍在 UI_DIR 内
        if not target.startswith(UI_DIR + os.sep) and target != UI_DIR:
            self._error("非法路径", 403)
            return
        if not os.path.isfile(target):
            self._send(404, "Not Found", "text/plain; charset=utf-8")
            return

        ext = os.path.splitext(target)[1].lower()
        ctype = STATIC_TYPES.get(ext, "application/octet-stream")
        try:
            with open(target, "rb") as fh:
                data = fh.read()
        except OSError:
            self._error("读取文件失败", 500)
            return
        self._send(200, data, ctype)


def _tail_log(lines):
    path = os.path.join(settings.var_dir(), "app.log")
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 200 * 1024))
            data = fh.read()
    except OSError:
        return []
    text = data.decode("utf-8", "replace").splitlines()
    return text[-lines:]


class UnixHTTPServer(ThreadingHTTPServer):
    address_family = socket.AF_UNIX
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):
        if os.path.exists(self.server_address):
            os.unlink(self.server_address)
        socketserver.TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = 0


def _watch_probe():
    """后台探测结束后把结论写进应用日志，方便排查。"""
    deadline = time.time() + 180
    while time.time() < deadline:
        rep = settings.report_snapshot()
        if rep["state"] != "probing":
            if rep["ok"]:
                log("浏览器内核：可用（%s）" % (rep["version"] or "版本未知"))
            else:
                log("浏览器内核：不可用（降级静态解析）— " + (rep["reason"] or "未知原因"))
            return
        time.sleep(2)


def _warm_capabilities():
    """启动时把 ffmpeg、TLS 指纹伪装的能力摸清并写日志，便于排查。"""
    try:
        ff = settings.ffmpeg_report()
        if ff["ok"]:
            log("ffmpeg：可用（%s）" % (ff["version"] or "版本未知"))
        else:
            log("ffmpeg：不可用 — " + (ff["reason"] or "未知原因")
                + "（音视频合流/HLS 收尾可能失败）")
    except Exception as exc:
        log("ffmpeg 自检异常：%s" % exc)
    try:
        if settings.impersonate_ready():
            log("TLS 指纹伪装：可用（%s）" % settings.IMPERSONATE_TARGET)
        else:
            log("TLS 指纹伪装：不可用 — " + (settings.impersonate_report()["reason"] or "未知原因"))
    except Exception as exc:
        log("指纹伪装自检异常：%s" % exc)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", default="app.sock")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("TRIM_SERVICE_PORT") or 0) or 18080)
    args = ap.parse_args()

    # 先把 socket 建出来：启动脚本以 socket 存在判定就绪，
    # 不能因为后面扫目录/探测内核把首次启动拖成 "pending"
    server = None
    try:
        server = UnixHTTPServer(args.socket, Handler)
        os.chmod(args.socket, 0o660)
        log("listening on unix:%s" % args.socket)
    except OSError as exc:
        log("unix socket 绑定失败(%s)，回退 TCP 127.0.0.1:%d" % (exc, args.port))
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
        server.daemon_threads = True

    tasks.MANAGER.start()
    log("下载目录：%s" % settings.resolve_download_dir())
    # ffmpeg / 指纹伪装的能力自检放到后台：都要起子进程，不能卡住首屏
    threading.Thread(target=_warm_capabilities, name="vh-caps", daemon=True).start()
    # 内核探测放到后台：慢机器上 --version 可能卡二十秒，不能挡住服务响应
    if settings.start_probe_async():
        log("浏览器内核：自检已在后台进行，稍后可在页面查看结果")
        threading.Thread(target=_watch_probe, name="vh-probe-log", daemon=True).start()
    else:
        rep = settings.report_snapshot()
        if rep["ok"]:
            log("浏览器内核：可用（%s）" % (rep["version"] or "版本未知"))
        elif rep["state"] == "probing":
            log("浏览器内核：自检进行中…")
        else:
            log("浏览器内核：不可用（降级静态解析）— " + (rep["reason"] or "尚未检测"))

    def shutdown(*_):
        log("收到退出信号，正在停止…")
        tasks.MANAGER.shutdown()
        # shutdown 必须由别的线程调用，主线程阻塞在 serve_forever
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if os.path.exists(args.socket):
            try:
                os.unlink(args.socket)
            except OSError:
                pass
        log("已退出")


if __name__ == "__main__":
    main()
