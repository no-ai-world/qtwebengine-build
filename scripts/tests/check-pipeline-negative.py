#!/usr/bin/env python3
"""`scripts/check-pipeline.py` 的**负向测试**：每条守卫都要能被真的破坏掉。

为什么需要它：`check-pipeline.py` 的全部价值就是「在派 CI 之前拦住问题」，而一条永远不报红的
检查只是装饰。这个脚本把每条守卫要防的东西**真的做出来**（把调用整行删掉、把开关拿掉、引一个
`.ps1`……），然后断言检查必须报红。开发时踩过两次同一个坑：判据写成「全文子串」，而理由注释里
正好写着那个子串，于是把调用删掉、注释留着，检查照样通过——这里每个变体都刻意保留注释。

做法：把 check-pipeline.py 真正需要的那几个文件复制到一个临时根目录，逐个变体改一份、跑一次、
断言报红。不改仓库里的任何文件。

用法：
    python scripts/tests/check-pipeline-negative.py

退出码 0 = 所有守卫都真的会报红；1 = 有守卫形同虚设（或干净副本被误判）。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BUILD_CMD = "scripts/qtwebengine/build.cmd"
WORKFLOW = ".github/workflows/build-qtwebengine.yml"

STAGE_CALL = (
    'call :run_py stage-webengine-runtime.py -Source "%INSTALL_PREFIX%" '
    '-Destination "%DIST_DIR%\\qtwebengine-%QT_VERSION%-win64-msvc2022" -Create'
)
SINGLE_CONFIG_CALL = 'call :run_py patch-single-config.py -SourceRoot "%SRC_DIR%"'
CPPGC_CALL = 'call :run_py patch-cppgc.py -SourceRoot "%SRC_DIR%"'
WATCH_LAUNCH = (
    "          $watchScript = Join-Path $env:GITHUB_WORKSPACE "
    "'scripts\\qtwebengine\\watch-build.py'\n"
)

failures: list[str] = []
checked = 0


def make_root(workdir: Path) -> Path:
    """一份 check-pipeline.py 会读的最小仓库副本。"""
    root = workdir / "repo"
    (root / "scripts/qtwebengine").mkdir(parents=True)
    (root / ".github/workflows").mkdir(parents=True)
    shutil.copy2(REPO / "scripts/check-pipeline.py", root / "scripts/check-pipeline.py")
    for py in sorted((REPO / "scripts/qtwebengine").glob("*.py")):
        shutil.copy2(py, root / "scripts/qtwebengine" / py.name)
    shutil.copy2(REPO / BUILD_CMD, root / BUILD_CMD)
    shutil.copy2(REPO / WORKFLOW, root / WORKFLOW)
    return root


def run_check(root: Path) -> str:
    proc = subprocess.run(
        [sys.executable, str(root / "scripts/check-pipeline.py"), "--root", str(root)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return (proc.stdout or "") + (proc.stderr or "")


def edit_build_cmd(root: Path, old: str, new: str) -> None:
    path = root / BUILD_CMD
    text = path.read_bytes().decode("ascii")
    assert old in text, f"mutation target not found in build.cmd: {old[:60]!r}"
    path.write_bytes(text.replace(old, new).encode("ascii"))


def edit_workflow(root: Path, old: str, new: str) -> None:
    path = root / WORKFLOW
    text = path.read_text(encoding="utf-8")
    assert old in text, f"mutation target not found in workflow: {old[:60]!r}"
    path.write_text(text.replace(old, new), encoding="utf-8")


def expect(name: str, expected: str, mutate) -> None:
    """一个干净副本 + 一处破坏：必须报红，且报的正是这条。"""
    global checked
    checked += 1
    with tempfile.TemporaryDirectory(prefix="negtest-") as tmp:
        root = make_root(Path(tmp))
        mutate(root)
        out = run_check(root)
    hit = expected in out
    print(f"  [{'PASS' if hit else 'FAIL'}] {name}")
    if not hit:
        print(f"        期望报出：{expected!r}\n        实际输出：{out.strip()[:300]}")
        failures.append(name)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="negtest-") as tmp:
        root = make_root(Path(tmp))
        out = run_check(root)

    print("负向测试：每条守卫都要能被真的破坏掉")
    if "OK: pipeline static checks passed" not in out:
        print("  [FAIL] 干净副本本身就没通过——先修这个，否则负向测试没有意义")
        print("        " + out.strip().replace("\n", "\n        ")[:400])
        return 1
    print("  [PASS] 干净副本通过（负向测试的前提）")

    # 迁移完整性
    expect(
        "scripts/ 下混回一个 .ps1",
        "scripts/ 下仍有 PowerShell 脚本",
        lambda r: (r / "scripts/qtwebengine/legacy.ps1").write_text("# x", encoding="utf-8"),
    )
    expect(
        "build.cmd 引用 .ps1",
        "仍然引用 .ps1",
        lambda r: edit_build_cmd(r, "patch-cppgc.py", "patch-cppgc.ps1"),
    )
    expect(
        "workflow 引用 .ps1",
        "仍然引用 .ps1",
        lambda r: edit_workflow(r, "watch-build.py", "watch-build.ps1"),
    )
    expect(
        "没人调用的脚本（死代码）",
        "没有被 build.cmd",
        lambda r: (r / "scripts/qtwebengine/nobody-calls-me.py").write_text("print(1)\n", encoding="utf-8"),
    )

    # 孤儿守卫要防的正是「调用没了、注释还在」（历史上漏过一次的那一步）
    expect(
        "打包调用整行删掉、注释保留",
        "没有被 build.cmd",
        lambda r: edit_build_cmd(r, STAGE_CALL, "rem NOTE: stage-webengine-runtime.py used to be called here"),
    )
    expect(
        "cppgc 补丁调用删掉",
        "没有被 build.cmd",
        lambda r: edit_build_cmd(r, CPPGC_CALL, "rem gone"),
    )
    expect(
        "workflow 里看门狗启动行删掉",
        "没有启动 watch-build",
        lambda r: edit_workflow(r, WATCH_LAUNCH, "          # watch-build.py used to be started here\n"),
    )

    # 调用点本身
    # 第 9 个参数起会被 :run_py 无声丢掉：这里要把调用点堆到超过 8 个
    # （脚本名之后已有 2 个：-SourceRoot "%SRC_DIR%"，再补 4 组开关 = 10 个）
    expect(
        ":run_py 多了第 9 个参数（会被无声丢掉）",
        "is called with",
        lambda r: edit_build_cmd(
            r, CPPGC_CALL, CPPGC_CALL + ' -A "1" -B "2" -C "3" -D "4"'
        ),
    )
    expect(
        ":run_py 少了 -u（重定向时块缓冲，看门狗读不到实时日志）",
        "没有加 -u",
        lambda r: edit_build_cmd(r, "python -u -X utf8", "python -X utf8"),
    )
    expect(
        ":run_py 少了 -X utf8（中文日志会抛 UnicodeEncodeError）",
        "没有加 -X utf8",
        lambda r: edit_build_cmd(r, "python -u -X utf8", "python -u"),
    )
    expect(
        ":run_py 的脚本路径没加引号（SCRIPT_DIR 可以带空格）",
        "脚本路径没有加引号",
        lambda r: edit_build_cmd(r, '"%SCRIPT_DIR%\\%~1"', "%SCRIPT_DIR%\\%~1"),
    )
    expect(
        "单配置补丁的调用没了",
        "single-config patch is never applied",
        lambda r: edit_build_cmd(r, SINGLE_CONFIG_CALL, "rem a comment still mentions patch-single-config.py"),
    )
    expect(
        "build.cmd 不再单独处理退出码 3（参数写错会被当成「判断不了」而放行）",
        "退出码 3",
        lambda r: edit_build_cmd(r, 'if "%RC%"=="3"', 'if "%RC%"=="9"'),
    )
    expect(
        "-LiteralPath 里写了通配符（不会被展开）",
        "-LiteralPath never expands it",
        lambda r: edit_workflow(
            r,
            "          $ErrorActionPreference = 'Stop'\n",
            "          $ErrorActionPreference = 'Stop'\n"
            "          Copy-Item -LiteralPath (Join-Path $env:DIST_DIR '*') -Destination x -Force\n",
        ),
    )

    print()
    if failures:
        print(f"FAIL: {len(failures)}/{checked} 条守卫形同虚设：{failures}")
        return 1
    print(f"OK: {checked}/{checked} 条守卫都能被真的破坏掉——检查不是装饰")
    return 0


if __name__ == "__main__":
    sys.exit(main())
