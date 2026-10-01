#!/usr/bin/env python3
"""把产出的一套轮子离线装进一次性 venv，逐条核对落地文件（纯标准库）。

前面几步证明的是"轮子本身没问题"（摘要对得上、RECORD 自洽），这一步证明的是
**"这一套轮子装得进去、装完是对的"**，而那正是用户唯一在乎的事：

  * 用 `--no-index --find-links` 装：任何一个轮子缺席（比如只发了改过的 Addons 而没发
    shiboken6）都会在这里变成硬失败，而不是等用户在自己机器上撞见"找不到满足条件的版本"；
  * 装完把运行时里的每个文件与 `site-packages\\PySide6\\` 下的同名文件逐字节比一遍。
    只查"文件在不在"是不够的：注入出错时最常见的表现是文件在、内容是旧的（覆盖没生效），
    或者大小一样但字节不对（把 v8 snapshot 换成另一个变体）；
  * 顺带读一遍四个发行版的元数据版本，确认发出去的就是我们说的那个版本。

刻意不做的：不 import PySide6、不看页面能不能放 H.264。那是浏览器里的事，本仓库的口径
是"只负责编出来/打包好并发布"，把它塞进门禁只会让门禁在无关的环境差异上变红。

用法：
    python verify-wheels.py -Runtime <运行时 zip 或目录> -Wheels <产物轮子目录> -Version 6.8.3
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

CHUNK = 1 << 20
EXPECTED_DISTRIBUTIONS = ("shiboken6", "PySide6_Essentials", "PySide6_Addons", "PySide6")


def log(msg: str) -> None:
    print(msg, flush=True)


def wheel_distribution(path: Path) -> str:
    """`PySide6_Addons-6.8.3-cp39-abi3-win_amd64.whl` → `PySide6_Addons`。

    文件名里发行版名用下划线占位（`-` 是字段分隔符），所以这里不做归一化——归一化是比对时
    的事，而且 PyPI 的项目名与文件名本来就差这一个字符。
    """
    return path.name.split("-")[0]


class Runtime:
    """与 inject 脚本同样的两种来源（目录 / zip），但这里只读，用不着那套写路径。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.is_zip = path.is_file() and path.suffix.lower() == ".zip"
        self._zip: zipfile.ZipFile | None = None
        if self.is_zip:
            self._zip = zipfile.ZipFile(path)
            self.names = sorted(n for n in self._zip.namelist() if not n.endswith("/"))
        else:
            self.names = sorted(
                p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file()
            )

    def open_stream(self, name: str):
        if self._zip is not None:
            return self._zip.open(name, "r")
        return (self.path / name).open("rb")

    def close(self) -> None:
        if self._zip is not None:
            self._zip.close()


def same_content(a: Path, runtime: Runtime, name: str) -> bool:
    with a.open("rb") as fa, runtime.open_stream(name) as fb:
        while True:
            chunk_a = fa.read(CHUNK)
            chunk_b = fb.read(CHUNK)
            if chunk_a != chunk_b:
                return False
            if not chunk_a:
                return True


def venv_python(venv_dir: Path) -> Path:
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def requires_python(wheel: Path) -> str:
    """从元数据轮子里读 Requires-Python（PySide6 的元数据轮子带着它）。读不到就返回空串。"""
    try:
        with zipfile.ZipFile(wheel) as z:
            meta = next((n for n in z.namelist() if n.endswith(".dist-info/METADATA")), None)
            if meta is None:
                return ""
            for line in z.read(meta).decode("utf-8", "replace").splitlines():
                if line.lower().startswith("requires-python:"):
                    return line.split(":", 1)[1].strip()
    except (OSError, zipfile.BadZipFile):
        return ""
    return ""


CLAUSE_RE = re.compile(r"(<=|>=|==|<|>|!=)\s*(\d+)(?:\.(\d+))?")


def check_requires_python(spec: str, version: tuple[int, int]) -> bool | None:
    """能不能判断这个解释器装得了这套轮子；判断不了返回 None（交给 pip 去报错）。

    只看 `<`/`<=`/`>`/`>=`/`==` 主次版本号这种最常见的写法。解析不了就返回 None——这个检查
    只是为了让"解释器版本不对"这条错误在日志里一眼可见，它不该成为新的失败来源。
    """
    clauses = [c.strip() for c in spec.split(",") if c.strip()]
    if not clauses:
        return None
    for clause in clauses:
        m = CLAUSE_RE.fullmatch(clause)
        if not m:
            return None
        op, major, minor = m.group(1), int(m.group(2)), int(m.group(3) or 0)
        got = version
        want = (major, minor)
        ok = {
            "<": got < want,
            "<=": got <= want,
            ">": got > want,
            ">=": got >= want,
            "==": got == want,
            "!=": got != want,
        }[op]
        if not ok:
            return False
    return True


def run(cmd: list[str], timeout: float) -> tuple[int, str]:
    proc = subprocess.run(  # noqa: S603 - 固定命令，参数来自命令行
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, proc.stdout or ""


def main() -> int:
    ap = argparse.ArgumentParser(description="install the produced wheels offline and verify them")
    ap.add_argument("-Runtime", required=True, help="运行时目录，或 build 阶段产出的 zip")
    ap.add_argument("-Wheels", required=True, help="产物轮子所在目录")
    ap.add_argument("-Version", required=True, help="要安装的 PySide6 版本，例如 6.8.3")
    ap.add_argument(
        "-KeepVenv", action="store_true", help="保留临时 venv（调试用；默认装完就删）"
    )
    ap.add_argument(
        "-PythonExecutable",
        default="",
        help="用来建 venv 的解释器（默认当前这个）。PySide6 6.8.3 要求 <3.14，本机是 3.14+ "
        "时必须指一个 3.12/3.13，否则装不上——这不是轮子的问题",
    )
    ap.add_argument("-Timeout", type=float, default=1800.0, help="pip 安装的超时（秒）")
    args = ap.parse_args()

    python_exe = args.PythonExecutable or sys.executable

    runtime_path = Path(args.Runtime)
    if not runtime_path.exists():
        log(f"[verify] 运行时不存在：{runtime_path}")
        return 1
    wheels_dir = Path(args.Wheels)
    wheels = sorted(wheels_dir.glob("*.whl")) if wheels_dir.is_dir() else []
    if not wheels:
        log(f"[verify] {wheels_dir} 里没有轮子")
        return 1

    version = args.Version
    distributions = {wheel_distribution(w) for w in wheels}
    missing = [d for d in EXPECTED_DISTRIBUTIONS if d not in distributions]
    if missing:
        # 这一条不必等 pip 去发现：缺一个就说明这一套不完整，而 pip 的报错会绕一圈
        # （"Could not find a version that satisfies the requirement ..."）。
        log(f"[verify] 这一套轮子缺了：{missing}（现有 {sorted(distributions)}）")
        return 1
    wrong_version = [w.name for w in wheels if w.name.split("-")[1:2] != [version]]
    if wrong_version:
        log(f"[verify] 这些轮子的版本不是 {version}：{wrong_version}")
        return 1

    runtime = Runtime(runtime_path)
    log(f"[verify] 运行时 {runtime_path}（{len(runtime.names)} 个文件）→ 离线装进一次性 venv")

    workdir = Path(tempfile.mkdtemp(prefix="pyside6-wheelcheck-"))
    venv_dir = workdir / "venv"
    failures: list[str] = []
    try:
        # 先量一下要用来建 venv 的解释器：PySide6 6.8.3 的 requires-python 是 <3.14，
        # 用一个装不了它的解释器去建 venv，pip 的报错是 "requires a different Python:
        # 3.14.7 not in '<3.14,>=3.9'"，离"我该换解释器"还有一段距离。
        meta_wheel = next((w for w in wheels if wheel_distribution(w) == "PySide6"), None)
        spec = requires_python(meta_wheel) if meta_wheel else ""
        code, out = run(
            [python_exe, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"], 120
        )
        if code != 0:
            log(f"[verify] 用不了这个解释器：{python_exe}（{out.strip()}）")
            return 1
        py_version = out.strip().splitlines()[-1]
        major, minor = (int(x) for x in py_version.split("."))
        verdict = check_requires_python(spec, (major, minor)) if spec else None
        log(f"[verify] 用 {python_exe}（{py_version}）建 venv；这套轮子要求 Python {spec or '（未知）'}")
        if verdict is False:
            log(
                f"[verify] 这个解释器装不了这套轮子：要求 {spec}，而它是 {py_version}。"
                "换一个（-PythonExecutable）再来；这不是轮子的问题"
            )
            return 1

        log(f"[verify] 建 venv：{venv_dir}")
        code, out = run([python_exe, "-m", "venv", "--clear", str(venv_dir)], args.Timeout)
        if code != 0:
            log(f"[verify] 建 venv 失败（退出码 {code}）：\n{out.strip()}")
            return 1
        py = venv_python(venv_dir)

        cmd = [
            str(py),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--find-links",
            str(wheels_dir),
            "--disable-pip-version-check",
            f"PySide6=={version}",
        ]
        log("[verify] " + " ".join(cmd))
        code, out = run(cmd, args.Timeout)
        for line in out.splitlines():
            log(f"[verify]   {line}")
        if code != 0:
            log(f"[verify] pip 离线安装失败（退出码 {code}）：这一套轮子不是一个能装上的集合")
            return 1

        code, out = run(
            [str(py), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"], 120
        )
        if code != 0:
            log(f"[verify] 取不到 venv 的 site-packages：{out.strip()}")
            return 1
        purelib = Path(out.strip().splitlines()[-1])
        log(f"[verify] site-packages = {purelib}")

        # 1. 运行时里的每个文件都要落地，且逐字节一致
        for name in runtime.names:
            installed = purelib / "PySide6" / name
            if not installed.is_file():
                failures.append(f"没落地：PySide6/{name}")
            elif not same_content(installed, runtime, name):
                failures.append(f"内容与运行时不一致（大小 {installed.stat().st_size:,}）：PySide6/{name}")

        # 2. 四个发行版的元数据版本
        code, out = run(
            [
                str(py),
                "-c",
                "import importlib.metadata as m, sys\n"
                "sys.stdout.write('\\n'.join(f'{p}\\t{m.version(p)}' for p in "
                "['PySide6','PySide6-Addons','PySide6-Essentials','shiboken6']))",
            ],
            120,
        )
        if code != 0:
            failures.append(f"读不到发行版元数据：{out.strip()}")
        else:
            for line in out.strip().splitlines():
                name, _, got = line.partition("\t")
                if got.strip() != version:
                    failures.append(f"{name} 的元数据版本是 {got.strip()!r}，期望 {version!r}")
                else:
                    log(f"[verify] 已安装 {name}=={got.strip()}")
    finally:
        runtime.close()
        if args.KeepVenv:
            log(f"[verify] 保留 venv：{venv_dir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)

    if failures:
        for f in failures:
            log(f"[verify] 失败：{f}")
        return 1
    log(
        f"[verify] 通过：{len(wheels)} 个轮子离线装得进去，运行时 {len(runtime.names)} 个文件"
        " 在 site-packages 里逐字节落位"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
