# 脚本：职责、退出码与行为契约

`scripts/` 下全部是**纯标准库 Python**（不需要任何第三方包），`build.cmd` 只负责编排。
workflow 里仍有约 450 行内联 `pwsh`（预检、页面文件、缓存下载、Qt 安装、判定、归档、汇总、
续跑），但**脚本层已经没有 PowerShell 了**。

| 脚本 | 职责 | 谁调用 | 退出码 |
| --- | --- | --- | --- |
| `build.cmd` | 编排：环境 → 源码 → 补丁 → configure → 编译 → 安装 → 打包 | workflow / 本地 | 1–7（见文件头注释），另写 `STATE_FILE` |
| `patch-cppgc.py` | v8/cppgc 的 C2352 修补（MSVC 14.44） | `:run_py` | 0 / 1 |
| `patch-gn-args.py` | 往 `src/core/CMakeLists.txt` 注入 `symbol_level` 与 `cc_wrapper` | `:run_py` | 0 / 1 |
| `patch-single-config.py` | 把 `CMAKE_CONFIGURATION_TYPES` 收成一个配置 | `:run_py` | 0 / 1 |
| `patch-msvc-ccache.py` | 让 Chromium 的 `cc_wrapper` 对 MSVC 工具链也生效 | `:run_py` | 0 / 1 |
| `check-ccache-bound.py` | prepare 阶段证明缓存已接线（跑 GN 生成 + 扫 ninja 规则） | `:run_py` | **0 / 1 / 2 / 3**，见下 |
| `watch-build.py` | 构建阶段看门狗：进度/静默/内存/缓存接线 + 哨兵 + 心跳 | workflow 直接启动 | 不判红（被杀掉即结束） |
| `stage-webengine-runtime.py` | 把安装前缀铺成待分发目录（zip 的来源） | `:run_py` | 0 / 1 |
| `fetch-pyside6-wheels.py` | 从 PyPI 取官方 PySide6 轮子（闭包由 `requires_dist` 推，逐文件核对 sha256） | 轮子 workflow | 0 / 1 |
| `inject-webengine-runtime.py` | 把运行时注入轮子（重算 RECORD，产出后自证，写账本） | 轮子 workflow | 0 / 1 |
| `stage-publish-set.py` | 按账本挑出"必须发"的轮子，生成 `SHA256SUMS` / `MANIFEST.json` / Release 说明 | 轮子 workflow | 0 / 1 |
| `verify-wheels.py` | 完整一套离线装进一次性 venv，逐字节核对落地文件 | 轮子 workflow | 0 / 1 |

补丁类脚本都是**幂等**的：目标已经是期望形态时不重写文件，重复跑不会插出第二段。

## 轮子流水线那四个脚本的契约

这四个脚本决定的是**发出去的东西**，所以它们宁可失败也不做"看起来成功"的事。核心是一条规则：

> **某个轮子必须发 ⟺ 我们的运行时实际改动了它的内容。**

* `fetch-pyside6-wheels.py`：取哪几个发行版是**推出来的**（`PySide6==<版本>` 的
  `requires_dist` 里那些精确钉住同版本的依赖 + 元包自己），不是写死的清单——写死的话上游一
  调整拆分方式（WebEngine 曾经在 Essentials 里），清单就成了假判据。平台/ABI 是筛出来的：
  只认文件名以 `-<平台>.whl` 结尾的那个文件，每个包必须**恰好一个**（0 个与 ≥2 个都失败）。
  下载边下边算 sha256 并比对 PyPI 给的摘要，不匹配就把半成品删掉；`-Manifest` 写出账本。
* `inject-webengine-runtime.py`：映射是推出来的——运行时里每个相对路径去每个轮子里找
  `<包根>/<相对路径>`，必须恰好命中一个轮子。0 个（上游布局变了，或是个新文件：用
  `-NewFileOwner <发行版>` 指定归属，这是**数据**不是代码改动）、≥2 个、以及"一个字节都没变"
  （那说明这份运行时是多余的）都算失败。重新打包时逐条重算 RECORD，产出后**重新打开产物**
  核对摘要与注入内容；挂了 `-LocalVersion`（例如 `codecs`）的还要核"文件名 / dist-info /
  METADATA 三者版本一致"，否则 pip 会直接拒收。一个字节都没变的轮子原样透传。
* `stage-publish-set.py`：从账本里读出"谁被改了"，**只把那些**拷进发布目录，并生成
  `SHA256SUMS`（只有实际发出去的文件，行尾 LF）、`MANIFEST.json`（含从 PyPI 解析的那几份的
  URL 与摘要）与 `RELEASE_NOTES.md`（Release 正文）。正文是**生成**的：文件名、改动项、
  安装命令都来自实际产物——手写文案会与产物漂移，漂了用户照说明装出来的就不是这里发的那个。
* `verify-wheels.py`：用 `--no-index --find-links` 装一次。**"这一套完不完整"由这次安装判断**
  （少任何一个发行版都会在这里硬失败），不再有写死的发行版清单。它先量一下要用的解释器
  （PySide6 6.8.3 要求 `>=3.9,<3.14`），免得把"解释器不对"报成"轮子装不上"。刻意**不 import**
  PySide6、不看页面能不能放 H.264——那是浏览器里的事。

## `check-ccache-bound.py` 的退出码是契约

| 码 | 含义 | build.cmd 的动作 |
| --- | --- | --- |
| 0 | 生成的规则里有 wrapper | 继续 |
| 1 | 规则生成了、但里面没有 wrapper | **中止整轮**（不排下一轮） |
| 2 | 生成不了 / 判断不了 | 打一句警告后**继续**（看门狗与轮末统计仍会覆盖） |
| 3 | **命令行参数错误** | 判红，并说明「是调用写错了」 |

3 必须与 2 分开。build.cmd 在这里是**按具体退出码分派**的（0 继续、2 打一句警告后继续、其余判红），
所以一次参数写错要是也落进 2，这道门禁就被静默关掉了——而它存在的全部意义就是拦住白烧一轮
（历史上 `-Wrapper` 被参数上限丢过一次）。同理，脚本内部的**意外异常**一律映射成 2（判断不了），
绝不映射成 1——那等于谎报「ccache 没接上」并中止一轮五小时的构建。

## 已知且刻意不对齐旧 PowerShell 版的地方

这些不是待修的缺陷，而是「旧脚本在这里会崩、会走偏，新脚本直接拒绝或做得更严」。**不要**为了
「和旧版一致」把它们改回去。

* **`Write-Error` 的装饰**：旧版打 5 行（`Write-Error: 脚本:行号` / `Line |` / 源码回显 / `~~~` /
  展开后的消息）且带 CRLF；现在只打一行。消息文本本身一致，**按子串匹配**的检查不受影响——
  但也因此不要写按整行锚定的日志检查。
* **`is_file()` 比 `Test-Path` 严格一档**：一个名叫 `Qt6WebEngineCore.dll` 的**目录**在旧版里能让
  必需文件检查放行（然后拷一个空目录当运行时），现在会拒绝。现在这个行为是对的。
* **`-AllowMissing:$true` / `:$false`**：旧版认这种开关写法，argparse 不认，会退出 3（明确报
  「命令行写错了」）。仓内没有调用方这么传。
* **0 字节的目标文件**：旧版在 `patch-cppgc` / `patch-single-config` 里会**崩**（`Get-Content -Raw`
  得到 `$null`，随后 `.Contains()` 抛异常），现在报错退出 1 并说明「源码没取全」——空文件是坏树，
  不会被 `-AllowMissing` 当成「上游已修好」而静默跳过。
* **`-RelativeFile` 不接受绝对路径**：`Path(root) / Path(abs)` 会丢掉 root 去改源码树外面的文件，
  现在直接拒绝。补丁永远不该碰到 `-SourceRoot` 之外的东西。
* **`--target help` 兜底比旧版强**：旧版那段是**死代码**（候选循环与兜底共用一个 8 次计数器，
  而候选恰好用满 8 次；实测把正确目标只放进 `help` 输出里仍然退出 2）。修好之后这条路径可能把
  「判断不了(2)」变成「已接线(0)」或「没接线(1)」——第三种是**更严**而不是错：规则确实没有
  wrapper 就该中止，而不是放行去白编五小时。
* **目录拷贝是「合并」**：旧版 `Copy-Item -Recurse` 在目标目录已存在时会把源目录整个塞进去
  （`translations\qtwebengine_locales\qtwebengine_locales\`），重跑一次打包就会多出这些文件，
  而打印的条数看不出来。现在重复跑是幂等的。
* **覆盖只读文件会先去掉只读属性**（对应 `Copy-Item -Force`）：否则「源只读 + 本地重跑打包」
  会抛 `PermissionError` 并留下半更新的树。
* **跳过 Hidden/System 项**：旧版 `Get-ChildItem` 不带 `-Force` 就会跳过，Python 的
  `iterdir`/`rglob` 不会，所以代码里显式过滤。影响拷贝集、打印条数、`MISSING in N ninja file(s)`。

## `build.cmd` 不能占用的环境变量名

批处理的 `set` 导出的是**环境变量**，每个子进程都继承，所以 `build.cmd` 里不许出现
`set "RC=..."`：CMake 把 `$ENV{RC}` 当成资源编译器的路径（`CMakeDetermineRCCompiler.cmake` 里值
不是文件就 `FATAL_ERROR`），而 `check-ccache-bound.py` 跑的 GN 生成会连带跑 `gn` 的
ExternalProject configure——踩上它，这道门禁每轮都只会答「判断不了(2)」，等于被静默关掉。
脚本里因此用 `STEP_RC`。第二道在 `check-ccache-bound.py`：起 cmake 之前把**不指向真实文件**的
`RC` 从子进程环境里摘掉（真指向 `rc.exe` 的值保持不动）；第一道由 `check-pipeline.py` 守着。

## 这些约定由 `scripts/check-pipeline.py` 守着

改调用点或换脚本名之后先跑一次（不编译、不联网）：

```bash
python scripts/check-pipeline.py
```

与脚本层相关的检查：`:run_py` 的参数个数（第 9 个会被无声丢掉）与 `-u`/`-X utf8`、`scripts/` 下
有没有混回 `.ps1`、`scripts/qtwebengine/` 与 `scripts/pyside6/` 下每个脚本是否**真的被调用**
（判据是调用而不是「注释里提到」——build.cmd 与 workflow 的注释、报错文案里都写着脚本名）、
每个 Python 脚本能否编译、build.cmd 有没有单独处理退出码 3、build.cmd 有没有占用 `RC` 这个名字，
以及 `auto_continue` 的预检步是否真的 `throw`（秘密缺失必须在几秒内失败，而不是轮末才发现）。
轮子流水线另有六条：运行时必须**从已有 Release 取**（`gh release download` 用 `WEBENGINE_TAG`，
不许出现 `build.cmd`）；一套的构成必须**从 `requires_dist` 推**（不许写死清单）、且 fetch 要写账本；
注入脚本要能挂本地版本段、要有新文件归属的逃生口、要写账本；**发布步必须发暂存集合
（`files: dist/publish/*`）而不是 `dist/wheels` 全部**，且挑集合那一步排在发布之前、正文用
`body_path`（生成的，不是手写）；离线安装自测必须在发布步之前**真的跑过**（判据是发布步引用
`steps.wheelcheck.outcome`，而不是全文里有没有 `--no-index`——发布说明的正文里就写着那条 pip
命令），并且那次安装必须带 `"--no-index"`（不带的解析器会去 PyPI 补齐，完整性判据静默失效；
判据是带引号的参数形式，不是全文子串——脚本的说明里就有这两个词）；发布步要保留
`overwrite_files` 与 `fail_on_unmatched_files`；以及**每一处启动脚本的调用行都要带 `-u -X utf8`**
（真踩过：run 36806802921 里取轮子那一步打印"完成：4 个轮子"时抛 `UnicodeEncodeError`，因为
CI 把输出接进管道、locale 是 cp1252，而四个轮子其实都已经下好了）。其余检查项见
[运行与参数](run.md#派之前先自检)。

### 三个回归测试

一条永远不报红的检查只是装饰，所以这些守卫自己也有测试（都在 `scripts/tests/`，用系统临时
目录，不写仓库里的任何文件）：

```bash
python scripts/tests/check-pipeline-negative.py   # 每条静态守卫都要能被真的破坏掉
python scripts/tests/wheel-packaging-negative.py  # 打包脚本不能静默地把坏产物发出去
python scripts/tests/run-py-integration.py        # build.cmd 的 :run_py 机制（不需要 Qt/MSVC）
```

* **负向测试**把每条守卫要防的事情真的做出来——把调用整行删掉、把 `-u` 拿掉、混进一个 `.ps1`、
  在 `-LiteralPath` 里写通配符——然后断言检查必须报红。这里每个变体都**刻意保留注释**：开发时
  两次踩到同一个坑，判据写成「全文子串」而理由注释里正好写着那个子串，于是把调用删掉、注释
  留着，检查照样通过。
* **打包负向测试**用几十字节的假轮子与假运行时（不联网、不需要真 PySide6）证明那几条"拒绝"
  真的会拒绝：运行时里的文件谁都认领不到、同一个位置落在两个轮子里、运行时与上游逐字节相同
  （这份运行时是多余的）、轮子版本对不上；另外把**发布规则本身**跑两遍——只改 Addons 时集合
  只有一个，`icudtl.dat` 也变时 Essentials 自动进集合——并断言 `SHA256SUMS` 是 LF 且摘要自洽。
* **`:run_py` 集成测试**把那个标签块从 `build.cmd` 里**逐字抽出来**再调用，所以验的是真正那条
  调用行（`python -u -X utf8 "%SCRIPT_DIR%\%~1" ...`）。harness 所在目录名故意带空格，验的
  就是那对引号；退出码透传、参数转发与 8 参数上限也一起钉住。
