# -*- coding: utf-8 -*-
"""扫描任务与下载队列。

下载用固定线程池 + 可调并发闸门（settings.workers），
状态落盘到 $TRIM_PKGVAR/state.json，服务重启后把没下完的重新排队。
"""

import json
import os
import tempfile
import threading
import time
import uuid
from collections import deque

from . import crawler, engine, settings

MAX_WORKERS = 8
KEEP_FINISHED = 200


def _new_id(prefix):
    return "%s%s" % (prefix, uuid.uuid4().hex[:10])


class ScanJob(object):
    def __init__(self, url):
        self.id = _new_id("s")
        self.url = url
        self.status = "pending"      # pending | running | done | error | cancelled
        self.stage = "queued"
        self.message = "排队中…"
        self.result = None
        self.error = ""
        self.started = time.time()
        self.finished = 0
        self.cancel = threading.Event()
        self.pause = threading.Event()

    def emit(self, stage, payload):
        self.stage = stage
        if payload.get("message"):
            self.message = payload["message"]

    def snapshot(self):
        return {
            "id": self.id,
            "url": self.url,
            "status": self.status,
            "stage": self.stage,
            "paused": self.pause.is_set(),
            "message": self.message,
            "error": self.error,
            "elapsed": round(time.time() - self.started, 1),
            "result": self.result,
        }


class Task(object):
    def __init__(self, url, title="", referer=""):
        self.id = _new_id("d")
        self.url = url
        self.title = title or url
        self.referer = referer or ""   # 来源页，下载时作为 Referer 带上
        self.status = "queued"       # queued | running | done | error | cancelled
        self.percent = 0.0
        self.downloaded = 0
        self.total = 0
        self.speed = 0
        self.eta = 0
        self.filepath = ""
        self.error = ""
        self.created = time.time()
        self.finished = 0
        self.cancel = threading.Event()

    def snapshot(self):
        return {
            "id": self.id, "url": self.url, "title": self.title,
            "status": self.status, "percent": self.percent,
            "downloaded": self.downloaded, "total": self.total,
            "speed": self.speed, "eta": self.eta,
            "filepath": self.filepath, "error": self.error,
            "referer": self.referer,
            "created": self.created, "finished": self.finished,
        }


class Manager(object):
    def __init__(self):
        self._lock = threading.RLock()
        self._scans = {}
        self._tasks = {}
        self._order = []
        self._queue = deque()
        self._cond = threading.Condition(self._lock)
        self._running = 0
        self._started = False

    # ---------------------------------------------------------- 生命周期

    def start(self):
        with self._lock:
            if self._started:
                return
            self._started = True
        self._restore()
        for i in range(MAX_WORKERS):
            t = threading.Thread(target=self._worker, name="dl-%d" % i, daemon=True)
            t.start()

    def shutdown(self):
        with self._cond:
            for task in self._tasks.values():
                if task.status in ("running", "queued"):
                    task.cancel.set()
            self._cond.notify_all()
        with self._lock:
            for job in self._scans.values():
                if not job.finished:
                    job.pause.clear()
                    job.cancel.set()
        self._persist()

    # ---------------------------------------------------------- 扫描

    def start_scan(self, url):
        job = ScanJob(url)
        with self._lock:
            self._scans[job.id] = job
            self._trim_scans()
        threading.Thread(target=self._run_scan, args=(job,), daemon=True).start()
        return job.id

    def _run_scan(self, job):
        job.status = "running"
        cfg = settings.load()
        try:
            job.result = crawler.scan(job.url, cfg, job.emit, job.cancel, job.pause)
            if job.cancel.is_set():
                job.status = "cancelled"
                found = len((job.result or {}).get("entries") or [])
                job.message = ("已停止：保留已扫到的 %d 个可下载视频" % found if found
                               else "已停止：这一轮还没扫到视频")
            else:
                job.status = "done"
        except Exception as exc:
            job.status = "error"
            job.error = str(exc)[:300]
            job.message = "扫描失败"
        finally:
            job.pause.clear()
            job.finished = time.time()

    def get_scan(self, job_id):
        with self._lock:
            job = self._scans.get(job_id)
            return job.snapshot() if job else None

    def cancel_scan(self, job_id):
        with self._lock:
            job = self._scans.get(job_id)
        if not job:
            return False
        job.cancel.set()
        # 暂停中直接停止：唤醒等待点，让扫描线程立刻收尾
        job.pause.clear()
        return True

    def pause_scan(self, job_id, paused=True):
        """暂停/继续扫描。暂停只是挂起，已扫到的结果一直保留。"""
        with self._lock:
            job = self._scans.get(job_id)
        if not job or job.finished or job.cancel.is_set():
            return False
        if paused:
            job.pause.set()
            job.message = "已暂停，点「继续」接着扫描"
        else:
            job.pause.clear()
            job.message = "继续扫描…"
        return True

    def _trim_scans(self):
        if len(self._scans) <= 20:
            return
        finished = sorted((j for j in self._scans.values() if j.finished),
                          key=lambda j: j.finished)
        for job in finished[:len(self._scans) - 20]:
            self._scans.pop(job.id, None)

    # ---------------------------------------------------------- 下载

    def enqueue(self, urls):
        created = []
        with self._cond:
            for item in urls:
                referer = ""
                if isinstance(item, dict):
                    url = (item.get("url") or "").strip()
                    title = (item.get("title") or "").strip()
                    referer = (item.get("referer") or "").strip()
                else:
                    url, title = str(item).strip(), ""
                if not url.startswith(("http://", "https://")):
                    continue
                # 同一地址只排一次，避免手抖重复点
                dup = next((t for t in self._tasks.values()
                            if t.url == url and t.status in ("queued", "running")), None)
                if dup:
                    created.append(dup.id)
                    continue
                task = Task(url, title, referer)
                self._tasks[task.id] = task
                self._order.append(task.id)
                self._queue.append(task.id)
                created.append(task.id)
            self._cond.notify_all()
        self._persist()
        return created

    def list_tasks(self):
        with self._lock:
            items = [self._tasks[i].snapshot() for i in self._order if i in self._tasks]
        return items[-200:]

    def cancel_task(self, task_id):
        with self._cond:
            task = self._tasks.get(task_id)
            if not task:
                return False
            task.cancel.set()
            if task.status == "queued":
                task.status = "cancelled"
                task.finished = time.time()
                try:
                    self._queue.remove(task_id)
                except ValueError:
                    pass
            self._cond.notify_all()
        self._persist()
        return True

    def retry_task(self, task_id):
        with self._cond:
            task = self._tasks.get(task_id)
            if not task or task.status not in ("error", "cancelled"):
                return False
            task.status = "queued"
            task.percent = 0.0
            task.error = ""
            task.cancel = threading.Event()
            if task_id not in self._queue:
                self._queue.append(task_id)
            self._cond.notify_all()
        self._persist()
        return True

    def clear_finished(self):
        with self._lock:
            keep = []
            for tid in self._order:
                task = self._tasks.get(tid)
                if not task:
                    continue
                if task.status in ("done", "error", "cancelled"):
                    self._tasks.pop(tid, None)
                else:
                    keep.append(tid)
            self._order = keep
        self._persist()

    # ---------------------------------------------------------- 工作线程

    def _worker(self):
        while True:
            with self._cond:
                while True:
                    if not self._queue:
                        self._cond.wait(timeout=2)
                        continue
                    limit = max(1, min(MAX_WORKERS, int(settings.load().get("workers", 2))))
                    if self._running < limit:
                        break
                    self._cond.wait(timeout=2)
                task_id = self._queue.popleft()
                task = self._tasks.get(task_id)
                if not task or task.status != "queued":
                    continue
                self._running += 1
                task.status = "running"
            try:
                self._run_task(task)
            finally:
                with self._cond:
                    self._running -= 1
                    self._cond.notify_all()

    def _run_task(self, task):
        cfg = settings.load()
        outdir = settings.resolve_download_dir(cfg)
        if not outdir:
            task.status = "error"
            task.error = "没有可写的下载目录"
            task.finished = time.time()
            return

        def on_progress(p):
            task.percent = p["percent"]
            task.downloaded = p["downloaded"]
            task.total = p["total"]
            task.speed = p["speed"]
            task.eta = p["eta"]

        marker = os.path.join(tempfile.gettempdir(), "vh-%s.txt" % task.id)
        ok, filepath, err = engine.download(task.url, outdir, cfg, on_progress,
                                            task.cancel, referer=task.referer,
                                            marker=marker)
        task.finished = time.time()
        if ok:
            task.status = "done"
            task.percent = 100.0
            task.filepath = filepath
            task.speed = 0
            task.eta = 0
            try:
                task.total = os.path.getsize(filepath) or task.total
                task.downloaded = task.total
            except OSError:
                pass
            if not task.title or task.title == task.url:
                task.title = os.path.basename(filepath)
        elif task.cancel.is_set():
            task.status = "cancelled"
            task.error = ""
        else:
            task.status = "error"
            task.error = err or "下载失败"
        self._persist()

    # ---------------------------------------------------------- 持久化

    def _state_file(self):
        return os.path.join(settings.var_dir(), "state.json")

    def _persist(self):
        with self._lock:
            running = [t.snapshot() for t in self._tasks.values()
                       if t.status in ("queued", "running")]
            done = [t.snapshot() for t in self._tasks.values()
                    if t.status in ("done", "error", "cancelled")]
            done = sorted(done, key=lambda t: t["finished"], reverse=True)[:KEEP_FINISHED]
            payload = {"version": 1, "tasks": running + done}
        path = self._state_file()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError:
            pass

    def _restore(self):
        path = self._state_file()
        if not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError):
            return

        with self._cond:
            for item in payload.get("tasks") or []:
                if not isinstance(item, dict) or not item.get("url"):
                    continue
                task = Task(item["url"], item.get("title") or "", item.get("referer") or "")
                task.id = item.get("id") or task.id
                status = item.get("status")
                task.referer = item.get("referer") or ""
                if status in ("queued", "running"):
                    # 上次没下完：重新排队，断点续传会接上
                    task.status = "queued"
                    task.percent = float(item.get("percent") or 0)
                    self._queue.append(task.id)
                else:
                    task.status = status if status in ("done", "error", "cancelled") else "error"
                    task.percent = float(item.get("percent") or 0)
                    task.error = item.get("error") or ""
                    task.filepath = item.get("filepath") or ""
                    task.referer = item.get("referer") or ""
                    task.finished = item.get("finished") or 0
                self._tasks[task.id] = task
                self._order.append(task.id)
            self._cond.notify_all()


MANAGER = Manager()
