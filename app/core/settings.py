# -*- coding: utf-8 -*-
"""配置读写与运行环境探测。

配置落在 $TRIM_PKGETC/app.json（安装后指向 /vol{n}/@appconf/videohunter），
下载目录默认取 fnOS 的共享目录，保证用户在文件管理器里能直接看到成品。
"""

import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

APPNAME = "videohunter"
PREFIX = "/app/" + APPNAME

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

DEFAULTS = {
    "download_dir": "",          # 空 = 自动解析出共享目录
    "workers": 2,                # 同时下载数
    "format": "bv*+ba/b",        # 画质选择
    "cookies_file": "",          # Netscape 格式 cookie 文件路径
    "use_browser": True,         # 是否用无头浏览器渲染页面
    "browser_timeout": 25,       # 单页渲染超时（秒）
    "crawl_depth": 2,            # 同域爬取深度
    "max_pages": 200,            # 单次扫描最大页数
    "rate_limit_ms": 300,        # 请求间隔
    "max_verify": 60,            # 单次最多验真多少条
    "proxy": "",
    "impersonate": True,         # 伪装 Chrome 的 TLS 指纹（需包内 curl_cffi）
}

FIELDS = tuple(DEFAULTS)


def log(msg):
    """统一日志：写 stderr，由 cmd/main 重定向进 app.log。"""
    sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    sys.stderr.flush()


def etc_dir():
    return os.environ.get("TRIM_PKGETC") or "/var/apps/%s/etc" % APPNAME


def var_dir():
    return os.environ.get("TRIM_PKGVAR") or "/var/apps/%s/var" % APPNAME


def app_dir():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def config_path():
    return os.path.join(etc_dir(), "app.json")


def _ensure_dir(path):
    try:
        os.makedirs(path, exist_ok=True)
        return True
    except OSError:
        return False


def load():
    cfg = dict(DEFAULTS)
    path = config_path()
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                saved = json.load(fh)
            if isinstance(saved, dict):
                for k in FIELDS:
                    if k in saved:
                        cfg[k] = saved[k]
        except (OSError, ValueError) as exc:
            sys.stderr.write("读取配置失败(%s)，使用默认值\n" % exc)
    return cfg


def save(cfg):
    """只接受已知字段，做一次类型兜底，避免前端乱传把配置写坏。"""
    clean = {}
    for k in FIELDS:
        if k not in cfg:
            continue
        val = cfg[k]
        default = DEFAULTS[k]
        if isinstance(default, bool):
            clean[k] = bool(val)
        elif isinstance(default, int):
            try:
                clean[k] = max(0, int(val))
            except (TypeError, ValueError):
                clean[k] = default
        else:
            clean[k] = str(val)
    # 并发数收敛到 1~8，避免用户填 0 或 50
    clean["workers"] = min(8, max(1, int(clean.get("workers", 2))))
    clean["crawl_depth"] = min(4, max(0, int(clean.get("crawl_depth", 2))))
    _ensure_dir(etc_dir())
    tmp = config_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(clean, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, config_path())
    return clean


def _share_root():
    """fnOS 共享目录。TRIM_DATA_SHARE_PATHS 形如 /vol1/1000/a:/vol1/1000/b。"""
    raw = os.environ.get("TRIM_DATA_SHARE_PATHS") or ""
    for item in raw.split(":"):
        item = item.strip()
        if item and os.path.isdir(item):
            return item
    # 兜底：data-share 在应用目录同级 share/ 下
    fallback = os.path.join(os.path.dirname(app_dir()), "share", APPNAME, "data")
    if os.path.isdir(fallback):
        return fallback
    return ""


_SHARE_CACHE = {"done": False, "items": []}


def share_candidates(limit=40):
    """列出用户可见的共享文件夹，供设置里填空时选。

    fnOS 把用户可见的目录放在 /vol{n}/{uid}/ 下；@ 开头的是系统私有目录
    （@appshare 之类），在文件管理器里看不到，所以不列。读不到就返回空。
    """
    if _SHARE_CACHE["done"]:
        return _SHARE_CACHE["items"]
    out = []
    try:
        vols = sorted(n for n in os.listdir("/")
                      if n.startswith("vol") and os.path.isdir("/" + n))
    except OSError:
        vols = []
    for name in vols:
        base = "/" + name
        try:
            subs = sorted(os.listdir(base))
        except OSError:
            continue
        for sub in subs:
            if sub.startswith("@") or sub.startswith("."):
                continue
            path = os.path.join(base, sub)
            if not os.path.isdir(path):
                continue
            try:
                inner = sorted(d for d in os.listdir(path)
                               if not d.startswith("@") and not d.startswith(".")
                               and os.path.isdir(os.path.join(path, d)))
            except OSError:
                inner = []
            if inner:
                out.extend(os.path.join(path, d) for d in inner)
            elif not sub.isdigit():
                out.append(path)
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break
    _SHARE_CACHE.update(done=True, items=out[:limit])
    return _SHARE_CACHE["items"]


def resolve_download_dir(cfg=None):
    """返回可写的下载根目录，必要时逐级降级。"""
    cfg = cfg or load()
    candidates = []
    if cfg.get("download_dir"):
        candidates.append(cfg["download_dir"])
    share = _share_root()
    if share:
        candidates.append(os.path.join(share, "视频猎手"))
    candidates.append(os.path.join(var_dir(), "downloads"))

    for path in candidates:
        if _ensure_dir(path) and os.access(path, os.W_OK):
            return path
    return var_dir()


def browser_path():
    """包内 Chromium 所在目录，供 Playwright 通过环境变量定位。"""
    return os.path.join(app_dir(), "vendor", "browsers")


def bundled_lib_dir():
    """包内自带的 Chromium 依赖库。

    fnOS 是精简系统，常见缺 libnss3 / libgbm1 / libatk 之类。
    把这些库一起打包，启动内核时优先用自带的，避免依赖系统包。
    """
    return os.path.join(browser_path(), "lib")


_REPORT = {"done": False, "ok": False, "exe": "", "version": "",
           "reason": "", "missing": [], "state": "idle"}

_PROBE_LOCK = threading.Lock()
_PROBE_DONE = threading.Event()
_PROBE_THREAD = None
_PROBE_STARTING = {"v": False}

# 内核启动超时：真机上极慢的机器给 10 秒，超时就换真实渲染再验一次
_VERSION_TIMEOUT = 20
_RENDER_TIMEOUT = 30

_VERSION_ARGS = ("--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
                 "--disable-crash-reporter", "--no-first-run", "--version")

# 内核自称有三种：Google Chrome / Chromium / HeadlessShell，
# 且中间可能夹着 "for Testing" 之类的词，所以名称与版本号之间允许若干非数字字符
_VER_RE = re.compile(r"(?:HeadlessShell|Chromium|Chrome)[^\d]{0,24}(\d+(?:\.\d+)+)", re.I)
_VER_BARE = re.compile(r"\b\d+\.\d+\.\d+(?:\.\d+)?\b")

# 最近一次真实渲染的结果，供界面排查
_RUNTIME = {"last_error": "", "last_ok": ""}


def _find_chrome():
    """在包内 browsers 目录里找出 chromium 可执行文件。"""
    root = browser_path()
    if not os.path.isdir(root):
        return ""
    for base, _dirs, files in os.walk(root):
        # 内核目录名随平台不同：x86 是 chrome-linux64，ARM 是 chrome-linux-arm64
        if os.path.basename(base) not in ("chrome-linux64", "chrome-linux", "chrome-linux-arm64"):
            continue
        for name in ("chrome", "headless_shell"):
            candidate = os.path.join(base, name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return ""


def _ldd_missing(exe):
    """列出 ELF 缺失的动态库名。ldd 不存在则返回空。"""
    try:
        proc = subprocess.run(["ldd", exe], capture_output=True, timeout=20,
                              env=base_env())
    except (OSError, subprocess.SubprocessError):
        return []
    out = (proc.stdout or b"").decode("utf-8", "replace")
    missing = []
    for line in out.splitlines():
        if "not found" in line:
            name = line.strip().split(" ")[0]
            if name and name not in missing:
                missing.append(name)
    return missing


def _parse_version(text):
    m = _VER_RE.search(text or "")
    if m:
        return m.group(1)
    m = _VER_BARE.search(text or "")
    return m.group(0) if m else ""


def _probe_render(exe):
    """跑一次真实无头渲染，确认内核能出页面。

    有些自编译/裁剪内核 --version 不打印版本号，只看返回码会误判。
    """
    for flag in ("--headless=new", "--headless"):
        args = [exe, flag, "--no-sandbox", "--disable-gpu",
                "--disable-dev-shm-usage", "--disable-crash-reporter",
                "--no-first-run", "--virtual-time-budget=5000",
                "--dump-dom", "data:text/html,<b>vh-ok</b>"]
        try:
            proc = subprocess.run(args, capture_output=True, timeout=_RENDER_TIMEOUT,
                                  env=base_env(), stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return False, "渲染探测超时（%d 秒无响应），内核可能被安全策略拦截" % _RENDER_TIMEOUT
        except (OSError, subprocess.SubprocessError) as exc:
            return False, "渲染探测异常：%s" % exc
        if "vh-ok" in (proc.stdout or b"").decode("utf-8", "replace"):
            return True, ""
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()
        last = err.splitlines()[-1][:200] if err else ""
        low = last.lower()
        if "unknown" in low or "unrecognized" in low:
            continue  # 这个内核不认 --headless=new，换老写法再试
        return False, "渲染探测失败（退出码 %s）%s" % (
            proc.returncode, "：" + last if last else "")
    return False, "渲染探测失败：不支持 headless 参数"


def _probe_version(exe):
    """读内核自称的版本号，返回 (版本号, 失败原因)。"""
    try:
        proc = subprocess.run([exe] + list(_VERSION_ARGS), capture_output=True,
                              timeout=_VERSION_TIMEOUT, env=base_env(),
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return "", "内核启动超时（%d 秒无响应）" % _VERSION_TIMEOUT
    except (OSError, subprocess.SubprocessError) as exc:
        return "", "内核启动异常：%s" % exc

    out = (proc.stdout or b"").decode("utf-8", "replace")
    err = (proc.stderr or b"").decode("utf-8", "replace")
    ver = _parse_version(out) or _parse_version(err)
    if proc.returncode == 0 and ver:
        return ver, ""
    tail = (err.strip().splitlines() or [""])[-1][:180]
    return ver, "内核无版本输出（退出码 %s）%s" % (
        proc.returncode, "：" + tail if tail else "")


def _do_probe():
    """真正跑一遍探测，结果写进 _REPORT（调用方负责加锁与收尾）。"""
    _REPORT.update(ok=False, version="", reason="", missing=[], exe="")

    exe = _find_chrome()
    if not exe:
        _REPORT["reason"] = "包内未找到 Chromium 可执行文件"
        return
    _REPORT["exe"] = exe

    try:
        import playwright  # noqa: F401
    except ImportError as exc:
        _REPORT["reason"] = "Playwright 运行库缺失：%s" % exc
        return

    ver, why = _probe_version(exe)
    if ver:
        _REPORT["ok"] = True
        _REPORT["version"] = ver
        return

    # 拿不到版本号（超时/无输出）：直接试真实渲染，能出页面就算可用
    ok, detail = _probe_render(exe)
    if ok:
        _REPORT["ok"] = True
        _REPORT["version"] = "未知"
        return

    missing = _ldd_missing(exe)
    if missing:
        _REPORT["missing"] = missing
        _REPORT["reason"] = "缺少动态库 %s；%s" % (", ".join(missing[:6]), detail or why)
    else:
        _REPORT["reason"] = detail or why


def browser_report(force=False):
    """探测内核能否启动，失败时给出可读原因。结果进程内缓存。"""
    if _REPORT["state"] == "probing":
        # 已有探测在跑：等它出结果，最多等 40 秒
        _PROBE_DONE.wait(timeout=40)
        return _REPORT
    if _REPORT["done"] and not force:
        return _REPORT

    with _PROBE_LOCK:
        if _REPORT["done"] and not force:
            return _REPORT
        _PROBE_DONE.clear()
        _REPORT["state"] = "probing"
        _REPORT["done"] = False
        try:
            _do_probe()
        finally:
            _REPORT["done"] = True
            _REPORT["state"] = "ok" if _REPORT["ok"] else "fail"
            _REPORT["at"] = time.time()
            _PROBE_DONE.set()
    return _REPORT


def start_probe_async(force=False):
    """后台探测内核，避免拖慢服务启动。返回 True 表示已排上队。"""
    global _PROBE_THREAD
    if _PROBE_STARTING["v"] or _REPORT["state"] == "probing":
        return False
    if _REPORT["done"] and not force:
        return False
    if force and (time.time() - _REPORT.get("at", 0)) < 5:
        return False  # 防抖：刚测过就不再重复跑

    def run():
        try:
            browser_report(force=True)
        finally:
            _PROBE_STARTING["v"] = False

    _PROBE_STARTING["v"] = True
    _PROBE_THREAD = threading.Thread(target=run, name="vh-browser-probe", daemon=True)
    _PROBE_THREAD.start()
    return True


def browser_available():
    return browser_report()["ok"]


def browser_ready(wait=20):
    """扫描前用：还没探测就顺手起一个，最多等 wait 秒，超时按不可用降级。"""
    if _REPORT["state"] == "idle" and not _REPORT["done"]:
        start_probe_async()
    if _REPORT["state"] == "probing":
        _PROBE_DONE.wait(timeout=wait)
    return _REPORT["ok"]


def report_snapshot():
    """不触发也不等待探测，只取当前状态（给 health 用）。"""
    return dict(_REPORT)


def browser_version():
    return browser_report()["version"]


def note_render(ok, detail=""):
    """记录最近一次真实渲染结果，供健康接口展示。"""
    if ok:
        _RUNTIME["last_ok"] = time.strftime("%H:%M:%S")
        _RUNTIME["last_error"] = ""
    else:
        _RUNTIME["last_error"] = str(detail)[:400]


def render_status():
    return dict(_RUNTIME)


def chrome_exe():
    return _REPORT["exe"] or _find_chrome()


def ytdlp_cmd():
    """内置 yt-dlp 的调用方式：-m yt_dlp + PYTHONPATH 指向 vendor。"""
    return [sys.executable, "-m", "yt_dlp"]


def vendor_path():
    return os.path.join(app_dir(), "vendor")


def bin_dir():
    """包内自带的可执行文件（ffmpeg 等）。"""
    return os.path.join(app_dir(), "vendor", "bin")


def ffmpeg_path():
    for name in ("ffmpeg", "ffmpeg.exe"):
        path = os.path.join(bin_dir(), name)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return ""


_FF = {"checked": False, "ok": False, "version": "", "path": "", "reason": ""}


def ffmpeg_ready():
    """包内静态 ffmpeg 是否可用：音视频合流、HLS 修复都要靠它。

    fnOS 精简系统普遍没装 ffmpeg，缺了它 `bv*+ba` 合流会失败、
    m3u8 收尾也会报错，用户只看到一句「下载失败」。
    """
    if _FF["checked"]:
        return _FF["ok"]
    _FF["checked"] = True
    exe = ffmpeg_path()
    _FF["path"] = exe
    if not exe:
        _FF["reason"] = "包内未找到 ffmpeg"
        return False
    try:
        # 静态二进制不依赖系统库，清掉 LD_LIBRARY_PATH 反而更稳
        proc = subprocess.run([exe, "-version"], capture_output=True, timeout=20,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        _FF["reason"] = "ffmpeg 无法运行：%s" % exc
        return False
    head = (proc.stdout or b"").decode("utf-8", "replace").splitlines()
    if proc.returncode == 0 and head:
        m = re.search(r"ffmpeg version (\S+)", head[0])
        _FF["ok"] = True
        _FF["version"] = m.group(1) if m else head[0][:60]
        _FF["reason"] = ""
    else:
        _FF["reason"] = "ffmpeg -version 退出码 %s" % proc.returncode
    return _FF["ok"]


def ffmpeg_report():
    ffmpeg_ready()
    return {"ok": _FF["ok"], "version": _FF["version"], "reason": _FF["reason"]}


IMPERSONATE_TARGET = "chrome"

# 指纹伪装能力探测结果（一次进程内缓存）
_IMP = {"checked": False, "ok": False, "reason": ""}
_IMP_OFF = {"v": False, "reason": ""}


def impersonate_dir():
    """curl_cffi 单独放一个目录。

    不能直接丢进 vendor 根：cffi 是通用底层库，放在根上会顶掉系统里
    其它包依赖的 cffi（版本不一致直接 import 失败），只在 yt-dlp 子进程里按需加路径。
    """
    return os.path.join(vendor_path(), "impersonate")


def impersonate_env():
    env = base_env()
    extra = impersonate_dir()
    if os.path.isdir(extra):
        env["PYTHONPATH"] = extra + ":" + env.get("PYTHONPATH", "")
    return env


def impersonate_ready(force=False):
    """yt-dlp 能否 `--impersonate chrome`：需要包内 curl_cffi 且目标列表非空。

    只在子进程里验证，不污染本进程的 sys.path。
    """
    if _IMP_OFF["v"]:
        return False
    if _IMP["checked"] and not force:
        return _IMP["ok"]
    _IMP["checked"] = True
    _IMP["ok"] = False
    if not os.path.isdir(impersonate_dir()):
        _IMP["reason"] = "包内未内置 curl_cffi"
        return False
    try:
        proc = subprocess.run(ytdlp_cmd() + ["--list-impersonate-targets"],
                              capture_output=True, timeout=40,
                              stdin=subprocess.DEVNULL, env=impersonate_env())
    except (OSError, subprocess.SubprocessError) as exc:
        _IMP["reason"] = "指纹库自检失败：%s" % exc
        return False
    text = ((proc.stdout or b"") + (proc.stderr or b"")).decode("utf-8", "replace")
    if proc.returncode != 0:
        tail = (text.strip().splitlines() or [""])[-1][:160]
        _IMP["reason"] = "指纹库不可用：%s" % (tail or "退出码 %s" % proc.returncode)
        return False
    if "chrome" not in text.lower():
        _IMP["reason"] = "指纹目标列表里没有 chrome"
        return False
    _IMP["ok"] = True
    _IMP["reason"] = ""
    return True



def impersonate_report():
    return {"ok": _IMP["ok"], "reason": _IMP["reason"], "disabled": _IMP_OFF["v"]}


def disable_impersonate(reason=""):
    """运行时发现伪装指纹把请求搞挂了：全局关掉，后续都走普通指纹。"""
    _IMP_OFF["v"] = True
    _IMP_OFF["reason"] = str(reason)[:200]
    _IMP["ok"] = False
    _IMP["reason"] = _IMP_OFF["reason"]
    log("[engine] 已停用 TLS 指纹伪装：%s" % _IMP_OFF["reason"])



_LIB_NEED = {"checked": False, "need": False}


def needs_bundled_libs():
    """系统缺库时才启用包内库。

    实测：库里齐全的机器上，把整套 Debian 库塞进 LD_LIBRARY_PATH 会让
    Chromium 渲染进程直接崩（版本交叉），所以必须按需启用。
    """
    if _LIB_NEED["checked"]:
        return _LIB_NEED["need"]
    _LIB_NEED["checked"] = True
    exe = _find_chrome()
    lib = bundled_lib_dir()
    if not exe or not os.path.isdir(lib):
        return False
    try:
        proc = subprocess.run(["ldd", exe], capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    _LIB_NEED["need"] = b"not found" in (proc.stdout or b"")
    return _LIB_NEED["need"]


def disable_bundled_libs():
    """渲染失败时回退：改回只用系统库。"""
    _LIB_NEED["checked"] = True
    _LIB_NEED["need"] = False


def proxy_url(cfg=None):
    """用户填的代理地址，规范成 URL；返回空串表示直连。

    支持只填 192.168.1.2:7890 这种写法，自动补 http://。
    """
    if cfg is None:
        cfg = load()
    raw = (cfg.get("proxy") or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "http://" + raw
    return raw


def apply_proxy_env(env, cfg=None):
    """把代理写进子进程环境：yt-dlp、Chromium、Playwright 都认这套变量。"""
    p = proxy_url(cfg)
    keys = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
            "http_proxy", "https_proxy", "all_proxy")
    if not p:
        for k in keys:
            env.pop(k, None)
        return env
    for k in keys:
        env[k] = p
    env.setdefault("NO_PROXY", "127.0.0.1,localhost,::1")
    env.setdefault("no_proxy", env["NO_PROXY"])
    return env


def _doh_lookup(host, timeout=5):
    """用公共 DoH 查 A 记录，用来对比系统 DNS 是否被污染。"""
    out = []
    for endpoint in ("https://dns.alidns.com/resolve?name=%s&type=A",
                     "https://doh.pub/dns-query?name=%s&type=A"):
        try:
            req = urllib.request.Request(
                endpoint % urllib.parse.quote(host),
                headers={"Accept": "application/dns-json",
                         "User-Agent": DEFAULT_UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read(200000).decode("utf-8", "replace"))
            for ans in data.get("Answer") or []:
                if ans.get("type") == 1 and ans.get("data"):
                    ip = ans["data"]
                    if ip not in out:
                        out.append(ip)
        except Exception:
            continue
        if out:
            break
    return out


def diagnose(url, timeout=6):
    """诊断“为什么连不上”，返回可读结论。

    输出示例：
      系统 DNS 解析到 108.160.167.167（与公共 DNS 的 13.35.190.126 不一致），
      且 TLS 握手被重置 —— 该域名在出口被阻断，必须走代理。
    """
    parts = urllib.parse.urlsplit(url if "://" in url else "http://" + url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    rep = {"host": host, "port": port, "dns": [], "doh": [], "tcp": False,
           "tls": "", "blocked": False, "verdict": ""}
    if not host:
        rep["verdict"] = "地址里没有域名"
        return rep

    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        for fam, _t, _p, _c, sa in infos:
            if sa[0] not in rep["dns"]:
                rep["dns"].append(sa[0])
    except OSError as exc:
        rep["verdict"] = "域名解析失败：%s" % exc
        return rep

    if port in (443, 8443) or parts.scheme == "https":
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
            rep["tcp"] = True
            ctx = ssl.create_default_context()
            with ctx.wrap_socket(sock, server_hostname=host) as ss:
                rep["tls"] = "ok:%s" % ss.version()
        except ssl.SSLError as exc:
            rep["tls"] = "sslerr:%s" % (exc.reason or exc)
        except (ConnectionResetError, BrokenPipeError):
            rep["tls"] = "reset"
        except socket.timeout:
            rep["tls"] = "timeout"
        except OSError as exc:
            rep["tls"] = "oserr:%s" % exc
    else:
        try:
            socket.create_connection((host, port), timeout=timeout).close()
            rep["tcp"] = True
        except OSError as exc:
            rep["tls"] = "oserr:%s" % exc

    rep["doh"] = _doh_lookup(host)
    dns_set, doh_set = set(rep["dns"]), set(rep["doh"])
    # CDN 站点各 DNS 返回的节点本来就不同（Akamai 一次给好几个），
    # 所以“解析不一致”只在连不上时才作为污染证据。
    poisoned = (bool(doh_set) and not dns_set & doh_set
                and not rep["tls"].startswith("ok"))

    if rep["tls"].startswith("ok"):
        # 能握手就说明域名没被墙；解析差异只是 CDN 调度
        rep["verdict"] = ("网络层可达（%s），超时更可能是页面本身慢、"
                          "需要 cookie，或站点对无头浏览器有反爬" % rep["tls"][3:])
    elif poisoned:
        rep["blocked"] = True
        rep["verdict"] = ("系统 DNS 解析到 %s，公共 DNS 是 %s，两者不一致（DNS 被污染）；"
                          "这类域名在国内网络下必须走代理"
                          % (", ".join(rep["dns"][:2]), ", ".join(rep["doh"][:2])))
    elif rep["tls"] == "reset":
        rep["blocked"] = True
        rep["verdict"] = ("TCP 能连上，但 TLS 握手被直接重置（域名被阻断）；"
                          "这种和站点无关，只能用代理访问")
    elif rep["tls"] == "timeout":
        rep["blocked"] = True
        rep["verdict"] = "连接 %s:%d 超时，出口被拦截或站点不可达" % (host, port)
    elif not rep["tcp"]:
        rep["blocked"] = True
        rep["verdict"] = "无法连接 %s:%d：%s" % (host, port, rep["tls"] or "网络不通")
    else:
        rep["verdict"] = rep["tls"] or "未知原因"
    return rep


def _is_private_host(host):
    import ipaddress
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


def precheck(url, cfg=None, timeout=5):
    """扫描前的快速体检：被墙/解析异常就立刻报错，别让用户干等一分钟。

    只在没有任何代理配置时判断——挂了代理就该让流量走代理试。
    返回 (ok, 原因)。
    """
    if proxy_url(cfg):
        return True, ""
    host = urllib.parse.urlsplit(url).hostname or ""
    # 内网地址（NAS 自己、路由器、局域网媒体库）不走这套判断：
    # 它们本来就不该被墙，连不上多半是端口没开，交给后面的失败提示更准确
    if "." not in host or _is_private_host(host):
        return True, ""
    try:
        rep = diagnose(url, timeout=timeout)
    except Exception:
        return True, ""
    if rep.get("blocked"):
        return False, rep["verdict"]
    return True, ""


def base_env(cfg=None, with_impersonate=False):
    """子进程环境：补上 vendor 的 PYTHONPATH，并去掉可能干扰的变量。

    with_impersonate=True 时额外把 curl_cffi 目录挂上（只有真正要用指纹伪装时）。
    """
    env = dict(os.environ)
    vp = vendor_path()
    old = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = vp + (":" + old if old else "")
    env["PYTHONUNBUFFERED"] = "1"
    if with_impersonate:
        extra = impersonate_dir()
        if os.path.isdir(extra):
            env["PYTHONPATH"] = extra + ":" + env["PYTHONPATH"]
    if os.path.isdir(browser_path()):
        env["PLAYWRIGHT_BROWSERS_PATH"] = browser_path()
    apply_proxy_env(env, cfg)
    # 自带依赖库只在系统缺库时启用（否则版本交叉会让渲染进程崩溃）
    lib = bundled_lib_dir()
    if os.path.isdir(lib) and needs_bundled_libs():
        old_ld = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = lib + (":" + old_ld if old_ld else "")
    return env


def free_disk(path):
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return -1


def disk_free_text(path):
    free = free_disk(path)
    if free < 0:
        return "未知"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if free < 1024 or unit == "TB":
            return "%.1f %s" % (free, unit) if unit != "B" else "%d B" % free
        free /= 1024.0


def local_ip():
    """给用户一个备用直连地址（网关异常时用）。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"
