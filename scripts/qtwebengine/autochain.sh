#!/usr/bin/env bash
# 本地自动续跑器：一轮几小时、一轮编不完，这条链路不该缺人守着。
#
# 用法（Git Bash，需 gh 已登录且对本仓库有 workflow 权限）：
#     scripts/qtwebengine/autochain.sh [起始轮号] [最多派发轮数]
#     scripts/qtwebengine/autochain.sh 6 8
#
# 规则：等 CI 队列空了（没有 in_progress/queued）之后
#   * "归档运行时（zip + sha256）"这一步成功 → 停（产物已生成，目标达成）
#   * 该轮结论是 failure               → 停（确定性失败；verdict 已让这种轮次变红）
#   * 否则（被时间预算打断）            → 派下一轮：带 ccache、预算 320 分钟，
#     并依据上一轮看门狗的内存采样自动定并行度（>=5GB 空闲 → parallel=6，
#     <1.5GB → parallel=3；NO_AUTOTUNE=1 关闭；.temp/autochain.env 可放 EXTRA_ARGS）
#
# 注意：被时间预算打断的轮次也会上传 artifact（只有日志，几十 KB），所以判断"有没有产物"
# 必须看归档那一步的结论，不能看 artifact 数量。
#
# 环境变量：NO_AUTOTUNE=1 关闭自动定档；日志写在 .temp/autochain.log。
REPO=no-ai-world/qtwebengine-build
LOG=.temp/autochain.log
ROUND=${1:-1}
MAX=${2:-8}
DISPATCHED=0
echo "=== autochain 启动 $(date -u) max=$MAX 起始轮次=$ROUND ===" >> "$LOG"
while [ "$DISPATCHED" -lt "$MAX" ]; do
  while true; do
    active=$(MSYS_NO_PATHCONV=1 gh api "repos/$REPO/actions/runs?status=in_progress" --jq '[.workflow_runs[] | select(.name=="build-qtwebengine")] | length' 2>/dev/null)
    queued=$(MSYS_NO_PATHCONV=1 gh api "repos/$REPO/actions/runs?status=queued" --jq '[.workflow_runs[] | select(.name=="build-qtwebengine")] | length' 2>/dev/null)
    [ -z "$active" ] && active=1
    [ -z "$queued" ] && queued=1
    [ "$active" = "0" ] && [ "$queued" = "0" ] && break
    sleep 120
  done
  last=$(MSYS_NO_PATHCONV=1 gh api "repos/$REPO/actions/runs?per_page=1" --jq '.workflow_runs[0].id' 2>/dev/null)
  concl=$(MSYS_NO_PATHCONV=1 gh api "repos/$REPO/actions/runs/$last" --jq '.conclusion' 2>/dev/null)
  job=$(MSYS_NO_PATHCONV=1 gh api "repos/$REPO/actions/runs/$last/jobs" --jq '.jobs[0].id' 2>/dev/null)
  # 注意：被时间预算打断的轮次也会上传 artifact（只有日志，几十 KB），所以不能按
  # "有没有 artifact" 判断，必须看"归档运行时（zip + sha256）"这一步有没有真的成功。
  zipstep=$(MSYS_NO_PATHCONV=1 gh api "repos/$REPO/actions/jobs/$job" --jq '[.steps[] | select(.name | test("归档运行时"))][0].conclusion // "none"' 2>/dev/null)
  echo "$(date -u) last_run=$last conclusion=$concl zip_step=${zipstep:-none}" >> "$LOG"
  if [ "$zipstep" = "success" ]; then echo "  已产出 zip 产物 → 停止自动续跑（目标达成）" >> "$LOG"; break; fi
  if [ "$concl" = "failure" ]; then echo "  上一轮是确定性失败（红）→ 停止，等人工看日志" >> "$LOG"; break; fi
  # 附加参数可从 .temp/autochain.env 覆盖（例如 EXTRA_ARGS="-f parallel=6"），
  # 这样按第 3/5 轮的内存数据调参时不必改脚本。
  EXTRA_ARGS=""
  [ -f .temp/autochain.env ] && . .temp/autochain.env

  # 自动定并行度：4 核机器上 -j 太小浪费核，太大在 16 GB 上换页假死。
  # 依据是上一轮看门狗留下的 free-ram / cl 占用采样（NO_AUTOTUNE=1 可关）。
  PARAM_ARG=""
  case "$EXTRA_ARGS" in
    *parallel*) echo "  EXTRA_ARGS 已指定 parallel，跳过自动定档" >> "$LOG" ;;
    *)
      if [ -z "$NO_AUTOTUNE" ] && [ -n "$job" ]; then
        MSYS_NO_PATHCONV=1 gh run view --repo "$REPO" --job="$job" --log > .temp/autochain-last.log 2>/dev/null
        if [ -s .temp/autochain-last.log ]; then
          minram=$(grep -o "free-ram=[0-9.]*GB" .temp/autochain-last.log | sed "s/free-ram=//;s/GB//" | sort -n | head -1)
          maxcl=$(grep -oE "cl=[0-9]+\([0-9.]+GB\)" .temp/autochain-last.log | sed "s/.*(//;s/GB)//" | sort -n | tail -1)
          echo "  上一轮内存采样：min_free_ram=${minram:-无}GB max_cl=${maxcl:-无}GB" >> "$LOG"
          if [ -n "$minram" ]; then
            if awk "BEGIN{exit !($minram >= 5)}"; then PARAM_ARG="-f parallel=6"; fi
            if awk "BEGIN{exit !($minram < 1.5)}"; then PARAM_ARG="-f parallel=3"; fi
          fi
          echo "  自动定档：parallel=${PARAM_ARG:-默认（按核数与内存自动算）}" >> "$LOG"
        fi
      fi
      ;;
  esac

  MSYS_NO_PATHCONV=1 gh workflow run build-qtwebengine.yml --repo "$REPO" --ref main \
    -f round="$ROUND" -f qt_version=6.8.3 -f use_ccache=true -f build_budget_minutes=320 $PARAM_ARG $EXTRA_ARGS >> "$LOG" 2>&1
  echo "  已派第 $ROUND 轮 $(date -u)" >> "$LOG"
  ROUND=$((ROUND+1)); DISPATCHED=$((DISPATCHED+1))
  sleep 300
done
echo "=== autochain 结束 $(date -u) dispatched=$DISPATCHED ===" >> "$LOG"
