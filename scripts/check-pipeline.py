#!/usr/bin/env python3
"""流水线静态自检：在派 CI 之前，先在本机把能查的都查掉。

这个脚本存在的理由是一次真实的教训：连续四轮 CI 都死在几秒钟到几分钟就能在本机
发现的问题上（未定义的环境变量、number 输入的 fromJSON、configure 不认的参数、
被复制坏的 chocolatey shim、配置集不匹配、把确定性失败误判成超时后无限续跑）。
每次「改完就派线上跑」都要烧掉一台 runner 和几十分钟到几小时。

它只做静态检查，不编译、不执行构建脚本、不碰网络。用法：

    python scripts/check-pipeline.py            # 检查当前工作区
    python scripts/check-pipeline.py --root DIR # 检查指定目录（例如旧提交导出的副本）

退出码 0 = 全部通过，1 = 有检查未通过。装了 PyYAML 的话会额外做一次 YAML 解析。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

BUILD_CMD = "scripts/qtwebengine/build.cmd"
WORKFLOW = ".github/workflows/build-qtwebengine.yml"

# 这些输入允许不被自动续跑转发：它们只影响本次运行的收尾动作或人工开关，
# 转发与否都不改变「第二轮编出来的东西」。
# 目前是空集：所有输入都会被转发。
FORWARD_EXEMPT: set[str] = set()

failures: list[str] = []
notes: list[str] = []


def fail(check: str, detail: str) -> None:
    failures.append(f"{check}: {detail}")


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def read_bytes(path: Path) -> bytes:
    return path.read_bytes()


# ---------------------------------------------------------------------------
# build.cmd
# ---------------------------------------------------------------------------

LABEL_RE = re.compile(r"^:([A-Za-z0-9_]+)\s*$")
CALL_RE = re.compile(r"\b(?:goto|call)\s+:([A-Za-z0-9_]+)", re.IGNORECASE)


def check_build_cmd(root: Path) -> None:
    path = root / BUILD_CMD
    if not path.is_file():
        fail("build.cmd", f"missing file {path}")
        return

    raw = read_bytes(path)
    text = raw.decode("ascii", errors="replace")
    lines = text.split("\r\n")

    # 1. cmd.exe 按字节偏移读批处理。非 ASCII 会在不同代码页下改变行长，
    #    行尾粘到下一行、命令首字符被吃掉，这类故障极难从日志反推。
    bad_ascii = [i for i, l in enumerate(lines, 1) if any(ord(c) > 127 for c in l)]
    if bad_ascii:
        fail("build.cmd/ascii", f"non-ASCII on lines {bad_ascii[:10]}")

    # 2. 必须是 CRLF，且不能混入裸 LF（.gitattributes 声明 *.cmd eol=crlf）。
    crlf = raw.count(b"\r\n")
    bare_lf = raw.count(b"\n") - crlf
    if bare_lf:
        fail("build.cmd/eol", f"{bare_lf} bare LF line ending(s); *.cmd must be CRLF")
    if crlf == 0:
        fail("build.cmd/eol", "no CRLF line endings at all")

    # 3. 每个 goto/call :label 都要有对应的定义，否则运行时直接 "找不到批处理标签"。
    labels = {m.group(1).lower() for l in lines if (m := LABEL_RE.match(l.strip()))}
    for i, l in enumerate(lines, 1):
        s = l.strip()
        if s.lower().startswith("rem"):
            continue
        for m in CALL_RE.finditer(s):
            if m.group(1).lower() not in labels:
                fail("build.cmd/label", f"line {i}: goto/call :{m.group(1)} has no label")

    # 4. 标签不能被「意外穿透」进来。上一个非空行必须是 exit/goto/rem，或者是
    #    形如 `if ... goto :thislabel` 的守卫（那是故意的分支汇合点）。
    #    静态分析分不清「故意汇合」和「漏写 exit」，所以故意的那种要在这里登记，
    #    登记本身就是一句说明：以后有人改动它，会先看到这里。
    allowed_fallthrough = {
        ":submodule_full": "shallow submodule fetch failed -> fall back to a full fetch",
        ":assert_config_skip_bt": "CMAKE_BUILD_TYPE matched -> continue to the config-set check",
    }
    for i, l in enumerate(lines):
        s = l.strip()
        if not (m := LABEL_RE.match(s)):
            continue
        name = f":{m.group(1)}"
        j = i - 1
        while j >= 0 and lines[j].strip() == "":
            j -= 1
        prev = lines[j].strip().lower() if j >= 0 else ""
        ok = (
            prev.startswith("exit")
            or prev.startswith("goto")
            or prev.startswith("rem")
            or (prev.startswith("if") and "goto " in prev)
            or name.lower() in allowed_fallthrough
        )
        if not ok:
            fail(
                "build.cmd/fallthrough",
                f"line {i + 1} label '{name}' is reachable by falling through from "
                f"{prev[:50]!r} (if that is deliberate, add it to allowed_fallthrough)",
            )

    # 5. 块的括号要配平：块少一个 ) 会让后面的语句落进块里，多一个 ) 会直接报
    #    「此时不应有 )」。按本文件的书写风格判断——开块行以 "(" 结尾，收块行以 ")"
    #    开头（`) else (` 同时算一次收和一次开，两边都会计入）。不能对整文件做括号
    #    计数：rem/说明文字里就有落单的括号。
    non_rem = [l.strip() for l in lines if not l.strip().lower().startswith("rem")]
    openers = sum(1 for l in non_rem if l.endswith("("))
    closers = sum(1 for l in non_rem if l.startswith(")"))
    if openers != closers:
        fail(
            "build.cmd/parens",
            f"block parentheses do not balance: {openers} line(s) open a block, "
            f"{closers} close one",
        )

    # 6. 配置必须显式传给 --build / --install。多配置生成器（Visual Studio）
    #    在没有 --config 时会一律构建 Debug（cmVS10Gen.cxx 把空 config 变成
    #    "Debug"），而安装步骤要的是 BUILD_TYPE，于是编完才发现装不上。
    for i, l in enumerate(lines, 1):
        s = l.strip()
        if s.lower().startswith("rem"):
            continue
        if "cmake --build" in s and "--config" not in s:
            fail("build.cmd/config", f"line {i}: cmake --build without --config: {s[:70]}")
        if "cmake --install" in s and "--config" not in s:
            fail("build.cmd/config", f"line {i}: cmake --install without --config: {s[:70]}")

    # 7. configure 传的 CMAKE_BUILD_TYPE 必须是变量，不能写死：写死就可能在
    #    Qt 实际提供的配置集之外，而 Qt 的 get_install_config() 会优先读它。
    for i, l in enumerate(lines, 1):
        if "qt-configure-module" in l and "-DCMAKE_BUILD_TYPE=" in l:
            m = re.search(r"-DCMAKE_BUILD_TYPE=(\S+)", l)
            if m and m.group(1) != "%BUILD_TYPE%":
                fail(
                    "build.cmd/config",
                    f"line {i}: -DCMAKE_BUILD_TYPE={m.group(1)} is a literal; "
                    "it must be %BUILD_TYPE% or it can name a configuration the tree lacks",
                )

    # 8. 状态文件协议：确定性失败必须落盘为 failed。
    #    只有「被外部打断」才允许文件不存在。prepare 阶段曾经什么都不写，
    #    于是 configure 报错 → 文件不存在 → 判定 killed → 自动派下一轮 → 死循环。
    def label_block(label: str) -> str | None:
        idx = next((i for i, l in enumerate(lines) if l.strip() == label), None)
        if idx is None:
            return None
        end = next(
            (j for j in range(idx + 1, len(lines)) if LABEL_RE.match(lines[j].strip())),
            len(lines),
        )
        return "\n".join(lines[idx:end])

    for label in (":phase_prepare", ":phase_all", ":step_build"):
        blk = label_block(label)
        if blk is None:
            fail("build.cmd/state", f"{label} not found")
        elif '> "%STATE_FILE%" echo failed' not in blk:
            fail(
                "build.cmd/state",
                f"{label} never records a failure into STATE_FILE; a deterministic "
                "error will be read as 'killed' and re-dispatched forever",
            )
    # :step_build 还要把「先写 failed、再在 cmake 之前删掉」的顺序保持住：
    # 顺序颠倒就等于把确定性失败重新伪装成超时。
    blk = label_block(":step_build") or ""
    i_fail = blk.find('> "%STATE_FILE%" echo failed')
    i_del = blk.find('del "%STATE_FILE%"')
    if i_fail >= 0 and i_del >= 0 and i_fail > i_del:
        fail(
            "build.cmd/state",
            ":step_build deletes STATE_FILE before writing 'failed'; a pre-cmake error "
            "would then look like a time-out",
        )
    # 成功必须写 ok，判定步骤按 ok* 匹配。
    if '> "%STATE_FILE%" echo ok' not in text:
        fail("build.cmd/state", "nothing ever writes 'ok' into STATE_FILE")

    # 9. bison/flex 必须真正执行一次。chocolatey 的 win_bison.exe 是 shim，
    #    复制到别处后按自身相对路径找目标，一执行就死，而 `where` 检查照样通过。
    if "bison --version" not in text:
        fail("build.cmd/tools", "bison is never executed before configure (FindBISON runs it)")
    if "flex --version" not in text:
        fail("build.cmd/tools", "flex is never executed before configure (FindFLEX runs it)")

    # 10. 被时间预算打断不能记成 failed。步超时会把进程树打掉，cmake 以 0xC000013A
    #     （-1073741510）返回；那是一次中断，不是编译错误。曾经它被写成 failed，
    #     于是编到 [8038/29705] 的那一轮被判成「失败、不再排下一轮」。
    blk = label_block(":build") or ""
    if "-1073741510" not in blk:
        fail(
            "build.cmd/state",
            ":build does not treat the step-timeout exit code (0xC000013A, "
            "-1073741510) as an interruption; a round cut off by the time budget is "
            "recorded as failed and never resumed",
        )
    else:
        i_int = blk.find("-1073741510")
        i_failw = blk.find('> "%STATE_FILE%" echo failed')
        if 0 <= i_failw < i_int:
            fail("build.cmd/state", ":build writes 'failed' before checking the interruption code")

    # 11. 必须只编一个配置。VS 是多配置生成器，Qt 把配置集设成 RelWithDebInfo;Debug，
    #     而 QtWebEngine 每个配置接一棵 gn/ninja 树且互为 WebEngineCore 的依赖，
    #     于是整个 Chromium 编两遍（实测第一棵树 56 分钟编到 8038/29705，第二棵排队）。
    if 'if /i not "%CFG_LINE%"=="%BUILD_TYPE%" goto :assert_config_multi' not in text:
        fail("build.cmd/config", ":assert_config does not require exactly one configuration")
    if "patch-single-config.ps1" not in text:
        fail("build.cmd/config", "the single-config patch is never applied")
    if "-DQTWE_BUILD_CONFIGURATION=%BUILD_TYPE%" not in text:
        fail(
            "build.cmd/config",
            "configure does not receive -DQTWE_BUILD_CONFIGURATION, so the narrowing "
            "block the patch injects cannot do anything",
        )


# ---------------------------------------------------------------------------
# workflow
# ---------------------------------------------------------------------------

INPUT_RE = re.compile(r"^ {6}([A-Za-z0-9_]+):\s*$", re.MULTILINE)
ID_RE = re.compile(r"^\s*id:\s*([A-Za-z0-9_-]+)\s*$", re.MULTILINE)
STEP_REF_RE = re.compile(r"steps\.([A-Za-z0-9_-]+)\.")
FORWARD_RE = re.compile(r"-f\s+([A-Za-z0-9_]+)=")


def check_workflow(root: Path) -> None:
    path = root / WORKFLOW
    if not path.is_file():
        fail("workflow", f"missing file {path}")
        return
    text = read_text(path)

    # 1. 引用的 step id 必须真的存在，否则表达式解析失败或取到空值。
    ids = set(ID_RE.findall(text))
    refs = set(STEP_REF_RE.findall(text))
    missing = sorted(refs - ids)
    if missing:
        fail("workflow/steps", f"references to undeclared step id(s): {missing}")

    # 2. inputs 的声明与自动续跑的转发必须一致。漏传 build_type 之类会让第二轮
    #    换一个配置去编，ccache 条目几乎全部作废（等于又从头编一遍）。
    m = re.search(r"^ {2}workflow_dispatch:\s*$", text, re.MULTILINE)
    if not m:
        fail("workflow/inputs", "on.workflow_dispatch not found")
        return
    # inputs 的名字缩进 6 空格；取到 on: 块结束（缩进回落到 ≤2）为止。
    tail = text[m.end() :]
    stop = re.search(r"^ {0,2}\S", tail, re.MULTILINE)
    inputs_block = tail[: stop.start()] if stop else tail
    declared = set(INPUT_RE.findall(inputs_block))
    if not declared:
        fail("workflow/inputs", "no inputs parsed; the 6-space indentation assumption broke")
        return

    forwarded = set()
    for fm in re.finditer(r"gh workflow run[\s\S]*?(?=\n {6}- name:|\Z)", text):
        forwarded.update(FORWARD_RE.findall(fm.group(0)))
    not_forwarded = sorted(declared - forwarded - FORWARD_EXEMPT)
    if not_forwarded:
        fail(
            "workflow/inputs",
            f"auto-continue does not forward: {not_forwarded} (add -f ... or list it in "
            "FORWARD_EXEMPT)",
        )
    unknown = sorted(forwarded - declared)
    if unknown:
        fail("workflow/inputs", f"forwards input(s) that are not declared: {unknown}")

    # 3. 状态文件协议两端要对得上：脚本写 ok / failed N，判定步骤按 ok* / failed* 读。
    if "-like 'ok*'" not in text or "-like 'failed*'" not in text:
        fail("workflow/state", "verdict no longer matches the ok*/failed* state file protocol")

    # 4. 确定性失败不能触发续跑。
    if "steps.ccachecheck.outcome != 'failure'" not in text:
        fail(
            "workflow/loop",
            "auto-continue is not guarded against the ccache step failing (a round with "
            "zero compilations would re-dispatch itself)",
        )
    if "steps.prepare.outcome" not in text:
        fail(
            "workflow/loop",
            "verdict does not treat a failed prepare step as 'failed' (a prepare that dies "
            "before build.cmd runs leaves no state file and looks like a time-out)",
        )

    # 5. 真正的 YAML 解析（可选，本机可能没有 PyYAML）。
    try:
        import yaml  # type: ignore
    except Exception:
        notes.append("workflow: PyYAML not installed, skipped full YAML parse")
    else:
        try:
            yaml.safe_load(text)
        except Exception as exc:  # noqa: BLE001
            fail("workflow/yaml", f"does not parse: {exc}")


# ---------------------------------------------------------------------------
# ccache 接线：一个永远不会被调用的缓存比没有缓存更糟——它把「下一轮接着编」
# 悄悄变成「下一轮从头再来」，而一轮就是五个小时
# ---------------------------------------------------------------------------

PATCH_MSVC_CCACHE = "scripts/qtwebengine/patch-msvc-ccache.ps1"
CHECK_CCACHE_BOUND = "scripts/qtwebengine/check-ccache-bound.ps1"
WATCH_BUILD = "scripts/qtwebengine/watch-build.ps1"


def check_ccache_wiring(root: Path) -> None:
    build = read_text(root / BUILD_CMD)
    workflow = read_text(root / WORKFLOW)

    # 1. Chromium 只在 toolchain_is_clang 为真时才把 cc_wrapper 拼到 cl.exe 前面
    #    （chromium/build/toolchain/win/toolchain.gni），而 Qt 的 MSVC 版 QtWebEngine
    #    是 is_clang=false：没有这个补丁，args.gn 里的 cc_wrapper 被整体丢掉，ccache
    #    一次都不会被调用（实测：编到 [8038/29705]，缓存 391 字节，没有 Cacheable calls）。
    if "call :run_ps patch-msvc-ccache.ps1" not in build:
        fail(
            "ccache/wiring",
            "build.cmd 没有调用 patch-msvc-ccache.ps1：MSVC 下注入的 cc_wrapper 会被忽略，"
            "ccache 永远不会被调用",
        )
    patch = root / PATCH_MSVC_CCACHE
    if not patch.is_file():
        fail("ccache/wiring", f"缺少 {PATCH_MSVC_CCACHE}")
    else:
        ptext = read_text(patch)
        needles = (
            ('toolchain_cc_wrapper != "" && toolchain_is_clang', "要找的上游条件"),
            ('} else if (toolchain_cc_wrapper != "") {', "要写回的新条件"),
        )
        for needle, why in needles:
            if needle not in ptext:
                fail("ccache/wiring", f"{PATCH_MSVC_CCACHE} 丢了{why}：{needle}")

    # 2. 绑定关系必须在五小时的构建之前证明，而不是构建之后才知道。
    if "call :run_ps check-ccache-bound.ps1" not in build:
        fail(
            "ccache/wiring",
            "build.cmd 没有跑 check-ccache-bound.ps1：缓存没接上这件事只能在一整轮白编之后才发现",
        )
    if not (root / CHECK_CCACHE_BOUND).is_file():
        fail("ccache/wiring", f"缺少 {CHECK_CCACHE_BOUND}")

    # 3. 看门狗是唯一能提前看到卡死的手段（runner 是一次性 VM，事后无从查证）。
    if "watch-build.ps1" not in workflow:
        fail("ccache/wiring", "workflow 没有启动 watch-build.ps1：卡死之后又只能靠猜")
    if not (root / WATCH_BUILD).is_file():
        fail("ccache/wiring", f"缺少 {WATCH_BUILD}")

    # 4. ninja 自己的默认并行度是 cores+2；在 4 核/16 GB 的 runner 上，这个数字就是
    #    决定「换页卡死」还是「编完」的内存上限。
    if 'set "NINJAFLAGS=-j%NINJA_JOBS%"' not in build:
        fail(
            "ccache/wiring",
            "build.cmd 没有把 NINJA_JOBS 导出成 NINJAFLAGS：内层 Chromium ninja 会用 cores+2",
        )
    if 'if not defined NINJA_JOBS if defined PARALLEL set "NINJA_JOBS=%PARALLEL%"' not in build:
        fail("ccache/wiring", "NINJA_JOBS 没有从 PARALLEL 取默认值")

    # 5. jumbo：旧的 JUMBO=0 从来没有关掉过它（Qt 的 configure 默认就是开、merge limit 8），
    #    真正的关开关和 limit 旋钮都必须留着。
    if "-no-webengine-jumbo-build" not in build:
        fail("ccache/wiring", "build.cmd 无法真正关掉 jumbo（Qt 默认是开、limit 8）")
    if "-webengine-jumbo-build=%JUMBO%" not in build:
        fail("ccache/wiring", "build.cmd 无法设置 jumbo 的 merge limit")
    if "JUMBO: ${{ inputs.jumbo }}" not in workflow:
        fail(
            "ccache/wiring",
            "workflow 仍然把 jumbo 输入压成 0/1（&& '1' || '0'）：这正是那个开关从未生效的原因",
        )

    # 6. symbol_level 旋钮要真的接到 build.cmd 上（Qt 在 RelWithDebInfo+MSVC 下写 2）。
    if "SYMBOL_LEVEL:" not in workflow or "-SymbolLevel" not in build:
        fail("ccache/wiring", "symbol_level 输入没有接到 patch-gn-args.ps1 -SymbolLevel 上")


def main() -> int:
    ap = argparse.ArgumentParser(description="流水线静态自检（不编译、不联网）")
    ap.add_argument("--root", default=None, help="仓库根目录，默认取本脚本的上两级")
    args = ap.parse_args()
    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parent.parent

    check_build_cmd(root)
    check_workflow(root)
    check_ccache_wiring(root)

    for n in notes:
        print(f"note: {n}")
    if failures:
        print(f"\nFAIL: {len(failures)} problem(s)")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK: pipeline static checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
