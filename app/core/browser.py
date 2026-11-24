# -*- coding: utf-8 -*-
"""无头浏览器渲染：对付只能靠 JS 出视频地址的站点。

三个产出：
  * 渲染后的 HTML —— 交给静态解析复用同一套规则
  * 网络请求里出现的媒体地址 —— 这类站真正能下载的往往在这里（m3u8/mp4）
  * 嗅探统计 —— 一共看到多少条、有多少条因分数不足被丢弃，用于向前端解释

任何异常都只记录、不外抛，扫描主流程必须能降级继续。
"""

import os
import re
import urllib.parse

from . import settings

# 嗅探打分：真实站点大量媒体地址没有后缀（/play?vid=123、/stream/abc），
# 只按后缀 + MIME 判断会漏；反过来正文 HTML 又会被误当视频。
# 规则参考 cat-catch 的思路（该项目 GPL-3.0，只学规则不复制代码）。
SNIFF_THRESHOLD = 30
MAX_MEDIA = 300
MAX_HITS = 2000

_STRONG_SUFFIX = re.compile(r'\.(m3u8|mpd)(?:[?#]|$)', re.I)
_MEDIA_SUFFIX = re.compile(r'\.(mp4|webm|mov|flv|m4v|ts|mkv|avi)(?:[?#]|$)', re.I)
_MEDIA_PATH_HINT = re.compile(r'/(?:play|player|stream|video|videos|media|hls|dash|vod)/', re.I)
# 这些类型一眼就不是视频，直接不记，免得「忽略 N 条」里全是 HTML/CSS
_NON_MEDIA_TYPE = ("text/html", "application/json", "text/css", "text/javascript",
                   "application/javascript", "application/xml", "image/", "font/",
                   "application/font", "application/wasm")

MAX_SCROLLS = 6
SCROLL_WAIT_MS = 700
PLAY_WAIT_MS = 2500

# 常见的"点一下才加载"的播放控件；命中就点，不中也不影响
PLAY_SELECTORS = (
    "video",
    ".vjs-big-play-button",
    ".plyr__control--overlaid",
    ".dplayer-play-icon",
    ".dplayer-video-wrap",
    ".jw-icon-display",
    ".xgplayer-start",
    "[class*='play-btn']",
    "[class*='playBtn']",
    "[class*='play-icon']",
    "[class*='play']",
    "#play",
    "[id*='play']",
    "[aria-label*='播放']",
    "[aria-label*='play']",
    "[title*='播放']",
)

LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    "--mute-audio",
]


def _score(href, info, page_host):
    """给一条嗅探结果打分，超过阈值的才当成视频候选。

    * HLS/DASH（后缀或 MIME）        +40
    * 常见视频后缀                    +30
    * video/* MIME                   +40
    * Content-Length ≥1MB            +25（≥200KB +12，<40KB -20）
    * 路径里带 play/stream/video 等    +8
    * 同域                            +8（CDN 跨域不加也不减）
    """
    score = 0
    ctype = (info.get("ctype") or "").lower()
    length = info.get("length") or 0
    if _STRONG_SUFFIX.search(href) or "mpegurl" in ctype or "dash+xml" in ctype:
        score += 40
    if _MEDIA_SUFFIX.search(href):
        score += 30
    if ctype.startswith("video/"):
        score += 40
    elif ctype.startswith("application/octet-stream"):
        # 无后缀直链常见返回类型，给点分但不足以单独入选
        score += 10
    elif ctype.startswith(("text/html", "application/json", "text/plain", "text/css")):
        score -= 40
    if length >= 1024 * 1024:
        score += 25
    elif length >= 200 * 1024:
        score += 12
    elif 0 < length < 40 * 1024:
        score -= 20
    if _MEDIA_PATH_HINT.search(href):
        score += 8
    try:
        if urllib.parse.urlsplit(href).hostname == page_host:
            score += 8
    except ValueError:
        pass
    return score + (info.get("boost") or 0)


def _prepare_env(cfg=None):
    path = settings.browser_path()
    if os.path.isdir(path):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = path
    # 只有在系统确实缺库时才用包内库；库齐全时用系统的，避免版本交叉崩溃
    lib = settings.bundled_lib_dir()
    if os.path.isdir(lib) and settings.needs_bundled_libs():
        old = os.environ.get("LD_LIBRARY_PATH", "")
        parts = [p for p in old.split(":") if p]
        if lib not in parts:
            os.environ["LD_LIBRARY_PATH"] = ":".join([lib] + parts)
    # 渲染进程同样要能走代理，否则被墙的站在这一步必然 goto 超时
    settings.apply_proxy_env(os.environ, cfg)


def render(url, cfg, cancel, pause=None):
    """对外入口：返回 (html, [媒体地址], [iframe 地址], 嗅探统计)。

    失败时记录原因并原样抛出，由扫描器降级。
    """
    try:
        result = _render(url, cfg, cancel, pause)
    except Exception as exc:
        # 包内库可能与系统库版本交叉导致渲染进程崩溃：去掉内置库重试一次
        if settings.needs_bundled_libs():
            settings.disable_bundled_libs()
            os.environ.pop("LD_LIBRARY_PATH", None)
            try:
                result = _render(url, cfg, cancel, pause)
            except Exception as exc2:
                settings.note_render(False, "%s: %s" % (type(exc2).__name__, exc2))
                raise
            settings.note_render(True)
            return result
        settings.note_render(False, "%s: %s" % (type(exc).__name__, exc))
        raise
    settings.note_render(True)
    return result


def _render(url, cfg, cancel, pause=None):
    """返回 (html, [媒体地址], [iframe 地址], 嗅探统计)。调用方负责异常兜底。"""
    _prepare_env(cfg)
    from playwright.sync_api import sync_playwright

    timeout = int(cfg.get("browser_timeout", 25)) * 1000
    page_host = urllib.parse.urlsplit(url).hostname or ""
    hits = {}

    def stopped():
        """停止或暂停都算「先别继续」：暂停时停在这里，恢复后接着走。"""
        return cancel.is_set() or (pause is not None and pause.is_set())

    def observe(href, ctype="", length=0, boost=0):
        if not href or href.startswith(("blob:", "data:")) or len(href) > 2048:
            return
        # 明确的非媒体类型：除非地址本身带媒体后缀（m3u8 有时被标成 text/plain），否则不记
        if (ctype and ctype.startswith(_NON_MEDIA_TYPE)
                and not _STRONG_SUFFIX.search(href) and not _MEDIA_SUFFIX.search(href)):
            return
        info = hits.get(href)
        if info is None:
            if len(hits) >= MAX_HITS:
                return
            info = {"ctype": "", "length": 0, "boost": 0}
            hits[href] = info
        if ctype:
            info["ctype"] = ctype
        if length:
            info["length"] = max(info["length"], int(length))
        if boost:
            info["boost"] = max(info["boost"], boost)

    def on_response(response):
        try:
            href = response.url
            headers = response.headers or {}
            ctype = headers.get("content-type", "") or ""
            raw_len = headers.get("content-length", "") or ""
            length = int(raw_len) if str(raw_len).isdigit() else 0
        except Exception:
            return
        observe(href, ctype, length)

    def on_request(request):
        # 有些站 m3u8 是 XHR/fetch 请求，响应被取消也照样记下来
        try:
            observe(request.url)
        except Exception:
            pass

    def try_play(page):
        """点一下播放控件：大量站点要点了才开始请求 m3u8。

        不检查可见性 —— 播放器常把 video 做成 0 高度占位、
        由遮罩层接管点击，看得见与否和能不能点没有必然关系。
        """
        clicked = 0
        # 不止点第一个命中的：video 本身、遮罩层、按钮都点一遍才够稳
        for frame in page.frames:
            if clicked >= 4:
                break
            for sel in PLAY_SELECTORS:
                if clicked >= 4:
                    break
                try:
                    loc = frame.locator(sel).first
                    if not loc.count():
                        continue
                    loc.click(timeout=1000, force=True, no_wait_after=True)
                    clicked += 1
                except Exception:
                    continue
        return clicked

    def pick_video_srcs(page):
        js = """() => {
            const out = [];
            document.querySelectorAll('video').forEach(v => {
                if (v.currentSrc) out.push(v.currentSrc);
                if (v.src) out.push(v.src);
                v.querySelectorAll('source').forEach(s => { if (s.src) out.push(s.src); });
            });
            return out;
        }"""
        for frame in page.frames:
            try:
                for src in frame.evaluate(js) or []:
                    # 页面里 video 标签自己声明的地址，可信度最高
                    observe(src, "video/", boost=20)
            except Exception:
                continue

    def has_media():
        return any(_score(h, i, page_host) >= SNIFF_THRESHOLD for h, i in hits.items())

    def finalize():
        kept, low = [], []
        for href, info in hits.items():
            sc = _score(href, info, page_host)
            (kept if sc >= SNIFF_THRESHOLD else low).append((sc, href))
        kept.sort(key=lambda x: -x[0])
        meta = {
            "seen": len(hits),
            "kept": min(len(kept), MAX_MEDIA),
            "dropped": len(low),
            "sample": [{"url": u[:180], "score": s} for s, u in low[:5]],
        }
        return [u for _s, u in kept][:MAX_MEDIA], meta

    html, frames = "", []
    with sync_playwright() as pw:
        # 直接用包内内核的绝对路径，绕开 Playwright 的版本注册表：
        # 新版默认会去找独立的 headless_shell，而我们只打包了完整内核。
        exe = settings.chrome_exe()
        attempts = []
        if exe:
            attempts.append({"executable_path": exe})
        attempts.append({"channel": "chromium"})
        attempts.append({})
        # Playwright 不会自动读环境变量里的代理，必须显式传
        proxy = settings.proxy_url(cfg)
        proxy_kw = {"proxy": {"server": proxy}} if proxy else {}
        browser, last_exc = None, None
        for kwargs in attempts:
            try:
                browser = pw.chromium.launch(headless=True, args=LAUNCH_ARGS,
                                             **proxy_kw, **kwargs)
                break
            except Exception as exc:
                last_exc = exc
        if browser is None:
            raise last_exc
        try:
            ctx = browser.new_context(
                user_agent=settings.DEFAULT_UA,
                viewport={"width": 1440, "height": 900},
                ignore_https_errors=True,
                locale="zh-CN",
            )
            page = ctx.new_page()
            page.on("response", on_response)
            page.on("request", on_request)
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)

            # 给 SPA 一点时间发首屏请求
            page.wait_for_timeout(1200)
            try:
                page.wait_for_load_state("networkidle", timeout=min(6000, timeout))
            except Exception:
                pass  # 长连接页面永远不 idle，不强求

            # 滚动触发懒加载、懒加载 iframe 与"加载更多"
            for _ in range(MAX_SCROLLS):
                if stopped():
                    break
                page.mouse.wheel(0, 2400)
                page.wait_for_timeout(SCROLL_WAIT_MS)

            pick_video_srcs(page)

            if not stopped() and not has_media():
                # 还没嗅到媒体：多半要点了播放才开始请求，点一次再等
                try:
                    if try_play(page):
                        page.wait_for_timeout(PLAY_WAIT_MS)
                        pick_video_srcs(page)
                except Exception:
                    pass

            html = page.content()
            try:
                for frame in page.frames[1:]:
                    if frame.url and frame.url not in frames:
                        frames.append(frame.url)
            except Exception:
                pass
        finally:
            try:
                browser.close()
            except Exception:
                pass

    media, meta = finalize()
    settings.log("[sniff] 看到 %d 条，入选 %d 条，丢弃 %d 条"
                 % (meta["seen"], meta["kept"], meta["dropped"]))
    return html, media, _dedupe(frames), meta


def _dedupe(items):
    out, seen = [], set()
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out
