# 运行与参数

## 怎么跑一轮

Actions → **build-qtwebengine** → Run workflow，或者：

```bash
gh workflow run build-qtwebengine.yml --repo <owner>/qtwebengine-build \
  -f round=1 -f qt_version=6.8.3 -f use_ccache=true
```

一轮 4–5 小时。默认参数：Qt 6.8.3、RelWithDebInfo、只编一个配置、`symbol_level=0`、
`use_ccache=false`（要分轮续跑就打开，见 [cache.md](cache.md)）。

## 参数

| 输入 | 默认 | 说明 |
| --- | --- | --- |
| `round` | `1` | 第几轮（纯记账，便于在作业列表里看出进度） |
| `qt_version` | `6.8.3` | Qt / PySide6 版本（同时决定 qtwebengine 源码分支） |
| `qtwebengine_ref` | 空 | qtwebengine 分支/标签（留空 = 取 `qt_version`） |
| `pyside_version` | 空 | 产物说明里写的目标 PySide6 版本（留空 = 取 `qt_version`） |
| `runner` | `windows-2022` | `runs-on` 标签；需要 90 GB+ 可用磁盘 |
| `build_type` | `RelWithDebInfo` | 只能是已安装 Qt 提供的配置。Debug 产物名带调试后缀、无法覆盖 PySide6 |
| `use_ccache` | `false` | 开启 ccache（往 gn 注入 `cc_wrapper`，并让 MSVC 工具链也认它）。分轮续跑的前提 |
| `cache_backend` | `actions-cache` | 缓存后端：`actions-cache`（仓库 10 GB 配额）或 `git-repo`（独立仓库，需 `CACHE_TOKEN`） |
| `cache_repo` | 空 | `git-repo` 后端用：缓存仓库（`owner/name`） |
| `ccache_max_size` | 空 | ccache 容量上限（留空 = actions-cache 9G、git-repo 20G） |
| `ccache_version` | `4.14.1` | ccache 官方 Windows 免安装包版本 |
| `build_budget_minutes` | `300` | 单轮 build 阶段的时间预算（分钟）；到点被打断留给下一轮，`0` = 不设 |
| `jumbo` | 空 | 留空 = Qt 默认（开，每个 TU 合并 8 个源）；`0/off/false` = 每源文件一个 TU（最省内存、总工作量最大）；数字 N = 每个 TU 合并 N 个源 |
| `symbol_level` | `0` | gn `symbol_level`。`0` = 不要调试信息：更快、内存与磁盘小得多，且不经过 `mspdbsrv`；`2` = Qt 在 RelWithDebInfo+MSVC 下的原值（每个 obj 写 PDB，慢且大） |
| `parallel` | 空 | 并行度（留空 = 按 CPU 与内存自动换算）；同时作用于外层 `--parallel` 与内层 ninja `-j` |
| `shallow_submodule` | `true` | Chromium 子模块浅克隆（省时间省磁盘；configure 报缺 git 历史时改 `false`） |
| `skip_patch` | `false` | 跳过补丁（仅当上游已修好 cppgc 的 C2352） |
| `min_free_gb` | `90` | 构建盘最小可用空间（GB），低于此值直接失败 |
| `pagefile_gb` | `24` | 构建盘页面文件上限（GB，`0` = 不动）；内存 ≤ 32 GB 时留默认值给链接兜底 |
| `skip_preflight` | `false` | 跳过磁盘门槛（只为在标准 runner 上试通管线，不产出可用产物） |
| `timeout_minutes` | `360` | 作业超时（分钟）；托管 runner 上限 360 |
| `auto_continue` | `false` | 未完则自动派下一轮（需要 `CACHE_TOKEN`，见 [cache.md](cache.md)） |
| `create_release` | `true` | 编译成功后发布 Release（见 [artifacts.md](artifacts.md)） |
| `release_tag` | 空 | Release 标签（留空 = `qtwebengine-<版本>-win64-msvc2022-codecs`） |

## 时间预算与三种结局

`build_budget_minutes` 到点会打断 build 阶段，把进度留给下一轮。这一步是"编完了""真错了"
还是"时间到了"，由 `STATE_FILE` 的内容区分：

| state 文件 | 含义 | 流水线动作 |
| --- | --- | --- |
| `ok` | 编完了 | 收尾（安装+打包）→ 归档 zip → 发 Release |
| `failed <code>` | 编译报错、根本没进到编译，或 ccache 没绑定（`failed ccache-not-bound`） | 直接失败，**不排下一轮** |
| 不存在 | 被时间预算打断（含被步超时杀掉的 `0xC000013A`） | 排下一轮；`use_ccache=true` 时带着缓存继续，否则从头再来 |

## 本地直接跑

CI 只是编排，所有步骤都在 `build.cmd` 里，本地同样能跑。`build.cmd` 会调
`scripts\qtwebengine\` 下的 Python 脚本（补丁、缓存接线自检、打包），所以 `python` 必须在
PATH 上——这本来就是构建依赖，Chromium 自己的构建也要它，`:check_tools` 会查：

```cmd
set "QT_PATH=C:\Qt\6.8.3\msvc2022_64"        & rem 需要装了 msvc2022_64 全量包（含私有头文件）
set "WORK_ROOT=D:\qtwebengine-work"           & rem 指向空间最大的盘
set "USE_CCACHE=1"
scripts\qtwebengine\build.cmd all
```

`prepare` / `build` / `finish` 也可以分开跑；环境变量与退出码见 `build.cmd` 头部注释。

## 派之前先自检

```powershell
python scripts/check-pipeline.py
```

只做静态检查，不编译、不联网：批处理的 CRLF/ASCII、`goto`/`call` 标签是否存在、块括号是否配平、
`cmake --build`/`--install` 是否带 `--config`、确定性失败有没有落盘成 `failed`（漏了会被误判成
超时并无限续跑）、workflow 里引用的 step id 是否存在、workflow 是否漏传输入；以及这轮迁移之后
新增的几条——`:run_py` 的参数个数（第 9 个会被无声丢掉）与 `-u`、`scripts/` 下有没有混回 `.ps1`、
`scripts/qtwebengine/` 下每个脚本是否**真的被调用**（不是被注释提到）、每个 Python 脚本能否编译、
`build.cmd` 有没有单独处理 `check-ccache-bound.py` 的用法错误退出码 3（它必须与「判断不了」的 2
分开，否则一次参数写错就静默关掉 ccache 门禁）。
