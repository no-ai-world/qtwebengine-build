#!/usr/bin/env python3
"""让 Chromium 的 cc_wrapper 能落到 MSVC 工具链上，这样 ccache 才真的会被调用。

Chromium 是用 cl_prefix 拼编译器命令行的（chromium/build/toolchain/win/toolchain.gni），
而它只在工具链是 clang 时才填 cl_prefix：

    } else if (toolchain_cc_wrapper != "" && toolchain_is_clang) {
      cl_prefix = toolchain_cc_wrapper + " "
    } else {
      cl_prefix = ""
    }

patch-gn-args.py 确实往 core 模块的 args.gn 里注入了 cc_wrapper="ccache"（构建日志里能
查到），但 Qt 在 Windows 上的 QtWebEngine 构建是 is_clang=false（args.gn：is_clang=false、
is_msvc=true），于是那个值被直接丢掉、ccache 一次都不会被调用。实测那个配置：编了 8038 个
目标，ccache 目录 391 字节，`ccache --show-stats` 里连 Cacheable calls 都没有。

这就是 kiwi 那套配方（一轮编一部分，缓存把它带进下一轮）在这里跑不通、而同一套配方在
kiwi browser 的 CI 上却好用的全部原因：kiwi 是在 Linux 上给 Android 编 Chromium，也就是
clang，而且它通过 CC/CXX 自己包住编译器。ccache 是支持 MSVC 的——它自己的支持表把 MSVC
评为 A 级——所以唯一缺的就是这个条件。

本补丁去掉 toolchain_is_clang 这层判断，于是 MSVC 下也会给编译器加前缀。cc_wrapper 为空
（USE_CCACHE=0）时什么都不会变：cl_prefix 仍然是空的。幂等，并且锚在上游那段原文上，所以
上游一旦改动或重写，它会响亮地失败，而不是被顺手改坏。

这是 scripts/qtwebengine/patch-msvc-ccache.ps1 的 Python 版（纯标准库）。行为逐条对齐，
包括「缩进跟随原行」「注释留在前一行以保持 if/else 链形状」「落盘后重新读回来断言新旧条件
的条数」，以及写回不带 BOM。唯一刻意的差别是读文件用严格 UTF-8：PowerShell 会把非法字节换
成 U+FFFD 再写回，等于静默改坏源码。

另外，目标存在但不是普通文件（同名目录）时直接失败，不会被 -AllowMissing 静默跳过——
原版的 Test-Path 只问「在不在」，会放行，随后 ReadAllText 才炸。

这一处原文用的是 `[System.IO.File]::ReadAllText`，所以它**没有**那两个 Get-Content -Raw 脚本
的 0 字节问题（ReadAllText 对空文件返回 ""，不是 $null）：空文件在这里本来就是
「找不到锚点」并退出 1，两边一致。

用法（与 PS 版一致，参数名不动，方便 build.cmd 只改脚本名）：
    python patch-msvc-ccache.py -SourceRoot <src>
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

RELATIVE_FILE = "src/3rdparty/chromium/build/toolchain/win/toolchain.gni"

ORIGINAL = '} else if (toolchain_cc_wrapper != "" && toolchain_is_clang) {'
PATCHED = '} else if (toolchain_cc_wrapper != "") {'



def display_path(root: str, relative: str) -> str:
    """PowerShell Join-Path 的显示形态（只用于消息）。

    Join-Path 是字符串拼接、并把分隔符统一成反斜杠；而 `Path(root) / Path(rel)` 会吃掉
    开头的 "./"（Path(".") / "x" 给 "x"，Join-Path 给 ".\\x"）。CI 传的是绝对路径，
    两者本来就一样；补上这一步是为了让 -SourceRoot . 这类调用下的日志也逐字节可比。
    """
    return root.rstrip("\\/") + "\\" + relative.replace("/", "\\")

def main() -> int:
    ap = argparse.ArgumentParser(
        description="make Chromium's cc_wrapper reach the MSVC toolchain (ccache)"
    )
    ap.add_argument("-SourceRoot", required=True, help="QtWebEngine source checkout")
    ap.add_argument(
        "-AllowMissing",
        action="store_true",
        help="文件或锚点不存在时按跳过处理（exit 0）",
    )
    args = ap.parse_args()

    target = Path(args.SourceRoot) / RELATIVE_FILE
    display = display_path(args.SourceRoot, RELATIVE_FILE)

    if not target.exists():
        if args.AllowMissing:
            print(f"[msvc-ccache] 目标文件不存在，按 AllowMissing 跳过：{display}")
            return 0
        print(
            f"[msvc-ccache] 目标文件不存在：{display}（源码未取全？要跳过补丁请设 SKIP_PATCH=1）",
            file=sys.stderr,
        )
        return 1
    if not target.is_file():
        # 同名目录：原版的 Test-Path 会放行，随后 ReadAllText 才炸。别把它当成「不存在」而被
        # -AllowMissing 静默跳过。
        print(f"[msvc-ccache] 目标不是普通文件：{display}", file=sys.stderr)
        return 1

    content = target.read_bytes().decode("utf-8-sig")
    eol = "\r\n" if "\r\n" in content else "\n"

    if ORIGINAL not in content:
        if PATCHED in content:
            print("[msvc-ccache] 已打过（cc_wrapper 对 MSVC 工具链也生效），跳过")
            return 0
        if args.AllowMissing:
            print("[msvc-ccache] 未找到锚点，按 AllowMissing 跳过（上游可能已改）")
            return 0
        print(
            f"[msvc-ccache] 未找到锚点：{ORIGINAL} （{RELATIVE_FILE} 结构变了，需要人工核对）",
            file=sys.stderr,
        )
        return 1

    lines = re.split(r"\r\n|\n", content)
    hits = [line for line in lines if ORIGINAL in line]
    if len(hits) != 1:
        if args.AllowMissing:
            return 0
        print(f"[msvc-ccache] 锚点出现 {len(hits)} 次，预期 1 次；拒绝猜测", file=sys.stderr)
        return 1

    # 缩进跟随原行：上游是 4 空格（toolchain.gni 在 if/else 链里多缩进一层）
    indent = re.match(r"(\s*)", hits[0]).group(1)

    out: list[str] = []
    for line in lines:
        if ORIGINAL in line:
            # 注释留在前一行：保持 if/else 链的形状不变，只把条件放宽
            out.append(
                f"{indent}# qtwebengine-build: also wrap the MSVC toolchain - ccache supports cl.exe,"
            )
            out.append(
                f"{indent}# and without this the injected cc_wrapper is silently ignored (is_clang=false)."
            )
            out.append(f"{indent}{PATCHED}")
        else:
            out.append(line)

    target.write_bytes(eol.join(out).encode("utf-8"))

    # 断言落盘结果：新条件正好一处，旧条件一处不剩
    after = target.read_bytes().decode("utf-8-sig")
    new_count = len(re.findall(re.escape(PATCHED), after))
    old_count = len(re.findall(re.escape(ORIGINAL), after))
    if new_count != 1 or old_count != 0:
        print(
            f"[msvc-ccache] 落盘校验失败：新条件 {new_count} 处（预期 1），旧条件 {old_count} 处（预期 0）",
            file=sys.stderr,
        )
        return 1

    print(f"[msvc-ccache] 已修补 {RELATIVE_FILE}：")
    print("[msvc-ccache]   cc_wrapper 现在对 MSVC（is_clang=false）工具链也生效")
    return 0


if __name__ == "__main__":
    sys.exit(main())
