"""BestDPI Finder Windows entry point; no third-party runtime dependencies."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def main() -> int:
    """Run the desktop application or its isolated native smoke test."""
    parser = argparse.ArgumentParser(description="BestDPI Finder · Windows FPS 灵敏度测评")
    parser.add_argument("--smoke-test", action="store_true", help="执行原生窗口/捕获释放自测，不生成测评记录")
    parser.add_argument("--smoke-report", type=Path, help="自测证据JSON路径，仅用于--smoke-test")
    parser.add_argument("--perf-report", type=Path,
                        help="退出时写入帧耗时/帧间隔/输入包率诊断JSON；正常运行不启用")
    parser.add_argument("--autotest", action="store_true", help="启用 QA-only 自主真人行为模拟与素材采集")
    parser.add_argument("--seed", type=int, default=20260928, help="自主测试可复现随机种子")
    parser.add_argument("--autotest-output", type=Path, default=Path("autotest_output"),
                        help="自主测试输出目录")
    parser.add_argument("--autotest-fast", action="store_true",
                        help="QA 开发验证用快速模式；不能用于正式素材采集")
    args: argparse.Namespace = parser.parse_args()
    if args.smoke_report and not args.smoke_test:
        parser.error("--smoke-report requires --smoke-test")
    if (args.seed != 20260928 or args.autotest_output != Path("autotest_output") or args.autotest_fast) and not args.autotest:
        parser.error("--seed/--autotest-output/--autotest-fast require --autotest")
    if args.autotest and args.smoke_test:
        parser.error("--autotest and --smoke-test are separate run modes")
    if sys.platform != "win32":
        print("桌面应用需要 Windows 10/11。纯函数测试可在其他平台运行。", file=sys.stderr)
        return 2
    from adaptive_ui import AdaptiveApp
    app = AdaptiveApp(smoke_test=args.smoke_test, perf_report=args.perf_report,
                      autotest=args.autotest, autotest_seed=args.seed,
                      autotest_output=args.autotest_output, autotest_fast=args.autotest_fast)
    result = app.run()
    if args.smoke_report:
        args.smoke_report.write_text(json.dumps(app.smoke_result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
