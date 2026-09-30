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

补丁类脚本都是**幂等**的：目标已经是期望形态时不重写文件，重复跑不会插出第二段。

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
有没有混回 `.ps1`、`scripts/qtwebengine/` 下每个脚本是否**真的被调用**（判据是调用而不是「注释里
提到」——build.cmd 与 workflow 的注释、报错文案里都写着脚本名）、每个 Python 脚本能否编译、
build.cmd 有没有单独处理退出码 3、build.cmd 有没有占用 `RC` 这个名字，以及 `auto_continue` 的
预检步是否真的 `throw`（秘密缺失必须在几秒内失败，而不是轮末才发现）。其余检查项见
[运行与参数](run.md#派之前先自检)。

### 两个回归测试

一条永远不报红的检查只是装饰，所以这两条守卫自己也有测试（都在 `scripts/tests/`，用系统临时
目录，不写仓库里的任何文件）：

```bash
python scripts/tests/check-pipeline-negative.py   # 每条守卫都要能被真的破坏掉
python scripts/tests/run-py-integration.py        # build.cmd 的 :run_py 机制（不需要 Qt/MSVC）
```

* **负向测试**把每条守卫要防的事情真的做出来——把调用整行删掉、把 `-u` 拿掉、混进一个 `.ps1`、
  在 `-LiteralPath` 里写通配符——然后断言检查必须报红。这里每个变体都**刻意保留注释**：开发时
  两次踩到同一个坑，判据写成「全文子串」而理由注释里正好写着那个子串，于是把调用删掉、注释
  留着，检查照样通过。
* **`:run_py` 集成测试**把那个标签块从 `build.cmd` 里**逐字抽出来**再调用，所以验的是真正那条
  调用行（`python -u -X utf8 "%SCRIPT_DIR%\%~1" ...`）。harness 所在目录名故意带空格，验的
  就是那对引号；退出码透传、参数转发与 8 参数上限也一起钉住。
