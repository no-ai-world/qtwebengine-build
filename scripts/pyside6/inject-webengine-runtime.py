#!/usr/bin/env python3
"""把自建的 QtWebEngine 运行时注入官方 PySide6 轮子，产出可 pip 安装的一整套轮子（纯标准库）。

这是「拿到运行时之后」的那一步：不再自己编译任何东西，而是把本仓库**已经发布**的
QtWebEngine 运行时（带私有编解码器）覆盖进 PyPI 官方轮子里同名位置的文件，重新打一个轮子。
用户于是不必解压 zip 手动铺文件——`pip install` 一套轮子就完事。

为什么不是"改一个轮子"而是"认领一批轮子"：

  运行时的 64 个文件**不落在同一个轮子里**。实测 PySide6 6.8.3：

    * `resources/icudtl.dat` 在 **PySide6_Essentials** 里（Qt6Core 也要它）；
    * 其余 63 个（`Qt6WebEngineCore.dll`、`QtWebEngineProcess.exe`、Widgets/Quick 的 DLL、
      `resources/*.pak`、`translations/qtwebengine_locales/*.pak`）在 **PySide6_Addons** 里。

  所以映射是**推出来的**，不是写死的：运行时里的每个相对路径，去每个轮子里找
  `<PackageRoot>/<相对路径>`，必须**恰好命中一个**轮子。0 个（上游布局变了）和 ≥2 个
  （不知道该改哪个）都直接失败——不做"默认塞进 Addons"这种猜测，猜错的表现是某个文件
  落在错误的发行版里，用户 `pip uninstall PySide6-Addons` 之后它还留在磁盘上。

一个字节都没变的轮子**原样透传**，不重新打包：

  重新压缩一个没有实质变化的轮子只会让它的 sha256 与 PyPI 上的对不上，而那种"看起来被
  改过、其实没改"的轮子让用户失去唯一的独立核对手段（PyPI 给出的摘要）。实测
  `resources/icudtl.dat` 在上游轮子与自建运行时里逐字节相同，`qtwebengine_resources.pak`
  也是——那样的话整个 PySide6_Essentials 就不该被重打。

重新打包必然要重算 RECORD：轮子里的 RECORD 是每个文件一行
`路径,sha256=<urlsafe-b64 去填充>,大小`，被换掉的文件大小与摘要都变了。这里逐条重算
（不复用上游那行），产出之后再**重新打开产物逐条核对**——一次成功的注入必须在几秒钟内自证。

用法：
    python inject-webengine-runtime.py -Runtime <运行时目录或 zip> -Wheels <上游轮子目录> \\
        -Destination <产物目录> -ExpectVersion 6.8.3
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path

RECORD_SUFFIX = ".dist-info/RECORD"
CHUNK = 1 << 20
# 新增条目用一个固定时间戳：同一套输入 + 同一个解释器重复打包要给出同一个 sha256。
# 跨解释器只能保证**内容**一致：容器里那层 deflate 字节由打包用的 Python 的 zlib 决定，
# 实测 3.12 与 3.14 压出来的总大小差 3.7 MB，而 RECORD/CRC/大小逐条相同。所以 workflow 拿
# `python_version` 钉住解释器（换解释器等于换一份字节），Release 里的 SHA256SUMS 钉住的是
# 实际发出去的那份字节。
FIXED_DATE_TIME = (2025, 3, 24, 22, 52, 20)


def log(msg: str) -> None:
    print(msg, flush=True)


def use_utf8_streams() -> None:
    """CI 把输出接进管道，此时 Python 用 locale 编码（cp1252）会对中文抛 UnicodeEncodeError，
    而且默认块缓冲会让日志攒在缓冲区里。两个都从脚本内部兜住，不依赖调用方记得加
    `-u -X utf8`（workflow 里加了）。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except (AttributeError, ValueError, OSError):
            pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def record_hash(data: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")


def hash_stream(fh) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: fh.read(CHUNK), b""):
        digest.update(chunk)
    return base64.urlsafe_b64encode(digest.digest()).rstrip(b"=").decode("ascii")


def parse_record(text: str) -> dict[str, tuple[str, str]]:
    """RECORD → {路径: (算法, 值)}。RECORD 自己那一行的值是空的。"""
    out: dict[str, tuple[str, str]] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.rsplit(",", 2)
        if len(parts) != 3:
            continue
        name, digest, _size = parts
        algo, _, value = digest.partition("=")
        out[name] = (algo, value)
    return out


class Runtime:
    """运行时文件清单：相对路径（posix）→ 取字节的方式。

    目录与 zip 两种来源都吃：CI 直接把刚下载的 zip 喂进来，省掉一次解压；本地调试时指一个
    解压目录更顺手。
    """

    def __init__(self, runtime: Path) -> None:
        self.path = runtime
        self.is_zip = runtime.is_file() and runtime.suffix.lower() == ".zip"
        self._zip: zipfile.ZipFile | None = None
        if self.is_zip:
            self._zip = zipfile.ZipFile(runtime)
            self.names = sorted(n for n in self._zip.namelist() if not n.endswith("/"))
        else:
            self.names = sorted(
                p.relative_to(runtime).as_posix() for p in runtime.rglob("*") if p.is_file()
            )

    def size(self, name: str) -> int:
        if self._zip is not None:
            return self._zip.getinfo(name).file_size
        return (self.path / name).stat().st_size

    def open_stream(self, name: str):
        if self._zip is not None:
            return self._zip.open(name, "r")
        return (self.path / name).open("rb")

    def close(self) -> None:
        if self._zip is not None:
            self._zip.close()


def make_info(src: zipfile.ZipInfo | None, name: str, date_time: tuple[int, ...]) -> zipfile.ZipInfo:
    """被替换的条目沿用上游的 ZipInfo 元数据（压缩方式/权限位/时间戳），新增条目给一组固定值。

    这样除了内容与 RECORD，产物的其余部分与上游逐位一致——出问题时"到底变了什么"是可回答的。
    """
    zi = zipfile.ZipInfo(name, date_time=date_time)
    if src is not None:
        zi.compress_type = src.compress_type
        zi.external_attr = src.external_attr
        zi.internal_attr = src.internal_attr
        zi.create_system = src.create_system
        zi.comment = src.comment
    else:
        zi.compress_type = zipfile.ZIP_DEFLATED
        # 上游轮子里的值就是 0o100666 << 16（DOS 意义上的 rw-rw-rw-）
        zi.external_attr = 0o100666 << 16
        zi.create_system = 0
    return zi


def stream_into(zout: zipfile.ZipFile, zi: zipfile.ZipInfo, src, expect_size: int | None):
    """把 src 流式写进 zip 条目，边写边算摘要与大小（最大的那个 DLL 有 150 MB，不整块进内存）。"""
    digest = hashlib.sha256()
    size = 0
    with zout.open(zi, "w") as out:
        while True:
            chunk = src.read(CHUNK)
            if not chunk:
                break
            out.write(chunk)
            digest.update(chunk)
            size += len(chunk)
    if expect_size is not None and size != expect_size:
        raise RuntimeError(f"{zi.filename}: 读到的字节数 {size:,} 与预期 {expect_size:,} 不一致")
    return base64.urlsafe_b64encode(digest.digest()).rstrip(b"=").decode("ascii"), size


def same_bytes(zf: zipfile.ZipFile, target: str, runtime: Runtime, name: str) -> bool:
    """轮子里某个位置的内容与运行时里的同名文件是否逐字节相同（分块比，不整块进内存）。"""
    if zf.getinfo(target).file_size != runtime.size(name):
        return False
    with zf.open(target, "r") as a, runtime.open_stream(name) as b:
        while True:
            chunk_a = a.read(CHUNK)
            chunk_b = b.read(CHUNK)
            if chunk_a != chunk_b:
                return False
            if not chunk_a:
                return True


def wheel_name_version(filename: str) -> tuple[str, str]:
    """`PySide6_Addons-6.8.3-cp39-abi3-win_amd64.whl` → ("PySide6_Addons", "6.8.3")。

    轮子文件名的字段是 `发行版-版本(-构建号)?-python-abi-平台.whl`，发行版里的 `-` 在文件名里
    写成 `_`，所以前两个字段就是这两个值。
    """
    parts = filename.split("-")
    if len(parts) < 5:
        raise RuntimeError(f"{filename}: 不像一个轮子文件名")
    return parts[0], parts[1]


def bump_local_version(text: bytes, old_version: str, new_version: str) -> bytes:
    """把 METADATA 里的 `Version:` 行换成带本地段的版本。只改那一行。"""
    out = []
    for line in text.decode("utf-8").splitlines(keepends=True):
        if line.startswith("Version:") and line.strip().split(":", 1)[1].strip() == old_version:
            out.append(f"Version: {new_version}\n")
        else:
            out.append(line)
    return "".join(out).encode("utf-8")


def repack(
    src: Path,
    dst: Path,
    runtime: Runtime,
    replaces: dict[str, str],
    additions: dict[str, str],
    local_version: str = "",
) -> dict:
    """写出注入后的轮子。

    逐条走上游的顺序，只换内容不换位置。目录条目照抄且不进 RECORD（PEP 427 的 RECORD 只列
    文件）——实测这几个轮子里没有目录条目，但这一条写错了在别的轮子上就是"RECORD 里多一行
    指向不存在的文件"。

    `local_version` 非空时，给**这个被改动过的**轮子挂上本地版本段（`6.8.3+codecs`）：改
    `*.dist-info/` 目录名与 METADATA 里的 `Version:`，输出文件名同时改名。这样它在任何解析器
    眼里都比 PyPI 上的 `6.8.3` **大**，即使索引里同时有官方包也会确定地选中我们的
    （PEP 440：说明符不带本地段时，匹配会忽略候选版本的本地段，所以 `pyside6==6.8.3` 声明的
    `pyside6-addons==6.8.3` 依然满足）。
    """
    stats = {
        "replaced": 0,
        "added": 0,
        "entries": 0,
        "injected_bytes": 0,
        "changed_files": [],
        "published_name": src.name,
    }
    dist, upstream_version = wheel_name_version(src.name)
    out_version = f"{upstream_version}+{local_version}" if local_version else upstream_version
    old_prefix = f"{dist}-{upstream_version}.dist-info/"
    new_prefix = f"{dist}-{out_version}.dist-info/"
    stats["published_name"] = src.name.replace(
        f"-{upstream_version}-", f"-{out_version}-", 1
    )

    with zipfile.ZipFile(src) as zin:
        infos = zin.infolist()
        record_name = next((i.filename for i in infos if i.filename.endswith(RECORD_SUFFIX)), None)
        if record_name is None:
            raise RuntimeError(f"{src.name}: 没有 {RECORD_SUFFIX}")
        records: list[str] = []

        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
            for info in infos:
                if info.filename == record_name:
                    continue  # RECORD 最后写
                name = info.filename
                if local_version and name.startswith(old_prefix):
                    name = new_prefix + name[len(old_prefix) :]
                if info.is_dir():
                    zout.writestr(make_info(info, name, info.date_time), b"")
                    continue
                if info.filename in replaces:
                    runtime_name = replaces[info.filename]
                    with runtime.open_stream(runtime_name) as src_fh:
                        digest, size = stream_into(
                            zout, make_info(info, name, info.date_time), src_fh, None
                        )
                    stats["replaced"] += 1
                    stats["changed_files"].append(name)
                elif local_version and name == new_prefix + "METADATA":
                    # 只有 METADATA 需要改内容；它很小，不必流式
                    data = bump_local_version(zin.read(info), upstream_version, out_version)
                    digest = record_hash(data)
                    size = len(data)
                    zout.writestr(make_info(info, name, info.date_time), data)
                else:
                    with zin.open(info, "r") as src_fh:
                        digest, size = stream_into(
                            zout,
                            make_info(info, name, info.date_time),
                            src_fh,
                            info.file_size,
                        )
                records.append(f"{name},sha256={digest},{size}")
                stats["entries"] += 1

            for wheel_name in sorted(additions):
                runtime_name = additions[wheel_name]
                with runtime.open_stream(runtime_name) as src_fh:
                    digest, size = stream_into(
                        zout, make_info(None, wheel_name, FIXED_DATE_TIME), src_fh, None
                    )
                log(f"[inject]   + 新增 {wheel_name}（上游轮子里没有这个位置）")
                records.append(f"{wheel_name},sha256={digest},{size}")
                stats["added"] += 1
                stats["entries"] += 1
                stats["changed_files"].append(wheel_name)

            record_info = make_info(None, new_prefix + "RECORD", FIXED_DATE_TIME)
            zout.writestr(
                record_info, "".join(r + "\n" for r in records) + f"{record_info.filename},,\n"
            )

    stats["injected_bytes"] = sum(runtime.size(n) for n in replaces.values()) + sum(
        runtime.size(n) for n in additions.values()
    )
    stats["version"] = out_version
    stats["distribution"] = dist
    return stats


def verify(dst: Path, runtime: Runtime, expect_files: dict[str, str]) -> list[str]:
    """打开产物逐条核对：RECORD 与实际内容是否一一对上、注入的文件是不是真的进去了。

    这一步不能省。RECORD 是我们自己重算的，一个便宜的写法错误（少写一行、base64 带了填充
    `=`、路径用了反斜杠）在 pip 那边的表现是"装完了但 uninstall 留下垃圾"或者干脆装不上，
    而它在本机几秒钟就能查出来。挂了本地版本段的轮子还要多核一件事：**文件名里的版本、
    dist-info 目录名、METADATA 里的 Version 三者必须一致**，否则 pip 会直接拒收这个轮子。
    """
    problems: list[str] = []
    with zipfile.ZipFile(dst) as z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        record_name = next((i.filename for i in infos if i.filename.endswith(RECORD_SUFFIX)), None)
        if record_name is None:
            return [f"{dst.name}: 产物里没有 {RECORD_SUFFIX}"]
        record = parse_record(z.read(record_name).decode("utf-8"))

        # 身份一致性：文件名 / dist-info 目录 / METADATA
        dist, version = wheel_name_version(dst.name)
        info_prefix = f"{dist}-{version}.dist-info/"
        if not record_name.startswith(info_prefix):
            problems.append(
                f"{dst.name}: dist-info 目录是 {record_name.split('/')[0]}，"
                f"与文件名里的 {dist}-{version} 对不上"
            )
        metadata = f"{info_prefix}METADATA"
        if metadata in record:
            for line in z.read(metadata).decode("utf-8").splitlines():
                if line.startswith("Version:"):
                    got = line.split(":", 1)[1].strip()
                    if got != version:
                        problems.append(
                            f"{dst.name}: METADATA 里写的是 {got}，文件名里是 {version}"
                        )
                    break

        seen: set[str] = set()
        for info in infos:
            if info.filename == record_name:
                if record.get(info.filename) != ("", ""):
                    problems.append(f"{dst.name}: RECORD 自己那一行不是空的")
                continue
            entry = record.get(info.filename)
            if entry is None:
                problems.append(f"{dst.name}: {info.filename} 不在 RECORD 里")
                continue
            seen.add(info.filename)
            algo, value = entry
            if algo != "sha256" or not value:
                problems.append(
                    f"{dst.name}: {info.filename} 的 RECORD 项不是 sha256：{algo}={value}"
                )
                continue
            data = z.read(info)
            if record_hash(data) != value:
                problems.append(f"{dst.name}: {info.filename} 的 sha256 与 RECORD 不一致")
            elif len(data) != info.file_size:
                problems.append(f"{dst.name}: {info.filename} 的实际大小与 RECORD 不一致")
        for name in record:
            if name != record_name and name not in seen:
                problems.append(f"{dst.name}: RECORD 里的 {name} 在轮子里不存在")

        # 注入必须真的发生了：注入清单里的每个位置，产物的字节要与运行时一致
        for wheel_name, runtime_name in expect_files.items():
            if wheel_name not in seen:
                problems.append(f"{dst.name}: 注入目标 {wheel_name} 不在产物里")
                continue
            entry = record.get(wheel_name, ("", ""))
            with runtime.open_stream(runtime_name) as fh:
                want = hash_stream(fh)
            if entry[1] != want:
                problems.append(
                    f"{dst.name}: {wheel_name} 的字节与运行时里的 {runtime_name} 不一致"
                )
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(
        description="inject the self-built QtWebEngine runtime into official PySide6 wheels"
    )
    ap.add_argument("-Runtime", required=True, help="运行时目录，或 build 阶段产出的 zip")
    ap.add_argument("-Wheels", required=True, help="上游 PySide6 轮子所在目录")
    ap.add_argument("-Destination", required=True, help="产物目录（不存在则创建）")
    ap.add_argument(
        "-PackageRoot",
        default="PySide6",
        help="轮子里的包根目录名（运行时文件按 <PackageRoot>/<相对路径> 认领轮子）",
    )
    ap.add_argument(
        "-ExpectVersion", default="", help="轮子文件名里必须出现的版本（例如 6.8.3）；留空不检查"
    )
    ap.add_argument(
        "-NewFileOwner",
        default="",
        help="运行时里有、但任何上游轮子里都没有对应位置的文件，归到这个发行版（例如 "
        "PySide6_Addons）；留空则直接失败。上游加了新文件时才需要它",
    )
    ap.add_argument(
        "-LocalVersion",
        default="",
        help="给**被改动过的**轮子挂本地版本段（例如 codecs → 6.8.3+codecs）。这样它在任何"
        "解析器眼里都比 PyPI 上的同名同版本轮子大，索引与本地目录同时存在时也会确定地选我们的",
    )
    ap.add_argument(
        "-Manifest", default="", help="把这一步的账写成 JSON（谁被改了、发出去的文件名与摘要）"
    )
    args = ap.parse_args()
    use_utf8_streams()

    runtime_path = Path(args.Runtime)
    if not runtime_path.exists():
        log(f"[inject] 运行时不存在：{runtime_path}")
        return 1
    wheels_dir = Path(args.Wheels)
    wheels = sorted(wheels_dir.glob("*.whl")) if wheels_dir.is_dir() else []
    if not wheels:
        log(f"[inject] {wheels_dir} 里没有轮子")
        return 1
    if args.ExpectVersion:
        wrong = [w.name for w in wheels if f"-{args.ExpectVersion}-" not in w.name]
        if wrong:
            log(f"[inject] 这些轮子的文件名里没有 {args.ExpectVersion}：{wrong}")
            return 1
    destination = Path(args.Destination)
    destination.mkdir(parents=True, exist_ok=True)

    runtime = Runtime(runtime_path)
    log(f"[inject] 运行时：{runtime_path}（{len(runtime.names)} 个文件）")
    try:
        return inject(args, runtime, wheels, destination)
    finally:
        runtime.close()


def inject(args: argparse.Namespace, runtime: Runtime, wheels: list[Path], destination: Path) -> int:
    """认领轮子 → 注入 → 逐个自证。

    运行时句柄由调用方持有并在 finally 里关掉：这一整段都在读它（比较大小时读、打包时读、
    校验时还要读）。
    """
    # 1. 认领：每个运行时文件必须恰好落在一个轮子里
    owners: dict[str, list[Path]] = {name: [] for name in runtime.names}
    for wheel in wheels:
        with zipfile.ZipFile(wheel) as zf:
            names = set(zf.namelist())
        for name in runtime.names:
            if f"{args.PackageRoot}/{name}" in names:
                owners[name].append(wheel)

    unclaimed = [n for n in runtime.names if not owners[n]]
    ambiguous = {n: [w.name for w in o] for n, o in owners.items() if len(o) > 1}
    if unclaimed and args.NewFileOwner:
        # 逃生口：上游加了运行时里有的新文件——归属是数据（-NewFileOwner），不是改代码
        target = next(
            (w for w in wheels if wheel_name_version(w.name)[0] == args.NewFileOwner), None
        )
        if target is None:
            log(
                f"[inject] -NewFileOwner {args.NewFileOwner} 不在这一套轮子里："
                f"{[wheel_name_version(w.name)[0] for w in wheels]}"
            )
            return 1
        for name in unclaimed:
            owners[name].append(target)
            log(f"[inject] {name} 归属到 {args.NewFileOwner}（-NewFileOwner 指定）")
        unclaimed = []
    if unclaimed or ambiguous:
        for name in unclaimed:
            log(
                f"[inject] 运行时里的 {name} 在任何轮子里都没有 {args.PackageRoot}/{name} "
                "这个位置：上游轮子布局变了（换了一个发行版？），或者这是个新文件——"
                "是后者就用 -NewFileOwner <发行版> 指定它归谁"
            )
        for name, ws in ambiguous.items():
            log(f"[inject] 运行时里的 {name} 同时出现在多个轮子里：{ws}")
        return 1

    # 2. 算出每个轮子要换/要加的条目：per_wheel[轮子] = (要换的, 要加的, 认领到几个位置)
    per_wheel: dict[Path, tuple[dict[str, str], dict[str, str], int]] = {}
    for wheel in wheels:
        with zipfile.ZipFile(wheel) as zf:
            names = set(zf.namelist())
            replaces: dict[str, str] = {}
            additions: dict[str, str] = {}
            owned = 0
            for name in runtime.names:
                if owners[name][0] != wheel:
                    continue
                owned += 1
                target = f"{args.PackageRoot}/{name}"
                if target not in names:
                    additions[target] = name
                elif not same_bytes(zf, target, runtime, name):
                    replaces[target] = name
        per_wheel[wheel] = (replaces, additions, owned)

    # 3. 写出并逐个自证：**只有真的变了内容的轮子才算"要发"**
    injected = 0
    manifest: list[dict] = []
    for wheel in wheels:
        replaces, additions, owned = per_wheel[wheel]
        src_sha = sha256_file(wheel)
        dist, upstream_version = wheel_name_version(wheel.name)
        entry: dict = {
            "name": dist,
            "upstream_filename": wheel.name,
            "upstream_version": upstream_version,
            "upstream_sha256": src_sha,
            "modified": bool(replaces or additions),
            "owned": owned,
            "replaced": len(replaces),
            "added": len(additions),
            "changed_files": sorted(set(replaces) | set(additions)),
        }
        if not replaces and not additions:
            # 原样透传：它的 sha256 与 PyPI 上一致，用户可以独立核对；不需要发这一份
            shutil.copy2(wheel, destination / wheel.name)
            entry.update(
                {
                    "published_filename": wheel.name,
                    "published_version": upstream_version,
                    "published_sha256": src_sha,
                }
            )
            manifest.append(entry)
            log(
                f"[inject] {wheel.name}\n"
                f"         认领 {owned} 个位置，全部与上游逐字节相同 → 原样透传（不需要发这一份）\n"
                f"         sha256={src_sha}"
            )
            continue
        published = (
            wheel.name.replace(
                f"-{upstream_version}-", f"-{upstream_version}+{args.LocalVersion}-", 1
            )
            if args.LocalVersion
            else wheel.name
        )
        dst = destination / published
        try:
            stats = repack(wheel, dst, runtime, replaces, additions, args.LocalVersion)
        except RuntimeError as exc:
            log(f"[inject] 打包 {wheel.name} 失败：{exc}")
            return 1
        injected += 1
        checks = verify(dst, runtime, {**replaces, **additions})
        if checks:
            for c in checks:
                log(f"[inject] 校验失败：{c}")
            return 1
        entry.update(
            {
                "published_filename": published,
                "published_version": stats["version"],
                "published_sha256": sha256_file(dst),
            }
        )
        manifest.append(entry)
        log(
            f"[inject] {wheel.name}\n"
            f"         认领 {owned} 个位置：替换 {stats['replaced']} 项 / 新增 {stats['added']} 项"
            f" / 共 {stats['entries']} 项 → 必须发这一份\n"
            f"         上游 sha256={src_sha}\n"
            f"         产物 {published}\n"
            f"         产物 sha256={entry['published_sha256']}"
        )

    if not injected:
        log("[inject] 没有任何轮子发生实质变化：运行时与上游轮子完全一致——这不该发生")
        return 1

    # 4. 透传的那些轮子也要过一遍 RECORD 自洽：它们是上游原件，但"是原件"这件事要证明
    for wheel in wheels:
        if per_wheel[wheel][0] or per_wheel[wheel][1]:
            continue
        checks = verify(destination / wheel.name, runtime, {})
        if checks:
            for c in checks:
                log(f"[inject] 校验失败：{c}")
            return 1

    log(
        f"[inject] 完成：{len(wheels)} 个轮子 → {args.Destination}"
        f"（内容有变化、必须发的 {injected} 个；与上游逐字节相同、可不上架的 {len(wheels) - injected} 个）"
    )
    if args.Manifest:
        payload = {
            "version": args.ExpectVersion or None,
            "local_version": args.LocalVersion,
            "wheels": manifest,
            "must_publish": [
                e["published_filename"] for e in manifest if e["modified"]
            ],
        }
        Path(args.Manifest).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        log(f"[inject] 账本：{args.Manifest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
