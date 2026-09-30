#!/usr/bin/env python3
"""应用 v8/cppgc 补丁：MSVC 14.44（VS 2022 17.14）编 Qt 6.8.3 的 V8 会报

  error C2352: 'cppgc::internal::MarkingStateBase::MarkNoPush':
               a call of a non-static member function requires an object

参考手册（WebEngineMP4Build_6.8.3.md）用 `git apply` 打这个补丁，但那份 diff 只有
增删两行、没有上下文行，行号一漂就失效；这里改为按「整条语句」精确替换，只依赖那一行
的内容而不依赖行号。两者语义等价（都是取基类成员），因此即便上游某天自己修好了，
本脚本找不到目标模式也只是安静跳过，不会把树改坏。

幂等：已修好则跳过；目标文件不存在且 -AllowMissing 时跳过；其余异常一律非零退出。

这是 scripts/qtwebengine/patch-cppgc.ps1 的 Python 版（纯标准库）。行为逐条对齐：
同样的退出码、同样的输出文本、同样「只在命中的那一行上做整串替换」。区别只有两处，
都是刻意的：一是读文件用严格 UTF-8 解码（PowerShell 会把非法字节换成 U+FFFD 再写回，
等于静默改坏文件），二是写回时明确不带 BOM（Chromium 源码无 BOM，带 BOM 会被 lint 视作污染）。

另外三处是「原版在这里会崩/会走偏，这版直接拒绝」，都落在坏树上：

  * 0 字节的目标文件：原版 Get-Content -Raw 得到 $null，再 .Contains() 就是
    "You cannot call a method on a null-valued expression"，退出 1；这版也给 1，
    但带一句能看懂的话，而且不会被 -AllowMissing 变成 0（空文件是坏树，不是「上游已修好」）；
  * 目标存在但不是普通文件（同名目录）：原版 Test-Path 放行、读取才炸；这版直接失败；
  * -RelativeFile 传绝对路径：`Path(root) / Path("D:/abs/x")` 会丢掉 root，于是补丁会去改
    源码树外面一个不相干的文件并报成功（原版 Join-Path 是字符串拼接，拼出来找不到）。
    这版拒绝绝对路径——补丁永远不该碰到 -SourceRoot 之外的东西。

已知且**不打算**对齐的差别：PowerShell 的 Write-Error 会打一个 5 行装饰块（脚本名/行号/源码
回显 + CRLF），这里只打一行。按子串匹配的检查不受影响。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

DEFAULT_RELATIVE_FILE = "src/3rdparty/chromium/v8/src/heap/cppgc/marking-state.h"

BROKEN_CALL = "MutatorMarkingState::BasicMarkingState::MarkNoPush(header);"
FIXED_CALL = "this->BasicMarkingState::MarkNoPush(header);"
BROKEN_STMT = f"return {BROKEN_CALL}"
FIXED_STMT = f"return {FIXED_CALL}"


def read_text(path: Path) -> str:
    """严格 UTF-8，允许开头的 BOM（原 PS 版写回时不带 BOM，这里保持一致）。"""
    return path.read_bytes().decode("utf-8-sig")


def write_text(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))


def resolve_target(source_root: str, relative_file: str) -> tuple[Path | None, str]:
    """把 -SourceRoot 与 -RelativeFile 拼成目标路径。

    必须自己判绝对路径：`Path(root) / Path("D:/abs/x")` 会**丢掉 root**，于是补丁会去改源码树
    外面一个不相干的文件并且报成功；PowerShell 的 Join-Path 是字符串拼接，拼出来找不到、报错。
    这里直接拒绝绝对路径，比两边都更安全（补丁永远不该碰到 -SourceRoot 之外的东西）。
    """
    rel = Path(relative_file)
    if rel.is_absolute():
        return None, f"[patch] -RelativeFile 必须是相对路径（拼在 -SourceRoot 之下）：{relative_file}"
    return Path(source_root) / rel, ""



def display_path(root: str, relative: str) -> str:
    """PowerShell Join-Path 的显示形态（只用于消息）。

    Join-Path 是字符串拼接、并把分隔符统一成反斜杠；而 `Path(root) / Path(rel)` 会吃掉
    开头的 "./"（Path(".") / "x" 给 "x"，Join-Path 给 ".\\x"）。CI 传的是绝对路径，
    两者本来就一样；补上这一步是为了让 -SourceRoot . 这类调用下的日志也逐字节可比。
    """
    return root.rstrip("\\/") + "\\" + relative.replace("/", "\\")

def main() -> int:
    ap = argparse.ArgumentParser(
        description="patch QtWebEngine's v8/cppgc for MSVC 14.44 (C2352)",
        add_help=True,
    )
    ap.add_argument("-SourceRoot", required=True, help="QtWebEngine source checkout")
    ap.add_argument("-RelativeFile", default=DEFAULT_RELATIVE_FILE)
    ap.add_argument(
        "-AllowMissing",
        action="store_true",
        help="找不到文件/语句时按跳过处理（上游已修好时用）",
    )
    args = ap.parse_args()

    target, problem = resolve_target(args.SourceRoot, args.RelativeFile)
    if target is None:
        print(problem, file=sys.stderr)
        return 1
    display = display_path(args.SourceRoot, args.RelativeFile)

    if not target.exists():
        if args.AllowMissing:
            print(f"[patch] 目标文件不存在，按 AllowMissing 跳过：{display}")
            return 0
        print(
            f"[patch] 目标文件不存在：{display}"
            "（源码或子模块未取全？要跳过补丁请设 SKIP_PATCH=1）",
            file=sys.stderr,
        )
        return 1
    if not target.is_file():
        # 同名目录：原版的 Test-Path 会放行，随后读取才炸。别把它当成「不存在」而被
        # -AllowMissing 静默跳过——那等于把一棵坏树当成「上游已修好」。
        print(f"[patch] 目标不是普通文件：{display}", file=sys.stderr)
        return 1

    text = read_text(target)

    if not text:
        # 0 字节文件：原版在这里崩（Get-Content -Raw 得到 $null，再 .Contains() 就是
        # "You cannot call a method on a null-valued expression"，退出 1）。空文件是坏树，
        # 不是「上游已修好」，不能安静跳过。
        print(f"[patch] 目标文件是空的：{display}（源码没取全？）", file=sys.stderr)
        return 1

    if FIXED_STMT in text:
        print("[patch] 已是修补后形态，跳过")
        return 0

    hits = text.count(BROKEN_STMT)
    if hits == 0:
        print("[patch] 未找到待修补语句（上游可能已自行修复），跳过")
        return 0
    if hits > 1:
        print(
            f"[patch] 待修补语句出现 {hits} 次，预期 1 次；拒绝猜测，请人工确认",
            file=sys.stderr,
        )
        return 1

    updated = text.replace(BROKEN_STMT, FIXED_STMT)
    if updated == text:
        print("[patch] 替换未生效（内容未变化）", file=sys.stderr)
        return 1

    write_text(target, updated)

    print(f"[patch] 已修补 {args.RelativeFile}")
    print(f"[patch]   - {BROKEN_STMT}")
    print(f"[patch]   + {FIXED_STMT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
