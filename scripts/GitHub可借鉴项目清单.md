# 视频猎手 · 可借鉴的开源项目清单

> 目的：找出 GitHub 上能补上「扫描漏抓 / 反爬 403 / HLS 拼装 / 播放器抓不到」这些短板的项目，
> 判断哪些能真装进 fnOS 的 fpk（python312 + 双架构 + 无 root + 无外网依赖）。
> 数据核对于 2026-11-24，星数与许可证取自 GitHub API。

## 一、结论先说

现在这套应用的短板只有四个，对应四类项目：

| 短板 | 典型表现 | 该看哪些项目 | 能否内置 |
|---|---|---|---|
| 站点反爬 / TLS 指纹识别 | 403、验证页、返回空壳 HTML | **curl_cffi**（yt-dlp `--impersonate` 的底层） | ✅ 能，双架构都有轮子 |
| HLS 分片下载与解密 | 下完拼不起来、AES-128 加密流 | **N_m3u8DL-RE**、yt-dlp 原生 HLS | ⚠️ 能但要带 .NET 二进制，不划算 |
| 播放器要「点了才加载」/ MSE | 嗅探 0 条、只有 blob: 地址 | **cat-catch**、**mediago** 的嗅探策略 | ✅ 思路可抄，代码不可抄（GPL） |
| 音视频合流缺 ffmpeg | `bv*+ba` 合并失败、HLS 后处理报错 | 静态 **ffmpeg** 构建（BtbN 等） | ✅ 能，代价是包体变大 |

一句话：**最值得做的是「内置 curl_cffi 让 yt-dlp 能伪装浏览器」和「内置静态 ffmpeg」，其余多为思路借鉴。**

## 二、清单

### A. 提取器 / 下载引擎

| 项目 | 星数 | 许可 | 是什么 | 对我们的价值 |
|---|---|---|---|---|
| [yt-dlp/yt-dlp](https://github.com/yt-dlp/yt-dlp) | 210k | Unlicense | 全能下载器，1800+ 站点提取器 | **已在包内**，版本 2026.10.26 |
| [iawia002/lux](https://github.com/iawia002/lux) | 31.9k | MIT | Go 写的下载器，国内站点支持好（B 站、优酷、抖音…） | 提取器思路可参考；Go 二进制每架构 +15MB，收益不如 yt-dlp |
| [soimort/you-get](https://github.com/soimort/you-get) | — | MIT | 老牌 Python 下载器，部分小众站点支持比 yt-dlp 好 | 可作为 yt-dlp 失败后的第二引擎（纯 Python，+2MB），优先级中 |
| [nilaoda/N_m3u8DL-RE](https://github.com/nilaoda/N_m3u8DL-RE) | 9.1k | MIT | 目前最强的 HLS/DASH 下载器，分片并发、AES-128、字幕轨、直播录制 | 能力确实强，但是 .NET AOT 二进制，双架构各 +30MB 起，且要自己接一套调用/进度解析 |
| [streamlink/streamlink](https://github.com/streamlink/streamlink) | — | BSD-2 | 专注直播流（twitch 等） | 与当前「点播批量下载」定位不符，暂不需要 |

### B. 浏览器嗅探（对付「点了才加载」和 blob:）

| 项目 | 星数 | 许可 | 关键做法 | 我们的差距 |
|---|---|---|---|---|
| [xifangczy/cat-catch](https://github.com/xifangczy/cat-catch) | 23.2k | **GPL-3.0** | 扩展里挂 `webRequest`，按扩展名/MIME/Content-Length 打分，还能读页面里的 `JSON.parse` 结果 | 我们只按 URL 后缀 + MIME 判断，**没有内容长度与启发式打分**，短链/无后缀地址会漏 |
| [mediago-dev/mediago](https://github.com/mediago-dev/mediago) | 9.4k | MIT | Electron，用 CDP 直接监听网络，嗅探 m3u8/HLS，支持边下边播 | 我们已等价（Playwright response/request 事件），缺的是**嗅探结果的前端展示**（让用户看到"嗅到了但没抓取"） |
| [putyy/res-downloader](https://github.com/putyy/res-downloader) | 21.5k | Apache-2.0 | 起本地 MITM 代理，把系统流量引进来解密抓包，连 App/小程序都能抓 | 能力最强，但要在 NAS 上装根证书 + 改系统代理，**副作用大，不建议内置** |

### C. 反爬 / 反指纹

| 项目 | 星数 | 许可 | 能解决什么 |
|---|---|---|---|
| [lexiforest/curl_cffi](https://github.com/lexiforest/curl_cffi) | 6.9k | MIT | 用 curl-impersonate 复刻 Chrome 的 TLS/JA3/HTTP2 指纹，yt-dlp 的 `--impersonate` 就靠它 |
| [NanmiCoder/MediaCrawler](https://github.com/NanmiCoder/MediaCrawler) | 71.7k | 非商用许可 | 登录态复用、CDP 接管已登录浏览器、滑块处理。**许可不允许商用/闭源分发，只能读思路** |

## 三、三个真正值得做的改造（按性价比排序）

### 1. 内置 curl_cffi，让 yt-dlp 能 `--impersonate chrome`（推荐先做）

**解决**：Cloudflare / 阿里云 WAF 这类按 TLS 指纹拦截的站——表现就是「人能打开、程序 403 或空页」。
**可行性已核实**：curl_cffi 0.17.0 提供 `cp310-abi3` 轮子（abi3 → 兼容 python3.12），
`manylinux2014_aarch64` 12.8MB、`manylinux2014_x86_64` 13.4MB，可分别内置进 ARM / x86 包。
**做法**：`vendor/` 加 `curl_cffi` + `certifi`/`cffi`/`pycparser`；`engine._base_args()` 加 `--impersonate chrome`；
失败时自动去掉该参数重试一次（老内核/老 yt-dlp 不认时不能把下载搞挂）。
**代价**：每包 +约 20MB；下载耗时略增（指纹库加载）。
**验证**：本地起一个校验 `ja3`/`User-Agent` 一致性的测试站，直连失败、impersonate 成功。

### 2. 内置静态 ffmpeg，并显式传 `--ffmpeg-location`

**解决**：现在包内**没有 ffmpeg**，也没传 `--ffmpeg-location`。fnOS 若未预装 ffmpeg，
`-f bv*+ba` 的音视频合流会失败，HLS 的 `FixupM3u8` 也会报错——用户只看到「下载失败」。
**做法**：取 linux64 / linuxarm64 静态构建（BtbN FFmpeg-Builds 等），只留 `ffmpeg` 一个二进制，
放 `vendor/bin/`，`engine` 统一带上 `--ffmpeg-location`；启动时 `/api/health` 显示 ffmpeg 是否可用。
**代价**：每包 +30MB 左右（需实测压缩后体积），是本次最大的一笔。
**验证**：把系统 ffmpeg 从 PATH 里摘掉，跑一次 `bv*+ba` 下载，确认仍能合流出 mp4。

### 3. 抄 cat-catch 的「嗅探打分」，不抄代码

**解决**：现在只认 `.m3u8/.mp4` 后缀和 `video/*` MIME。真实站点常见
`/play?vid=123`（无后缀，靠 `Content-Type` 才知道是视频）、`/stream/abc`（返回 `application/octet-stream`）。
**做法**：给嗅探结果打分——`Content-Type` 命中视频 +40；`Content-Length > 1MB` +30；
URL 后缀命中 +30；命中 `m3u8/mpd` +40；同域 +10；`blob:`/`data:` 直接丢弃。超过阈值才入候选，
并把「嗅到了但分数不够」的数量回报给前端（能解释"为什么只扫到 0 条"）。
**代价**：纯逻辑改动，无新增依赖，风险低。
**注意**：cat-catch 是 **GPL-3.0**，只能借鉴规则，不能复制其源码。

## 四、不建议做的

| 方案 | 原因 |
|---|---|
| 内置 cat-catch / 任何 GPL 代码 | 与闭源分发的 fpk 许可冲突 |
| 内置 res-downloader 式 MITM 代理 | 需在 NAS 装根证书、改全局代理，故障面太大，不适合做默认能力 |
| 集成 MediaCrawler | 非商用许可 + 它的强项是「登录态爬社交平台」，与「给个网址扫全站视频」不是一回事 |
| 引入 N_m3u8DL-RE 替换 yt-dlp | 双架构各 +30MB 二进制，还要重写进度/命名/断点逻辑；yt-dlp 的自带 HLS + ffmpeg 已够用 |

## 五、下一步

按 `1 → 2 → 3` 做，每步单独出包、单独验证，出问题能立刻回退。
建议先做 **1（curl_cffi impersonate）**：包体只涨 20MB，直接解决「人能打开、程序 403」这一类最常见的漏抓。
