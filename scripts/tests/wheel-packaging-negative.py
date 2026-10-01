#!/usr/bin/env python3
"""打包脚本的**负向测试**：那几条「拒绝把坏产物发出去」的守卫要真的会报红。

为什么需要它：`inject-webengine-runtime.py` 与 `verify-wheels.py` 决定的是**发出去的东西**，
而它们最容易退化的方式是"静默地什么都不做"——运行时里的文件一个都没认领到、一个字节都没变、
少了一个发行版，全都可能走到 `exit 0`。那时 CI 是绿的、Release 是有的，坏在用户那边。

做法与 check-pipeline-negative.py 一致：在系统临时目录里造一套**最小的假轮子与假运行时**
（几十字节，不联网、不需要真 PySide6），逐个变体跑一次，断言脚本必须非零退出且报的正是那一条。

覆盖：
  1. 正常运行一次（前提：干净输入确实能过，否则下面的断言没意义）；
  2. 运行时里的文件在任何轮子里都没有对应位置（上游布局变了）；
  3. 同一个位置同时出现在两个轮子里（不知道该改哪个）；
  4. 运行时与上游逐字节相同（没有任何轮子会变——那说明这份运行时是多余的）；
  5. 一套轮子少一个发行版（verify-wheels 要在建 venv 之前就拒绝）。

用法：
    python scripts/tests/wheel-packaging-negative.py

退出码 0 = 每条守卫都真的会报红；1 = 有守卫形同虚设（或干净输入被误判）。
"""

from __future__ import annotations

import base64
import hashlib
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
INJECT = REPO / "scripts/pyside6/inject-webengine-runtime.py"
VERIFY = REPO / "scripts/pyside6/verify-wheels.py"

VERSION = "6.8.3"
failures: list[str] = []
checked = 0


def digest(data: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")


def make_wheel(path: Path, distribution: str, files: dict[str, bytes]) -> None:
    """造一个结构齐全（有 RECORD/WHEEL/METADATA）的假轮子——RECORD 不齐的话，注入脚本的
    校验步骤会因为输入本身是坏的而报红，测的就不是我们想测的那条守卫了。"""
    info = f"{distribution}-{VERSION}.dist-info"
    body = dict(files)
    body[f"{info}/METADATA"] = (
        f"Metadata-Version: 2.1\nName: {distribution}\nVersion: {VERSION}\n".encode()
    )
    body[f"{info}/WHEEL"] = b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        records = []
        for name, data in body.items():
            z.writestr(name, data)
            records.append(f"{name},sha256={digest(data)},{len(data)}")
        z.writestr(f"{info}/RECORD", "\n".join(records) + f"\n{info}/RECORD,,\n")


def make_runtime(path: Path, files: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)


RUNTIME_FILES = {
    "Qt6WebEngineCore.dll": b"self-built-webengine-core\n",
    "resources/icudtl.dat": b"icu-data\n",
}


def scaffold(workdir: Path, *, runtime_files=None, wheels=None, wheel_files=None) -> tuple[Path, Path, Path]:
    """一套最小输入：假运行时 + 假轮子目录。返回 (runtime, wheels_dir, out_dir)。"""
    runtime = workdir / "runtime.zip"
    make_runtime(runtime, RUNTIME_FILES if runtime_files is None else runtime_files)
    wheels_dir = workdir / "upstream"
    wheels_dir.mkdir(parents=True, exist_ok=True)
    for name, files in (wheels or default_wheels()).items():
        make_wheel(wheels_dir / name, name.split("-")[0], files)
    return runtime, wheels_dir, workdir / "out"


def default_wheels() -> dict[str, dict[str, bytes]]:
    """与真实 PySide6 6.8.3 同样的分布：icudtl.dat 在 Essentials，其余在 Addons。

    内容刻意与运行时**不同**（真正要测的是"注入了没有"），大小也不同。
    """
    return {
        f"PySide6_Addons-{VERSION}-py3-none-any.whl": {
            "PySide6/Qt6WebEngineCore.dll": b"official-webengine-core\n",
            "PySide6/Qt6WebEngineWidgets.dll": b"official-widgets\n",
        },
        f"PySide6_Essentials-{VERSION}-py3-none-any.whl": {
            "PySide6/resources/icudtl.dat": b"official-icu\n",
        },
        f"shiboken6-{VERSION}-py3-none-any.whl": {
            "shiboken6/Shiboken.pyd": b"official-shiboken\n",
        },
        f"PySide6-{VERSION}-py3-none-any.whl": {
            "PySide6/Qt3DCore.pyi": b"stub\n",
        },
    }


def run_script(script: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def expect(name: str, script: Path, build, expected: str, expect_rc: int = 1) -> None:
    """一处破坏 + 跑一次脚本：必须报出 expected（并且退出码是 expect_rc）。"""
    global checked
    checked += 1
    with tempfile.TemporaryDirectory(prefix="wheelneg-") as tmp:
        workdir = Path(tmp)
        runtime, wheels_dir, out_dir = scaffold(workdir)
        build(workdir, runtime, wheels_dir, out_dir)
        rc, out = run_script(
            script,
            "-Runtime",
            str(runtime),
            "-Wheels",
            str(wheels_dir),
            *(["-Destination", str(out_dir), "-ExpectVersion", VERSION] if script == INJECT else []),
            *(["-Version", VERSION] if script == VERIFY else []),
        )
    hit = expected in out and rc == expect_rc
    print(f"  [{'PASS' if hit else 'FAIL'}] {name}")
    if not hit:
        print(f"        期望 rc={expect_rc} 且报出 {expected!r}\n        实际 rc={rc}：{out.strip()[:300]}")
        failures.append(name)


def main() -> int:
    # 前提：干净输入必须能过，否则下面的"报红"可能只是别的原因
    global checked
    checked += 1
    with tempfile.TemporaryDirectory(prefix="wheelneg-") as tmp:
        runtime, wheels_dir, out_dir = scaffold(Path(tmp))
        rc, out = run_script(
            INJECT,
            "-Runtime",
            str(runtime),
            "-Wheels",
            str(wheels_dir),
            "-Destination",
            str(out_dir),
            "-ExpectVersion",
            VERSION,
        )
        produced = sorted(p.name for p in out_dir.glob("*.whl")) if out_dir.is_dir() else []
    print("打包脚本负向测试：每条守卫都要能被真的破坏掉")
    if rc != 0 or len(produced) != 4:
        print(f"  [FAIL] 干净输入就没过（rc={rc}，产物 {produced}）——先修这个")
        print("        " + out.strip().replace("\n", "\n        ")[:400])
        return 1
    print("  [PASS] 干净输入通过（负向测试的前提）")

    def identical_runtime(w: Path, r: Path, wd: Path, o: Path) -> None:
        """只有一个轮子、且它与运行时逐字节相同：注入不会改变任何东西。"""
        make_runtime(r, {"Qt6WebEngineCore.dll": b"official-webengine-core\n"})
        shutil.rmtree(wd)
        wd.mkdir()
        make_wheel(
            wd / f"PySide6_Addons-{VERSION}-py3-none-any.whl",
            "PySide6_Addons",
            {"PySide6/Qt6WebEngineCore.dll": b"official-webengine-core\n"},
        )

    expect(
        "运行时里的文件在任何轮子里都没有对应位置（上游布局变了）",
        INJECT,
        lambda w, r, wd, o: make_runtime(r, {**RUNTIME_FILES, "Qt6WebEngineQuick.dll": b"x\n"}),
        "在任何轮子里都没有",
    )
    expect(
        "同一个位置同时出现在两个轮子里（不知道该改哪个）",
        INJECT,
        lambda w, r, wd, o: make_wheel(
            wd / f"PySide6-{VERSION}-py3-none-any.whl",
            "PySide6",
            {"PySide6/Qt6WebEngineCore.dll": b"another-official-core\n"},
        ),
        "同时出现在多个轮子里",
    )
    expect(
        "运行时与上游逐字节相同（这份运行时是多余的）",
        INJECT,
        identical_runtime,
        "没有任何轮子发生实质变化",
    )
    expect(
        "一套轮子少了一个发行版（不该等到用户机器上才发现）",
        VERIFY,
        lambda w, r, wd, o: (wd / f"shiboken6-{VERSION}-py3-none-any.whl").unlink(),
        "这一套轮子缺了",
    )

    print()
    if failures:
        print(f"FAIL: {len(failures)}/{checked} 条守卫形同虚设：{failures}")
        return 1
    print(f"OK: {checked}/{checked} 条守卫都能被真的破坏掉——打包脚本不会静默地把坏产物发出去")
    return 0


if __name__ == "__main__":
    sys.exit(main())
