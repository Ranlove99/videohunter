# -*- coding: utf-8 -*-
"""yt-dlp 子进程封装：探测、取元信息、下载并回报进度。

设计约束：
  * 不 import yt_dlp —— 走子进程，避免解析异常拖垮整个服务进程。
  * 每次调用都显式设置超时，进程能杀干净（独立进程组）。
  * 进度用自定义模板输出，格式固定，便于按行解析。
"""

import hashlib
import json
import os
import re
import signal
import subprocess
import threading
import time

import urllib.parse as _urlparse

from . import settings

PROBE_TIMEOUT = 45
INFO_TIMEOUT = 30

_PROGRESS_TPL = ("PROG|%(progress.downloaded_bytes)s|%(progress.total_bytes)s|"
                 "%(progress.total_bytes_estimate)s|%(progress.speed)s|%(progress.eta)s")

_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


class Cancelled(Exception):
    pass


def _base_args(cfg, impersonate=None):
    """公共参数。

    impersonate=True 时加 `--impersonate chrome`：靠包内 curl_cffi 复刻
    Chrome 的 TLS/JA3 指纹，专治「浏览器能开、程序 403 / 返回空壳」的站。
    运行时发现它反而添乱会自动去掉（见 _imp_hint）。
    """
    args = settings.ytdlp_cmd() + [
        "--no-warnings",
        "--no-colors",
        "--no-check-certificates",
        "--socket-timeout", "20",
        "--user-agent", settings.DEFAULT_UA,
    ]
    if impersonate is None:
        impersonate = bool(cfg.get("impersonate", True)) and settings.impersonate_ready()
    if impersonate:
        args += ["--impersonate", settings.IMPERSONATE_TARGET]
    ffmpeg = settings.ffmpeg_path()
    if ffmpeg:
        # 不传这个，fnOS 上没装 ffmpeg 的机器合流/HLS 收尾都会失败
        args += ["--ffmpeg-location", ffmpeg]
    if cfg.get("cookies_file") and os.path.isfile(cfg["cookies_file"]):
        args += ["--cookies", cfg["cookies_file"]]
    if cfg.get("proxy"):
        args += ["--proxy", cfg["proxy"]]
    return args


def _strip_impersonate(args):
    if "--impersonate" not in args:
        return list(args)
    out = list(args)
    i = out.index("--impersonate")
    del out[i:i + 2]
    return out


def _imp_hint(err):
    """错误信息像不像「指纹伪装不可用」。"""
    text = (err or "").lower()
    if "impersonat" not in text and "curl_cffi" not in text:
        return False
    return any(word in text for word in ("not available", "unavailable", "not supported",
                                         "unsupported", "no module", "could not", "error"))



def _referer_args(referer):
    """直链大多校验 Referer：带上来源页，否则 403。

    yt-dlp 对 HLS/DASH 分片同样会带上这些头。
    """
    referer = (referer or "").strip()
    if not referer.startswith(("http://", "https://")):
        return []
    # 注意：用 --referer + --add-header，--http-header 在这个版本里不存在
    args = ["--referer", referer]
    try:
        parts = _urlparse.urlsplit(referer)
        if parts.scheme and parts.netloc:
            args += ["--add-header", "Origin: %s://%s" % (parts.scheme, parts.netloc)]
    except ValueError:
        pass
    return args


def _run(args, timeout, cfg, cancel=None):
    """跑一次 yt-dlp，返回 CompletedProcess；超时/取消返回 None。

    用 Popen + 读线程，而不是 subprocess.run：扫描时点「停止」要能立刻
    把探测进程杀掉，否则用户得等满 45 秒才看到反应。
    """
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True,
                                env=settings.base_env(cfg, with_impersonate="--impersonate" in args),
                                stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        raise RuntimeError("无法启动下载引擎：%s" % exc)

    box = {}

    def drain():
        try:
            box["out"], box["err"] = proc.communicate()
        except Exception:  # 进程被杀时管道可能已断
            box.setdefault("out", "")
            box.setdefault("err", "")

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    deadline = time.time() + timeout
    while reader.is_alive():
        if cancel is not None and cancel.is_set():
            _terminate(proc)
            reader.join(timeout=2)
            return None
        if time.time() >= deadline:
            _terminate(proc)
            reader.join(timeout=2)
            return None
        reader.join(timeout=0.2)
    return subprocess.CompletedProcess(args, proc.returncode,
                                       box.get("out") or "", box.get("err") or "")



def _clean(text):
    return _ANSI.sub("", text or "").strip()


def _last_error(proc):
    for line in reversed(_clean(proc.stderr).splitlines()):
        line = line.strip()
        if line.startswith("ERROR:"):
            return line[6:].strip()[:300]
    tail = _clean(proc.stderr).splitlines()
    return (tail[-1][:300] if tail else "未知错误")


def _entry_from_info(info, fallback_url=""):
    """把 yt-dlp 的单条 info 归一化成前端用的结构。"""
    if not isinstance(info, dict):
        return None
    url = info.get("webpage_url") or info.get("url") or fallback_url
    if not url:
        return None
    title = info.get("title") or info.get("id") or url
    duration = info.get("duration")
    size = info.get("filesize") or info.get("filesize_approx")
    height = info.get("height")
    return {
        "url": url,
        "title": str(title)[:300],
        "duration": int(duration) if isinstance(duration, (int, float)) else None,
        "size": int(size) if isinstance(size, (int, float)) else None,
        "height": int(height) if isinstance(height, (int, float)) else None,
        "thumbnail": info.get("thumbnail") or "",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "extractor": info.get("extractor_key") or info.get("extractor") or "",
        "verified": True,
        "kind": "video",
    }


def probe(url, cfg, cancel=None):
    """判断 yt-dlp 能否解析该地址。

    返回 (ok, entries, message)：
      ok=True 且 entries 非空 → 这是一个可展开的视频页/播放列表
      ok=False → 交给静态解析或浏览器
    """
    args = _base_args(cfg) + [
        "--flat-playlist", "--dump-single-json", "--skip-download",
        "-I", "1:%d" % max(1, int(cfg.get("max_verify", 60))),
        "--extractor-retries", "1",
        url,
    ]
    proc = _run(args, PROBE_TIMEOUT, cfg, cancel)
    if proc is not None and proc.returncode != 0:
        err = _last_error(proc)
        if "--impersonate" in args and _imp_hint(err):
            # 指纹伪装反而被拦/库加载失败：去掉参数重试一次，别让整次扫描失败
            settings.disable_impersonate(err)
            proc = _run(_strip_impersonate(args), PROBE_TIMEOUT, cfg, cancel)
    if proc is None:
        return False, [], "探测已停止" if (cancel is not None and cancel.is_set()) else "探测超时"
    if proc.returncode != 0:
        return False, [], _last_error(proc)


    try:
        data = json.loads(_clean_stdout(proc.stdout))
    except ValueError:
        return False, [], "探测结果无法解析"

    if not isinstance(data, dict):
        return False, [], "非视频页面"

    entries = []
    if data.get("_type") in ("playlist", "multi_video") or "entries" in data:
        for item in data.get("entries") or []:
            if not isinstance(item, dict):
                continue
            # flat 模式下条目字段少，url 可能是 id 形式
            piece = _entry_from_info(item, fallback_url=url)
            if piece:
                # flat 模式的 url 才是真实目标：可能是媒体直链（generic 多视频），
                # 也可能是视频页（平台列表）。webpage_url 会把这些抹成同一个页面地址。
                direct = item.get("url")
                if isinstance(direct, str) and direct.startswith(("http://", "https://")):
                    piece["url"] = direct
                piece["title"] = str(item.get("title") or piece["title"])
                piece["duration"] = item.get("duration") if isinstance(
                    item.get("duration"), (int, float)) else None
                piece["verified"] = True
                entries.append(piece)
    else:
        one = _entry_from_info(data, fallback_url=url)
        if one:
            entries.append(one)

    if not entries:
        return False, [], "未发现视频条目"
    return True, entries, "ok"


def _clean_stdout(text):
    """stdout 里可能混入警告行，取第一个 '{' 到最后一个 '}' 之间的 JSON。"""
    text = _clean(text)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return text
    return text[start:end + 1]


def info(url, cfg, referer="", cancel=None):
    """取单条视频的元信息，失败的返回 None（调用方标为未验证）。"""
    args = _base_args(cfg) + _referer_args(referer) + [
        "--dump-single-json", "--skip-download", "--no-playlist",
        "--extractor-retries", "1",
        url,
    ]
    proc = _run(args, INFO_TIMEOUT, cfg, cancel)
    if proc is not None and proc.returncode != 0 and "--impersonate" in args:
        err = _last_error(proc)
        if _imp_hint(err):
            settings.disable_impersonate(err)
            proc = _run(_strip_impersonate(args), INFO_TIMEOUT, cfg, cancel)
    if proc is None or proc.returncode != 0:
        return None
    try:
        data = json.loads(_clean_stdout(proc.stdout))
    except ValueError:
        return None
    return _entry_from_info(data, fallback_url=url)


def guess_height(text):
    for tag, h in (("2160", 2160), ("1440", 1440), ("1080", 1080),
                   ("720", 720), ("480", 480), ("360", 360)):
        if tag in text:
            return h
    return None


def download(url, outdir, cfg, on_progress, cancel_event, referer="", marker=""):
    """下载单条视频。

    on_progress(dict) 会被高频调用（每 ~200ms 一次），调用方负责节流。
    返回 (ok, filepath, error)
    """
    ok, filepath, err = _download_once(url, outdir, cfg, on_progress, cancel_event,
                                       referer, marker, True)
    if ok or cancel_event.is_set() or not _imp_hint(err):
        return ok, filepath, err
    # 指纹伪装在这条地址上反而失败：全局关掉再试一次（普通指纹通常也能下）
    settings.disable_impersonate(err)
    return _download_once(url, outdir, cfg, on_progress, cancel_event,
                          referer, marker, False)


def _download_once(url, outdir, cfg, on_progress, cancel_event, referer, marker,
                   impersonate):
    # 文件名带 URL 指纹：不同地址撞同名（CDN 上 index.m3u8 之类）时不会互相覆盖，
    # 同一地址重复下载仍然命中同一个文件，断点续传照旧有效
    tag = hashlib.md5(url.encode("utf-8")).hexdigest()[:6]
    template = os.path.join(outdir, "%%(title).80s [%%(id)s-%s].%%(ext)s" % tag)
    args = _base_args(cfg, impersonate) + _referer_args(referer) + [
        "--newline",
        "--continue",
        "--no-overwrites",
        "--retries", "3",
        "--fragment-retries", "10",
        "--concurrent-fragments", "8",
        "--merge-output-format", "mp4",
        "--no-playlist",
        "-f", cfg.get("format") or "bv*+ba/b",
        "-o", template,
        "--progress-template", _PROGRESS_TPL,
        "--print", "after_move:FINAL|%(filepath)s",
        url,
    ]
    # 每条任务一个标记文件：并发下载时靠它精确认领自己的产物，
    # 不能拿"目录里最新的文件"去猜，那会把别的任务文件算到自己头上。
    if marker:
        args += ["--print-to-file", "after_move:FINAL|%(filepath)s", marker]

    try:
        proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            bufsize=1, env=settings.base_env(cfg, with_impersonate=impersonate),
            start_new_session=True)
    except OSError as exc:
        return False, "", "无法启动下载引擎：%s" % exc

    filepath = ""
    errors = []

    def reader():
        for raw in proc.stderr:
            line = _clean(raw)
            if line.startswith("ERROR:"):
                errors.append(line[6:].strip()[:300])

    err_thread = threading.Thread(target=reader, daemon=True)
    err_thread.start()

    last_emit = 0.0
    started = time.time()
    try:
        for raw in proc.stdout:
            if cancel_event.is_set():
                _terminate(proc)
                raise Cancelled()

            line = _clean(raw)
            if line.startswith("FINAL|"):
                filepath = line[6:].strip()
                continue
            if not line.startswith("PROG|"):
                continue

            now = time.time()
            if now - last_emit < 0.25:
                continue
            last_emit = now
            parts = line.split("|")
            if len(parts) < 7:
                continue
            got = _num(parts[1])
            total = _num(parts[2]) or _num(parts[3])
            speed = _num(parts[4])
            eta = _num(parts[5])
            pct = 0.0
            if total and got:
                pct = min(99.9, got * 100.0 / total)
            on_progress({
                "percent": round(pct, 1),
                "downloaded": got,
                "total": total,
                "speed": speed,
                "eta": eta,
            })
        proc.wait(timeout=30)
    except Cancelled:
        err_thread.join(timeout=2)
        return False, filepath, "已取消"
    except subprocess.TimeoutExpired:
        _terminate(proc)
        return False, filepath, "进程未正常退出"
    finally:
        if proc.poll() is None:
            _terminate(proc)

    err_thread.join(timeout=2)

    if cancel_event.is_set():
        return False, filepath, "已取消"
    if marker:
        try:
            if os.path.isfile(marker):
                raw = open(marker, "r", encoding="utf-8", errors="replace").read()
                if "FINAL|" in raw:
                    hit = raw.split("FINAL|", 1)[1].splitlines()[0].strip()
                    if hit:
                        filepath = hit
        except OSError:
            pass
        finally:
            try:
                os.remove(marker)
            except OSError:
                pass

    if proc.returncode != 0:
        # 后处理失败（如 HLS 的 FixupM3u8 缺 ffmpeg 能力）但文件已经下全了：
        # 文件本身能播，按成功处理并留一句提示，别让用户白等一场。
        salvaged = _newest_file(outdir, since=started - 2)
        if salvaged and os.path.getsize(salvaged) > 64 * 1024:
            _clean_temp(outdir, started)
            return True, salvaged, ""
        return False, filepath, (errors[-1] if errors else "下载失败(退出码 %s)" % proc.returncode)
    if not filepath or not os.path.exists(filepath):
        # after_move 没触发时的兜底：只有目录里恰好新增了一个成品才认领
        fresh = _new_files(outdir, started - 2)
        filepath = fresh[0] if len(fresh) == 1 else ""
    if filepath and os.path.exists(filepath):
        # --no-overwrites 撞上同名旧文件时会直接跳过，别把旧文件当成这次的成功
        try:
            if os.path.getmtime(filepath) < started - 2:
                return False, filepath, "目录里已有同名文件，已跳过；如需重新下载请先改名或删除"
        except OSError:
            pass
    return True, filepath, ""


def _new_files(directory, since=0):
    """本次下载新增的成品文件（排除临时文件与空文件）。"""
    out = []
    try:
        for name in os.listdir(directory):
            path = os.path.join(directory, name)
            if not os.path.isfile(path) or name.endswith((".part", ".ytdl", ".tmp")):
                continue
            if ".temp." in name:
                continue
            try:
                if os.path.getsize(path) <= 0 or os.path.getmtime(path) < since:
                    continue
            except OSError:
                continue
            out.append(path)
    except OSError:
        return []
    return out


def _clean_temp(directory, since=0):
    """清理本次下载残留的 .temp / .part 文件。"""
    try:
        for name in os.listdir(directory):
            path = os.path.join(directory, name)
            if not os.path.isfile(path):
                continue
            if not (".temp." in name or name.endswith((".part", ".ytdl"))):
                continue
            try:
                if since and os.path.getmtime(path) < since:
                    continue
                os.remove(path)
            except OSError:
                pass
    except OSError:
        pass


def _num(text):
    try:
        val = float(text)
    except (TypeError, ValueError):
        return 0
    return int(val) if val > 0 else 0


def _terminate(proc):
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except OSError:
        try:
            proc.terminate()
        except OSError:
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            pass


def _newest_file(directory, since=0):
    """目录里最新的成品文件；since 用于排除上一次下载留下的旧文件。"""
    newest, newest_mtime = "", 0
    try:
        for name in os.listdir(directory):
            path = os.path.join(directory, name)
            if not os.path.isfile(path) or name.endswith((".part", ".ytdl", ".tmp")):
                continue
            if ".temp." in name or ".part-Frag" in name:
                continue
            try:
                if os.path.getsize(path) <= 0:
                    continue
            except OSError:
                continue
            mtime = os.path.getmtime(path)
            if mtime < since:
                continue
            if mtime > newest_mtime:
                newest, newest_mtime = path, mtime
    except OSError:
        return ""
    return newest
