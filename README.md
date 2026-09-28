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

然后在 Actions → **build-qtwebengine** → Run workflow。默认参数就是 Qt 6.8.3 + RelWithDebInfo、只编一个配置、ccache 关闭（原因见下）。

## 运行环境（门槛在这儿，不在时间）

| 项 | 要求 | 说明 |
| --- | --- | --- |
| 磁盘 | **≥ 90 GB 可用** | Chromium 源码+子模块 ≈40 GB、构建中间产物 ≈20–40 GB、Qt 与下载的 Chromium 工具链 ≈10 GB；ccache 另算 |
| CPU / 内存 | ≥ 8 核，建议 ≥ 32 GB | 16 GB 也能跑，靠页面文件兜底（流水线会自动扩） |
| 工具链 | VS 2022 17.14（MSVC 14.44） | cppgc 补丁针对这个编译器版本 |

标准 GitHub 托管 runner（文档标称 14 GB SSD）装不下，别拿它试。预检会先量盘，不达标直接失败并打印三条出路：
大规格 runner（larger runners，300 GB+ SSD）／自托管 Windows 机器／本地直接跑 `scripts/qtwebengine/build.cmd`。

## 一轮编不完怎么办：现状是「一轮编完」，不是分轮续跑

本来照 kiwi browser 的 CI（<https://github.com/AoEiuV020/kiwibrowser-build>）做了 ccache + 分轮续跑，**实测这条路在本方案里走不通**：

1. Chromium 的 `cc_wrapper` 只被 gcc/clang 工具链读取，而 QtWebEngine 在 Windows 上用 MSVC，工具链不看它。
   证据：`args.gn` 里确实写进了 `cc_wrapper="ccache"`（已核对构建日志），但编到 `[8038/29705]` 之后
   `ccache --show-stats` 显示缓存仍是 `0.0 GB`，`Cacheable calls` 一项根本不存在——一次都没被调用。
2. runner 每次都是干净的，构建目录（几十 GB）不跨轮保留，10–20 GB 的缓存配额也放不下；
   所以被时间预算打断就等于这一轮白编。

因此现在的策略是**一轮编完**，为此做了两件事：

1. **只编一个配置。** VS 是多配置生成器，装了 Qt 之后 `CMAKE_CONFIGURATION_TYPES` 被强制设成
   `RelWithDebInfo;Debug`；QtWebEngine 给每个配置都接一棵 gn/ninja 树、互为 `WebEngineCore` 的依赖，
   于是整个 Chromium 要编两遍（实测：第一棵树 56 分钟编到 `8038/29705`，第二棵排队等着）。
   `patch-single-config.ps1` 把配置集收成一个；`build.cmd` 在 configure 后立刻校验，收窄没生效就秒失败。
2. **时间预算给足。** `build_budget_minutes` 默认 300 分钟——打断了没有第二次机会。

`use_ccache` 因此默认关闭；开着只会在 ccache 一步报错（那是有意的：它在告诉你这个开关没用）。

三种结局由 `STATE_FILE` 区分，别改名：

| state 文件 | 含义 | 流水线动作 |
| --- | --- | --- |
| `ok` | 编完了 | 继续 finish / 打包 / 编解码验收 /（可选）发 Release |
| `failed <code>` | 编译报错，或根本没进到编译（环境/工具/未 configure） | 直接失败，**不排下一轮**（否则会在坏代码上无限轮下去） |
| 不存在 | 被时间预算打断（含被步超时杀掉的 `0xC000013A`） | 排下一轮；但注意上面说的：没有编译器缓存，下一轮是从头编 |

### 缓存存哪儿：两个后端

`CCACHE_DIR` 的存取仍然可用（`actions-cache` 免密钥、`git-repo` 更大），但在 MSVC 下它缓存不到东西。

| 后端 | 上限 | 需要密钥 | 适用 |
| --- | --- | --- | --- |
| `actions-cache`（默认） | 仓库 10 GB 配额，故 ccache 上限设 9G | 无 | 想零配置先跑起来 |
| `git-repo` | 20G（kiwi 的取值） | `CACHE_TOKEN`（PAT，可写缓存仓库） | 需要更大缓存、更高命中率 |

`git-repo` 要另填 `cache_repo`（`owner/name`）。缓存仓库只是个空仓库，脚本每轮把 ccache 目录提交推送进去。

### 自动续跑

`auto_continue=true` 会在本轮被打断后自动派下一轮，但**必须提供 `CACHE_TOKEN`**：
`GITHUB_TOKEN` 无法触发新的 workflow 运行，这是平台限制，不是配置问题。

## 产物与消费

`ok` 且验收通过后产出：

- `qtwebengine-<版本>-win64-msvc2022-codecs.zip`（`Qt6WebEngineCore.dll`、`QtWebEngineProcess.exe`、
  `Qt6WebEngineWidgets/Quick*.dll`、`resources/`、`translations/qtwebengine_locales/`）+ `.sha256`
- `codec-probe.json`（编解码验收证据）

验收门不是走过场：产物会被铺进一个临时 venv 里**真装的 PySide6**，离屏起 WebEngine 实测
H.264/AAC，并用 vp8/opus 做对照（防止「整条媒体管线被关掉」被误判成成功）。不通过就不产 zip、不发 Release。

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
  patch-gn-args.ps1                       往 QtWebEngine 的 Chromium 构建注入 gn 参数（cc_wrapper）
  install-webengine-runtime.ps1           把产物铺进 PySide6（版本核对、备份）
  verify-codecs.py                        编解码验收探针（退出码即结论）
  check-pipeline.py                       流水线静态自检（派 CI 之前先跑，不编译不联网）
```
