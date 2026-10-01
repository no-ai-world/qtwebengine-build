# 打包 PySide6 轮子（用已有的 QtWebEngine 产物）

`build-pyside6-wheels` 把 [artifacts.md](artifacts.md) 里那份 QtWebEngine 运行时打进官方
PySide6 轮子，发一套**可以直接 `pip install`** 的轮子。这条流水线**不编译任何东西**，
几分钟跑完：运行时从**已经发布**的 Release 里取。

## 规则：发哪几个文件

> **某个轮子必须发 ⟺ 我们的运行时实际改动了它的内容。**

不是"发四个"，也不是"发 Addons"，而是每次算出来的：

1. **归属**：运行时里每个相对路径，去这一套轮子（由 `PySide6==<版本>` 的 `requires_dist`
   推出来）里找 `<包根>/<相对路径>`，必须**恰好命中一个**发行版。0 个（上游布局变了，或者
   那是个新文件——用 `-NewFileOwner` 指定归属）和 ≥2 个（不知道该改哪个）都直接失败，
   不做"默认塞进 Addons"这种猜测。
2. **改动**：逐字节比对，只有内容真的不同（或轮子里根本没有这个位置）才算改动。
3. **发布集合** = 至少被改动过一个文件的那些轮子。

实测 PySide6 6.8.3（运行时 64 个文件）：

| 轮子 | 结果 |
| --- | --- |
| `PySide6-Addons` | **必须发**：6 个文件的内容真的变了（`Qt6WebEngineCore.dll`、`QtWebEngineProcess.exe`、Widgets/Quick 的 DLL、`resources/v8_context_snapshot.bin`） |
| `PySide6-Essentials` | 不发：它认领的 `resources/icudtl.dat` 与上游**逐字节相同** |
| `PySide6`（元包）、`shiboken6` | 不发：一个位置都没认领到 |

**这不是"写死只发 Addons"**：`icudtl.dat` 哪天变了（换一轮构建就可能），Essentials 就自动
进发布集合——不需要改代码，也不需要有人重新判断。这条规则有测试：
`scripts/tests/wheel-packaging-negative.py` 用最小的假轮子跑两种数据，断言集合随之变化。

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
| `pyside_version` | `6.8.3` | PySide6 / Qt 版本；同时决定取哪个 QtWebEngine Release |
| `webengine_tag` | 空 | 运行时所在的 Release 标签（留空 = `qtwebengine-<版本>-win64-msvc2022-codecs`） |
| `local_version` | `codecs` | 挂在**被改动过的**轮子上的本地版本段（`6.8.3` → `6.8.3+codecs`）。见下面"为什么需要它" |
| `new_file_owner` | 空 | 运行时里有、上游轮子里都没有的新文件归哪个发行版；留空则遇到就失败 |
| `python_version` | `3.12` | 打包与自测用的 Python。**必须 <3.14**：PySide6 6.8.3 的 `requires-python` 是 `>=3.9,<3.14` |
| `runner` | `windows-2022` | 不需要大规格 runner：这条流水线不编译（约 3 GB 磁盘） |
| `timeout_minutes` | `60` | 作业超时（分钟） |
| `create_release` | `true` | 打包成功后发 Release；传 `false` 就只留 artifact |
| `release_tag` | 空 | Release 标签（留空 = `pyside6-<版本>-win64-msvc2022-codecs`） |

## 产物

| 资产 | 内容 |
| --- | --- |
| 被改动过的轮子 | 实测 = 一个 `PySide6_Addons-6.8.3+codecs-cp39-abi3-win_amd64.whl`；集合由上面那条规则决定 |
| `SHA256SUMS` | 只有实际发出去的文件；行尾 LF，`sha256sum -c SHA256SUMS` 可直接核 |
| `MANIFEST.json` | 机器可读的账：发出去的（版本/摘要/改动了哪些文件）+ **从 PyPI 解析的那几份的 URL 与摘要** |
| `RELEASE_NOTES.md` | Release 正文。**生成**的：文件名、改动项、安装命令都来自实际产物，不是手写文案（手写文案会与产物漂移） |

**不在这里的轮子与 PyPI 上的原件逐字节相同**（实测摘要一致），所以从 PyPI 解析即可，
`MANIFEST.json` 里钉住了它们的版本与 sha256 供核对。想离线就在有网的机器上按 `MANIFEST.json`
把这一套下齐，再 `--no-index --find-links <目录>` 装。

## 装

**版本必须钉 `==6.8.3`**：运行时只对 Qt 6.8.3 有效，不钉版本时解析器会去拿 PyPI 上更新的
版本（那与这份产物无关）。

Release 说明里给的就是下面这条（用实际文件名生成，直接粘）：

```bash
uv add "pyside6==6.8.3" "PySide6-Addons @ https://github.com/<owner>/qtwebengine-build/releases/download/pyside6-6.8.3-win64-msvc2022-codecs/PySide6_Addons-6.8.3+codecs-cp39-abi3-win_amd64.whl"
pip install "pyside6==6.8.3" "PySide6-Addons @ <同一个直链>"
```

只有被改动的那一个包用直链，其余（shiboken6 / PySide6-Essentials / 元包 PySide6）自动从
PyPI 拿，版本由 `pyside6==6.8.3` 锁死。

### 为什么必须钉版本

实测 `uv add pyside6`（不钉）装的是 **6.11.2**——PyPI 上的最新版。这是"运行时要与 Qt 版本
对齐"的必然结果，不是包装问题。

### 本地版本段（`+codecs`）解决的是另一件事

同名同版本的两个轮子同时可见时，"装到哪个"取决于解析器，而且是**静默**的：

| 命令 | 没有本地段 | 有 `+codecs` |
| --- | --- | --- |
| `pip install "pyside6==6.8.3" --find-links <目录>` | ❌ 装成官方包（实测 3/3 次，连 `--no-cache-dir` 都一样，不报错） | ✅ 装我们的 |
| `uv add "pyside6==6.8.3" --find-links <目录>` | ✅（uv 优先 flat index） | ✅ |

挂上 `+codecs` 之后它比 `6.8.3` **严格更大**，任何解析器都会选它，`pip freeze` 也一眼能看出
装的是编解码器版。PEP 440：说明符不带本地段时匹配会忽略候选的本地段，所以 `pyside6==6.8.3`
声明的 `pyside6-addons==6.8.3` 依然被满足（`pip check` 通过，实测）。

### 装完怎么确认

```bash
python -c "import sys,pathlib;print((pathlib.Path(sys.prefix)/'Lib/site-packages/PySide6/Qt6WebEngineCore.dll').stat().st_size)"
```

`154831360` = 自建（带私有编解码器）；`154433672` = 官方（装错了）。
或者直接看 `pip freeze | grep Addons`：`6.8.3+codecs` 就是我们的。

## 发布门禁（三条，都是打包正确性）

1. 运行时 zip 与 Release 里随包的 `.sha256` 一致（下载没被换掉）；
2. 注入后每个轮子的 RECORD 与实际内容逐条对得上，挂了本地版本段的还要核"文件名 /
   dist-info 目录 / METADATA 里的版本"三者一致（脚本重新打开产物核对）；
3. 完整的一套在一个**一次性 venv** 里离线装得上（`--no-index --find-links`），并且运行时里的
   每个文件在 `site-packages` 里逐字节落位。

第 3 条用的是**完整的一套**（含从 PyPI 取来的那几份），不是只发出去的那些：少发不等于可以不验，
而"能不能一起装上"正是用户那里的第一步。

**不验**的是编解码器到底有没有生效（H.264 能不能放）。口径与 [artifacts.md](artifacts.md) 一致。

## 可复现性

同一套输入 + 同一个解释器 → **逐字节相同**的产物。新增的 zip 条目用固定时间戳，被替换的条目
沿用上游的元数据，所以时间戳不会漂。

换一个解释器（`python_version`）则只有容器那一层会变：deflate 的字节由打包用的 Python 的 zlib
决定，实测 3.12 与 3.14 压出来的总大小差 3.7 MB，而 **RECORD、CRC、每个条目的大小逐条相同**。
所以 `python_version` 是"产物摘要的一部分"；Release 里的 `SHA256SUMS` 钉的是实际发出去的字节。

## 本地复现整条链

```bash
# 1. 取运行时（或直接用 Release 里下载的 zip）
gh release download qtwebengine-6.8.3-win64-msvc2022-codecs --pattern '*' --clobber -D runtime/

# 2. 从 PyPI 取官方轮子（闭包由 requires_dist 推出，带摘要核对）
python scripts/pyside6/fetch-pyside6-wheels.py \
  -Version 6.8.3 -Destination upstream/ -Manifest upstream-manifest.json

# 3. 注入（重算 RECORD、产出后自证、写账本）
python scripts/pyside6/inject-webengine-runtime.py \
  -Runtime runtime/qtwebengine-6.8.3-win64-msvc2022-codecs.zip \
  -Wheels upstream/ -Destination wheels/ -ExpectVersion 6.8.3 \
  -LocalVersion codecs -Manifest inject-manifest.json

# 4. 按账本挑出必须发的轮子，并生成 Release 说明
python scripts/pyside6/stage-publish-set.py \
  -Stage wheels/ -Inject inject-manifest.json -Upstream upstream-manifest.json \
  -Destination publish/ -ReleaseUrlBase https://github.com/<owner>/<repo>/releases/download/<tag>/

# 5. 完整一套离线装一遍并逐字节核对（本机 Python 必须 <3.14）
python scripts/pyside6/verify-wheels.py \
  -Runtime runtime/qtwebengine-6.8.3-win64-msvc2022-codecs.zip \
  -Wheels wheels/ -Version 6.8.3
```

本机 Python 是 3.14+ 时，第 5 步要指一个 3.12/3.13：
`-PythonExecutable <path-to-python3.12>`（脚本会先把这件事说清楚，而不是丢一句 pip 的报错）。
