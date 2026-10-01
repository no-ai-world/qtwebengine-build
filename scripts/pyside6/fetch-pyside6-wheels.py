#!/usr/bin/env python3
"""从 PyPI 拉一份官方 PySide6 的 Windows x64 轮子（纯标准库）。

这条流水线不再自己编译任何东西：它拿本仓库 **已经发布** 的 QtWebEngine 运行时去替换官方
轮子里的同名文件。所以第一步是把官方轮子原封不动地取下来——原封不动是有要求的，见下。

为什么要自己解析 PyPI 的 JSON 而不是 `pip download`：

  * 要的是**精确的四个轮子**（shiboken6 / PySide6-Essentials / PySide6-Addons / PySide6），
    不是一个由解析器算出来的闭包。少一个，"完整的一套"就不成立——用户 `pip install
    PySide6==6.8.3 --no-index --find-links <Release>` 会因为缺依赖而直接失败；
  * `pip download` 的 --platform/--abi 组合是一堆容易写错的开关（写错的表现是解析器
    悄悄去拿别的变体或者干脆报"找不到匹配分发"，而不是明确地说"没有 win_amd64"）；
  * PyPI 的 JSON 里带着每个文件的 sha256，可以下载后立刻核对。轮子是我们唯一的输入，
    输入被人换掉这件事必须在几秒钟内暴露，而不是在用户装完之后。

平台/ABI 是筛出来的，不是猜出来的：只认文件名以 `-<platform>.whl` 结尾的那个文件。
每个包在当前版本下必须**恰好**有一个这样的文件——0 个（没有这个平台）和 ≥2 个
（不知道该选哪个）都直接失败，不做"挑第一个"这种静默选择。

用法：
    python fetch-pyside6-wheels.py -Version 6.8.3 -Destination wheels/
    python fetch-pyside6-wheels.py -Version 6.8.3 -Destination wheels/ -Platform win_amd64
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

# PyPI 的**项目名**（带连字符）。轮子文件名里是下划线，这是上游的命名，不要"修正"。
DEFAULT_PACKAGES = (
    "shiboken6",
    "PySide6-Essentials",
    "PySide6-Addons",
    "PySide6",
)

# PyPI 会拒掉没有 User-Agent 的请求，也给一句可读的来源标识，便于上游排查流量。
USER_AGENT = "qtwebengine-build/1.0 (+https://github.com/no-ai-world/qtwebengine-build)"

DEFAULT_INDEX = "https://pypi.org/pypi"
CHUNK = 1 << 20


def log(msg: str) -> None:
    print(msg, flush=True)


def http_get(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - 固定 https
        return resp.read()


def download(url: str, target: Path, timeout: float, expect_sha256: str) -> str:
    """流式下载并核对 sha256；不匹配就删掉半成品再抛。

    边下边算，避免把一个 128 MB 的轮子整个塞进内存；核对失败时留下半截文件比不留更糟
    （打包脚本会把它当成一个合法输入）。
    """
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    digest = hashlib.sha256()
    size = 0
    tmp = target.with_name(target.name + ".part")
    with urllib.request.urlopen(req, timeout=timeout) as resp, tmp.open("wb") as fh:  # noqa: S310
        while True:
            chunk = resp.read(CHUNK)
            if not chunk:
                break
            fh.write(chunk)
            digest.update(chunk)
            size += len(chunk)
    got = digest.hexdigest()
    if got != expect_sha256:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(
            f"sha256 不匹配：{target.name}\n  期望 {expect_sha256}\n  实得 {got}\n"
            "（PyPI 上的文件在下载期间被替换，或网络中间人）"
        )
    tmp.replace(target)
    log(f"[fetch] {target.name}  {size / (1 << 20):,.1f} MB  sha256={got}")
    return got


def pick_file(release: dict, package: str, version: str, platform: str) -> dict:
    """在当前版本的发布文件里挑出该平台的轮子；0 个或 ≥2 个都算失败。"""
    files = release.get("urls") or []
    suffix = f"-{platform}.whl"
    matches = [f for f in files if str(f.get("filename", "")).endswith(suffix)]
    if len(matches) == 1:
        return matches[0]
    available = sorted(str(f.get("filename", "")) for f in files)
    listed = "\n".join(f"    - {name}" for name in available) or "    （这个版本没有任何文件）"
    if not matches:
        raise RuntimeError(
            f"{package} {version} 在 PyPI 上没有以 {suffix} 结尾的文件：\n{listed}"
        )
    dupes = "\n".join(f"    - {f['filename']}" for f in matches)
    raise RuntimeError(
        f"{package} {version} 在 PyPI 上有 {len(matches)} 个 {suffix} 文件，不知道该用哪个：\n{dupes}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="fetch official PySide6 wheels from PyPI")
    ap.add_argument("-Version", required=True, help="PySide6 / Qt 版本，例如 6.8.3")
    ap.add_argument("-Destination", required=True, help="下载目录（不存在则创建）")
    ap.add_argument("-Platform", default="win_amd64", help="轮子的平台标签（默认 win_amd64）")
    ap.add_argument(
        "-Packages",
        default=",".join(DEFAULT_PACKAGES),
        help="要取的 PyPI 项目名，逗号分隔（默认四个：shiboken6 / PySide6-Essentials / "
        "PySide6-Addons / PySide6）",
    )
    ap.add_argument("-Index", default=DEFAULT_INDEX, help="PyPI JSON API 前缀")
    ap.add_argument("-Timeout", type=float, default=120.0, help="单次 HTTP 超时（秒）")
    args = ap.parse_args()

    version = args.Version.strip()
    packages = [p.strip() for p in args.Packages.split(",") if p.strip()]
    if not packages:
        log("[fetch] -Packages 是空的")
        return 1

    destination = Path(args.Destination)
    destination.mkdir(parents=True, exist_ok=True)
    index = args.Index.rstrip("/")

    failures: list[str] = []
    for package in packages:
        url = f"{index}/{package}/{version}/json"
        try:
            release = json.loads(http_get(url, args.Timeout).decode("utf-8"))
        except urllib.error.HTTPError as exc:
            failures.append(f"{package} {version}: PyPI 返回 HTTP {exc.code}（版本号写错了？）")
            continue
        except Exception as exc:  # noqa: BLE001 - 网络层面什么都可能，汇总成一句话
            failures.append(f"{package} {version}: 取 {url} 失败：{exc}")
            continue

        try:
            chosen = pick_file(release, package, version, args.Platform)
        except RuntimeError as exc:
            failures.append(str(exc))
            continue

        expect = str((chosen.get("digests") or {}).get("sha256") or "")
        if not expect:
            failures.append(f"{package} {version}: PyPI 没有给出 {chosen['filename']} 的 sha256")
            continue

        target = destination / str(chosen["filename"])
        if target.is_file():
            got = hashlib.sha256(target.read_bytes()).hexdigest()
            if got == expect:
                log(f"[fetch] {target.name}  已存在且 sha256 一致，跳过下载")
                continue
            log(f"[fetch] {target.name}  已存在但 sha256 不一致，重新下载")

        try:
            download(str(chosen["url"]), target, args.Timeout, expect)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{package} {version}: 下载 {chosen['filename']} 失败：{exc}")

    if failures:
        for f in failures:
            log(f"[fetch] 失败：{f}")
        return 1

    wheels = sorted(p for p in destination.glob("*.whl") if p.is_file())
    log(f"[fetch] 完成：{len(wheels)} 个轮子 → {args.Destination}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
