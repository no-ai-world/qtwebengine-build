#!/usr/bin/env python3
"""`build.cmd` 的 `:run_py` 机制集成测试——用的是它**真实的那几行**。

`:run_py` 是从 `scripts/qtwebengine/build.cmd` 里逐字抽出来的（不是抄一遍），塞进一个临时
harness 再调用，所以这里验的是真正的那条调用行：

    python -u -X utf8 "%SCRIPT_DIR%\\%~1" %2 %3 %4 %5 %6 %7 %8 %9

以及它周围的参数转发与退出码透传。不需要 Qt、MSVC、cmake 或真实源码树，几秒钟跑完。

harness 所在目录名里**故意带空格**：那条调用行的引号要活下来的就是这个（自托管 runner 的
工作区路径可以带空格）。另外 8 参数上限也在这里钉住——第 9 个会被批处理无声丢掉，而丢掉一个
开关的值会让脚本报「参数缺值」，看起来就像它要检查的那件事失败了（历史上 `-Wrapper` 就这么
丢过一次，白烧一轮）。

用法：
    python scripts/tests/run-py-integration.py

退出码 0 = 全部通过；1 = 有失败。需要在 PATH 上有 `python` 与 cmd.exe（Windows）。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BUILD_CMD = REPO / "scripts/qtwebengine/build.cmd"
SCRIPTS = REPO / "scripts/qtwebengine"
LABEL_RE = re.compile(r"^:([A-Za-z0-9_]+)\s*$")

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f"  {detail}"))
    if not ok:
        failures.append(name)


def extract_run_py() -> str:
    """从 build.cmd 里逐字取出 :run_py 这个标签块（到下一个标签为止）。"""
    lines = BUILD_CMD.read_bytes().decode("ascii").split("\r\n")
    start = next(i for i, line in enumerate(lines) if line.strip() == ":run_py")
    end = next(
        (j for j in range(start + 1, len(lines)) if LABEL_RE.match(lines[j].strip())),
        len(lines),
    )
    return "\r\n".join(lines[start:end])


def run(harness: Path, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["cmd.exe", "/c", str(harness), *args], capture_output=True)


def main() -> int:
    block = extract_run_py()
    if "python -u -X utf8" not in block:
        print("  [FAIL] :run_py 的调用行不是预期的 python -u -X utf8 —— 先看 build.cmd")
        print("        " + block.replace("\r\n", "\n        "))
        return 1

    workdir = Path(tempfile.mkdtemp(prefix="run-py-test-"))
    try:
        # 目录名带空格，正是那条调用行的引号要处理的
        script_dir = workdir / "script dir" / "qtwebengine"
        script_dir.mkdir(parents=True)
        for py in sorted(SCRIPTS.glob("*.py")):
            shutil.copy2(py, script_dir / py.name)

        harness = workdir / "harness.cmd"
        harness.write_bytes(
            (
                "@echo off\r\n"
                "setlocal\r\n"
                f'set "SCRIPT_DIR={script_dir}"\r\n'
                "call :run_py %*\r\n"
                "exit /b %errorlevel%\r\n"
                "\r\n" + block + "\r\n"
            ).encode("ascii")
        )
        print(f":run_py 逐字取自 build.cmd（{len(block.splitlines())} 行），SCRIPT_DIR 含空格：{' ' in str(script_dir)}")

        # 1) 真的补丁脚本 + 中文输出 + 带空格的源路径
        src = workdir / "src dir" / "qtwebengine"
        src.mkdir(parents=True)
        (src / "CMakeLists.txt").write_text(
            "cmake_minimum_required(VERSION 3.16)\n"
            "find_package(Qt6 6.5 CONFIG REQUIRED COMPONENTS BuildInternals Core)\n",
            encoding="utf-8",
        )
        proc = run(harness, ["patch-single-config.py", "-SourceRoot", str(src)])
        check("单个参数：退出码 0", proc.returncode == 0, f"rc={proc.returncode} {proc.stderr[:200]!r}")
        check(
            "带空格的路径下真的注入了块",
            "qtwebengine-build: single configuration" in (src / "CMakeLists.txt").read_text(encoding="utf-8"),
        )
        try:
            out = proc.stdout.decode("utf-8")
            check("中文输出按 UTF-8 到达", "[single-config] 已注入" in out, repr(out[:120]))
        except UnicodeDecodeError as exc:
            check("中文输出按 UTF-8 到达", False, str(exc))

        # 2) 多个参数都要转发到
        core = src / "src" / "core"
        core.mkdir(parents=True)
        (core / "CMakeLists.txt").write_text("append_toolchain_setup(gnArgArg)\n", encoding="utf-8")
        proc = run(harness, ["patch-gn-args.py", "-SourceRoot", str(src), "-SymbolLevel", "0", "-UseCcache"])
        check("三个参数：退出码 0", proc.returncode == 0, f"rc={proc.returncode} {proc.stderr[:200]!r}")
        text = (core / "CMakeLists.txt").read_text(encoding="utf-8")
        check("两个注入值都完整转发", "symbol_level=0" in text and 'cc_wrapper="ccache"' in text, repr(text[-160:]))

        # 3) 非零退出码要透传出来（脚本报错 = 这一阶段失败）
        proc = run(harness, ["patch-msvc-ccache.py", "-SourceRoot", str(src)])
        check("退出码透传（找不到锚点 → 1）", proc.returncode == 1, f"rc={proc.returncode}")

        # 4) 脚本不存在要响亮地非零，不能静默通过
        proc = run(harness, ["does-not-exist.py"])
        check("脚本不存在 → 非零", proc.returncode != 0, f"rc={proc.returncode}")

        # 5) 8 参数上限：第 9 个必须发出警告，而不是无声丢掉
        proc = run(
            harness,
            ["patch-gn-args.py", "-SourceRoot", str(src), "-A", "1", "-B", "2", "-C", "3", "-D", "4", "-E", "5"],
        )
        combined = (proc.stdout + proc.stderr).decode("utf-8", "replace")
        check(
            "第 9 个参数会发出警告",
            "[warn] :run_py received more than 8 arguments" in combined,
            repr(combined[:200]),
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print()
    if failures:
        print(f"FAIL: {len(failures)} 项：{failures}")
        return 1
    print("OK: build.cmd 的 :run_py 机制全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
