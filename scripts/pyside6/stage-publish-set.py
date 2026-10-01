#!/usr/bin/env python3
"""按账本挑出**必须发**的轮子，并生成 Release 的说明（纯标准库）。

这一步存在的理由是一条规则，而不是一次判断：

    某个轮子必须发 ⟺ 我们的运行时实际改动了它的内容。

其余轮子与 PyPI 上的原件逐字节相同（实测摘要一致），re-host 它们只是搬运，不是产物。所以
"发哪几个文件"不能写死在 workflow 里（写死的那一刻就与规则脱钩了：哪天 `resources/icudtl.dat`
变了，Essentials 就必须跟着发，而写死的 glob 不会知道），而是从注入脚本的账本里读出来。

顺带把 Release 说明也**生成**出来：里面的文件名、版本、改动项数、安装命令全部来自实际产物，
不是手写的文案。手写文案会漂——漂的那一刻，用户照着说明装出来的东西就不是这里发的那个。

产出（都在 -Destination 里）：

    <必须发的轮子>.whl      从 -Stage 拷过来
    SHA256SUMS              只有实际发出去的文件，行尾 LF（sha256sum -c 能直接核）
    MANIFEST.json           发出去的 + 从 PyPI 解析的，各自的来源与摘要（机器可读）
    RELEASE_NOTES.md        Release 正文（人读的，从同样的数据生成）

用法：
    python stage-publish-set.py -Stage dist/wheels -Inject dist/inject-manifest.json \\
        -Upstream dist/upstream-manifest.json -Destination dist/publish \\
        -ReleaseUrlBase https://github.com/<owner>/<repo>/releases/download/<tag>/
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path


def log(msg: str) -> None:
    print(msg, flush=True)


def use_utf8_streams() -> None:
    """CI 把输出接进管道，此时 Python 用 locale 编码（cp1252）会对中文抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except (AttributeError, ValueError, OSError):
            pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_text_lf(path: Path, text: str, encoding: str = "utf-8") -> None:
    """写文本文件，行尾固定 LF。

    `Path.write_text` 在 Windows 上会把 `\\n` 翻译成 CRLF（文本模式的默认行为），而
    `sha256sum -c` / `shasum -c` 会把 CR 当成文件名的一部分——整份清单一条都核不了
    （实测报的是 `'PySide6_Addons-…whl'$'\\r': No such file or directory`）。这个坑在
    workflow 的内联 pwsh 里踩过一次，挪到 Python 里又踩了一次，所以这里写成唯一入口。
    """
    with path.open("w", encoding=encoding, newline="\n") as fh:
        fh.write(text)


def requirement_name(distribution: str) -> str:
    """轮子文件名里的发行版名（`PySide6_Addons`）→ 依赖里写的名字（`PySide6-Addons`）。

    两者本来就只差这一个字符，但**不要**在文件名那边"修正"它：文件名用下划线是上游的命名，
    而 PEP 508 的依赖名两种写法等价。
    """
    return distribution.replace("_", "-")


def main() -> int:
    ap = argparse.ArgumentParser(description="stage the wheels that must be published")
    ap.add_argument("-Stage", required=True, help="注入后的轮子目录（含透传的那些）")
    ap.add_argument("-Inject", required=True, help="注入脚本写出的账本 JSON")
    ap.add_argument("-Upstream", default="", help="取官方轮子的账本 JSON（用来钉住从 PyPI 解析的那几份）")
    ap.add_argument("-Destination", required=True, help="发布目录（不存在则创建）")
    ap.add_argument(
        "-ReleaseUrlBase",
        default="",
        help="Release 资产的 URL 前缀（以 / 结尾）。给了它，说明里就直接给出可粘贴的直链安装命令",
    )
    ap.add_argument("-WebEngineTag", default="", help="运行时取自哪个 QtWebEngine Release（写进说明）")
    args = ap.parse_args()
    use_utf8_streams()

    stage = Path(args.Stage)
    inject = json.loads(Path(args.Inject).read_text(encoding="utf-8"))
    upstream = (
        json.loads(Path(args.Upstream).read_text(encoding="utf-8")) if args.Upstream else None
    )
    destination = Path(args.Destination)
    destination.mkdir(parents=True, exist_ok=True)

    must = [w for w in inject.get("wheels", []) if w.get("modified")]
    passthrough = [w for w in inject.get("wheels", []) if not w.get("modified")]
    if not must:
        log("[publish] 账本里没有任何「必须发」的轮子：运行时没有改动任何一个发行版")
        return 1

    # 1. 拷必须发的那些（先清掉上一轮留下的同名/旧名文件）
    for old in destination.glob("*.whl"):
        old.unlink()
    published: list[dict] = []
    for wheel in must:
        src = stage / wheel["published_filename"]
        if not src.is_file():
            log(f"[publish] 账本里说要发 {src.name}，但 {stage} 里没有")
            return 1
        dst = destination / src.name
        shutil.copy2(src, dst)
        got = sha256_file(dst)
        if got != wheel["published_sha256"]:
            log(f"[publish] {src.name} 拷过去之后 sha256 变了：{got} != {wheel['published_sha256']}")
            return 1
        published.append(dict(wheel, sha256=got))
        log(f"[publish] 发 {src.name}（改动 {wheel['replaced']}+{wheel['added']} 项）")

    # 2. SHA256SUMS：只有实际发出去的文件；LF 行尾（sha256sum -c 在 Linux/macOS 上直接用）
    lines = [
        f"{w['sha256']}  {w['published_filename']}"
        for w in sorted(published, key=lambda w: w["published_filename"])
    ]
    sums = destination / "SHA256SUMS"
    write_text_lf(sums, "\n".join(lines) + "\n", encoding="ascii")
    # 写完自己查一遍：少了这一步，CRLF 会一路混到用户手里才被发现（整份清单读不了）
    if b"\r" in sums.read_bytes():
        log("[publish] SHA256SUMS 里混进了 CR：sha256sum -c 在 Linux/macOS 上会整份读不了")
        return 1

    # 3. MANIFEST.json：发出去的 + 从 PyPI 解析的，各自的来源与摘要
    pinned = []
    if upstream:
        for pkg in upstream.get("packages", []):
            pinned.append(
                {
                    "name": pkg["name"],
                    "filename": pkg["filename"],
                    "url": pkg["url"],
                    "sha256": pkg["sha256"],
                    "resolution": "PyPI",
                }
            )
    manifest = {
        "version": inject.get("version") or (upstream or {}).get("version"),
        "local_version": inject.get("local_version") or "",
        "webengine_tag": args.WebEngineTag,
        "published": [
            {
                "name": w["name"],
                "filename": w["published_filename"],
                "version": w["published_version"],
                "sha256": w["sha256"],
                "replaced": w["replaced"],
                "added": w["added"],
                "changed_files": w["changed_files"],
                "upstream_sha256": w["upstream_sha256"],
            }
            for w in published
        ],
        "unchanged_from_upstream": [
            {
                "name": w["name"],
                "filename": w["published_filename"],
                "version": w["published_version"],
                "sha256": w["published_sha256"],
                "owned_runtime_files": w["owned"],
            }
            for w in passthrough
        ],
        "from_pypi": pinned,
    }
    write_text_lf(
        destination / "MANIFEST.json",
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )

    # 4. Release 说明：所有数字都来自上面这份数据
    base = args.ReleaseUrlBase.rstrip("/") + "/" if args.ReleaseUrlBase else ""
    version = manifest["version"] or ""
    out: list[str] = []
    out.append(f"可以直接 `pip install` 的 PySide6 {version}（Windows x64）：QtWebEngine 换成了")
    out.append("本仓库自建的、带私有编解码器（H.264/AAC/MP3）的运行时。")
    out.append("")
    out.append("## 这个 Release 里有什么")
    out.append("")
    out.append("**只有被运行时实际改动过的轮子**——其余发行版与 PyPI 上的原件逐字节相同，")
    out.append("从 PyPI 解析即可（下面钉了它们的版本与摘要，可以核对）。")
    out.append("")
    for w in published:
        dist = requirement_name(w["name"])
        out.append(f"### {dist} {w['published_version']}")
        out.append("")
        out.append(f"`{w['published_filename']}`")
        out.append("")
        out.append(f"覆盖 {w['replaced'] + w['added']} 个文件：")
        out.append("")
        for name in w["changed_files"]:
            out.append(f"* `{name}`")
        out.append("")
    if manifest["unchanged_from_upstream"]:
        out.append("## 与上游逐字节相同、因此不在这里的轮子")
        out.append("")
        out.append("| 发行版 | 版本 | 文件 | sha256 |")
        out.append("| --- | --- | --- | --- |")
        for w in sorted(manifest["unchanged_from_upstream"], key=lambda w: w["name"]):
            out.append(
                f"| {requirement_name(w['name'])} | {w['version']} | `{w['filename']}` | `{w['sha256']}` |"
            )
        out.append("")
    out.append("## 装")
    out.append("")
    out.append(f"**版本必须钉 `=={version}`**：运行时只对 Qt {version} 有效，不钉版本时解析器会去")
    out.append(f"拿 PyPI 上更新的版本（那与这份产物无关）。")
    out.append("")
    if base:
        out.append("```bash")
        reqs = " ".join(f'"{requirement_name(w["name"])} @ {base}{w["published_filename"]}"' for w in published)
        out.append(f"uv add \"pyside6=={version}\" {reqs}")
        out.append(f"pip install \"pyside6=={version}\" {reqs}")
        out.append("```")
        out.append("")
        out.append(
            f"离线：按 `MANIFEST.json` 把这一套（本 Release 的 + 从 PyPI 取的那几份）下到一个"
            f"目录，再 `--no-index --find-links <目录>` 装。"
        )
    else:
        out.append("见 Release 资产与本仓库的 `docs/wheels.md`。")
    out.append("")
    if args.WebEngineTag:
        out.append("## 运行时来源与口径")
        out.append("")
        out.append(f"运行时取自本仓库的 Release `{args.WebEngineTag}`，由 `build-qtwebengine` 用")
        out.append("`-webengine-proprietary-codecs` 从 Qt 源码编出。")
        out.append("")
        out.append("本流水线保证的是**打包正确**（摘要核对、RECORD 自洽、离线装得上、落地字节一致），")
        out.append("**不验**产物里的编解码器是否真的生效——那是浏览器里的事。")
        out.append("")
    write_text_lf(destination / "RELEASE_NOTES.md", "\n".join(out) + "\n")

    log(
        f"[publish] 发布集合：{len(published)} 个轮子（未上架 {len(passthrough)} 个与上游逐字节相同）"
        f" → {args.Destination}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
