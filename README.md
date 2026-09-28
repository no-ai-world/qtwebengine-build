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

然后在 Actions → **build-qtwebengine** → Run workflow。默认参数就是 Qt 6.8.3 + Release + ccache。

## 运行环境（门槛在这儿，不在时间）

| 项 | 要求 | 说明 |
| --- | --- | --- |
| 磁盘 | **≥ 90 GB 可用** | Chromium 源码+子模块 ≈40 GB、构建中间产物 ≈20–40 GB、Qt 与下载的 Chromium 工具链 ≈10 GB；ccache 另算 |
| CPU / 内存 | ≥ 8 核，建议 ≥ 32 GB | 16 GB 也能跑，靠页面文件兜底（流水线会自动扩） |
| 工具链 | VS 2022 17.14（MSVC 14.44） | cppgc 补丁针对这个编译器版本 |

标准 GitHub 托管 runner（文档标称 14 GB SSD）装不下，别拿它试。预检会先量盘，不达标直接失败并打印三条出路：
大规格 runner（larger runners，300 GB+ SSD）／自托管 Windows 机器／本地直接跑 `scripts/qtwebengine/build.cmd`。

## 一轮编不完怎么办：ccache + 分轮续跑

做法取自 kiwi browser 的 CI（<https://github.com/AoEiuV020/kiwibrowser-build>）。要点是「一轮」不等于「一次构建」：

1. `build.cmd` 拆成 **prepare / build / finish** 三阶段，阶段之间只靠 ccache 传递进度；
2. 注入 gn 参数 `cc_wrapper="ccache"`，让 Chromium 的编译器缓存真的生效
   （Windows 上 Chromium 用自带 clang-cl，`chromium/build/toolchain/win/toolchain.gni` 里 `cl_prefix` 会拼上 cc_wrapper）；
3. `build` 阶段带**时间预算**（默认 300 分钟），到点被作业步超时打断；
4. 每轮结束把 ccache 回写到外部存储，下一轮取回来接着编；
5. 冷缓存要若干轮收敛，缓存热了之后一轮编完并打包。

三种结局由 `STATE_FILE` 区分，别改名：

| state 文件 | 含义 | 流水线动作 |
| --- | --- | --- |
| `ok` | 编完了 | 继续 finish / 打包 / 编解码验收 /（可选）发 Release |
| `failed <code>` | 编译报错，或根本没进到编译（环境/工具/未 configure） | 直接失败，**不排下一轮**（否则会在坏代码上无限轮下去） |
| 不存在 | 被时间预算打断 | 回写缓存并排下一轮 |

### 缓存存哪儿：两个后端

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

## 文件

```
.github/workflows/build-qtwebengine.yml   流水线（阶段编排、缓存、验收、Release）
scripts/qtwebengine/
  build.cmd                               构建驱动：环境→源码→补丁→configure→编译→安装→打包
  patch-cppgc.ps1                         Chromium v8/cppgc 补丁（MSVC 14.44 的 C2352）
  patch-gn-args.ps1                       往 QtWebEngine 的 Chromium 构建注入 gn 参数（cc_wrapper）
  install-webengine-runtime.ps1           把产物铺进 PySide6（版本核对、备份）
  verify-codecs.py                        编解码验收探针（退出码即结论）
```
