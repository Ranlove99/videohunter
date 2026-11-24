# -*- coding: utf-8 -*-
"""站点扫描：从一个网址出发，尽可能全地找出可下载的视频。

四层策略，逐层加码，后一层只在前一层结果不足时兜底：
  1. yt-dlp 探测      —— 平台页/播放列表，直接拿到结构化条目
  2. 静态 HTML 解析    —— <video>/<source>/<a>/内嵌 m3u8|mp4 字符串
  3. 无头浏览器渲染    —— JS 动态站，顺带监听网络请求里的媒体地址
  4. 同域有限深爬      —— 列表页/分页跟进，深度与页数双上限
"""

import html as html_mod
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser

from . import engine, settings

MAX_HTML = 4 * 1024 * 1024
VERIFY_WORKERS = 4

VIDEO_EXT = (".mp4", ".m3u8", ".mpd", ".webm", ".mov", ".flv", ".mkv", ".ts", ".avi", ".m4v")

# 媒体地址：绝对地址 / 协议相对(//cdn/..) / 站内相对(/videos/..) 都要认。
# 老版本只认 http(s) 绝对地址，JS 里写 "/xxx.m3u8" 的站会被整站漏掉。
_MEDIA_ANY = re.compile(
    r'(?:(?:https?:)?//|/)[^\s"\'<>\\()]+?\.(?:m3u8|mpd|mp4|webm|mov|flv|m4v)'
    r'(?:\?[^\s"\'<>\\()]*)?', re.I)

# 这些属性里常放真实播放地址（懒加载站点把 src 留空）
_DATA_ATTRS = ("data-src", "data-url", "data-video", "data-video-url",
               "data-play-url", "data-file", "data-original", "data-href")
_DURATION = re.compile(r'(\d{1,2}:)?\d{1,2}:\d{2}')

_LIST_HINT = re.compile(
    r'/(?:list|lists|category|categories|channel|channels|album|albums|collection|'
    r'collections|tag|tags|topic|topics|page|pages|archive|archives|series|course|'
    r'courses|lesson|lessons|grid)(?:/|$|\?)|[?&](?:page|p|pn|offset)=\d+', re.I)
_DETAIL_HINT = re.compile(
    r'/(?:video|videos|watch|play|player|detail|details|view|v|p|post|posts|item|'
    r'episode|episodes|movie|movies|clip|clips)/[^/]+', re.I)


class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.media = []
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "title":
            self._in_title = True
        if tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])
        elif tag in ("video", "audio") and attrs.get("src"):
            self.media.append(attrs["src"])
        elif tag == "source" and attrs.get("src"):
            self.media.append(attrs["src"])
        elif tag == "iframe" and attrs.get("src"):
            self.links.append(attrs["src"])
        # 懒加载：src 为空、真地址在 data-* 上
        if tag in ("video", "audio", "source"):
            for key in _DATA_ATTRS:
                val = attrs.get(key)
                if val and not val.startswith(("javascript:", "blob:", "data:")):
                    self.media.append(val)
        elif tag == "meta":
            prop = (attrs.get("property") or attrs.get("name") or "").lower()
            if prop in ("og:video", "og:video:url", "og:video:secure_url",
                        "twitter:player:stream") and attrs.get("content"):
                self.media.append(attrs["content"])

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def _norm(url):
    """去掉锚点与常见追踪参数，用于去重。"""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=False)
    query = [(k, v) for k, v in query
             if not k.lower().startswith(("utm_", "spm", "from", "share_", "ref"))]
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path,
                                    urllib.parse.urlencode(query), ""))


def _same_site(url, host):
    try:
        return urllib.parse.urlsplit(url).netloc == host
    except ValueError:
        return False


def _is_media(url):
    path = urllib.parse.urlsplit(url).path.lower()
    return path.endswith(VIDEO_EXT)


_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1", "0.0.0.0")


def _fetch(url, cfg, timeout=20):
    """标准库抓页面，显式处理编码与大小上限。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": settings.DEFAULT_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "identity",
    })
    proxy = settings.proxy_url(cfg)
    host = urllib.parse.urlsplit(url).hostname or ""
    opener = (urllib.request.build_opener() if (not proxy or host in _LOCAL_HOSTS)
              else urllib.request.build_opener(
                  urllib.request.ProxyHandler({"http": proxy, "https": proxy})))
    with opener.open(req, timeout=timeout) as resp:
        raw = resp.read(MAX_HTML)
        charset = resp.headers.get_content_charset()
    for enc in (charset, "utf-8", "gb18030"):
        if not enc:
            continue
        try:
            return raw.decode(enc, "replace")
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace")


def _parse(html, base_url):
    parser = _LinkParser()
    try:
        parser.feed(html)
    except Exception:  # 畸形 HTML 不能中断整次扫描
        pass

    found = []
    for item in parser.media:
        found.append(("media", urllib.parse.urljoin(base_url, item)))
    for item in parser.links:
        if item.startswith(("javascript:", "mailto:", "#", "tel:")):
            continue
        found.append(("link", urllib.parse.urljoin(base_url, item)))
    # JSON 里的地址常写成 https:\/\/cdn\/a.m3u8，先还原再匹配
    plain = html.replace("\\/", "/")
    for match in _MEDIA_ANY.finditer(plain):
        found.append(("media", urllib.parse.urljoin(base_url, match.group(0))))

    title = html_mod.unescape(parser.title).strip()
    return found, title


def _classify(url, base_host):
    if not _same_site(url, base_host):
        return None
    if _is_media(url):
        return "media"
    if _DETAIL_HINT.search(url):
        return "detail"
    if _LIST_HINT.search(url):
        return "list"
    return "page"


def _title_from_url(url):
    name = urllib.parse.urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
    name = urllib.parse.unquote(name)
    for ext in VIDEO_EXT:
        if name.lower().endswith(ext):
            name = name[:-len(ext)]
            break
    return name or url


def _checkpoint(cancel, pause, emit=None):
    """扫描过程中的停/暂停检查点。

    暂停 = 原地等，恢复后接着跑（已扫到的结果一直留着）；
    停止 = 返回 True，调用方立刻收尾，把已有结果交给前端。
    """
    if pause is not None and pause.is_set():
        if emit:
            emit("paused", {"message": "已暂停，点「继续」接着扫描"})
        while pause.is_set():
            if cancel.wait(0.3):
                return True
        if emit:
            emit("crawl", {"message": "继续扫描…"})
    return cancel.is_set()


def scan(url, cfg, emit, cancel, pause=None):
    """执行一次扫描。emit(stage, payload) 用于向前端推送进度。"""
    started = time.time()
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    host = urllib.parse.urlsplit(url).netloc

    # 先花 1 秒做网络体检：域名被污染/被阻断时直接给出结论，
    # 否则三层依次超时要白等 60 秒以上，还只看到一句 timed out。
    ok, why = settings.precheck(url, cfg)
    if not ok:
        settings.log("[scan] 预检拦截 %s → %s" % (url, why))
        raise RuntimeError("这个网址在当前网络下打不开：" + why)

    warnings = []
    entries = []
    seen = set()
    sniff = {"seen": 0, "kept": 0, "dropped": 0}

    def merge_sniff(meta):
        if not meta:
            return
        for key in ("seen", "kept", "dropped"):
            sniff[key] += int(meta.get(key) or 0)

    def add(entry):
        key = _norm(entry["url"])
        if key in seen:
            return False
        seen.add(key)
        entries.append(entry)
        return True

    # ---------- 第一层：yt-dlp 直接探测 ----------
    emit("probe", {"message": "正在识别页面类型…"})
    if _checkpoint(cancel, pause, emit):
        return _result(entries, 1, started, warnings, cancelled=True, sniff=sniff)

    ok, probed, msg = engine.probe(url, cfg, cancel)
    # generic / HTML5MediaEmbed 会把"当前页面"本身当成一条视频回报：
    # 它既不是直链，又压着后面的静态解析不让跑，是漏扫的常见来源。
    # 处理办法：先收下当兜底，继续走后面的层，最后若这页真产出了直链再删掉。
    self_entries = []
    if ok and probed and len(probed) == 1:
        only = probed[0]
        if ((only.get("extractor") or "").lower() in ("", "generic", "html5mediaembed")
                and _norm(only.get("url") or "") == _norm(url)
                and not _is_media(only.get("url") or "")):
            self_entries = [only]
            ok, probed = False, []
            msg = ""
    if ok and probed:
        # 播放列表可能只拿到前 N 条，提示用户
        for item in probed:
            if not item.get("referer"):
                item["referer"] = url
            add(item)
        emit("probe", {"message": "站点已识别，取到 %d 个视频" % len(probed)})
        if len(probed) >= int(cfg.get("max_verify", 60)):
            warnings.append("该播放列表条目较多，已取前 %d 条" % len(probed))
        entries.sort(key=lambda e: e["title"])
        return _result(entries, 1, started, warnings, sniff=sniff)
    elif msg and msg not in ("非视频页面", "未发现视频条目"):
        warnings.append("引擎识别未命中：%s" % msg)

    for item in self_entries:
        if not item.get("referer"):
            item["referer"] = url
        add(item)

    if _checkpoint(cancel, pause, emit):
        return _result(entries, 1, started, warnings, cancelled=True, sniff=sniff)

    # ---------- 第二层：静态解析 ----------
    emit("fetch", {"message": "抓取页面内容…"})
    candidates = []
    pages_done = 1
    page_title = ""

    cand_seen = set()
    page_hits = {}   # 页面 → 该页已产出多少条直链，用于验真阶段去重

    def harvest(page_url, html, depth_tag="html"):
        """解析一页，返回 (新增直链数, 标题, 新候选页)。

        候选页要回传，好让上一层决定要不要继续跟进——
        老版本只读首页的链接，等于只爬了一层。
        """
        found, page_title = _parse(html, page_url)
        title = (page_title or "").strip()
        # 本页有几个视频：多于一个时给标题加序号，列表里才分得清
        media_n = len({_norm(_l) for _k, _l in found
                       if _classify(_norm(_l), host) == "media"})
        media_i = 0
        local = 0
        new_cands = []
        for kind, link in found:
            link = _norm(link)
            cls = _classify(link, host)
            if not cls:
                continue
            if cls == "media":
                media_i += 1
                head = title
                if head and media_n > 1:
                    head = "%s %d" % (head, media_i)
                if add({"url": link, "title": head or _title_from_url(link),
                        "duration": None, "size": None, "height": None,
                        "thumbnail": "", "uploader": "", "extractor": "",
                        "referer": page_url, "verified": False, "kind": "direct",
                        "source": depth_tag}):
                    local += 1
            elif link not in cand_seen:
                cand_seen.add(link)
                candidates.append((cls, link))
                new_cands.append((cls, link))
        if local:
            page_hits[page_url] = page_hits.get(page_url, 0) + local
        return local, title, new_cands

    try:
        html = _fetch(url, cfg)
        direct_count, page_title, _nc = harvest(url, html)
        settings.log("[scan] 静态解析 %s → 直链 %d 个，HTML %d KB"
                     % (url, direct_count, len(html) // 1024))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
        warnings.append("页面抓取失败：%s" % exc)
        settings.log("[scan] 页面抓取失败 %s → %s" % (url, exc))
        direct_count = 0

    emit("parse", {"message": "静态解析完成，发现 %d 个直链" % direct_count,
                   "pages": pages_done})

    # ---------- 第三层：浏览器渲染（可选） ----------
    if cfg.get("use_browser") and settings.browser_ready() and not _checkpoint(cancel, pause, emit):
        emit("browser", {"message": "启动无头浏览器渲染…"})
        try:
            from . import browser
            rendered_html, media_urls, frames, sniff_meta = browser.render(
                url, cfg, cancel, pause)
            merge_sniff(sniff_meta)
            added = 0
            if rendered_html:
                added, _t, _nc = harvest(url, rendered_html, "browser")
            for media in media_urls:
                media = _norm(media)
                if add({"url": media, "title": _title_from_url(media),
                        "duration": None, "size": None, "height": None,
                        "thumbnail": "", "uploader": "", "extractor": "",
                        "referer": url, "verified": False, "kind": "direct",
                        "source": "browser"}):
                    added += 1
            settings.log("[scan] 渲染 %s → 嗅探 %d 条（入选 %d，丢弃 %d），iframe %d 个，新增 %d"
                         % (url, (sniff_meta or {}).get("seen", 0), len(media_urls),
                            (sniff_meta or {}).get("dropped", 0), len(frames or []), added))
            emit("browser", {"message": "渲染完成，新增 %d 个候选" % added})
        except Exception as exc:  # 浏览器失败必须降级，不能让扫描整体失败
            warnings.append("浏览器渲染未成功：%s" % exc)
            settings.log("[scan] 渲染失败 %s → %s: %s"
                         % (url, type(exc).__name__, exc))
    elif cfg.get("use_browser"):
        warnings.append("未检测到浏览器内核，已跳过动态渲染")
        settings.log("[scan] 内核不可用，跳过渲染：%s" % (settings.report_snapshot().get("reason") or ""))

    # ---------- 第四层：同域有限深爬 ----------
    depth_limit = int(cfg.get("crawl_depth", 2))
    max_pages = int(cfg.get("max_pages", 200))
    interval = max(0, int(cfg.get("rate_limit_ms", 300))) / 1000.0

    visited = {_norm(url)}
    queue = []
    queued = set()

    def push(cls, link, depth):
        key = _norm(link)
        if key in visited or key in queued or depth > depth_limit:
            return
        if len(queue) >= max_pages:
            return
        queued.add(key)
        queue.append((cls, link, depth))

    # 列表页/详情页优先；其它同站页面也给个机会——
    # 很多站的视频页就是 /archives/123、/p/abc，不含任何关键字。
    loose_pages = 0
    for cls, link in candidates:
        if cls in ("list", "detail"):
            push(cls, link, 1)
        elif cls == "page" and loose_pages < 40:
            loose_pages += 1
            push(cls, link, depth_limit)   # 只扫这一层，不再往下扩

    render_budget = 2   # 详情页静态解析为空时，最多补渲染 2 页
    browser_ready = cfg.get("use_browser") and settings.browser_ready(5)

    while queue and pages_done < max_pages:
        if _checkpoint(cancel, pause, emit):
            break
        cls, link, depth = queue.pop(0)
        key = _norm(link)
        if key in visited:
            continue
        visited.add(key)
        if interval and cancel.wait(interval):
            break
        try:
            html = _fetch(link, cfg, timeout=15)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
            visited.discard(key)
            continue

        pages_done += 1
        added, _t, new_cands = harvest(link, html, "crawl")

        # 详情页一条都没抓到：多半视频藏在 JS 里，补一次渲染
        if added == 0 and cls == "detail" and render_budget > 0 and browser_ready:
            render_budget -= 1
            try:
                from . import browser
                r_html, r_media, _f, r_sniff = browser.render(link, cfg, cancel, pause)
                merge_sniff(r_sniff)
                if r_html:
                    a2, _t2, nc2 = harvest(link, r_html, "browser")
                    added += a2
                    new_cands = new_cands + nc2
                for media in r_media:
                    media = _norm(media)
                    if add({"url": media, "title": _title_from_url(media),
                            "duration": None, "size": None, "height": None,
                            "thumbnail": "", "uploader": "", "extractor": "",
                            "referer": link, "verified": False,
                            "kind": "direct", "source": "browser"}):
                        added += 1
                settings.log("[scan] 详情页补渲染 %s → 新增 %d" % (link, added))
            except Exception as exc:
                settings.log("[scan] 详情页渲染失败 %s → %s" % (link, exc))

        for c2, l2 in new_cands:
            push(c2, l2, depth + 1)

        if added:
            emit("crawl", {"message": "已扫描 %d 页，累计 %d 个视频"
                           % (pages_done, len(entries)), "pages": pages_done})

    # ---------- 验真：把"疑似视频页"变成有标题时长的条目 ----------
    verify_targets = [u for u in dict.fromkeys(c[1] for c in candidates)
                      if _norm(u) not in seen][:int(cfg.get("max_verify", 60))]
    if verify_targets and not cancel.is_set():
        emit("verify", {"message": "校验 %d 个候选地址…" % len(verify_targets)})
        done = [0]
        lock = threading.Lock()

        def work(target):
            if _checkpoint(cancel, pause):
                return
            meta = engine.info(target, cfg, referer=url, cancel=cancel)
            if meta and page_hits.get(_norm(meta.get("url") or ""), 0) > 0:
                return   # 这个页面已经产出直链，别再单列一条重复的
            with lock:
                done[0] += 1
                if meta and add(meta):
                    emit("verify", {"message": "已确认 %d / %d" % (done[0], len(verify_targets)),
                                    "checked": done[0], "total": len(verify_targets)})

        with ThreadPoolExecutor(max_workers=VERIFY_WORKERS) as pool:
            list(pool.map(work, verify_targets))

    if self_entries and page_hits.get(_norm(url), 0) > 0:
        killed = {_norm(e["url"]) for e in self_entries}
        for item in self_entries:
            seen.discard(_norm(item["url"]))
        entries = [e for e in entries if _norm(e["url"]) not in killed]

    if not entries:
        # 一条都没扫到：跑一次网络诊断，把"为什么"写清楚，
        # 别只甩一句 timed out 让用户猜。内网/本机地址没有公网 DNS 可比，
        # 诊断只会给出「未知原因」，跳过。
        if host in _LOCAL_HOSTS or settings._is_private_host(host):
            settings.log("[scan] 空结果 %s：内网地址，跳过网络诊断" % url)
        else:
            try:
                d = settings.diagnose(url)
                warnings.append("该地址扫描不到：" + d["verdict"])
                settings.log("[scan] 诊断 %s → %s（DNS %s / 公共 %s / TLS %s）"
                             % (url, d["verdict"], ",".join(d["dns"][:2]),
                                ",".join(d["doh"][:2]), d["tls"]))
            except Exception as exc:
                settings.log("[scan] 诊断失败：%s" % exc)

    if sniff.get("dropped"):
        warnings.append("浏览器共看到 %d 条媒体地址，其中 %d 条特征不足已忽略"
                        "（多为无后缀短链、HTML 页或过小文件）"
                        % (sniff["seen"], sniff["dropped"]))

    stopped = cancel.is_set()
    entries.sort(key=lambda e: (not e["verified"], e["title"]))
    settings.log("[scan] %s %s → %d 条（已验真 %d），页面 %d，耗时 %.1fs%s"
                 % ("已停止" if stopped else "完成", url, len(entries),
                    sum(1 for e in entries if e.get("verified")),
                    pages_done, time.time() - started,
                    "，警告：" + "；".join(warnings) if warnings else ""))
    return _result(entries, pages_done, started, warnings, cancelled=stopped, sniff=sniff)


def _result(entries, pages, started, warnings, cancelled=False, sniff=None):
    verified = sum(1 for e in entries if e.get("verified"))
    return {
        "entries": entries,
        "stats": {
            "count": len(entries),
            "verified": verified,
            "downloadable": len(entries),
            "pages": pages,
            "elapsed": round(time.time() - started, 1),
            "cancelled": cancelled,
            "stopped": cancelled,
            "sniff": sniff or {},
        },
        "warnings": warnings,
    }
