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
WHEEL_WORKFLOW = ".github/workflows/build-pyside6-wheels.yml"

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
    (root / "scripts/pyside6").mkdir(parents=True)
    (root / ".github/workflows").mkdir(parents=True)
    shutil.copy2(REPO / "scripts/check-pipeline.py", root / "scripts/check-pipeline.py")
    for py in sorted((REPO / "scripts/qtwebengine").glob("*.py")):
        shutil.copy2(py, root / "scripts/qtwebengine" / py.name)
    for py in sorted((REPO / "scripts/pyside6").glob("*.py")):
        shutil.copy2(py, root / "scripts/pyside6" / py.name)
    shutil.copy2(REPO / BUILD_CMD, root / BUILD_CMD)
    shutil.copy2(REPO / WORKFLOW, root / WORKFLOW)
    shutil.copy2(REPO / WHEEL_WORKFLOW, root / WHEEL_WORKFLOW)
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


def edit_wheel_workflow(root: Path, old: str, new: str) -> None:
    path = root / WHEEL_WORKFLOW
    text = path.read_text(encoding="utf-8")
    assert old in text, f"mutation target not found in wheel workflow: {old[:60]!r}"
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
        lambda r: edit_build_cmd(r, 'if "%STEP_RC%"=="3"', 'if "%STEP_RC%"=="9"'),
    )
    # 环境变量 RC 会撞上 CMake 的资源编译器：子进程继承它之后 GN 的 configure 直接失败，
    # check-ccache-bound.py 只能答「判断不了」，门禁静默失效（真实踩过的那次就是这个）。
    expect(
        "build.cmd 又拿 RC 当环境变量（CMake 会当成资源编译器路径）",
        "用 RC 当环境变量",
        lambda r: edit_build_cmd(r, 'set "STEP_RC=%errorlevel%"', 'set "RC=%errorlevel%"'),
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
    # auto_continue 少了 CACHE_TOKEN 本来就是「轮末才知道」的失败，所以这条守卫要守住两件事：
    # 预检步还在，而且它真的 throw（换成 Write-Host 就又变回静默断链了）。
    expect(
        "auto_continue 的预检步被删掉（秘密缺失时只能在轮末才发现）",
        "的预检步",
        lambda r: edit_workflow(r, "预检：auto_continue 需要 CACHE_TOKEN", "预检（已删除）"),
    )
    expect(
        "auto_continue 的预检步不再 throw（只打日志，续跑静默断链）",
        "在 CACHE_TOKEN 为空时不 throw",
        lambda r: edit_workflow(
            r,
            "throw 'auto_continue=true 但没有 CACHE_TOKEN",
            "Write-Host 'auto_continue=true 但没有 CACHE_TOKEN",
        ),
    )

    # 轮子流水线：它存在的意义就是"别再编一遍"+"发出去的确实装得上"
    expect(
        "轮子流水线不再从已有 Release 取运行时（改成自己编）",
        "没有从已有 Release 取运行时",
        lambda r: edit_wheel_workflow(
            r,
            "          gh release download $env:WEBENGINE_TAG `\n",
            "          # 取运行时那一步删掉了（注释里还写着 gh release download）\n",
        ),
    )
    expect(
        "gh release download 的标签写死（换版本会取到错的运行时）",
        "没有用 WEBENGINE_TAG",
        lambda r: edit_wheel_workflow(
            r,
            "gh release download $env:WEBENGINE_TAG `",
            "gh release download qtwebengine-6.8.3-win64-msvc2022-codecs `",
        ),
    )
    expect(
        "注入脚本的调用整行删掉、注释保留",
        "没有被 workflow 的非注释行实际调用",
        lambda r: edit_wheel_workflow(
            r,
            "          python -u -X utf8 scripts/pyside6/inject-webengine-runtime.py `\n",
            "          # python -u -X utf8 scripts/pyside6/inject-webengine-runtime.py 以前在这里\n",
        ),
    )
    expect(
        "离线安装自测的调用整行删掉、注释保留",
        "verify-wheels.py 没有被 workflow 的非注释行实际调用",
        lambda r: edit_wheel_workflow(
            r,
            "          python -u -X utf8 scripts/pyside6/verify-wheels.py `\n",
            "          # python -u -X utf8 scripts/pyside6/verify-wheels.py 以前在这里\n",
        ),
    )
    expect(
        "发布步不再以离线安装自测为前置（装不上也照发）",
        "没有以离线安装自测",
        lambda r: edit_wheel_workflow(
            r,
            "if: ${{ success() && steps.wheelcheck.outcome == 'success' && inputs.create_release }}",
            "if: ${{ success() && inputs.create_release }}",
        ),
    )
    # 真踩过：run 36806802921 里取轮子那一步打印"完成：4 个轮子"时抛 UnicodeEncodeError
    # （管道下 locale 是 cp1252），整步退出码 1——四个轮子都已经下好了。
    expect(
        "启动脚本时没带 -X utf8 / -u（中文日志会抛 UnicodeEncodeError）",
        "启动脚本没有 -u -X utf8",
        lambda r: edit_wheel_workflow(
            r,
            "python -u -X utf8 scripts/pyside6/inject-webengine-runtime.py",
            "python scripts/pyside6/inject-webengine-runtime.py",
        ),
    )
    expect(
        "发布步不再覆盖同名资产（重发会判红）",
        "overwrite_files: true",
        lambda r: edit_wheel_workflow(r, "overwrite_files: true", "overwrite_files: false"),
    )
    expect(
        "发布集合不再由账本决定（退回发 dist/wheels 全部）",
        "发布步不是发暂存集合",
        lambda r: edit_wheel_workflow(r, "files: dist/publish/*", "files: dist/wheels/*.whl"),
    )
    expect(
        "Release 正文退回手写（会与产物漂移）",
        "Release 正文不是生成出来的",
        lambda r: edit_wheel_workflow(r, "          body_path: dist/publish/RELEASE_NOTES.md\n", ""),
    )
    # 实测踩过：发布集合从四个缩成一个之后，Release 里仍留着上一轮的三份资产，
    # 连旧的 PySide6_Addons-6.8.3（没有 +codecs）都在。
    expect(
        "发布前不清旧资产（action-gh-release 只增不删，集合变小时会自相矛盾）",
        "没有清掉不属于本次发布集合的旧资产",
        lambda r: edit_wheel_workflow(
            r,
            "            gh release delete-asset $env:RELEASE_TAG $a.name --repo $env:GITHUB_REPOSITORY --yes\n",
            "            # 删旧资产那一步去掉了（注释里还写着 delete-asset 这个词会被检查抓到吗）\n",
        ),
    )
    expect(
        "fetch 退回写死清单（不再从 requires_dist 推一套的构成）",
        "不是从 PySide6 的 requires_dist 推的",
        lambda r: (r / "scripts/pyside6/fetch-pyside6-wheels.py").write_text(
            (r / "scripts/pyside6/fetch-pyside6-wheels.py")
            .read_text(encoding="utf-8")
            .replace("requires_dist", "REPLACED"),
            encoding="utf-8",
        ),
    )
    expect(
        "注入脚本不再写账本（发布集合就没有依据了）",
        "注入脚本不写账本",
        lambda r: (r / "scripts/pyside6/inject-webengine-runtime.py").write_text(
            (r / "scripts/pyside6/inject-webengine-runtime.py")
            .read_text(encoding="utf-8")
            .replace('"-Manifest"', '"-NoManifestAnymore"'),
            encoding="utf-8",
        ),
    )
    expect(
        "自测不再离线装（解析器会去 PyPI 补齐，完整性判据静默失效）",
        'verify-wheels.py 的安装命令里少了 "--no-index"',
        lambda r: (r / "scripts/pyside6/verify-wheels.py").write_text(
            (r / "scripts/pyside6/verify-wheels.py")
            .read_text(encoding="utf-8")
            .replace('"--no-index",', '')
            .replace('"--find-links",', ""),
            encoding="utf-8",
        ),
    )
    expect(
        "scripts/pyside6 下多了一个没人调用的脚本（死代码）",
        "没有被 workflow 的非注释行实际调用",
        lambda r: (r / "scripts/pyside6/nobody-calls-me.py").write_text(
            "print(1)\n", encoding="utf-8"
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
