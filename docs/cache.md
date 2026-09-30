# 缓存与分轮续跑

一轮编不完时靠 ccache 分轮收敛：每轮结束把 ccache 目录存起来，下一轮恢复后继续。
开关是 `use_ccache=true`（默认 `false`）。

## 缓存存哪儿

`ccache_max_size` 留空时按下表取默认值。

| 后端 | 上限 | 需要密钥 | 适用 |
| --- | --- | --- | --- |
| `actions-cache`（默认） | 9G（仓库配额 10 GB） | 无 | 零配置先跑起来 |
| `git-repo` | 20G | `CACHE_TOKEN`（PAT，可写缓存仓库） | 需要更大缓存、更高命中率 |

`git-repo` 要另填 `cache_repo`（`owner/name`）——缓存仓库只是个空仓库，脚本每轮把 ccache
目录提交推送进去。

回写时机：`actions/cache` 的 post 步骤带 `post-if: success()`，job 里任何后续步骤变红都会把
整轮缓存赔掉，因此改成 `actions/cache/restore` + 构建阶段之后的显式 `actions/cache/save`
（`if: always()`）。存失败（配额/网络）不判红轮。

## 余量：pdfium 不在缓存范围内

`patch-gn-args.py` 只改 QtWebEngine 自己的 `src/core/CMakeLists.txt`，所以 `src/pdf`
那棵树不参与缓存；它的目标数不多。

## 自动续跑

`auto_continue=true` 会在本轮被时间预算打断后自动派下一轮，但**必须提供 `CACHE_TOKEN`**：
`GITHUB_TOKEN` 无法触发新的 workflow 运行，这是平台限制，不是配置问题。

没配这个秘密时，派发会**在几秒钟内失败**（workflow 里的「预检：auto_continue 需要
CACHE_TOKEN」一步）；轮末那一步在秘密缺失时也会判红，而不是只打一行日志就放过——那种静默
降级恰好发生在轮次被打断、最需要续跑的时候。配上它：

```bash
gh secret set CACHE_TOKEN --repo <owner>/qtwebengine-build
# 经典 PAT：勾 repo + workflow
# 细粒度 PAT：只给本仓库的 Actions: read and write
```

配好之后，被打断的那一轮编出来的目标已经在缓存里，下一轮接着编（`git-repo` 后端也用这个
秘密推送缓存仓库）。

## 看进度：prepare 阶段与心跳

- **prepare 阶段**会跑一次 GN 生成并读 GN 写出的 ninja 规则，确认里面出现 `ccache`；
  没出现就当场失败（prepare 退出码 4），不会等到几小时后才发现缓存是空的。
  这一步同时让构建阶段省掉 GN 生成（约四分钟）。
- **构建阶段**的 `watch-build.py` 每 5 分钟往日志写一行：最后完成的目标、日志静默多久、
  可用内存、`cl`/`mspdbsrv` 的进程数与占用、ccache 计数，另存 `watch-roundN.log` 进产物。
  它在两个独立信号一致（规则里没有 wrapper **且** ccache 计数为 0）时才会中止编译，
  把 `ccache-not-bound` 落盘成本轮的 `failed`。
- **心跳**：每轮状态写进本作业的 check run（作业日志在 `in_progress` 时拿不到）。边跑边查：

  ```bash
  gh api repos/<owner>/<repo>/check-runs/<check_run_id> --jq .output.summary
  ```

  那行摘要里有进度、内存、ccache 命中与缓存大小，不必等作业结束。
