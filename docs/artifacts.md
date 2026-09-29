# 产物与发布

## 产物

编完（`state=ok`）后产出：

| 文件 | 内容 |
| --- | --- |
| `qtwebengine-<版本>-win64-msvc2022-codecs.zip` | `Qt6WebEngineCore.dll`、`QtWebEngineProcess.exe`、`Qt6WebEngineWidgets/Quick*.dll`、`resources/`、`translations/qtwebengine_locales/` |
| 同名 `.sha256` | zip 的 sha256 |
| `*.log` | 同轮的 prepare / build / finish / watch 日志 |

## 发布

**编译成功即自动发 Release**（`create_release` 默认 `true`）：

| 项 | 取值 |
| --- | --- |
| 标签 | `qtwebengine-<版本>-win64-msvc2022-codecs`（可用 `release_tag` 覆盖） |
| 资产 | zip + sha256 |
| 重复发布 | 同一版本再次编成功会更新同一个 Release，并覆盖同名资产（`overwrite_files: true`） |
| 跳过发布 | 派发时带 `-f create_release=false` |

## 怎么用：铺进 PySide6

解压 zip，把内容覆盖进 `<site-packages>\PySide6\` 下（同名文件直接替换）：

| zip 里的位置 | 目标位置 |
| --- | --- |
| 根下的 `Qt6WebEngineCore.dll`、`QtWebEngineProcess.exe`、`Qt6WebEngineWidgets/Quick*.dll` | `<site-packages>\PySide6\` |
| `resources\` | `<site-packages>\PySide6\resources\` |
| `translations\` | `<site-packages>\PySide6\translations\` |

**版本必须对齐**：产物只适用于构建它的那个 Qt 版本。`.pyd` 只按 DLL 名解析、不做版本校验，
错配会变成运行期崩溃而不是加载期报错。
