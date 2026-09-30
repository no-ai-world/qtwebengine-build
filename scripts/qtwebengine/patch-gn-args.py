#!/usr/bin/env python3
"""往 QtWebEngine 的 Chromium 构建里注入 gn 参数：symbol_level，以及（可选）cc_wrapper。

为什么需要它：QtWebEngine 的 src/core/CMakeLists.txt 自己拼 gnArgs（gnArgArg），
没有给外部留「额外 gn 参数」的通道，所以只能改这处 CMakeLists。改动是加一段带标记的
list(APPEND gnArgArg ...)，可重复执行（先删旧块再插新块）。

关于 cc_wrapper：光有这个文件不够。这里原来断言「Windows 上 Chromium 用自带 clang-cl、
is_clang 为真」——实测是错的：构建日志里的 args.gn 明确写着 is_clang=false、is_msvc=true
（Qt 的 MSVC 版 QtWebEngine 就是这么配的），而
  chromium/build/toolchain/win/toolchain.gni 里
      } else if (toolchain_cc_wrapper != "" && toolchain_is_clang) {
        cl_prefix = toolchain_cc_wrapper + " "
只在 toolchain_is_clang 为真时才把 cc_wrapper 拼到 cl.exe 前面。于是 args.gn 里的
cc_wrapper="ccache" 被整体丢掉：实测编到 [8038/29705]，ccache 目录 391 字节、
`ccache --show-stats` 里连 Cacheable calls 都没有——一次都没被调用。
patch-msvc-ccache.py 补上那一处条件（ccache 官方把 MSVC 列为 A 级支持）。
  代价：设了 cc_wrapper 后 show_includes 会从 /showIncludes:user 退回 /showIncludes
  （源码里有注释说明，是绕 sccache 的老问题），.ninja_deps 变大、依赖解析变慢一点。
  好在 /showIncludes:user 的条件同样要求 toolchain_is_clang，所以 MSVC 分支本来就用
  完整的 /showIncludes，ccache 解析依赖不受影响。

关于 symbol_level：Qt 在 RelWithDebInfo + MSVC 下自己写 symbol_level=2
（cmake/Functions.cmake：WIN32 AND NOT CLANG -> symbol_level=2），也就是每个 obj 都
走 /Zi + mspdbsrv 写 PDB：更慢、更占内存，而且 PDB 服务一旦卡住所有 cl.exe 一起堵死
（正是「日志几十分钟一行不出」那种症状的候选原因之一）。产物只当运行时用，不需要调试
信息，所以这里注入 symbol_level=0；要调试信息就用 -SymbolLevel 2。

幂等与可断言：注入后断言 list(APPEND gnArgArg 的出现次数正好 +1；不带任何开关调用则只清除注入块。

这是 scripts/qtwebengine/patch-gn-args.ps1 的 Python 版（纯标准库），行为逐条对齐，包括
「保留原文件的换行风格」和「只在真的摘掉了块时才写回」。

几处刻意的差别（都是 PowerShell 语义，Python 默认不一样）：

  * 标记块与锚点的比较用 casefold：PowerShell 的 -eq 对字符串不区分大小写。不对齐的话，
    手改过大小写的旧块会认不出来（留下旧块再插一段 = 两个 list(APPEND gnArgArg），
    而 CMake 命令被改成大写时又会报「未找到锚点」而中止 patch 阶段；
  * list(APPEND gnArgArg 的计数同样不区分大小写（实测不影响结果，只是少一处差异）；
  * -RelativeFile 拒绝绝对路径：`Path(root) / Path("D:/abs/x")` 会丢掉 root，去改源码树
    外面的文件并报成功；
  * 目标存在但不是普通文件（同名目录）时直接失败，不会被 -AllowMissing 静默跳过。

注入的标记文案写的是本脚本自己的名字（.py）。它同时认得 .ps1 时代写的旧块并会摘掉，
所以从 .ps1 迁移过来的树第一次跑就会换成新标记，不会插出第二段。

用法（与 PS 版一致，参数名不动，方便 build.cmd 只改脚本名）：
    python patch-gn-args.py -SourceRoot <src> -SymbolLevel 0 -UseCcache
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

BEGIN = "# >>> qtwebengine-build gn args (managed by scripts/qtwebengine/patch-gn-args.py) >>>"
END = "# <<< qtwebengine-build gn args <<<"

# 旧版标记：.ps1 时代写进去的块也要能摘掉，否则第一次用 .py 版本跑会插出第二段
LEGACY_BEGIN = "# >>> qtwebengine-build gn args (managed by scripts/qtwebengine/patch-gn-args.ps1) >>>"
LEGACY_END = "# <<< qtwebengine-build gn args <<<"

# 幂等靠这几个标记做「是不是自己上一轮写的」判定。大小写不敏感：PowerShell 的 -eq 对字符串
# 就是不敏感的，手改过大小写的旧块原版照样认得出、这里也必须认得出——否则会留下旧块再插一段，
# 变成两个 list(APPEND gnArgArg（而 before+1 的断言照样通过，没人拦得住）。
BEGIN_KEYS = {BEGIN.casefold(), LEGACY_BEGIN.casefold()}
END_KEYS = {END.casefold(), LEGACY_END.casefold()}

APPEND_RE = re.compile(r"list\(APPEND gnArgArg", re.IGNORECASE)


def resolve_target(source_root: str, relative_file: str) -> tuple[Path | None, str]:
    """把 -SourceRoot 与 -RelativeFile 拼成目标路径。

    必须自己判绝对路径：`Path(root) / Path("D:/abs/x")` 会**丢掉 root**，于是补丁会去改源码树
    外面一个不相干的文件并且报成功；PowerShell 的 Join-Path 是字符串拼接，拼出来找不到、报错。
    这里直接拒绝绝对路径，比两边都更安全。
    """
    rel = Path(relative_file)
    if rel.is_absolute():
        return None, f"[gn-args] -RelativeFile 必须是相对路径（拼在 -SourceRoot 之下）：{relative_file}"
    return Path(source_root) / rel, ""



def display_path(root: str, relative: str) -> str:
    """PowerShell Join-Path 的显示形态（只用于消息）。

    Join-Path 是字符串拼接、并把分隔符统一成反斜杠；而 `Path(root) / Path(rel)` 会吃掉
    开头的 "./"（Path(".") / "x" 给 "x"，Join-Path 给 ".\\x"）。CI 传的是绝对路径，
    两者本来就一样；补上这一步是为了让 -SourceRoot . 这类调用下的日志也逐字节可比。
    """
    return root.rstrip("\\/") + "\\" + relative.replace("/", "\\")

def main() -> int:
    ap = argparse.ArgumentParser(description="inject gn args into QtWebEngine's core CMakeLists")
    ap.add_argument("-SourceRoot", required=True)
    ap.add_argument(
        "-UseCcache",
        action="store_true",
        help='注入 cc_wrapper="ccache"（还需要 patch-msvc-ccache.py 放行 MSVC 工具链）',
    )
    ap.add_argument("-SymbolLevel", default="", help="注入 symbol_level=<N>；留空表示不注入")
    ap.add_argument("-RelativeFile", default="src/core/CMakeLists.txt")
    ap.add_argument("-Anchor", default="append_toolchain_setup(gnArgArg)")
    ap.add_argument("-AllowMissing", action="store_true")
    args = ap.parse_args()

    target, problem = resolve_target(args.SourceRoot, args.RelativeFile)
    if target is None:
        print(problem, file=sys.stderr)
        return 1
    display = display_path(args.SourceRoot, args.RelativeFile)

    if not target.exists():
        if args.AllowMissing:
            print(f"[gn-args] 目标文件不存在，按 AllowMissing 跳过：{display}")
            return 0
        print(
            f"[gn-args] 目标文件不存在：{display}（源码未取全？要跳过请设 SKIP_PATCH=1）",
            file=sys.stderr,
        )
        return 1
    if not target.is_file():
        # 同名目录：原版的 Test-Path 会放行，随后读取才炸。别把它当成「不存在」而被
        # -AllowMissing 静默跳过。
        print(f"[gn-args] 目标不是普通文件：{display}", file=sys.stderr)
        return 1

    raw = target.read_bytes().decode("utf-8-sig")
    # 保留原文件的换行风格：把整份文件改成 CRLF 会让后续 diff 全红，没必要
    eol = "\r\n" if "\r\n" in raw else "\n"
    lines = re.split(r"\r\n|\n", raw)

    # 先摘掉上一次注入的块（幂等）
    kept: list[str] = []
    inside = False
    for line in lines:
        key = line.strip().casefold()
        if key in BEGIN_KEYS:
            inside = True
            continue
        if key in END_KEYS:
            inside = False
            continue
        if not inside:
            kept.append(line)

    # 组装本次要注入的内容
    inject: list[str] = []
    if args.SymbolLevel != "":
        inject.append(f"        symbol_level={args.SymbolLevel}")
    if args.UseCcache:
        inject.append('        cc_wrapper="ccache"')

    anchor_key = args.Anchor.casefold()
    anchor_idx = -1
    for i, line in enumerate(kept):
        # casefold：原版是 $kept[$i].Trim() -eq $Anchor，PowerShell 的 -eq 不区分大小写。
        # 不对齐的话，上游把 CMake 命令改成大写（CMake 本身不区分命令大小写）就会报
        # 「未找到锚点」而中止 patch 阶段。
        if line.strip().casefold() == anchor_key:
            if anchor_idx >= 0:
                print(f"[gn-args] 锚点 '{args.Anchor}' 出现多次，拒绝猜测", file=sys.stderr)
                return 1
            anchor_idx = i

    if not inject:
        if len(kept) != len(lines):
            print("[gn-args] 无注入内容，已清除既有注入块")
            # 只在真的摘掉了块时才写回：无条件重写会让「文件未改动」名不副实，
            # 而且会把混合行尾或带 BOM 的文件整份改写
            target.write_bytes(eol.join(kept).encode("utf-8"))
        else:
            print("[gn-args] 无注入内容，文件未改动")
        return 0

    if anchor_idx < 0:
        if args.AllowMissing:
            print(f"[gn-args] 未找到锚点 '{args.Anchor}'，按 AllowMissing 跳过（源码版本可能已变）")
            return 0
        print(
            f"[gn-args] 未找到锚点 '{args.Anchor}'：{args.RelativeFile} 结构变了，需要人工核对",
            file=sys.stderr,
        )
        return 1

    before = sum(1 for line in kept if APPEND_RE.search(line))

    result: list[str] = []
    for i, line in enumerate(kept):
        result.append(line)
        if i == anchor_idx:
            result.append("")
            result.append(BEGIN)
            result.append("    list(APPEND gnArgArg")
            result.extend(inject)
            result.append("    )")
            result.append(END)

    after = sum(1 for line in result if APPEND_RE.search(line))
    if after != before + 1:
        print(
            f"[gn-args] 注入后 list(APPEND gnArgArg 数量为 {after}，预期 {before + 1}",
            file=sys.stderr,
        )
        return 1

    target.write_bytes(eol.join(result).encode("utf-8"))
    print(f"[gn-args] 已注入到 {args.RelativeFile}（锚点后）：")
    for line in inject:
        print(f"[gn-args]   {line.strip()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
