# qtwebengine-build

自建**带私有编解码器**的 QtWebEngine（Windows x64 / MSVC 2022），并把它打包成两样东西：
一份可直接铺进 PySide6 的运行时 zip，和一套**直接能 `pip install`** 的 PySide6 轮子。

这个仓库只做两件事：**把它编出来**、**打包并发布**（不校验产物里的编解码器/特性是否真的生效）。

## 产物

| 项 | 取值 |
| --- | --- |
| 运行时 | `qtwebengine-<版本>-win64-msvc2022-codecs.zip` + `.sha256` |
| 运行时内容 | `Qt6WebEngineCore.dll`、`QtWebEngineProcess.exe`、`Qt6WebEngineWidgets/Quick*.dll`、`resources/`、`translations/qtwebengine_locales/` |
| 运行时去向 | 编译成功（`state=ok`）自动发 Release，标签 `qtwebengine-<版本>-win64-msvc2022-codecs` |
| 轮子 | **只发被运行时实际改动过的那些**（实测 6.8.3 = 一个 `PySide6-Addons`）+ `SHA256SUMS` + `MANIFEST.json`；其余发行版与 PyPI 逐字节相同，从 PyPI 解析 |
| 轮子去向 | 打包与离线安装自测都通过后发 Release，标签 `pyside6-<版本>-win64-msvc2022-codecs` |

## 跑一轮

编运行时（4–5 小时）：

```bash
gh workflow run build-qtwebengine.yml --repo <owner>/qtwebengine-build \
  -f round=1 -f qt_version=6.8.3 -f use_ccache=true
```

打轮子（几分钟，**不重新编译**，直接用上面已经发布的那份运行时）：

```bash
gh workflow run build-pyside6-wheels.yml --repo <owner>/qtwebengine-build \
  -f pyside_version=6.8.3
```

默认 Qt 6.8.3 / RelWithDebInfo / 单配置 / `symbol_level=0`；想分轮续跑就打开 `use_ccache`
（默认走 actions-cache，零配置）。轮子那条流水线不需要大规格 runner，但用的 Python 必须
<3.14（PySide6 6.8.3 的 `requires-python`）。

## 用产物

打轮子那条路（推荐，命令由 Release 说明按实际产物生成）：

```bash
uv add "pyside6==6.8.3" "PySide6-Addons @ <Release 里的直链>"
# pip 同形
```

**版本必须钉死**（运行时只对 Qt 6.8.3 有效），只有被改动的那一个包用直链，其余自动从 PyPI
拿。为什么钉版本、为什么直链、装完怎么确认，见 [打包 PySide6 轮子](docs/wheels.md#装)。

手动铺运行时那条路：解压 zip，把内容覆盖进目标 PySide6 安装目录的 `PySide6\` 下（同名文件直接替换）：

```
根下的 DLL/exe  →  <site-packages>\PySide6\
resources\      →  <site-packages>\PySide6\resources\
translations\   →  <site-packages>\PySide6\translations\
```

**版本必须对齐**：产物只适用于构建它的那个 Qt 版本。`.pyd` 只按 DLL 名解析、不做版本校验，
错配会变成运行期崩溃而不是加载期报错。

## 仓库结构

```
.github/workflows/build-qtwebengine.yml     编译流水线（准备 → 构建 → 打包 → 发布）
.github/workflows/build-pyside6-wheels.yml  轮子流水线（取已有 Release → 注入 → 自测 → 发布）
docs/                                       说明文档，见下
scripts/check-pipeline.py                   派 CI 之前的静态自检（不编译、不联网）
scripts/tests/
  check-pipeline-negative.py                负向测试：每条静态检查都要能被真的破坏掉
  wheel-packaging-negative.py               负向测试：打包脚本不能静默地把坏产物发出去
  run-py-integration.py                     build.cmd 的 :run_py 机制集成测试（不需要 Qt/MSVC）
scripts/qtwebengine/
  build.cmd                                构建驱动：环境→源码→补丁→configure→编译→安装→打包
  patch-cppgc.py                           Chromium v8/cppgc 补丁（MSVC 14.44 的 C2352）
  patch-gn-args.py                         注入 gn 参数（symbol_level、cc_wrapper）
  patch-single-config.py                   把 CMAKE_CONFIGURATION_TYPES 收成一个配置（否则编两遍）
  patch-msvc-ccache.py                     让 cc_wrapper 对 MSVC 工具链也生效（ccache 真被调用的前提）
  check-ccache-bound.py                    prepare 阶段就证明缓存已接线（跑 GN 生成 + 读 ninja 规则）
  watch-build.py                           构建阶段看门狗（进度/静默/内存/缓存接线 + check run 心跳）
  stage-webengine-runtime.py               打包：把安装前缀铺成待分发目录（zip 的来源）
scripts/pyside6/
  fetch-pyside6-wheels.py                  从 PyPI 取官方轮子（闭包由 requires_dist 推，核对 sha256）
  inject-webengine-runtime.py              把运行时注入轮子（重算 RECORD + 产出后自证 + 写账本）
  stage-publish-set.py                     按账本挑出"必须发"的轮子，并生成 Release 说明
  verify-wheels.py                         完整一套离线装进一次性 venv，逐字节核对落地文件
```

## 文档

- [运行与参数](docs/run.md) —— 怎么派一轮、全部输入与默认值、时间预算与三种结局、本地直接跑
- [打包 PySide6 轮子](docs/wheels.md) —— 不编译那条路：参数、产物、装法、改了什么、发布门禁
- [缓存与分轮续跑](docs/cache.md) —— 缓存后端、回写时机、自动续跑、怎么看进度
- [产物与发布](docs/artifacts.md) —— 产物构成、Release 语义、铺进 PySide6
- [脚本契约](docs/scripts.md) —— 各脚本职责与退出码、`check-ccache-bound` 的 0/1/2/3、刻意保留的行为差异
