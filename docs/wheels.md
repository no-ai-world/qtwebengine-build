# 打包 PySide6 轮子（用已有的 QtWebEngine 产物）

`build-pyside6-wheels` 把 [artifacts.md](artifacts.md) 里那份 QtWebEngine 运行时打进官方
PySide6 轮子，发一套**可以直接 `pip install`** 的轮子。这条流水线**不编译任何东西**，
几分钟跑完：运行时从**已经发布**的 Release 里取。

## 怎么跑

Actions → **build-pyside6-wheels** → Run workflow，或者：

```bash
gh workflow run build-pyside6-wheels.yml --repo <owner>/qtwebengine-build \
  -f pyside_version=6.8.3
```

前提是那个版本的 QtWebEngine Release 已经存在（标签 `qtwebengine-6.8.3-win64-msvc2022-codecs`，
由 `build-qtwebengine` 编出来）。没有的话这一步会在几秒钟内失败并说明该先去编哪一轮。

## 参数

| 输入 | 默认 | 说明 |
| --- | --- | --- |
| `pyside_version` | `6.8.3` | PySide6 / Qt 版本；同时决定默认取哪个 QtWebEngine Release |
| `webengine_tag` | 空 | 运行时所在的 Release 标签（留空 = `qtwebengine-<版本>-win64-msvc2022-codecs`） |
| `python_version` | `3.12` | 打包与自测用的 Python。**必须 <3.14**：PySide6 6.8.3 的 `requires-python` 是 `>=3.9,<3.14` |
| `runner` | `windows-2022` | 不需要大规格 runner：这条流水线不编译（约 3 GB 磁盘） |
| `timeout_minutes` | `60` | 作业超时（分钟） |
| `create_release` | `true` | 打包成功后发 Release；传 `false` 就只留 artifact |
| `release_tag` | 空 | Release 标签（留空 = `pyside6-<版本>-win64-msvc2022-codecs`） |

## 产物

| 项 | 取值 |
| --- | --- |
| 文件 | `shiboken6`、`PySide6-Essentials`、`PySide6-Addons`、`PySide6` 四个轮子 + `SHA256SUMS` |
| 去向 | 打包与离线安装自测都通过后自动发 Release，标签 `pyside6-<版本>-win64-msvc2022-codecs` |
| 重发 | 同一版本再打一次会更新同一个 Release 并覆盖同名资产（`overwrite_files: true`） |

## 装

**版本必须钉住 `==6.8.3`**：运行时只对 Qt 6.8.3 有效，而不钉版本时解析器会去拿 PyPI 上最新的
（实测 `uv add pyside6` 装的是 6.11.2，跟这份产物无关）。

下面几条都是在同一套 Release 资产上实测过的（uv 0.12.11 / pip 25.0.1，判据是落地后的
`Qt6WebEngineCore.dll`：自建 154,831,360 字节 / `d47a1923…`，官方 154,433,672 字节 / `3b5daf77…`）。

### 1. 显式把 Addons 指到我们的轮子（推荐，pip 与 uv 都确定）

```bash
# uv（其余三个自动从 PyPI 拿，版本由 pyside6==6.8.3 锁死）
uv add "pyside6==6.8.3" "pyside6-addons @ https://github.com/no-ai-world/qtwebengine-build/releases/download/pyside6-6.8.3-win64-msvc2022-codecs/PySide6_Addons-6.8.3-cp39-abi3-win_amd64.whl"

# pip 同形
pip install "pyside6==6.8.3" "pyside6-addons @ <同一个直链>"
```

`uv add` 会把它落成 pyproject 里的规范形态，之后 `uv sync` 一直是对的（lock 里记了 URL 与摘要）：

```toml
dependencies = ["pyside6==6.8.3", "pyside6-addons"]

[tool.uv.sources]
pyside6-addons = { url = "https://github.com/…/PySide6_Addons-6.8.3-cp39-abi3-win_amd64.whl" }
```

> ⚠️ **只写 `[tool.uv.sources]` 是不够的**：`sources` 只对项目的**直接依赖**生效。如果
> `pyside6-addons` 没同时列进 `dependencies`，uv 会**静默忽略**这条 source，然后从 PyPI 装官方包
> （第一次试就是这么翻车的，DLL 大小一眼看出不对）。

### 2. 全离线：先把四个轮子下到一个目录

```bash
gh release download pyside6-6.8.3-win64-msvc2022-codecs --pattern '*' -D wheels
uv add  "pyside6==6.8.3" --no-index --find-links wheels
pip install "pyside6==6.8.3" --no-index --find-links wheels
```

`--no-index` 关掉索引，两个工具都确定；代价是**所有**依赖都得能从 `wheels/` 解析
（只装 PySide6 的项目没问题，项目里还有别的依赖就别用这条）。

### 3. uv 专用：`--find-links`（不带 `--no-index`）也可以

```bash
uv add "pyside6==6.8.3" --find-links wheels
```

uv 在同名同版本时**优先 flat index**，所以拿到的是我们的（实测 3/3）。想让它长期有效就写进
pyproject，之后 `uv sync` 不带参数也对：

```toml
[tool.uv]
find-links = ["wheels"]
```

注意 uv 的 lock 里记的是**本机绝对路径**，换机器或删掉那个目录就失效；要可复现就用第 1 条的 URL。

### 4. ❌ 不要用：`pip install "pyside6==6.8.3" --find-links wheels`

同名同版本时 **pip 优先索引**，实测 3/3 次（连 `--no-cache-dir` 都是）静默装成**官方包**，
不报任何错——用户以为自己有编解码器，其实没有。pip 请用第 1 条或第 2 条。

### 装完怎么确认

```bash
python -c "import sys,pathlib;print((pathlib.Path(sys.prefix)/'Lib/site-packages/PySide6/Qt6WebEngineCore.dll').stat().st_size)"
```

`154831360` = 自建（带私有编解码器）；`154433672` = 官方（装错了）。

## 里面改了什么

运行时的 64 个文件**不落在同一个轮子里**，这是实测出来的布局（PySide6 6.8.3）：

| 轮子 | 认领到的位置 |
| --- | --- |
| `PySide6-Addons` | `Qt6WebEngineCore.dll`、`QtWebEngineProcess.exe`、`Qt6WebEngineWidgets/Quick*.dll`、`resources/*.pak`、`translations/qtwebengine_locales/*.pak` |
| `PySide6-Essentials` | `resources/icudtl.dat`（Qt6Core 也要它） |

所以映射是**推出来的**：运行时里的每个相对路径去每个轮子里找 `<包根>/<相对路径>`，
必须恰好命中一个轮子——0 个（上游布局变了）和 ≥2 个（不知道该改哪个）都直接失败，
不做"默认塞进 Addons"这种猜测。

一个字节都没变的轮子**原样透传**，不重新打包。当前这份运行时（H.264/AAC 那次构建）实测：

* 64 个位置里 **6 个**内容真的变了：`Qt6WebEngineCore.dll`、`QtWebEngineProcess.exe`、
  `Qt6WebEngineWidgets.dll`、`Qt6WebEngineQuick.dll`、`Qt6WebEngineQuickDelegatesQml.dll`、
  `resources/v8_context_snapshot.bin`；
* 其余 58 个（含全部 `.pak` 语言包与 `icudtl.dat`）逐字节相同，所以
  **`PySide6-Essentials`、`shiboken6`、`PySide6` 三个轮子与 PyPI 上的原件完全一致**
  （sha256 都没变），只有 `PySide6-Addons` 是重打的。

透传不是省事，是为了保住用户唯一能独立核对的东西：一个被重新压缩过、却没有任何实质变化的
轮子，它的 sha256 与 PyPI 上对不上——"看起来改过、其实没改"比"改过"更难解释。

重打的轮子会**按新内容重算 RECORD**（每个文件一行 `路径,sha256=…,大小`），所以
`pip uninstall` 不会留下垃圾文件。

## 为什么是"一套四个"而不是"一个改过的 Addons"

只发一个改过的 `PySide6-Addons`，用户还得自己去 PyPI 补齐另一个版本的 Essentials 与
shiboken6，而 `pip install` 的依赖解析会直接失败或者装出混搭版本。四个一起发，
`--no-index --find-links .` 才真的装得上——离线安装自测守的就是这件事。

## 发布门禁（三条，都是打包正确性）

1. 运行时 zip 与 Release 里随包的 `.sha256` 一致（下载没被换掉）；
2. 注入后每个轮子的 RECORD 与实际内容逐条对得上（脚本重新打开产物核对，并核对注入的文件
   字节与运行时一致）；
3. 四个轮子在一个**一次性 venv** 里离线装得上，并且运行时的 64 个文件在 `site-packages`
   里逐字节落位。

**不验**的是编解码器到底有没有生效（H.264 能不能放）。那是浏览器里的事，口径与
[artifacts.md](artifacts.md) 一致：本仓库只负责把它编出来、打包好、发出去。

## 可复现性（同一个摘要复核得动吗）

同一套输入 + 同一个解释器 → **逐字节相同**的产物：本机用 Python 3.12.13 打出来的
`PySide6_Addons`，与 CI 发出去的那份 sha256 完全相同（`c561284b…`）。新增的 zip 条目用固定
时间戳，被替换的条目沿用上游的元数据，所以时间戳不会漂。

换一个解释器（`python_version` 输入）则只有容器那一层会变：deflate 的字节由打包用的 Python 的
zlib 决定，实测 3.12 与 3.14 压出来的总大小差 3.7 MB，而 **RECORD、CRC、每个条目的大小逐条
相同**（内容一致，只是压缩结果不同）。所以：

* `python_version` 是"产物摘要的一部分"——想复核摘要就用同一个解释器；
* Release 里的 `SHA256SUMS` 钉的是**实际发出去的那份字节**，与 GitHub 自己的资产摘要一致，
  行尾是 LF（`sha256sum -c SHA256SUMS` 在 Linux/macOS 上也能直接核；Windows 的
  `Set-Content` 默认写 CRLF，会把整份清单核废，所以那一步写完还会自己查一遍有没有 CR）；
* 三个透传的轮子不受这件事影响，它们的 sha256 与 PyPI 上完全相同。

## 本地复现整条链

不需要 CI，也不联网编译（只有取轮子那一步联网）：

```bash
# 1. 取运行时（或者直接用 Release 里下载的 zip）
gh release download qtwebengine-6.8.3-win64-msvc2022-codecs \
  --pattern '*' --clobber -D runtime/

# 2. 从 PyPI 取官方轮子（带摘要核对）
python scripts/pyside6/fetch-pyside6-wheels.py -Version 6.8.3 -Destination upstream/

# 3. 注入（重算 RECORD，产出后自证）
python scripts/pyside6/inject-webengine-runtime.py \
  -Runtime runtime/qtwebengine-6.8.3-win64-msvc2022-codecs.zip \
  -Wheels upstream/ -Destination wheels/ -ExpectVersion 6.8.3

# 4. 离线装一遍并逐字节核对（本机 Python 必须 <3.14）
python scripts/pyside6/verify-wheels.py \
  -Runtime runtime/qtwebengine-6.8.3-win64-msvc2022-codecs.zip \
  -Wheels wheels/ -Version 6.8.3
```

本机 Python 是 3.14+ 时，第 4 步要指一个 3.12/3.13：
`-PythonExecutable <path-to-python3.12>`（脚本会先把这件事说清楚，而不是丢一句 pip 的报错）。
