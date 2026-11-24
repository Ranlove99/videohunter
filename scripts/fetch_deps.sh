#!/usr/bin/env bash
# 从零补齐 vendor/ 下的第三方运行时。
# 仓库只含业务源码，不含任何二进制依赖，避免把 500MB 的包塞进 git。
#
# 用法：
#   ./scripts/fetch_deps.sh x86      # Intel / AMD
#   ./scripts/fetch_deps.sh arm      # ARM64
#   ./scripts/fetch_deps.sh x86 311  # 本地用 Python 3.11 调试（额外补 cp311 greenlet/cffi）
#
# 产物与 fnpack 打包目录完全一致：
#   app/vendor/{yt_dlp,playwright,pyee,greenlet,impersonate,bin,browsers}
set -euo pipefail

ARCH="${1:-}"
LOCAL_PYVER="${2:-}"
case "$ARCH" in
  x86) PLAT=manylinux2014_x86_64;  FFMPEG_NAME=ffmpeg-linux-x86_64-v7.0.2;
       CHROME_DIR=chromium-1208/chrome-linux64;           TRIPLE=x86_64-linux-gnu ;;
  arm) PLAT=manylinux2014_aarch64; FFMPEG_NAME=ffmpeg-linux-aarch64-v7.0.2;
       CHROME_DIR=chromium-1243/chrome-linux-arm64;       TRIPLE=aarch64-linux-gnu ;;
  *) echo "用法: $0 x86|arm [本地调试用的Python版本，如 311]" >&2; exit 1 ;;
esac

PYVER=312   # 飞牛 fnOS 的 python312
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENDOR="$ROOT/app/vendor"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

log() { printf '\033[36m[deps]\033[0m %s\n' "$*"; }

dl() {  # dl <python-version> <packages...>
  local pv="$1"; shift
  python3 -m pip download --no-deps --only-binary=:all: \
      --platform "$PLAT" --python-version "$pv" -d "$WORK" "$@"
}

unpack_all() {  # 解包 WORK 下所有 wheel，然后清空
  for w in "$WORK"/*.whl; do
    [ -e "$w" ] || continue
    unzip -qo "$w" -d "$VENDOR"
    rm -f "$w"
  done
}

mkdir -p "$VENDOR"

# ---------- 1. 下载引擎（纯 Python，与架构无关） ----------
log "yt-dlp"
dl "$PYVER" yt-dlp
unpack_all

# ---------- 2. 浏览器驱动 ----------
log "playwright 1.58.0 / pyee / greenlet (cp$PYVER)"
dl "$PYVER" playwright==1.58.0 pyee==13.0.1
# greenlet 是编译扩展，需要与目标 Python ABI 一致
dl "$PYVER" greenlet
unpack_all
if [ -n "$LOCAL_PYVER" ] && [ "$LOCAL_PYVER" != "$PYVER" ]; then
  log "补 cp$LOCAL_PYVER greenlet（仅本地调试用）"
  dl "$LOCAL_PYVER" greenlet
  unpack_all
fi

# ---------- 3. TLS 指纹库（隔离在 impersonate/，避免污染 yt-dlp 的 sys.path） ----------
log "curl_cffi 0.17.0 → vendor/impersonate/"
dl "$PYVER" curl_cffi==0.17.0 cffi pycparser certifi
IMP="$VENDOR/impersonate"
mkdir -p "$IMP"
# _cffi_backend 的 so 名字带 cp 版本号，目标 3.12 的那份必须在 impersonate/ 下
for w in "$WORK"/*.whl; do
  [ -e "$w" ] || continue
  unzip -qo "$w" -d "$IMP"
done
find "$IMP" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
rm -rf "$IMP/curl_cffi/cli"   # CLI 用不到，省几 MB
if [ -n "$LOCAL_PYVER" ] && [ "$LOCAL_PYVER" != "$PYVER" ]; then
  log "补 cp$LOCAL_PYVER cffi backend（仅本地调试用）"
  dl "$LOCAL_PYVER" cffi
  for w in "$WORK"/cffi-*.whl; do
    [ -e "$w" ] || continue
    unzip -qo "$w" -d "$IMP"
    rm -f "$w"
  done
fi
rm -f "$WORK"/*.whl
# cffi / pycparser / certifi / _cffi_backend 只被 curl_cffi 用，从顶层挪走
for name in cffi pycparser certifi; do
  [ -d "$VENDOR/$name" ] && rm -rf "$VENDOR/$name"
done
rm -f "$VENDOR"/_cffi_backend.*.so

# ---------- 4. 静态 ffmpeg（HLS 合并 / 音视频合流） ----------
log "ffmpeg 7.0.2 → vendor/bin/ffmpeg"
dl "$PYVER" imageio-ffmpeg==0.7.0
for w in "$WORK"/imageio_ffmpeg-*.whl; do
  [ -e "$w" ] || continue
  unzip -qo "$w" -d "$WORK/ff"
done
mkdir -p "$VENDOR/bin"
cp "$WORK/ff/imageio_ffmpeg/binaries/$FFMPEG_NAME" "$VENDOR/bin/ffmpeg"
chmod 755 "$VENDOR/bin/ffmpeg"
rm -f "$WORK"/*.whl

# ---------- 5. Chromium 内核 ----------
BROWSERS="$VENDOR/browsers/$CHROME_DIR"
if [ -x "$BROWSERS/chrome" ]; then
  log "Chromium 已就位：$BROWSERS/chrome"
else
  cat <<EOF

[deps] 还需要手工放 Chromium 内核（约 300MB，不建议进 git）：
  $VENDOR/browsers/$CHROME_DIR/chrome
  并在该目录创建两个空标记文件：
    INSTALLATION_COMPLETE
    DEPENDENCIES_VALIDATED

  获取方式（任选其一）：
    A. python3 -m playwright install chromium   # 会把内核放到 ~/.cache/ms-playwright
       再把对应目录整个拷到 $VENDOR/browsers/
    B. 从 Chrome for Testing 官网下载对应 revision 的压缩包

  注意：settings.py::_find_chrome 只认这三个目录名：
    chrome-linux64 / chrome-linux / chrome-linux-arm64
  代码用 executable_path 显式指定内核，所以 revision 与 Playwright 默认值不同也能跑：
  实测 x86 用 1208、ARM 用 1243 均正常。
EOF
fi

# ---------- 6. 内核依赖库 ----------
if [ -x "$BROWSERS/chrome" ] && [ -f "$ROOT/scripts/collect_libs.py" ]; then
  cat <<EOF

[deps] 最后一步：收集内核依赖库（脚本排除 glibc/libstdc++，避免混版崩溃）
  python3 $ROOT/scripts/collect_libs.py \\
      "$BROWSERS/chrome" < Debian bookworm sysroot 路径 > \\
      "$VENDOR/browsers/lib" $TRIPLE

  不收集也能跑：settings.py 只在 ldd 报缺库时才启用这些自带库。
EOF
fi

# ---------- 自检 ----------
echo
log "目录检查："
ls -d "$VENDOR"/* 2>/dev/null | sed "s#$ROOT/##"
echo
log "完成。接下来按 README 附录的 build_fpk.sh 打包即可。"
