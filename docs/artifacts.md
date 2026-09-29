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

## 铺进 PySide6

```powershell
scripts/qtwebengine/install-webengine-runtime.ps1 `
  -Source <解压后的目录> -Destination <site-packages>\PySide6
```

- `-Source` 既接受 CMake 安装前缀（`bin/` 下有 DLL），也接受解压 zip 后的暂存树（DLL 平铺在根上）；
- 脚本先核对 DLL 版本再覆盖同名文件；默认把旧文件备份到 `_webengine-backup/`（回滚即从该目录拷回）；
- **版本必须对齐**：产物是给 `PySide6==<构建的 Qt 版本>` 用的，大版本不一致会被拒绝。
