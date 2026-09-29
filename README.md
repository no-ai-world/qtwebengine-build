# qtwebengine-build

自建**带私有编解码器**的 QtWebEngine（Windows x64 / MSVC 2022），产物可直接铺进 PySide6。

## 为什么单独一个仓库

- 它是一次几小时、几十 GB 的重型构建，与日常代码仓库的节奏完全不同；
- 它需要 `contents: write`（发 Release）、可选的 PAT（缓存仓库、自动续跑）——这些权限不该挂在代码仓库上；
- 缓存与产物体量大（ccache 数 GB、zip 上百 MB），独立仓库不挤占其他仓库的 Releases 与缓存配额。

## 为什么必须自建

PySide6/Qt 的开源二进制里 QtWebEngine **不含私有编解码器**。实测 PySide6 6.6.2：

```
MediaSource.isTypeSupported('video/mp4; codecs="avc1.42E01E"') === false
document.createElement('video').canPlayType('video/mp4; codecs="avc1.42E01E"') === ""
```

页面里的 `<video>` 因此放不了 MP4/H.264/AAC。只有用 `-webengine-proprietary-codecs` 从源码构建才能改掉。
构建步骤逐条对照参考手册：<https://github.com/open-prison-education/ope-lms/blob/main/WebEngineMP4Build_6.8.3.md>

## 推成仓库

```bash
cd qtwebengine-build
git init -b main
git add -A
git commit -m "feat: QtWebEngine 私有编解码器构建流水线"
gh repo create <owner>/qtwebengine-build --private --source . --push
```

然后在 Actions → **build-qtwebengine** → Run workflow。默认参数是 Qt 6.8.3 + RelWithDebInfo、只编一个配置、
`symbol_level=0`，ccache 由 `use_ccache` 控制（默认关；要分轮续跑就打开，见下）。

## 运行环境（门槛在这儿，不在时间）

| 项 | 要求 | 说明 |
| --- | --- | --- |
| 磁盘 | **≥ 90 GB 可用** | Chromium 源码+子模块 ≈40 GB、构建中间产物 ≈20–40 GB、Qt 与下载的 Chromium 工具链 ≈10 GB；ccache 另算 |
| CPU / 内存 | 越宽裕越好 | 标准 `windows-2022` runner（4 核 / 16 GB，D: 实测 147 GB 可用）能过预检，但一轮要 4–5 小时，内存紧张时还会换页、出现长时间假死；大规格 runner 或自托管机器舒服得多 |
| 工具链 | VS 2022 17.14（MSVC 14.44） | cppgc 补丁针对这个编译器版本 |

预检会先量盘，不达标直接失败并打印三条出路：大规格 runner（larger runners）／自托管 Windows 机器／
本地直接跑 `scripts/qtwebengine/build.cmd`。

## 一轮编不完怎么办：ccache 现在真的接上了（第一轮的实测会给出结论）

照 kiwi browser 的 CI（<https://github.com/AoEiuV020/kiwibrowser-build>）做的 ccache + 分轮续跑，
一度被判成「在本方案里走不通」。那个结论对了一半：**ccache 之前确实一次都没被调用过**，但原因不是
ccache 不行，而是少接了一根线：

1. Chromium 只在 `toolchain_is_clang` 为真时才把 `cc_wrapper` 拼到编译器前面
   （`chromium/build/toolchain/win/toolchain.gni`）。Qt 的 MSVC 版 QtWebEngine 是
   `is_clang=false`（构建日志里的 args.gn：`is_clang=false`、`is_msvc=true`），于是
   `patch-gn-args.ps1` 注进去的 `cc_wrapper="ccache"` 被整体丢掉。实测：编到 `[8038/29705]`，
   ccache 目录 391 字节，`ccache --show-stats` 里连 `Cacheable calls` 都没有。
   kiwi 之所以行，是因为它编 Android/clang，而且干脆用 `CC=ccache clang` 直接包住编译器；
   ccache 自己是支持 MSVC 的（官方支持表把 MSVC 列为 A 级）。补上这处条件的是
   `patch-msvc-ccache.ps1`。
2. 光有补丁还不够：16 GB 的机器上并行度失控会换页，表现就是「几十分钟一行日志都没有」。
   因此固定了三件事：`symbol_level=0`（Qt 在 RelWithDebInfo+MSVC 下写 2，每个 obj 都走
   `mspdbsrv` 写 PDB：更慢、更吃内存，还多一条卡死的路）、`NINJAFLAGS=-j<PARALLEL>`
   （不设的话 ninja 默认 cores+2，4 核上就是 6 个并发 MSVC 编译），以及真正可用的 jumbo 开关。

`patch-gn-args.ps1` 只改 QtWebEngine 自己的 `src/core/CMakeLists.txt`，所以 **pdfium 那棵树
（`src/pdf`）不在缓存范围内**；它的目标数不多，先这样。

三个决定速度与内存的开关（都是 workflow 输入）：

| 输入 | 默认 | 说明 |
| --- | --- | --- |
| `jumbo` | 空 = Qt 默认（开，每个 TU 合并 8 个源） | `0/off/false` = `-no-webengine-jumbo-build`（每源文件一个 TU，最省内存、总工作量最大）；数字 N = 每个 TU 合并 N 个源（N 越小峰值内存越小） |
| `symbol_level` | `0` | `2` = 取回 Qt 的默认值（PDB + 调试信息，慢且大） |
| `parallel` | 按核数与内存自动算 | 同时决定外层 MSBuild 的 `--parallel` 和内层 ninja 的 `-j` |

**接线是否生效，prepare 阶段就会告诉你**：`check-ccache-bound.ps1` 先跑 GN 生成（约四分钟，
构建阶段因此省掉这一步），再读 GN 写出的 ninja 规则——里面必须出现 `ccache`。没出现就当场失败
（prepare 退出码 4），不会等到五小时之后才发现缓存是空的。

构建阶段还有 `watch-build.ps1` 看门狗：每 5 分钟往日志写一行「最后完成的目标、日志静默多久、
可用内存、cl/mspdbsrv 的进程数与占用、ccache 计数」，并直接读 GN 生成的 ninja 规则判断 wrapper
有没有进编译器命令行（`wrapper=ccache BOUND/MISSING`），另存 `watch-roundN.log` 进产物。
它平时只观察；只有在两个独立信号一致——规则里没有 wrapper **且** ccache 计数为 0——时才动手：
落一个 `ccache-not-bound.txt` 让 `build.cmd` 把本轮记成 `failed`（而不是超时后无限续跑），
并停掉编译器进程，把一轮五小时缩成几分钟。

它还会把每轮的状态写进本作业的 check run（`output.summary`，即"心跳"）：作业日志在
`in_progress` 时拿不到（`logs` 端点是 404，日志 blob 要等作业结束才生成），check run 却能边走边查：

```bash
gh api repos/<owner>/<repo>/check-runs/<check_run_id> --jq .output.summary
```

于是"这一轮进度多少、内存够不够、缓存有没有在跑"不必等五小时。

时间预算照旧：`build_budget_minutes` 默认 300 分钟。够不够一轮编完要看实测；不够就靠 ccache 分轮，
`auto_continue` 这才重新有意义（仍需要 `CACHE_TOKEN`）。

三种结局由 `STATE_FILE` 区分，别改名：

| state 文件 | 含义 | 流水线动作 |
| --- | --- | --- |
| `ok` | 编完了 | 继续 finish / 打包 / 编解码验收 /（可选）发 Release |
| `failed <code>` | 编译报错、根本没进到编译，或 ccache 没绑定（`failed ccache-not-bound`） | 直接失败，**不排下一轮** |
| 不存在 | 被时间预算打断（含被步超时杀掉的 `0xC000013A`） | 排下一轮；`USE_CCACHE=1` 时下一轮带着 ccache 继续，`=0` 时等于从头再来 |

### 缓存存哪儿：两个后端

`CCACHE_DIR` 由下面两个后端存取。接上 `patch-msvc-ccache.ps1` 之后它才真的会被写入；
第一轮编完先看一眼 `ccache --show-stats` 的 `Cacheable calls` 是不是非零。

| 后端 | 上限 | 需要密钥 | 适用 |
| --- | --- | --- | --- |
| `actions-cache`（默认） | 仓库 10 GB 配额，故 ccache 上限设 9G | 无 | 想零配置先跑起来 |
| `git-repo` | 20G（kiwi 的取值） | `CACHE_TOKEN`（PAT，可写缓存仓库） | 需要更大缓存、更高命中率 |

`git-repo` 要另填 `cache_repo`（`owner/name`）。缓存仓库只是个空仓库，脚本每轮把 ccache 目录提交推送进去。

### 自动续跑

`auto_continue=true` 会在本轮被打断后自动派下一轮，但**必须提供 `CACHE_TOKEN`**：
`GITHUB_TOKEN` 无法触发新的 workflow 运行，这是平台限制，不是配置问题。

## 产物与消费

`ok` 后产出：

- `qtwebengine-<版本>-win64-msvc2022-codecs.zip`（`Qt6WebEngineCore.dll`、`QtWebEngineProcess.exe`、
  `Qt6WebEngineWidgets/Quick*.dll`、`resources/`、`translations/qtwebengine_locales/`）+ `.sha256`

**编解码验收不在流水线里**（构建端不再跑 `verify-codecs.py`，产物里也没有 `codec-probe.json`）。
需要确认时在铺入之后自己跑一遍：它会离屏起 WebEngine 问 H.264/AAC，并用 vp8/opus 做对照，
防止「整条媒体管线被关掉」被误判成成功：

```powershell
<venv>\Scripts\python.exe scripts\qtwebengine\verify-codecs.py --expect-qt-version <构建的 Qt 版本>
```

铺到开发机/打包机（会把旧文件备份到 `_webengine-backup/`，并核对 DLL 版本一致）：

```powershell
scripts/qtwebengine/install-webengine-runtime.ps1 -Source <解压后的目录> -Destination <site-packages>\PySide6
```

**版本必须对齐**：产物是给 `PySide6==<构建的 Qt 版本>` 用的，大版本不一致会被脚本拒绝（运行期崩溃比加载期报错难查得多）。

## 本地直接跑

CI 只是编排，所有步骤都在这份脚本里，本地同样能跑：

```cmd
set "QT_PATH=C:\Qt\6.8.3\msvc2022_64"        & rem 需要装了 msvc2022_64 全量包（含私有头文件）
set "WORK_ROOT=D:\qtwebengine-work"           & rem 指向空间最大的盘
set "USE_CCACHE=1"
scripts\qtwebengine\build.cmd all
```

`build.cmd` 头部注释列了全部环境变量与退出码；`prepare` / `build` / `finish` 也可以分开跑。

改完先自检，再决定要不要派线上跑 —— 它只做静态检查，不编译、不联网：

```powershell
python scripts/check-pipeline.py
```

一轮跑完之后（或任何一轮的历史日志）这样读：

```powershell
python scripts/qtwebengine/summarize-round.py --run <run_id>      # 自动下载并汇总
python scripts/qtwebengine/summarize-round.py .temp/r3.log         # 或直接读已有日志
```

它回答的正是"还能不能靠分轮续跑收口"的几个问题：编到第几个目标、最长静默多久、重活阶段还剩
多少内存、ccache 被调用了多少次/命中多少/缓存多大、以及按当前速率投影还要多久。

### 不需要守着：本地自动续跑器

一轮编不完、每轮几小时，所以"接着跑下一轮"这件事可以交给脚本（Git Bash，`gh` 已登录即可）：

```bash
scripts/qtwebengine/autochain.sh 6 8      # 从第 6 轮开始，最多自动派 8 轮
```

它的规则：CI 队列空了之后——归档出 zip 就停（目标达成）；该轮结论是 `failure` 也停（确定性失败，
等人工看日志）；否则（被时间预算打断）派下一轮，带 ccache、预算 320 分钟，并按上一轮看门狗的
内存采样自动定并行度（空闲 ≥ 5 GB → `parallel=6`，< 1.5 GB → `parallel=3`）。日志在 `.temp/autochain.log`。

它盯的是那些「几分钟就能发现、却要烧掉一台 runner 和半小时到几小时才能暴露」的问题：
批处理的 CRLF/ASCII、`goto`/`call` 标签是否存在、块括号是否配平、`cmake --build`/`--install`
是否带了 `--config`、确定性失败有没有落盘成 `failed`（漏了就会被误判成超时并无限续跑）、
workflow 里引用的 step id 是否存在、自动续跑是否漏传输入。

## 文件

```
.github/workflows/build-qtwebengine.yml   流水线（阶段编排、缓存、验收、Release）
scripts/qtwebengine/
  build.cmd                               构建驱动：环境→源码→补丁→configure→编译→安装→打包
  patch-cppgc.ps1                         Chromium v8/cppgc 补丁（MSVC 14.44 的 C2352）
  patch-gn-args.ps1                       注入 gn 参数（symbol_level，可选 cc_wrapper）
  patch-single-config.ps1                 把 CMAKE_CONFIGURATION_TYPES 收成一个配置（否则编两遍）
  patch-msvc-ccache.ps1                   让 cc_wrapper 对 MSVC 工具链也生效（ccache 真被调用的前提）
  check-ccache-bound.ps1                  prepare 阶段就证明缓存已接线（跑 GN 生成 + 读 ninja 规则）
  watch-build.ps1                         构建阶段看门狗（进度/静默/内存/ccache 接线 + check run 心跳）
  summarize-round.py                      读一轮作业日志：进度/静默/内存/缓存效率/结局，压成一页
  autochain.sh                            本地自动续跑器：队空就按规则派下一轮（有 zip 或红轮才停）
  install-webengine-runtime.ps1           把产物铺进 PySide6（版本核对、备份）
  verify-codecs.py                        编解码验收探针（退出码即结论）
  check-pipeline.py                       流水线静态自检（派 CI 之前先跑，不编译不联网）
```
