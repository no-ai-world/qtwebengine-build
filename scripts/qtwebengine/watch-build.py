#!/usr/bin/env python3
"""构建阶段的看门狗：进度、静默、内存压力、缓存活动。

构建阶段有 300 分钟预算，所以一次卡死——或者一轮什么都留不下的构建——就能把它整个吃掉。
两者都不是假设：有一轮 ninja 计数冻住了很久（日志最后一行是一个早已完成的 ACTION），而在
ccache 接线修好之前，每一轮都是编了几个小时、缓存却从没被调用过。runner 是一次性 VM，事后
也没法查。

本看门狗跑在构建旁边（由 workflow 步骤启动，那个步骤同时收走它的输出文件），每隔几分钟往
同一份标准输出写一行。每一行都回答上次答不上来的问题：最后完成的是哪个 ninja 目标、日志
静默了多久、还剩多少内存、有几个编译器在跑、各占多大、ccache 自认为干了什么，以及——
决定性的那一个——GN 生成的 ninja 规则里到底有没有那个 wrapper（直接从 src/core 读，所以
不依赖任何 CMake 目标名）。

两个副作用，都是为了不把一轮浪费在已经知道答案的事情上：

  * 当编译明显在进行、而 ccache 报告零调用时，写一个哨兵文件（ccache-not-bound.txt）。
    build.cmd 读到它就把这一轮记成 failed 而不是超时，于是死缓存不会永远自己派发自己；
  * 有了 -AbortOnDeadCache，且**仅当**生成的规则确凿地不含那个 wrapper、并且按当前速率
    外推的总时长超过预算时，停掉编译器进程：这样一轮既编不完、也留不下缓存条目，几分钟
    结束它严格优于五小时结束它。接线正确的缓存会在首次编译后几秒内出现在统计里，而
    wrapper 缺失是从生成的规则里读出来的，所以这两个信号不可能同时看错一个可用的缓存。

这是 scripts/qtwebengine/watch-build.ps1 的 Python 版（纯标准库）。行为逐条对齐，包括每一行
日志的字段与格式、心跳的单行摘要、哨兵文件的触发条件与内容、以及中止判定。替换掉的能力都是
实测过的标准库等价物：

  * Get-CimInstance Win32_OperatingSystem  → ctypes GlobalMemoryStatusEx
  * Get-Process -Name cl | Measure -Sum    → ctypes Toolhelp32 + GetProcessMemoryInfo
  * Stop-Process -Force                    → taskkill /F /IM
  * Get-Content -Tail 60                   → 反向分块读（构建日志可以到几百 MB，不整份读）
  * gh api -X PATCH <check run>            → 仍然调 runner 自带的 gh（心跳那条通道）

为了逐字段对齐而刻意做的事（都是 PowerShell 语义，Python 默认不一样）：

  * 大小写：`Select-String -SimpleMatch` 与 `Select-String` 都不区分大小写，所以 wrapper 扫描
    与三个 ccache 统计正则都按不敏感处理；
  * 行分割：只认 \r\n / \n / \r（再用 splitlines() 会在 \v \f \x85 \u2028 等处多断行，
    改变「最后一行 ninja 进度」的判断）；
  * Hidden/System 项跳过（Get-ChildItem 不带 -Force 的默认行为，rglob 不会）；
  * `[math]::Round($x,1)` 与 `[math]::Round($x)` 的取整语义、负零打印成 "-0"；
  * 哨兵文件带 CRLF、非 ASCII 换成 '?'（Set-Content -Encoding ascii）。

用法（与 PS 版一致，参数名不动，方便 workflow 只改启动命令）：
    python -u watch-build.py -LogFile <log> -OutFile <log2> -WorkRoot <root> \
        -BuildDir <build> -BudgetMinutes 300 -AbortOnDeadCache -Heartbeat
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wintypes
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from pathlib import Path

NINJA_PROGRESS_RE = re.compile(r"\[\d+/\d+\]")
NINJA_NUMBERS_RE = re.compile(r"\[(\d+)/(\d+)\]")
# re.IGNORECASE：PS 版的 Select-String 不区分大小写，ccache 把统计项换个大小写也照样认。
CACHEABLE_RE = re.compile(r"Cacheable calls:\s*(\d+)", re.IGNORECASE)
HITS_RE = re.compile(r"^\s*Hits:\s*(\d+)", re.MULTILINE | re.IGNORECASE)
CACHE_SIZE_RE = re.compile(r"Cache size \(GB\):\s*([\d.]+)", re.IGNORECASE)

# 行分隔符：与 .NET 的 StreamReader（也就是 Get-Content）一致，只认 \r\n / \n / 单独的 \r。
# 不能用 str.splitlines()——它还会在 \v \f \x1c-\x1e \x85 \u2028 处断行，一个换页符就能让
# 「最后一行 ninja 进度」从 [3/9] 变成 [4/9]（实测），进而影响 >200 门槛与外推。
LINE_SPLIT_RE = re.compile(r"\r\n|\r|\n")

# Hidden/System：PowerShell 的 Get-ChildItem 不带 -Force 时会跳过这类项，rglob 不会。
FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4

# 需要取工作集的进程。cl 单独一个字段（cl=N(xGB)），其余四个拼进 otherText——
# 这两个列表不是一个东西：PS 版里 $bits 的循环是 ('mspdbsrv','ninja','link','ccache')，
# 不含 cl，合用一个列表会让日志里出现两次 cl=1(xGB)。
WATCHED_PROCESSES = ("cl", "mspdbsrv", "ninja", "link", "ccache")
OTHER_PROCESSES = ("mspdbsrv", "ninja", "link", "ccache")
# 中止时要停掉的编译器/中间进程（顺序与 PS 版一致）
ABORT_PROCESSES = ("ninja", "cl", "ccache", "link", "mspdbsrv")

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def use_utf8_streams() -> None:
    """CI 把输出接进管道，此时 Python 用 locale 编码（cp1252）会对中文抛 UnicodeEncodeError，
    而且默认块缓冲会让看门狗的行攒在缓冲区里——恰恰毁掉它存在的意义。两个都从脚本内部兜住，
    不依赖调用方记得加 -u -X utf8（workflow 里加了）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except (AttributeError, ValueError, OSError):
            pass


def is_hidden(path: Path) -> bool:
    """PowerShell 的 Get-ChildItem 不带 -Force 时会跳过 Hidden/System 项，Python 不会。

    （非 Windows 上没有 st_file_attributes，直接当可见。）
    """
    try:
        attrs = path.stat().st_file_attributes  # type: ignore[attr-defined]
    except (OSError, AttributeError):
        return False
    return bool(attrs & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM))


def fmt_round1(value: float) -> str:
    """PowerShell `[math]::Round($x, 1)` 的字符串形态：整数不带小数点（4 而不是 4.0）。

    .NET 的 Math.Round 是对这个 double 的**最短十进制表示**做「四舍六入五成双」，Python 的
    round() 是对精确二进制值算的。同一个 double 在 0.05 / 0.15 / 0.35 / 4.35 这类值上会差
    一个刻度（实测：.NET 给 0 / 0.2 / 0.4 / 4.4，round() 给 0.1 / 0.1 / 0.3 / 4.3）。
    RAM/工作集那几项是「字节 / 2^30」的二进制小数，中点只可能是 .25/.75，两边一致；
    但 log-idle 是「秒数 / 60」，会命中这些中点，所以必须按 .NET 的算法来。
    另外 .NET 把负零打印成 "-0"（日志 mtime 略在未来时会走到）。
    """
    try:
        quantized = Decimal(repr(float(value))).quantize(Decimal("0.1"), rounding=ROUND_HALF_EVEN)
    except (InvalidOperation, ValueError):
        return str(value)
    if not quantized:
        return "-0" if quantized.is_signed() else "0"
    text = format(quantized, "f")
    return text[:-2] if text.endswith(".0") else text


def read_tail_lines(path: Path, count: int, chunk_size: int = 65536) -> list[str]:
    """读文件最后 count 行。构建日志可以到几百 MB，所以反向分块读，不整份进内存。"""
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            pos = fh.tell()
            data = b""
            while pos > 0 and data.count(b"\n") <= count:
                step = min(chunk_size, pos)
                pos -= step
                fh.seek(pos)
                data = fh.read(step) + data
    except OSError:
        return []
    # cmake/ninja 的输出不一定合法 UTF-8；中文日志按 UTF-8 解，坏字节替换掉。
    # 按 LINE_SPLIT_RE 断行（不是 splitlines），并且把结尾换行产生的那一个空行去掉——
    # Get-Content -Tail 就是这么算行的。
    lines = LINE_SPLIT_RE.split(data.decode("utf-8", errors="replace"))
    if lines and lines[-1] == "":
        lines.pop()
    return lines[-count:]


def first_line_with(path: Path, needle: str) -> str | None:
    """流式找第一个含 needle 的行（不整份读进内存：ninja 规则文件可以很大）。

    大小写不敏感：PS 版的 `Select-String -Pattern $Wrapper -SimpleMatch` 就不区分大小写
    （-SimpleMatch 只关掉正则，不动大小写）。不对齐的话，规则里写 CCACHE.EXE 时看门狗会报
    「wrapper MISSING」，而那正是中止判定的两个输入之一。
    """
    lowered = needle.lower()
    try:
        with path.open("rb") as fh:
            for raw in fh:
                line = raw.decode("utf-8", errors="replace")
                if lowered in line.lower():
                    return line.strip()
    except OSError:
        return None
    return None


# ---------------------------------------------------------------------------
# ctypes：内存与进程工作集（替代 Get-CimInstance / Get-Process Measure-Object）
# ---------------------------------------------------------------------------


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class _PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_char * 260),
    ]


class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def free_physical_gb() -> float:
    """可用物理内存（GB）。等价于 Win32_OperatingSystem.FreePhysicalMemory（那个是 KB）。"""
    status = _MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise OSError("GlobalMemoryStatusEx failed")
    return status.ullAvailPhys / (1024**3)


def process_counts_and_working_sets() -> tuple[dict[str, int], dict[str, int]]:
    """一次 Toolhelp32 快照得到 {进程名: 实例数} 与 {进程名: 工作集字节和}。

    只对 WATCHED_PROCESSES 里的名字调 GetProcessMemoryInfo（OpenProcess 有开销，而
    runner 上有几百个进程）。进程名统一小写并去掉 .exe，对齐 PowerShell 的 -Name 语义。
    """
    kernel32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
    counts: dict[str, int] = {}
    working_sets: dict[str, int] = {}

    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == ctypes.c_void_p(-1).value or not snapshot:
        raise OSError("CreateToolhelp32Snapshot failed")
    try:
        entry = _PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32)
        more = kernel32.Process32First(snapshot, ctypes.byref(entry))
        while more:
            name = entry.szExeFile.decode("ascii", "replace").lower()
            if name.endswith(".exe"):
                name = name[:-4]
            counts[name] = counts.get(name, 0) + 1
            if name in WATCHED_PROCESSES:
                handle = kernel32.OpenProcess(
                    PROCESS_QUERY_LIMITED_INFORMATION, False, entry.th32ProcessID
                )
                if handle:
                    counters = _PROCESS_MEMORY_COUNTERS()
                    counters.cb = ctypes.sizeof(_PROCESS_MEMORY_COUNTERS)
                    if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                        working_sets[name] = working_sets.get(name, 0) + counters.WorkingSetSize
                    kernel32.CloseHandle(handle)
            more = kernel32.Process32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return counts, working_sets


def kill_processes(name: str) -> None:
    """Stop-Process -Force 的等价物：按映像名结束，忽略「本来就没有」的报错。"""
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", f"{name}.exe"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


def main() -> int:
    use_utf8_streams()

    ap = argparse.ArgumentParser(description="watchdog for the QtWebEngine build phase")
    ap.add_argument("-LogFile", required=True, help="构建日志（workflow 步骤 Tee-Object 的落点）")
    ap.add_argument("-OutFile", default="", help="可选第二份拷贝（workflow 收走的产物）")
    ap.add_argument("-CcacheExe", default="", help="要查询的 ccache.exe（这轮不用 ccache 时不传）")
    ap.add_argument("-WorkRoot", default="", help="哨兵文件的落点（build.cmd 的 WORK_ROOT）")
    ap.add_argument("-BuildDir", default="", help="CMake 构建目录（给了才读 src/core 下的 ninja 规则）")
    ap.add_argument("-Wrapper", default="ccache", help="要在生成的规则里找的 wrapper 名")
    ap.add_argument("-BudgetMinutes", type=int, default=0, help="构建步的时间预算（只用于判定死缓存那轮是否还编得完）")
    ap.add_argument("-IntervalSeconds", type=int, default=300, help="每行日志的间隔")
    ap.add_argument("-StallMinutes", type=int, default=20, help="日志静默超过这么多分钟就报卡住")
    ap.add_argument("-BindingGraceMinutes", type=int, default=25, help="缓存允许「零调用」多久，超过就算坏了")
    ap.add_argument("-AbortOnDeadCache", action="store_true", help="证明缓存是死的且这轮编不完时停掉编译器")
    # 把每轮的状态写进本作业的 check run（output.summary）。作业日志在 in_progress 时
    # 拿不到（logs 端点是 404，日志 blob 要等作业结束才生成），而 check run 可以边走边
    # 查（gh api repos/<o>/<r>/check-runs/<id>），所以这是唯一能中途看见进度的通道。
    # 需要 checks: write 权限与 GH_TOKEN。
    ap.add_argument("-Heartbeat", action="store_true", help="把进度写进 check run（需要 checks:write + GH_TOKEN）")
    args = ap.parse_args()

    log_file = Path(args.LogFile)
    out_file = args.OutFile
    build_dir = args.BuildDir
    sentinel = Path(args.WorkRoot) / "ccache-not-bound.txt" if args.WorkRoot else None
    wrapper = args.Wrapper

    started = time.time()
    sentinel_written = False
    last_signature = ""
    last_wrapper_report = ""
    wrapper_missing = False
    wrapper_bound = False
    reported_no_calls = False
    aborted = False

    check_run_url = ""
    heartbeat_warned = False

    def write_watch(text: str) -> None:
        line = f"[watch] {datetime.now().strftime('%H:%M:%S')} {text}"
        print(line)
        if out_file:
            try:
                with open(out_file, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass

    def get_check_run_url() -> str:
        nonlocal check_run_url
        if check_run_url:
            return check_run_url
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        run_id = os.environ.get("GITHUB_RUN_ID", "")
        if not (os.environ.get("GH_TOKEN") and run_id and repo):
            return ""
        try:
            proc = subprocess.run(
                [
                    "gh",
                    "api",
                    f"repos/{repo}/actions/runs/{run_id}/jobs",
                    "--jq",
                    ".jobs[0].check_run_url",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            return ""
        url = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
        if url:
            check_run_url = url
        return check_run_url

    def send_heartbeat(summary: str) -> None:
        nonlocal heartbeat_warned
        if not args.Heartbeat:
            return
        url = get_check_run_url()
        if not url:
            if not heartbeat_warned:
                heartbeat_warned = True
                print(
                    "[watch] heartbeat unavailable (no GH_TOKEN / GITHUB_RUN_ID, or gh could not read the check run)"
                )
            return
        try:
            subprocess.run(
                [
                    "gh",
                    "api",
                    "-X",
                    "PATCH",
                    url,
                    "-f",
                    "output[title]=ccache / progress heartbeat",
                    "-f",
                    f"output[summary]={summary}",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            pass

    write_watch(
        f"start pid={os.getpid()} log={args.LogFile} every {args.IntervalSeconds}s "
        f"stall>={args.StallMinutes}min budget={args.BudgetMinutes}min "
        f"abort={args.AbortOnDeadCache}"
    )

    while True:
        try:
            progress = "n/a"
            progress_num = -1
            total_edges = -1
            idle_min = -1.0
            last_line = ""

            if log_file.is_file():
                try:
                    idle_min = round((time.time() - log_file.stat().st_mtime) / 60.0, 1)
                except OSError:
                    pass
                try:
                    tail = read_tail_lines(log_file, 60)
                    ninja_lines = [line for line in tail if NINJA_PROGRESS_RE.search(line)]
                    if ninja_lines:
                        last_line = ninja_lines[-1].strip()
                        match = NINJA_NUMBERS_RE.search(last_line)
                        if match:
                            progress_num = int(match.group(1))
                            total_edges = int(match.group(2))
                            progress = f"{match.group(1)}/{match.group(2)}"
                except OSError:
                    pass

            # 下面三段各自 try/catch（PS 版就是 $ErrorActionPreference='Continue' 下的
            # try{}catch{}，捕的是全部异常）。宽catch 在这里是刻意的：某一个指标取不到时，
            # 结果必须是「那一项留默认值」，而不是整行日志丢掉——看门狗每次迭代就一行输出，
            # 丢一行就等于这几分钟什么都没记。
            free_ram_text = "?"
            try:
                free_ram_text = fmt_round1(free_physical_gb())
            except Exception:  # noqa: BLE001
                pass

            cl_text = "cl=?"
            other_text = ""
            try:
                counts, working_sets = process_counts_and_working_sets()
                cl_count = counts.get("cl", 0)
                if cl_count > 0:
                    cl_gb = working_sets.get("cl", 0) / (1024**3)
                    cl_text = f"cl={cl_count}({fmt_round1(cl_gb)}GB)"
                else:
                    cl_text = "cl=0"
                bits = []
                for name in OTHER_PROCESSES:
                    count = counts.get(name, 0)
                    if count > 0:
                        gb = working_sets.get(name, 0) / (1024**3)
                        bits.append(f"{name}={count}({fmt_round1(gb)}GB)")
                other_text = " ".join(bits)
            except Exception:  # noqa: BLE001 - 见上：只丢这一项，不丢整行
                pass

            ccache_text = ""
            cacheable: int | None = None
            if args.CcacheExe and Path(args.CcacheExe).is_file():
                try:
                    proc = subprocess.run(
                        [args.CcacheExe, "--show-stats"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                    )
                    stats = (proc.stdout or b"").decode("utf-8", errors="replace")
                    match = CACHEABLE_RE.search(stats)
                    if match:
                        cacheable = int(match.group(1))
                    hits_match = HITS_RE.search(stats)
                    hits = hits_match.group(1) if hits_match else "?"
                    size_match = CACHE_SIZE_RE.search(stats)
                    size = size_match.group(1) if size_match else "?"
                    # PS 里 $cacheable 为 $null 时插值成空串，这里对齐
                    cacheable_text = "" if cacheable is None else str(cacheable)
                    ccache_text = f"ccache(cacheable={cacheable_text} hits={hits} size={size}GB)"
                except Exception:  # noqa: BLE001 - 见上：只丢这一项，不丢整行
                    ccache_text = "ccache(?)"

            # wrapper 是否出现在 GN 生成的 ninja 规则里：这是「ccache 会不会被调用」的决定性证据
            wrapper_report = ""
            if build_dir:
                core = Path(build_dir) / "src" / "core"
                if core.is_dir():
                    ninja_files = sorted(
                        (p for p in core.rglob("*.ninja") if p.is_file() and not is_hidden(p)),
                        key=lambda p: str(p),
                    )
                    if ninja_files:
                        try:
                            newest = max(p.stat().st_mtime for p in ninja_files)
                        except (OSError, ValueError):
                            # 每个文件都 stat 不到时 max() 会抛 ValueError（空序列）；
                            # 当作「很旧」处理，让下面真的去扫一遍规则，而不是丢掉整行
                            newest = 0.0
                        if time.time() - newest > 60:
                            hit = False
                            for path in ninja_files:
                                if first_line_with(path, wrapper) is not None:
                                    hit = True
                                    break
                            wrapper_missing = not hit
                            wrapper_bound = hit
                            wrapper_report = (
                                f"wrapper={wrapper} BOUND"
                                if hit
                                else f"wrapper={wrapper} MISSING in {len(ninja_files)} ninja file(s)"
                            )
                        else:
                            wrapper_report = "wrapper check waits for gn to finish writing"

            signature = f"{progress}|{last_line}"
            changed = "new" if signature != last_signature else "unchanged"
            last_signature = signature
            write_watch(
                " ".join(
                    [
                        f"progress={progress}",
                        f"log-idle={fmt_round1(idle_min)}min",
                        f"free-ram={free_ram_text}GB",
                        cl_text,
                        other_text,
                        ccache_text,
                        f"[{changed}]",
                    ]
                )
            )
            if last_line:
                write_watch(f"  last: {last_line}")
            # 单行摘要：多行参数在 cmd 系包装器上会被拆开（实测 .cmd 垫片收不全），
            # gh.exe 本身能吃多行，但一行更省事，UI 里也更好看
            send_heartbeat(
                f"progress={progress} | log-idle={fmt_round1(idle_min)}min | "
                f"free-ram={free_ram_text}GB | {cl_text} | {ccache_text} | {wrapper_report} | "
                f"{last_line[:90]}"
            )
            if wrapper_report and wrapper_report != last_wrapper_report:
                write_watch(f"  {wrapper_report}")
                last_wrapper_report = wrapper_report
                # 也发一条 job annotation：步骤日志在作业结束前拿不到，而 annotation 在
                # check run 上边跑边能查（gh api .../check-runs/<id>/annotations），
                # 这样「缓存到底有没有接上」不必等五个小时。
                print(
                    f"::notice title=ccache binding::{wrapper_report}; {ccache_text}; progress={progress}"
                )

            if idle_min >= args.StallMinutes and progress_num > 0:
                write_watch(
                    f"STALLED: no ninja line for {fmt_round1(idle_min)}min at {progress} - "
                    "if this keeps up the round is lost; check the free RAM and cl/mspdbsrv above"
                )

            elapsed_min = (time.time() - started) / 60.0

            # 「编译在进行、ccache 却一次没被调用」= 这一轮留不下任何缓存
            no_cache_calls = (
                cacheable == 0
                and progress_num > 200
                and elapsed_min > args.BindingGraceMinutes
            )
            if no_cache_calls:
                if wrapper_missing:
                    why = f"the generated rules do not mention {wrapper}"
                elif wrapper_bound:
                    why = (
                        f"the rules do mention {wrapper}, so it is invoked but counts "
                        "nothing cacheable"
                    )
                else:
                    why = "binding unknown"
                if not reported_no_calls:
                    write_watch(
                        f"ERROR: ccache reported no calls after {round(elapsed_min)}min and "
                        f"{progress_num} targets - {why}"
                    )
                reported_no_calls = True
                if sentinel is not None and not sentinel_written:
                    try:
                        # PS 是 Set-Content -Encoding ascii：会补一个 CRLF 行尾，非 ASCII 一律换成
                        # '?'。照做——写 bytes 而不是 write_text，后者不加行尾，而且 ASCII 编码遇到
                        # 非 ASCII 会直接抛异常（wrapper 名里万一有中文，哨兵就干脆写不出来了，
                        # 而 build.cmd 只按「文件在不在」判红）。
                        sentinel.write_bytes(
                            f"ccache was never called (progress={progress}, {wrapper_report})\r\n".encode(
                                "ascii", "replace"
                            )
                        )
                        sentinel_written = True
                        write_watch(
                            f"wrote sentinel {sentinel} (build.cmd turns this round into 'failed')"
                        )
                        print(
                            f"::warning title=ccache never called::{why}; progress={progress}; "
                            f"free-ram={free_ram_text}GB"
                        )
                    except OSError:
                        pass

            # 只有「缓存肯定不会有」且「这一轮按当前速率也编不完」时才停手：否则继续跑还有意义
            if (
                no_cache_calls
                and wrapper_missing
                and args.AbortOnDeadCache
                and not aborted
            ):
                if args.BudgetMinutes > 0 and progress_num > 0 and total_edges > 0:
                    projected = elapsed_min * (total_edges / progress_num)
                    if projected > args.BudgetMinutes:
                        aborted = True
                        write_watch(
                            f"ABORT: dead cache (rules have no {wrapper}) and projected total "
                            f"{projected:,.0f}min > budget {args.BudgetMinutes}min - stopping the "
                            "compilers, build.cmd will record this round as failed"
                        )
                        for name in ABORT_PROCESSES:
                            kill_processes(name)
                    else:
                        write_watch(
                            f"dead cache, but projected total {projected:,.0f}min fits the "
                            f"{args.BudgetMinutes}min budget - letting it run"
                        )
                else:
                    write_watch("dead cache, but no budget/progress to project from - not aborting")
        except Exception as exc:  # noqa: BLE001 - 看门狗绝不能因为一次迭代出错就退出
            write_watch(f"iteration failed (ignored): {exc}")

        try:
            time.sleep(args.IntervalSeconds)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
