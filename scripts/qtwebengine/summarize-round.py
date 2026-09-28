#!/usr/bin/env python3
"""读一轮 CI 日志，把「这一轮到底发生了什么」压成一页。

用法（在仓库根目录）：
    python scripts/qtwebengine/summarize-round.py --run 36460244981
    python scripts/qtwebengine/summarize-round.py .temp/r3.log --budget 320

为什么要它：这一轮几小时，而作业日志在 in_progress 时拿不到（logs 端点 404，日志 blob 要等
作业结束）。事后要回答的问题总是同一批——编到第几个目标、最长静默多久、内存够不够、ccache
到底有没有在跑、缓存够不够装——所以把它们固化成脚本，而不是每次手敲 grep。

输出分八段：结局 / ccache 接线与看门狗 / ccache 统计 / ninja 进度与静默 / 缓存效率与收敛估计 /
编译错误 / 各步骤耗时。`--run` 会自动用 gh 下载日志（需要 gh 已登录）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import subprocess
import sys
from pathlib import Path

TS = re.compile(r"\t(\d{4}-\d{2}-\d{2}T[\d:.]+)Z ")
NINJA = re.compile(r"\[(\d+)/(\d+)\] (.*)$")
STEP = re.compile(r"^QtWebEngine.*?\t([^\t]+)\t")


def load(path: Path) -> list[tuple[dt.datetime, str, str]]:
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").split("\n"):
        m = TS.search(line)
        if not m:
            continue
        ts = dt.datetime.fromisoformat(m.group(1))
        step = STEP.match(line)
        rows.append((ts, step.group(1) if step else "?", line[m.end():]))
    return rows


def download(run: str, dest: Path) -> Path:
    jobs = subprocess.run(
        ["gh", "api", f"repos/no-ai-world/qtwebengine-build/actions/runs/{run}/jobs", "--jq", ".jobs[0].id"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    with dest.open("w", encoding="utf-8") as fh:
        subprocess.run(
            ["gh", "run", "view", "--repo", "no-ai-world/qtwebengine-build", f"--job={jobs}", "--log"],
            stdout=fh, stderr=subprocess.DEVNULL, check=False,
        )
    return dest


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("log", nargs="?", help="作业日志文件")
    ap.add_argument("--run", help="运行 id（会先下载日志）")
    ap.add_argument("--core-total", type=int, default=29705, help="核心 ninja 树的目标总数")
    ap.add_argument("--budget", type=int, default=300, help="构建阶段预算（分钟）")
    args = ap.parse_args()

    if args.run:
        path = download(args.run, Path(f".temp/run-{args.run}.log"))
    elif args.log:
        path = Path(args.log)
    else:
        ap.error("需要日志文件或 --run")
    rows = load(path)
    log_text = path.read_text(encoding="utf-8", errors="replace")
    if not rows:
        print(f"{path}: 没有解析到带时间戳的行")
        return 1

    print(f"# {path}  {rows[0][0]:%H:%M:%S} → {rows[-1][0]:%H:%M:%S} UTC  （{(rows[-1][0]-rows[0][0]).total_seconds()/60:.0f} 分钟）")

    print("\n## 结局")
    for ts, _, text in rows:
        if re.match(r"\s*(BUILD_RESULT|BUILD_STATE)=", text) or "本轮结局" in text or "state 文件内容" in text:
            print(f"  {ts:%H:%M:%S} {text.strip()[:150]}")
    for pat in ("build failed", "interrupted by the step time budget", "state file says", "failed ccache-not-bound"):
        hit = [r for r in rows if pat in r[2]]
        if hit:
            print(f"  ({pat}) {hit[-1][0]:%H:%M:%S} {hit[-1][2].strip()[:140]}")

    print("\n## ccache 接线 / 看门狗")
    seen = set()
    for ts, _, text in rows:
        t = text.strip()
        if t.startswith("[ccache-bound]") or t.startswith("[watch]"):
            key = re.sub(r"^\S+ ", "", t)[:60]
            if key in seen and not re.search(r"ERROR|ABORT|STALLED|BOUND|MISSING|start pid", t):
                continue
            seen.add(key)
            print(f"  {ts:%H:%M:%S} {t[:170]}")
    for ts, _, text in rows:
        if text.startswith("::notice") or text.startswith("::warning") or text.startswith("::error"):
            print(f"  ANNOTATION {ts:%H:%M:%S} {text.strip()[:170]}")

    print("\n## ccache 统计（作业末尾那一步）")
    stats = [r for r in rows if re.search(r"Cacheable calls|Cacheable calls|Hits:|Cache size|ccache: ", r[2])]
    if not stats:
        print("  （没跑到那一步，或被 use_ccache=false 跳过）")
    for ts, _, text in stats[-14:]:
        print(f"  {ts:%H:%M:%S} {text.strip()[:140]}")

    core = []
    for ts, _, text in rows:
        if text.startswith("[") or text.startswith("ninja: Entering"):
            m = NINJA.match(text.strip())
            if m and int(m.group(2)) == args.core_total:
                core.append((ts, int(m.group(1)), m.group(3)))
    print(f"\n## 核心 ninja 树（/{args.core_total}）")
    if not core:
        print("  没有进度行")
    else:
        gaps = sorted(((core[i][0] - core[i - 1][0]).total_seconds(), core[i - 1][2]) for i in range(1, len(core)))
        print(f"  完成行 {len(core)}  首 {core[0][0]:%H:%M:%S} [{core[0][1]}]  末 {core[-1][0]:%H:%M:%S} [{core[-1][1]}]"
              f"  (剩 {args.core_total - core[-1][1]})")
        if gaps:
            d, a = gaps[-1]
            print(f"  最长静默 {d/60:.1f} 分钟（在 {a[:60]} 之后）")
        marks = [n for n in (1000, 5000, 8038, 12000, 17012, 20000, 24000, 27000) if n <= core[-1][1]]
        prev_t, prev_n = core[0][0], core[0][1]
        for mark in marks + [core[-1][1]]:
            nxt = next((r for r in core if r[1] >= mark), None)
            if not nxt or nxt[1] == prev_n:
                continue
            mins = (nxt[0] - prev_t).total_seconds() / 60
            if mins > 0:
                print(f"  [{prev_n}→{nxt[1]}] {mins:6.1f} 分钟 = {(nxt[1]-prev_n)/mins:6.1f} 目标/分钟")
            prev_t, prev_n = nxt[0], nxt[1]
        elapsed = (core[-1][0] - core[0][0]).total_seconds() / 60
        rate = core[-1][1] / max(elapsed, 1e-9)
        if rate > 0:
            projected = args.core_total / rate
            print(f"  全程 {elapsed:.0f} 分钟、{rate:.1f} 目标/分钟 → 按均速投影总时长 {projected:.0f} 分钟"
                  f"（预算 {args.budget}）；末段速率见上表，尾巴才是决定性的")
            tail = [g for g in gaps if True]
            last_hour = [r for r in core if (core[-1][0] - r[0]).total_seconds() <= 3600]
            if last_hour:
                tail_rate = len(last_hour) / 60.0
                remain = args.core_total - core[-1][1]
                print(f"  最后 60 分钟完成 {len(last_hour)} 个目标（{tail_rate:.1f}/分钟）"
                      f" → 剩 {remain} 个按此速率还要 {remain / max(tail_rate, 1e-9):.0f} 分钟")

    print("\n## 缓存效率 / 收敛估计")
    m = re.search(r"Cacheable calls:\s*(\d+)\s*/\s*(\d+)\s*\(([\d.]+)%\)", log_text)
    mh = re.search(r"Hits:\s*(\d+)\s*/\s*(\d+)", log_text)
    ms = re.search(r"Cache size \(GB\):\s*([\d.]+)\s*/\s*([\d.]+)", log_text)
    if m:
        calls, pct = int(m.group(1)), float(m.group(3))
        print(f"  本轮 ccache 调用 {calls} 次（可缓存率 {pct:.1f}%）—— 这些编译的产物进了缓存，下一轮可以直接命中")
    else:
        print("  没找到 Cacheable calls（USE_CCACHE=0 或没跑到那一步）")
    if mh:
        hits, htotal = int(mh.group(1)), int(mh.group(2))
        print(f"  命中 {hits}/{htotal}（{100.0 * hits / max(htotal, 1):.1f}%）—— 第一次带缓存跑应为 0，之后逐轮上升")
    if ms:
        size, cap = float(ms.group(1)), float(ms.group(2))
        note = "（贴着上限：对象比配额多，ccache 会淘汰最旧的）" if size > cap * 0.8 else "（离上限还有余量：整棵树大概率装得下）"
        print(f"  缓存体积 {size} GB / 上限 {cap} GB{note}")
    watch = [r[2] for r in rows if r[2].strip().startswith("[watch]") and "free-ram" in r[2]]
    if watch:
        blob = " ".join(watch)
        rams = [float(x) for x in re.findall(r"free-ram=([\d.]+)GB", blob)]
        cls = re.findall(r"cl=(\d+)\(([\d.]+)GB\)", blob)
        series = re.findall(r"cacheable=(\d+)", blob)
        if rams:
            print(f"  [watch] 可用内存：最小 {min(rams)} GB / 最大 {max(rams)} GB（{len(rams)} 次采样）")
        if cls:
            print(f"  [watch] 并发 cl：最多 {max(int(c) for c, _ in cls)} 个，合计最大 {max(float(g) for _, g in cls)} GB")
        if series:
            print(f"  [watch] cacheable 轨迹：{series[0]} → {series[-1]}（{len(series)} 次采样）")

    print("\n## 编译错误")
    bad = [r for r in rows if re.search(r"FAILED: \[code=|error C\d{4}|ninja: build stopped|error LNK", r[2])]
    if not bad:
        print("  没有")
    for ts, _, text in bad[:8]:
        print(f"  {ts:%H:%M:%S} {text.strip()[:160]}")
    if len(bad) > 8:
        print(f"  ...以及另外 {len(bad)-8} 行")

    print("\n## 各步骤耗时")
    for ts, step, text in rows:
        if "##[group]Run " in text and "构建阶段" in step:
            pass
    print("  （用 gh api .../actions/jobs/<id> 取 started_at/completed_at 更准；这里只看日志起止）")
    per: dict[str, list[dt.datetime]] = {}
    for ts, step, _ in rows:
        per.setdefault(step, [ts, ts])
        per[step][1] = ts
    for step, (a, b) in per.items():
        print(f"  {step[:40]:42s} {(b-a).total_seconds()/60:7.1f} 分钟")
    return 0


if __name__ == "__main__":
    sys.exit(main())
