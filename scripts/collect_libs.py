#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 sysroot 收集某个 ELF 的运行时依赖库闭包，输出到指定目录。

用法: collect_libs.py <chrome路径> <sysroot> <输出目录> [triple]
排除 glibc 家族与 libstdc++/libgcc（这些必须用系统自带的，混用会崩）。
"""
import os
import shutil
import subprocess
import sys

# 必须由系统提供：换版本会直接崩
SKIP_PREFIX = ("ld-linux", "libc.so", "libm.so", "libpthread.so", "libdl.so",
               "librt.so", "libresolv.so", "libnsl.so", "libutil.so",
               "libcrypt.so", "libanl.so", "libnss_files.so", "libnss_dns.so",
               "libmvec.so", "libstdc++.so", "libgcc_s.so")


def needed(path):
    try:
        proc = subprocess.run(["readelf", "-d", path], capture_output=True,
                              timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    out = (proc.stdout or b"").decode("utf-8", "replace")
    deps = []
    for line in out.splitlines():
        if "(NEEDED)" not in line:
            continue
        dep = line.split("[")[-1].split("]")[0].strip()
        if dep:
            deps.append(dep)
    return deps


def find_lib(sysroot, name, triples):
    for t in triples:
        for base in ("usr/lib", "lib"):
            candidate = os.path.join(sysroot, base, t, name)
            if os.path.exists(candidate):
                return candidate
    for base in ("usr/lib", "lib", "usr/lib64", "lib64"):
        candidate = os.path.join(sysroot, base, name)
        if os.path.exists(candidate):
            return candidate
    return ""


def main():
    chrome, sysroot, outdir = sys.argv[1], sys.argv[2], sys.argv[3]
    triples = sys.argv[4:] or ["x86_64-linux-gnu", "aarch64-linux-gnu"]
    os.makedirs(outdir, exist_ok=True)

    queue = list(needed(chrome))
    seen = set()
    copied = []
    unresolved = []
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        if name.startswith(SKIP_PREFIX):
            continue
        src = find_lib(sysroot, name, triples)
        if not src:
            unresolved.append(name)
            continue
        dst = os.path.join(outdir, name)
        shutil.copy2(os.path.realpath(src), dst)
        link = os.path.realpath(src)
        chmod_mode = os.stat(link).st_mode
        os.chmod(dst, chmod_mode | 0o644)
        copied.append(name)
        for dep in needed(link):
            if dep not in seen:
                queue.append(dep)

    total = sum(os.path.getsize(os.path.join(outdir, f)) for f in os.listdir(outdir))
    print("收集 %d 个库，共 %.1f MB" % (len(copied), total / 1048576.0))
    if unresolved:
        print("未在 sysroot 找到 %d 个:" % len(unresolved))
        for u in unresolved:
            print("  -", u)


if __name__ == "__main__":
    main()
