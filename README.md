# qtwebengine-build

自建**带私有编解码器**的 QtWebEngine（Windows x64 / MSVC 2022），产物可直接铺进 PySide6。

这个仓库只做一件事：**把它编出来并发布**（不校验产物里的编解码器/特性是否真的生效）。

## 产物

| 项 | 取值 |
| --- | --- |
| 文件 | `qtwebengine-<版本>-win64-msvc2022-codecs.zip` + `.sha256` |
| 内容 | `Qt6WebEngineCore.dll`、`QtWebEngineProcess.exe`、`Qt6WebEngineWidgets/Quick*.dll`、`resources/`、`translations/qtwebengine_locales/` |
| 去向 | 编译成功（`state=ok`）自动发 Release，标签 `qtwebengine-<版本>-win64-msvc2022-codecs` |

## 跑一轮

Actions → **build-qtwebengine** → Run workflow，或者：

```bash
gh workflow run build-qtwebengine.yml --repo <owner>/qtwebengine-build \
  -f round=1 -f qt_version=6.8.3 -f use_ccache=true
```

默认 Qt 6.8.3 / RelWithDebInfo / 单配置 / `symbol_level=0`。一轮 4–5 小时；想分轮续跑就打开
`use_ccache`（默认走 actions-cache，零配置）。

## 用产物

```powershell
scripts/qtwebengine/install-webengine-runtime.ps1 `
  -Source <解压后的目录> -Destination <site-packages>\PySide6
```

脚本会核对 DLL 版本并备份旧文件。产物是给 `PySide6==<构建的 Qt 版本>` 用的，大版本不一致会被拒绝。

## 仓库结构

```
.github/workflows/build-qtwebengine.yml   流水线（准备 → 构建 → 打包 → 发布）
docs/                                     说明文档，见下
scripts/check-pipeline.py                 派 CI 之前的静态自检（不编译、不联网）
scripts/qtwebengine/
  build.cmd                                构建驱动：环境→源码→补丁→configure→编译→安装→打包
  patch-cppgc.ps1                          Chromium v8/cppgc 补丁（MSVC 14.44 的 C2352）
  patch-gn-args.ps1                        注入 gn 参数（symbol_level、cc_wrapper）
  patch-single-config.ps1                  把 CMAKE_CONFIGURATION_TYPES 收成一个配置（否则编两遍）
  patch-msvc-ccache.ps1                    让 cc_wrapper 对 MSVC 工具链也生效（ccache 真被调用的前提）
  check-ccache-bound.ps1                   prepare 阶段就证明缓存已接线（跑 GN 生成 + 读 ninja 规则）
  watch-build.ps1                          构建阶段看门狗（进度/静默/内存/缓存接线 + check run 心跳）
  install-webengine-runtime.ps1            打包：把安装前缀铺成待分发目录（也用于铺进 PySide6）
```

## 文档

- [运行与参数](docs/run.md) —— 怎么派一轮、全部输入与默认值、时间预算与三种结局、本地直接跑
- [缓存与分轮续跑](docs/cache.md) —— 缓存后端、回写时机、自动续跑、怎么看进度
- [产物与发布](docs/artifacts.md) —— 产物构成、Release 语义、铺进 PySide6
