# 编解码验收探针：跑在「已铺入自建 QtWebEngine 的 PySide6」之上，断言 H.264/AAC 真的可用。
#
# 这是构建链路的验收门（CI 里不通过就不产 zip、不发 Release），也是本仓库既有探针方法学的
# 延续（.temp/webengine-probe/、.temp/probe-codecs.py 同形）：离屏起一个 QWebEnginePage，
# 在页面里问 MediaSource.isTypeSupported 与 canPlayType。
#
# 判据说明（为什么是这几条）：
#   * mse_h264  —— MSE 的 fMP4/H.264。bilibili 直播与分段 MP4 都走 MSE，这条是主判据；
#   * mse_aac   —— MSE 的 AAC 音频，同上；
#   * canPlay_h264 / canPlay_aac —— 渐进式 MP4 文件（<video src="x.mp4">）的能力；
#   * mse_vp8 / mse_opus —— 对照项：开源编解码器必须仍然可用，否则说明整条媒体管线被关掉，
#     而不是「私有编解码器被打开」，那种情况要当失败而不是成功；
#   * User-Agent 里的 QtWebEngine 版本 —— 确认页面跑的确实是这次自建的运行时，而不是
#     环境里残留的官方轮子（版本不符直接失败）。
#
# 退出码：0 通过，1 断言不通过，2 环境/初始化失败（起不来页就别谈编解码）。

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

JS_PROBE = r"""
(function () {
  var v = document.createElement('video');
  var a = document.createElement('audio');
  var ms = window.MediaSource;
  function mse(t) { return ms && ms.isTypeSupported ? ms.isTypeSupported(t) : false; }
  return JSON.stringify({
    mse_h264: mse('video/mp4; codecs="avc1.42E01E"'),
    mse_h264_high: mse('video/mp4; codecs="avc1.64001f"'),
    mse_aac: mse('audio/mp4; codecs="mp4a.40.2"'),
    mse_mp3: mse('audio/mpeg'),
    mse_vp8: mse('video/webm; codecs="vp8"'),
    mse_opus: mse('audio/webm; codecs="opus"'),
    canPlay_h264: v.canPlayType('video/mp4; codecs="avc1.42E01E"'),
    canPlay_aac: a.canPlayType('audio/mp4; codecs="mp4a.40.2"'),
    canPlay_mp3: a.canPlayType('audio/mpeg'),
    canPlay_vp8: v.canPlayType('video/webm; codecs="vp8"'),
    ua: navigator.userAgent
  });
})()
"""

CAN_PLAY_OK = ("maybe", "probably")


def build_report(probe: dict, expect_qt_version: str, pyside_version: str) -> dict:
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    check("mse_h264", probe.get("mse_h264") is True, f"got {probe.get('mse_h264')!r}")
    check("mse_aac", probe.get("mse_aac") is True, f"got {probe.get('mse_aac')!r}")
    check("mse_vp8_baseline", probe.get("mse_vp8") is True, f"got {probe.get('mse_vp8')!r}")
    check("mse_opus_baseline", probe.get("mse_opus") is True, f"got {probe.get('mse_opus')!r}")
    check(
        "canPlay_h264",
        str(probe.get("canPlay_h264", "")) in CAN_PLAY_OK,
        f"got {probe.get('canPlay_h264')!r}",
    )
    check(
        "canPlay_aac",
        str(probe.get("canPlay_aac", "")) in CAN_PLAY_OK,
        f"got {probe.get('canPlay_aac')!r}",
    )
    ua = str(probe.get("ua", ""))
    check(
        "runtime_version",
        f"QtWebEngine/{expect_qt_version}" in ua,
        f"expect QtWebEngine/{expect_qt_version} in UA, got {ua!r}",
    )

    return {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "python": sys.version.split()[0],
        "pyside6": pyside_version,
        "expected_qt_version": expect_qt_version,
        "probe": probe,
        "checks": checks,
        "ok": all(c["ok"] for c in checks),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="QtWebEngine 编解码验收探针")
    parser.add_argument("--expect-qt-version", default="6.8.3")
    parser.add_argument("--json-out", default="")
    parser.add_argument("--timeout-seconds", type=int, default=90)
    args = parser.parse_args()

    # 这个脚本的提示语是中文，而下面几行 print 是唯一一条会让整件事失败的非断言路径：
    # Windows 上 stdout 默认是 cp1252（CI 的 hostedtoolcache Python 就是），中文编不出去
    # 会抛 UnicodeEncodeError，把一次**已经通过**的验收变成退出码 1。第 6 轮就是这么红的：
    # CODEC-PROBE-JSON 里 mse_h264/mse_aac 全是 true，紧接着在"证据写入…"这句崩掉，
    # codec-probe.json 与检查清单都没写出来。把两个流都钉成 UTF-8，并把编不出的字符降级
    # 成占位符——打印永远不该有能力否决验收结论。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 老 Python 或已被重定向的流，忽略即可
            pass

    # 必须在 import QtWebEngine 之前设置：Chromium 只在初始化时读这些开关
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault(
        "QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --no-sandbox --disable-software-rasterizer"
    )
    os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")

    try:
        import PySide6
        from PySide6.QtCore import QCoreApplication, Qt, QTimer
        from PySide6.QtWebEngineCore import QWebEnginePage
        from PySide6.QtWidgets import QApplication
    except Exception as exc:  # noqa: BLE001 - 起不来就是环境问题，如实报出
        print(f"CODEC-PROBE-RESULT: ERROR 环境初始化失败：{exc!r}", flush=True)
        return 2

    pyside_version = PySide6.__version__
    print(f"CODEC-PROBE: PySide6 {pyside_version}, Python {sys.version.split()[0]}", flush=True)
    print(
        "CODEC-PROBE: QT_QPA_PLATFORM="
        f"{os.environ.get('QT_QPA_PLATFORM')} QTWEBENGINE_CHROMIUM_FLAGS="
        f"{os.environ.get('QTWEBENGINE_CHROMIUM_FLAGS')}",
        flush=True,
    )

    try:
        QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
        app = QApplication([])
        page = QWebEnginePage()
    except Exception as exc:  # noqa: BLE001
        print(f"CODEC-PROBE-RESULT: ERROR 创建 QApplication/QWebEnginePage 失败：{exc!r}", flush=True)
        return 2

    got: list[str] = []

    def on_result(result: object) -> None:
        got.append(str(result))
        app.quit()

    # PySide6 6.6 的 runJavaScript 没有 callable 重载之外的重载，只有 (str, int, object)
    page.setHtml("<html><body>codec-probe</body></html>")
    QTimer.singleShot(2500, lambda: page.runJavaScript(JS_PROBE, 0, on_result))
    QTimer.singleShot(args.timeout_seconds * 1000, app.quit)
    app.exec()

    if not got:
        print(
            f"CODEC-PROBE-RESULT: ERROR 探针 {args.timeout_seconds}s 内无结果"
            "（页面没跑起来或被沙箱拦下）",
            flush=True,
        )
        return 2

    print(f"CODEC-PROBE-JSON: {got[0]}", flush=True)
    try:
        probe = json.loads(got[0])
    except json.JSONDecodeError as exc:
        print(f"CODEC-PROBE-RESULT: ERROR 探针回吐不是 JSON：{exc!r}", flush=True)
        return 2

    report = build_report(probe, args.expect_qt_version, pyside_version)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        print(f"CODEC-PROBE: 证据写入 {args.json_out}", flush=True)

    for check in report["checks"]:
        mark = "PASS" if check["ok"] else "FAIL"
        print(f"CODEC-PROBE-CHECK: {mark} {check['name']} — {check['detail']}", flush=True)

    print(f"CODEC-PROBE-RESULT: {'PASS' if report['ok'] else 'FAIL'}", flush=True)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
