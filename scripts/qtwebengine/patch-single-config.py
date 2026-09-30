#!/usr/bin/env python3
"""把 QtWebEngine 的模块构建收成单个 CMake 配置。

模块构建不传 -G，于是 CMake 用它自己的 Windows 默认生成器 "Visual Studio 17 2022"——
多配置生成器。装好的 Qt 再通过 QtBuildInternalsExtra.cmake 把 CMAKE_CONFIGURATION_TYPES
强制设成 "RelWithDebInfo;Debug"（实测：configure 打印 "Building for multiple
configurations: RelWithDebInfo;Debug."）。

QtWebEngine 每个配置接一棵 gn/ninja 树，而每一棵都是 WebEngineCore 目标的依赖，所以编
这棵树会把两个配置都编一遍。第一次成功编译时实测：ninja 在 .../RelWithDebInfo/AMD64 上
56 分钟编到 [8038/29705]，而 Debug 那棵的 gn 生成早已跑完、它的 ninja 正排在后面等着。
对一个只安装一个配置的运行时来说（Debug 产物带 CMAKE_DEBUG_POSTFIX，根本铺不到 PySide6
上），这等于把小时数和磁盘都翻倍。

本补丁在 Qt 的包说完话之后、工程生成之前，把 CMAKE_CONFIGURATION_TYPES 重新强制成我们
要的那一个。值来自命令行上的 -DQTWE_BUILD_CONFIGURATION，所以 build.cmd 的 BUILD_TYPE
仍是唯一事实来源；没有这个 define 时整块不做事，旧行为原样保留。build.cmd 的
:assert_config 会在收窄没生效的几秒内让这一轮失败，所以它不可能悄悄赔掉一次数小时构建。

幂等，带标记块。

这是 scripts/qtwebengine/patch-single-config.ps1 的 Python 版（纯标准库）。行为逐条对齐，
两处刻意的差别与 patch-cppgc.py 相同：读文件用严格 UTF-8（PowerShell 的 Get-Content 会把
非法字节换成 U+FFFD 再写回，等于静默改坏文件），写回明确不带 BOM。第三处差别在写入时机：
内容一致时**不重写文件**，这样「文件未改动」名副其实，混合行尾或带 BOM 的文件也不会被整份
改写。

对齐 PowerShell 语义的几处（Python 默认不一样）：

  * 锚点比较与「内容一致」判定都用 casefold：PowerShell 的 -eq 对字符串不区分大小写。
    不对齐的话，上游把 FIND_PACKAGE(...) 写成大写时原版照样注入，而这版会报「锚点出现 0 次」
    并把 patch 阶段整段判失败；
  * 目标存在但不是普通文件（同名目录）时直接失败，不会被 -AllowMissing 静默跳过；
  * 0 字节的目标文件也直接失败：原版在这里是崩（Get-Content -Raw 得到 $null，
    再 .Contains() 就是 "You cannot call a method on a null-valued expression"，退出 1）。
    空文件是坏树，不该被当成「上游已改好」而安静跳过。

已知且**不打算**对齐的差别：PowerShell 的 Write-Error 会打一个 5 行装饰块（脚本名/行号/源码
回显 + CRLF），这里只打一行。按子串匹配的检查不受影响。

用法（与 PS 版一致，参数名不动，方便 build.cmd 只改脚本名）：
    python patch-single-config.py -SourceRoot <src>
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

BEGIN = "# --- qtwebengine-build: single configuration ---"
END = "# --- end qtwebengine-build: single configuration ---"

# 锚点必须在 QtBuildInternalsExtra.cmake 之后：那个文件正是在 find_package 里把
# CMAKE_CONFIGURATION_TYPES FORCE 成 RelWithDebInfo;Debug 的，早于它就会被覆盖。
ANCHOR = "find_package(Qt6 6.5 CONFIG REQUIRED COMPONENTS BuildInternals Core)"

BLOCK_LINES = [
    BEGIN,
    "if(DEFINED QTWE_BUILD_CONFIGURATION)",
    '    set(CMAKE_CONFIGURATION_TYPES "${QTWE_BUILD_CONFIGURATION}" CACHE STRING "" FORCE)',
    "endif()",
    END,
]


def read_text(path: Path) -> str:
    """严格 UTF-8，允许开头的 BOM（写回时不带 BOM）。"""
    return path.read_bytes().decode("utf-8-sig")



def display_path(root: str, relative: str) -> str:
    """PowerShell Join-Path 的显示形态（只用于消息）。

    Join-Path 是字符串拼接、并把分隔符统一成反斜杠；而 `Path(root) / Path(rel)` 会吃掉
    开头的 "./"（Path(".") / "x" 给 "x"，Join-Path 给 ".\\x"）。CI 传的是绝对路径，
    两者本来就一样；补上这一步是为了让 -SourceRoot . 这类调用下的日志也逐字节可比。
    """
    return root.rstrip("\\/") + "\\" + relative.replace("/", "\\")

def main() -> int:
    ap = argparse.ArgumentParser(
        description="narrow the QtWebEngine module build to a single CMake configuration"
    )
    ap.add_argument("-SourceRoot", required=True, help="QtWebEngine source checkout")
    ap.add_argument(
        "-AllowMissing",
        action="store_true",
        help="文件或锚点不存在时按跳过处理（exit 0）",
    )
    args = ap.parse_args()

    target = Path(args.SourceRoot) / "CMakeLists.txt"
    display = display_path(args.SourceRoot, "CMakeLists.txt")
    if not target.exists():
        if args.AllowMissing:
            return 0
        print(
            f"[single-config] 目标文件不存在：{display}（源码未取全？要跳过补丁请设 SKIP_PATCH=1）",
            file=sys.stderr,
        )
        return 1
    if not target.is_file():
        # 同名目录：原版的 Test-Path 会放行，随后读取才炸。别把它当成「不存在」而被
        # -AllowMissing 静默跳过。
        print(f"[single-config] 目标不是普通文件：{display}", file=sys.stderr)
        return 1

    content = read_text(target)

    if not content:
        # 0 字节文件：原版在这里崩（Get-Content -Raw 得到 $null，再 .Contains() 就是
        # "You cannot call a method on a null-valued expression"，退出 1，-AllowMissing 也一样）。
        # 空文件是坏树，不是「上游改好了」，不能安静跳过。
        print(f"[single-config] 目标文件是空的：{display}（源码没取全？）", file=sys.stderr)
        return 1

    # 保留原文件的换行风格：把整份文件改成 CRLF 会让后续 diff 全红，没必要
    eol = "\r\n" if "\r\n" in content else "\n"
    block = eol.join(BLOCK_LINES)

    if BEGIN in content:
        # 已经打过：整块替换，保证内容与当前脚本一致（幂等）。
        # 用函数而不是替换串：块里有 ${...} 和 $ 字符，替换串会把它们当反向引用
        # （PowerShell 版就是因为这个才用了 MatchEvaluator）。
        pattern = re.escape(BEGIN) + ".*?" + re.escape(END)
        updated = re.sub(pattern, lambda _m: block, content, flags=re.DOTALL)
        # casefold：原版是 $updated -eq $content，PowerShell 的 -eq 不区分大小写，所以只差
        # 大小写时它判定「内容一致」而不写回。这里对齐，免得无谓地重写文件（也免得打印
        # 的「已更新注入块」和原版对不上）。
        if updated.casefold() == content.casefold():
            print("[single-config] 已注入且内容一致，跳过")
            return 0
        target.write_bytes(updated.encode("utf-8"))
        print("[single-config] 已更新注入块")
        return 0

    # 锚点必须唯一命中，否则拒绝猜测
    # casefold：原版是 $_.Trim() -eq $anchor，PowerShell 的 -eq 不区分大小写。CMake 的命令名
    # 本身就不区分大小写，上游写成 FIND_PACKAGE(...) 时原版照样注入；不对齐会报
    # 「锚点出现 0 次」并把 patch 阶段整段判失败。
    anchor_key = ANCHOR.casefold()
    lines = re.split(r"\r\n|\n", content)
    hits = [line for line in lines if line.strip().casefold() == anchor_key]
    if len(hits) != 1:
        if args.AllowMissing:
            return 0
        print(
            f"[single-config] 锚点出现 {len(hits)} 次，预期 1 次；拒绝猜测，请人工确认：{ANCHOR}",
            file=sys.stderr,
        )
        return 1

    out: list[str] = []
    inserted = False
    for line in lines:
        out.append(line)
        if not inserted and line.strip().casefold() == anchor_key:
            out.append("")
            out.extend(BLOCK_LINES)
            inserted = True

    if not inserted:
        if args.AllowMissing:
            return 0
        print("[single-config] 未能插入注入块（锚点匹配但循环未命中）", file=sys.stderr)
        return 1

    target.write_bytes(eol.join(out).encode("utf-8"))
    print("[single-config] 已注入到 CMakeLists.txt（锚点后）：")
    print('[single-config]   set(CMAKE_CONFIGURATION_TYPES "${QTWE_BUILD_CONFIGURATION}" ... FORCE)')
    return 0


if __name__ == "__main__":
    sys.exit(main())
