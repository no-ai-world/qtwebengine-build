#!/usr/bin/env python3
"""用几分钟——而不是五小时之后——证明 ccache 真的够得着编译器。

一轮构建有 300 分钟预算。cc_wrapper 要是从没绑上，那整份预算就被编掉再扔掉，而这正是
发生过的事：args.gn 里带着 cc_wrapper="ccache"，8038 个目标编完，缓存里只有 391 字节，
因为 Chromium 只在 clang 工具链上才接 cc_wrapper，而 Qt 的 MSVC 构建是 is_clang=false
（见 patch-msvc-ccache.py）。

本脚本在 prepare 阶段就把这个洞堵上：它替 core 模块跑一次 GN 生成（约四分钟；构建阶段随后
会看到它已是最新，不浪费时间），然后读 GN 写出来的 ninja 文件——CC/CXX 规则里必须出现那个
wrapper 的名字。

说说目标名这件事。QtWebEngine 把 GN_TARGET 拼成 "core_${config}_${arch}"
（src/core/CMakeLists.txt），而这里的生成器是 Ninja Multi-Config，可用的目标名是
"<name>:<config>"——直接写 "runGn_core_RelWithDebInfo_AMD64" 会得到
"ninja: error: unknown target ..., did you mean 'runGn_core_RelWithDebInfo_AMD64:RelWithDebInfo'?"。
所以候选按顺序试，并且跟着 ninja 自己的建议走；`--target help` 查询是最后的手段。
判断不出来**不算失败**——见退出码 2。

退出码：
  0 = 生成的规则里有 wrapper（已接线）
  1 = 规则生成了，但里面没有 wrapper（硬失败）
  2 = 生成不了 / 判断不了（调用方按警告处理；构建阶段的看门狗会再读一遍同一批规则，
      并且有能力停掉一轮死缓存）
  3 = 命令行参数错误。**不能**用 2：2 是一个结论（「判断不了」→ 调用方放行），把一次参数
      写错当成「判断不了」就等于静默关掉这道门禁，而它存在的意义正是拦住这种事（历史上
      check-ccache-bound.ps1 -Wrapper 被 :run_ps 的 8 参数上限丢掉过一次，白烧一轮）。

同一条道理，任何**意外**异常都不能映射到 1（那是「规则里没有 wrapper，中止整轮」这个结论）：
内部错误按 2 处理，见文件末尾的顶层兜底。

这是 scripts/qtwebengine/check-ccache-bound.ps1 的 Python 版（纯标准库）。行为逐条对齐：
同样的候选顺序、同样的「ninja 建议插到队首」、同样的 8 次尝试上限、同样的 `--target help`
兜底、同样的退出码语义与消息文本。几处刻意的差别：

  * 尝试预算按阶段分开算（PS 版两处共用一个计数器，导致 `--target help` 兜底是死代码）；
  * `--target help` 阶段可以捡起「因预算被跳过、从没真跑过」的目标（PS 版把它们一起标成
    已试过，于是兜底即使能跑也轮不到它们）；
  * 大小写：PS 的 `Select-String -SimpleMatch` 与 `-eq` 对字符串都不区分大小写，这里逐处
    对齐（wrapper 扫描）；
  * 跳过 Hidden/System 项（PS 的 Get-ChildItem 不带 -Force 就是这么做的）；
  * 顺带做对的两件事：ninja 文件按流式逐行扫（不整份读进内存），stdout 强制 UTF-8 + 行缓冲
    （CI 里输出是管道，locale 编码会让中文抛 UnicodeEncodeError，块缓冲还会让「等四分钟」
    看起来像卡死）。

已知且**不打算**对齐的差别：PS 的 Write-Error 会打一个 5 行装饰块（脚本名/行号/源码回显 +
CRLF），这里只打一行。任何按整行锚定的检查都会失配，按子串匹配的不受影响。

用法（与 PS 版一致，参数名不动，方便 build.cmd 只改脚本名）：
    python check-ccache-bound.py -BuildDir <build> -BuildType RelWithDebInfo -Wrapper ccache
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

# Hidden/System：PowerShell 的 Get-ChildItem（不带 -Force）会跳过这类项，iterdir/rglob 不会。
FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4


class _ArgParser(argparse.ArgumentParser):
    """用法错误退出 3，而不是 argparse 默认的 2。

    2 在本脚本里是「判断不了」这个**结论**：build.cmd 的 :assert_ccache_bound 读到 2 会打一句
    警告然后放行整轮。用 2 表示「参数写错了」，等于把一次调用错误静默降级成门禁失效。
    PowerShell 版在参数绑定失败时退出 1（响亮中止）；3 同样落进硬失败分支，但能与
    「真的没接线」区分开。
    """

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        sys.exit(3)

# 尝试次数上限，按阶段分开算。8 次是给候选循环的预算（候选本身最多就有 12 个），
# `--target help` 那次兜底另给一份——它必须够把 help 列出来的目标走完：失败的目标只会被 ninja
# 立刻打回（几秒），而兜底存在的唯一理由就是「上游改了目标命名」，在正确名字之前用光预算
# 等于这条安全网又白装。
# PS 版两处共用同一个计数器，而候选循环恰好会用满 8 次，于是兜底那一段永远动不了——实测：
# 把正确目标只放进 `--target help` 的输出里，仍然 exit 2。
# 分开计数不改变任何一种判定结果：它只可能把「判断不了(2)」变成「已接线(0)」或
# 「没接线(1)」，不会把通过变成失败。
ATTEMPT_BUDGET = {"candidates": 8, "help": 8}

DID_YOU_MEAN_RE = re.compile(r"did you mean '([^']+)'")
RUNGN_RE = re.compile(r"runGn_\S+")


def use_utf8_streams() -> None:
    """CI 把输出接进管道，此时 Python 用 locale 编码（cp1252）会对中文抛 UnicodeEncodeError，
    而且默认块缓冲会让「要等四分钟」看起来像卡死。两个都从脚本内部兜住，不依赖调用方记得加
    -u -X utf8（build.cmd 的 :run_py 两者都加了）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except (AttributeError, ValueError, OSError):
            pass


def decode_console_output(raw: bytes) -> str:
    """cmake / ninja 在 Windows 上按控制台代码页输出，不一定合法 UTF-8。"""
    return raw.decode("utf-8", errors="replace")


def first_line_with(path: Path, needle: str) -> str | None:
    """流式找第一个含 needle 的行（不整份读进内存：ninja 规则文件可以很大）。

    大小写不敏感：PowerShell 的 `Select-String -Pattern $Wrapper -SimpleMatch` 就是不区分
    大小写的（-SimpleMatch 只关掉正则，不动大小写），所以规则里写 CCACHE.EXE 时原版照样
    算「已接线」。这里必须一样，否则会把一轮本来能跑的构建判成「ccache 没接上」而中止。
    """
    lowered = needle.lower()
    try:
        with path.open("rb") as fh:
            for raw in fh:
                line = decode_console_output(raw)
                if lowered in line.lower():
                    return line.strip()
    except OSError:
        return None
    return None


def is_hidden(path: Path) -> bool:
    """PowerShell 的 Get-ChildItem 不带 -Force 时会跳过 Hidden/System 项，Python 不会。

    （非 Windows 上没有 st_file_attributes，直接当可见。）
    """
    try:
        attrs = path.stat().st_file_attributes  # type: ignore[attr-defined]
    except (OSError, AttributeError):
        return False
    return bool(attrs & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM))


def main() -> int:
    use_utf8_streams()

    ap = _ArgParser(
        description="prove that ccache reaches the compiler, in minutes instead of hours"
    )
    ap.add_argument("-BuildDir", required=True, help="CMake build directory (BUILD_DIR of build.cmd)")
    ap.add_argument("-BuildType", default="RelWithDebInfo", help="Configuration to generate for")
    ap.add_argument("-GnTarget", default="", help="Explicit CMake target that runs GN generation")
    ap.add_argument("-Wrapper", default="ccache", help="Wrapper name that must appear in the rules")
    args = ap.parse_args()

    build_dir = Path(args.BuildDir)
    build_type = args.BuildType
    wrapper = args.Wrapper
    cmake_exe = shutil.which("cmake") or "cmake"

    if not (build_dir / "CMakeCache.txt").is_file():
        print(f"[ccache-bound] 没有 CMakeCache.txt：{build_dir}（未 configure？）")
        return 2

    # 架构从构建树里读（src/core/<config>/<arch>/），读不到再退回常见写法
    core_root = build_dir / "src" / "core"
    arch = ""
    config_dir = core_root / build_type
    if config_dir.is_dir():
        # PS 版这里是 -ErrorAction SilentlyContinue：列目录失败（权限/重定向/被删）时它继续用
        # 空 arch 往下走。Python 不兜住的话异常会冒到顶层，最后按「意外错误」处理——虽然那也
        # 是 2，但没必要把一次可恢复的失败走成异常路径。
        try:
            dirs = sorted(
                (p for p in config_dir.iterdir() if p.is_dir() and not is_hidden(p)),
                key=lambda p: p.name,
            )
        except OSError as exc:
            print(f"[ccache-bound] 列 {config_dir} 失败（忽略，退回常见架构名）：{exc}")
            dirs = []
        if dirs:
            arch = dirs[0].name

    names: list[str] = []
    if args.GnTarget:
        names.append(args.GnTarget)
    if arch:
        names.append(f"runGn_core_{build_type}_{arch}")
    for a in ("AMD64", "x64", "ARM64"):
        names.append(f"runGn_core_{build_type}_{a}")
    names.append("runGn_WebEngineCore")

    # Ninja Multi-Config 的目标名带 :<config>，两种写法都排在前面
    seen: set[str] = set()
    candidates: list[str] = []
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        candidates.append(f"{name}:{build_type}")
        candidates.append(name)

    gn_log = Path(tempfile.gettempdir()) / f"ccache-bound-gn-{os.getpid()}.log"

    ran: set[str] = set()
    attempted: set[str] = set()
    attempts = {"candidates": 0, "help": 0}

    def invoke_gn_target(target: str, phase: str = "candidates") -> bool:
        if phase == "candidates":
            # 候选循环必须记住「碰过」的目标：不然 ninja 的建议会被反复插回队首，队列自锁。
            # 注意「碰过」不等于「真跑过」——预算用完时目标只是被碰过、并没有交给 cmake。
            # PS 版把两者混在一个 tried 里，于是候选 9+ 被标成已试过，兜底即使能跑也轮不到它们
            # （实测：架构名不同时有 10 个候选，只有 runGn_WebEngineCore 能通、--target help 也
            # 报了它，仍然 exit 2）。分开记，兜底阶段就能把这些目标真正试一次。
            if target in attempted:
                return False
            attempted.add(target)
        elif target in ran:
            # 兜底阶段可以捡起「被预算跳过」的目标，但不再重跑已经跑失败过的。
            return False
        if attempts[phase] >= ATTEMPT_BUDGET[phase]:
            return False
        attempts[phase] += 1
        ran.add(target)
        print(
            f"[ccache-bound] GN 生成：cmake --build --target {target} --config {build_type}（约四分钟）"
        )
        # 与构建阶段用的是同一个 target；生成完之后构建阶段会看到它已是最新，不浪费时间。
        # 输出直接落文件而不是经 Python 中转：cmake 的进度要逐行实时可见，而这里不需要
        # 在控制台上再看一遍（要核对时 gn_log 的路径会打出来）。
        rc: int | None = None
        try:
            with gn_log.open("ab") as fh:
                proc = subprocess.run(
                    [
                        cmake_exe,
                        "--build",
                        str(build_dir),
                        "--config",
                        build_type,
                        "--target",
                        target,
                    ],
                    stdout=fh,
                    stderr=subprocess.STDOUT,
                )
            rc = proc.returncode
        except OSError as exc:
            print(f"[ccache-bound] target '{target}' 起不来：{exc}")
            return False
        if rc == 0:
            return True
        print(f"[ccache-bound] target '{target}' 没能跑通（cmake 退出码 {rc}）")
        return False

    def suggested_target() -> str:
        # ninja 会直接说出它认得的名字（Ninja Multi-Config 是 "<name>:<config>"）
        try:
            text = decode_console_output(gn_log.read_bytes())
        except OSError:
            return ""
        hits = DID_YOU_MEAN_RE.findall(text)
        return hits[-1] if hits else ""

    generated = False
    used_target = ""
    pending = list(candidates)

    while pending and not generated:
        target = pending.pop(0)
        if target in attempted:
            continue
        if invoke_gn_target(target):
            generated = True
            used_target = target
            break
        # ninja 的建议要插到队首：不然它会排在剩下所有猜测之后，甚至轮不到
        hint = suggested_target()
        if hint and hint not in attempted:
            print(f"[ccache-bound] ninja 建议的目标名：'{hint}'，先试它")
            pending.insert(0, hint)

    if not generated:
        # 最后再问一次 CMake 有哪些 runGn_* 目标（上游改了命名也还能兜住）
        print("[ccache-bound] 候选 target 都不通，向 cmake 要一次目标列表")
        help_text = ""
        try:
            proc = subprocess.run(
                [cmake_exe, "--build", str(build_dir), "--target", "help"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            help_text = decode_console_output(proc.stdout or b"")
        except OSError as exc:
            print(f"[ccache-bound] cmake --target help 起不来：{exc}")

        found: list[str] = []
        for m in RUNGN_RE.finditer(help_text):
            name = m.group(0).rstrip(":")
            if "core" in name.lower() and name not in found:
                found.append(name)

        for target in found:
            # help 的输出里，Ninja Multi-Config 的目标本身就带 ":<config>"。再拼一次会得到
            # "runGn_...:RelWithDebInfo:RelWithDebInfo" 这种必然是错的候选，白白吃掉一次预算——
            # 而这个预算是兜底阶段唯一的资源。
            variants = [target] if ":" in target else [f"{target}:{build_type}", target]
            for variant in variants:
                if invoke_gn_target(variant, "help"):
                    generated = True
                    used_target = variant
                    break
            if generated:
                break

    if not generated:
        print("[ccache-bound] GN 生成没能跑通，本检查跳过（视为「无法判断」）；完整输出见：")
        print(f"  {gn_log}")
        try:
            # 与 Get-Content -Tail 20 一致：只认 \r\n / \n / \r（splitlines() 还会在 \v \f
            # \x85 \u2028 等处断行，把 MSVC 的横幅拆成好几行）。
            tail = re.split(r"\r\n|\r|\n", decode_console_output(gn_log.read_bytes()))[-20:]
        except OSError:
            tail = []
        for line in tail:
            print(f"  {line}")
        return 2

    print(f"[ccache-bound] GN 生成完成（target {used_target}）")

    if not core_root.is_dir():
        print(f"[ccache-bound] 没有 {core_root}，无法判断")
        return 2

    ninja_files = sorted(
        (p for p in core_root.rglob("*.ninja") if p.is_file() and not is_hidden(p)),
        key=lambda p: str(p),
    )
    if not ninja_files:
        print(f"[ccache-bound] {core_root} 下没有 .ninja 文件，无法判断")
        return 2

    hit_path: Path | None = None
    hit_line = ""
    for path in ninja_files:
        line = first_line_with(path, wrapper)
        if line is not None:
            hit_path = path
            hit_line = line
            break

    if hit_path is None:
        print(f"[ccache-bound] 扫了 {len(ninja_files)} 个 .ninja 文件，没有一处提到 '{wrapper}'")
        print("[ccache-bound] 说明 cc_wrapper 没被拼进编译器命令行：ccache 一次都不会被调用")
        print("[ccache-bound] 检查 patch-msvc-ccache.py（win/toolchain.gni）与 patch-gn-args.py（args.gn）")
        return 1

    print(f"[ccache-bound] 已确认：{hit_path}")
    print(f"[ccache-bound]   {hit_line}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except KeyboardInterrupt:
        # 中断不是「内部错误」，别把它降级成「判断不了 → 放行」。
        raise
    except BaseException as exc:  # noqa: BLE001
        # 意外异常**不能**映射到 1：1 在本脚本里是「规则里没有 wrapper，中止整轮」这个结论，
        # 一次内部错误（比如列目录失败、状态文件读不了）没有资格中止一轮五小时的构建。
        # 2 =「判断不了」才是它的位置；构建阶段的看门狗与轮末的 ccache 统计仍会覆盖这种情况。
        traceback.print_exc()
        print(f"[ccache-bound] 内部错误，按「无法判断」处理（exit 2）：{exc}", file=sys.stderr)
        sys.exit(2)
