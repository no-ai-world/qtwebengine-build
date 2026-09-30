#!/usr/bin/env python3
"""把 CMake 安装前缀里的 QtWebEngine 运行时文件铺成一个待分发目录（staging tree）。

这是打包的前提：build.cmd 的 :package 阶段调它，CI 的「归档运行时」再把该目录压成
zip + sha256 并发 Release。清单只写在这里一处——required 缺一个就失败，optional 存在才拷。

布局：bin\\ 下的 DLL/exe 平铺到目标根，resources\\ 与 translations\\ 按子目录保留。
平铺是刻意的：PySide6 的包根就是这个样子（QtWebEngineCore.pyd 只按 DLL 名解析），
所以解压之后可以直接把根下文件与 resources\\、translations\\ 覆盖进 PySide6。

这是 scripts/qtwebengine/stage-webengine-runtime.ps1 的 Python 版（纯标准库）。行为逐条对齐：
同样的必需/可选清单、同样的平铺与子目录规则、同样的「尺寸/sha256 只是证据、不致命」。
刻意的差别：

  * 目录拷贝的语义是「合并」，不是 PowerShell 那种嵌套。`Copy-Item -Recurse` 在目标目录
    **已存在**时会把源目录整个塞进去（translations\\qtwebengine_locales\\qtwebengine_locales\\），
    实测重跑一次打包就会多出这些文件，而打印的条数看不出来。
  * 覆盖已存在的只读文件时先去掉只读属性（对应 `Copy-Item -Force`）。shutil.copy2 会直接
    PermissionError，而且它自己又把源的只读属性复制过去，所以「源只读 + 本地重跑」这一组
    原版能过、移植版会抛异常并留下半更新的树。
  * 跳过 Hidden/System 项（Get-ChildItem 不带 -Force 的默认行为）。
  * 必需文件用 is_file() 判定，比原版的 Test-Path 严格一档：原版只问「在不在」，所以一个
    名叫 Qt6WebEngineCore.dll 的**目录**也能让它放行，然后拷出一个空目录当运行时。
  * 尺寸/sha256 那段整体包在 try 里：它只是证据，铺入本身在上面已经成功，不该把一次
    成功的打包判成失败。PS 版只对 Get-FileHash 做了 try，取尺寸那步仍可能抛。

已知且**不打算**对齐的差别：PowerShell 的 Write-Error 会打一个 5 行装饰块（脚本名/行号/源码
回显 + CRLF），这里只打一行。按子串匹配的检查不受影响。

用法（与 PS 版一致，参数名不动，方便 build.cmd 只改脚本名）：
    python stage-webengine-runtime.py -Source <安装前缀> -Destination <待分发目录> -Create
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import stat
import sys
from pathlib import Path

# 必须存在的条目：缺任何一个，这套运行时都不完整
REQUIRED_BIN = [
    "Qt6WebEngineCore.dll",
    "QtWebEngineProcess.exe",
]
REQUIRED_RESOURCES = [
    "resources\\icudtl.dat",
    "resources\\qtwebengine_resources.pak",
]
# 存在才拷的条目：Widgets/Quick 是同一份构建的一部分，版本必须一起换
OPTIONAL_BIN = [
    "Qt6WebEngineWidgets.dll",
    "Qt6WebEngineQuick.dll",
    "Qt6WebEngineQuickDelegatesQml.dll",
]

# Hidden/System：PowerShell 的 Get-ChildItem 不带 -Force 时会跳过这类项，iterdir 不会。
FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4


def is_hidden(path: Path) -> bool:
    """PowerShell 的 Get-ChildItem 不带 -Force 时会跳过 Hidden/System 项，Python 不会。

    源树里出现隐藏文件本来就不正常（CMake 装出来的都不是），但「拷贝集」和「打印的条数」
    跟着变的话，产物清单就和原版对不上了，所以照 Get-ChildItem 的规矩来。
    （非 Windows 上没有 st_file_attributes，直接当可见。）
    """
    try:
        attrs = path.stat().st_file_attributes  # type: ignore[attr-defined]
    except (OSError, AttributeError):
        return False
    return bool(attrs & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM))


def copy_file(src: Path, dst: Path) -> None:
    """等价于 Copy-Item -Force：目标已存在时先去掉只读属性。

    shutil.copy2 会用 'wb' 打开目标，目标只读时直接 PermissionError；而 copy2 自己又把源的
    只读属性一起复制过去，所以「源只读 + 目标已存在」这一组（本地重跑打包）原版能过、移植版
    会抛异常并留下半更新的树。
    """
    if dst.exists():
        try:
            dst.chmod(dst.stat().st_mode | stat.S_IWRITE)
        except OSError:
            pass
    shutil.copy2(src, dst)


def copy_tree(src: Path, dst: Path) -> None:
    """等价于 Copy-Item -Recurse -Force，但目标已存在时是**合并**而不是嵌套。

    （原版 `Copy-Item -Recurse` 在目标目录已存在时会把源目录整个塞进去，实测重跑一次会多出
    translations\\qtwebengine_locales\\qtwebengine_locales\\——而打印的条数看不出来。）
    """
    dst.mkdir(parents=True, exist_ok=True)
    for entry in sorted(src.iterdir(), key=lambda p: p.name.lower()):
        if is_hidden(entry):
            continue
        target = dst / entry.name
        if entry.is_dir():
            copy_tree(entry, target)
        else:
            copy_file(entry, target)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="stage the QtWebEngine runtime from a CMake install prefix for distribution"
    )
    ap.add_argument("-Source", required=True, help="CMake 安装前缀（cmake --install 的落点，bin\\ 下有 DLL）")
    ap.add_argument("-Destination", required=True, help="待分发目录")
    ap.add_argument("-Create", action="store_true", help="目标目录不存在时创建")
    args = ap.parse_args()

    source = Path(args.Source)
    destination = Path(args.Destination)
    bin_src = source / "bin"

    if not source.is_dir():
        print(f"[stage] 源目录（安装前缀）不存在：{source}", file=sys.stderr)
        return 1

    # 缺文件报告用 Windows 风格的分隔符：这条消息是给人看的，也是 build.cmd 与 CI 日志里
    # 被反复引用的一句（「源目录缺少必需文件：bin\\..., bin\\...」）。
    # is_file() 比原版的 Test-Path 严格一档：原版只问「在不在」，所以一个名叫
    # Qt6WebEngineCore.dll 的**目录**也能让它放行，然后拷出一个空目录当运行时。这是刻意的。
    missing: list[str] = []
    for name in REQUIRED_BIN:
        if not (bin_src / name).is_file():
            missing.append(f"bin\\{name}")
    for rel in REQUIRED_RESOURCES:
        if not (source / rel).is_file():
            missing.append(rel)
    if missing:
        print(f"[stage] 安装前缀缺少必需文件：{', '.join(missing)}", file=sys.stderr)
        return 1

    if not destination.is_dir():
        if not args.Create:
            print(f"[stage] 目标目录不存在：{destination}（要新建请加 -Create）", file=sys.stderr)
            return 1
        try:
            destination.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # 典型情况：目标路径已经存在、但是个文件。原来这里会抛一个未捕获的 FileExistsError
            # 回溯，rc 1 一样，但日志里是一堆栈而不是一句话。
            print(f"[stage] 目标目录建不出来：{destination}（{exc}）", file=sys.stderr)
            return 1

    copied: list[Path] = []

    # bin\ 下的文件平铺到目标根
    for name in REQUIRED_BIN + OPTIONAL_BIN:
        src = bin_src / name
        if not src.is_file():
            # Write-Warning 走的是警告流，在重定向的 CI 里落在 stdout 上，而且带 "WARNING: " 前缀。
            # 这里照抄，免得「少了哪个可选 DLL」这条信息在不看 stderr 的地方消失。
            print(f"WARNING: [stage] 跳过（源不存在）：bin\\{name}")
            continue
        dst = destination / name
        copy_file(src, dst)
        copied.append(dst)

    # resources\：文件 + 名字里带 locales 的子目录（语言包在不同 Qt 版本里位置不同）
    res_src = source / "resources"
    if res_src.is_dir():
        res_dst = destination / "resources"
        res_dst.mkdir(parents=True, exist_ok=True)
        for entry in sorted(res_src.iterdir(), key=lambda p: p.name.lower()):
            if is_hidden(entry):
                continue
            if entry.is_file():
                copy_file(entry, res_dst / entry.name)
                copied.append(res_dst / entry.name)
            elif entry.is_dir() and "locales" in entry.name.lower():
                copy_tree(entry, res_dst / entry.name)
                copied.append(res_dst / entry.name)

    # translations\qtwebengine*（Qt 6.8.3 的语言包在 translations\qtwebengine_locales\）
    tr_src = source / "translations"
    if tr_src.is_dir():
        tr_dst = destination / "translations"
        tr_dst.mkdir(parents=True, exist_ok=True)
        for entry in sorted(tr_src.iterdir(), key=lambda p: p.name.lower()):
            if is_hidden(entry):
                continue
            if not entry.name.lower().startswith("qtwebengine"):
                continue
            dst = tr_dst / entry.name
            if entry.is_dir():
                copy_tree(entry, dst)
            else:
                copy_file(entry, dst)
            copied.append(dst)

    # 回显调用方给的原始字符串，而不是规范化后的 Path（原版插值 $Destination 就是这个行为）
    print(f"[stage] 完成，共铺入 {len(copied)} 项 → {args.Destination}")

    # 尺寸/sha256 只是留证据，铺入本身到上面已经成功；这段不该把成功的打包判成失败
    try:
        core = destination / "Qt6WebEngineCore.dll"
        if core.is_file():
            mb = core.stat().st_size / (1024 * 1024)
            try:
                digest = hashlib.sha256(core.read_bytes()).hexdigest()
            except OSError:
                digest = ""
            if digest:
                print(f"[stage] Qt6WebEngineCore.dll  {mb:,.1f} MB  sha256={digest}")
            else:
                print(f"[stage] Qt6WebEngineCore.dll  {mb:,.1f} MB")
    except OSError as exc:  # noqa: BLE001
        print(f"[stage] 尺寸/sha256 证据不可得（不影响铺入结果）：{exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
