"""AIClientCenter 独立启动入口。"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
DEFAULT_CONFIG = PACKAGE_ROOT / "config" / "config.yaml"
EXAMPLE_CONFIG = PACKAGE_ROOT / "config" / "example.yaml"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from AIClientCenter.factory_engine import (  # noqa: E402
    build_manager, load_config, validate_config,
)
from AIClientCenter.core.observation import CallObservationSQLiteLogger  # noqa: E402
from AIClientCenter.web.dashboard import AIDashboardService  # noqa: E402


logger = logging.getLogger("AIClientCenterLauncher")


def resolve_config_path(config_path: Path | None):
    if config_path is not None:
        return config_path, None
    if DEFAULT_CONFIG.is_file():
        return DEFAULT_CONFIG, None
    if not EXAMPLE_CONFIG.is_file():
        raise ValueError(
            f"默认配置和示例均不存在: {DEFAULT_CONFIG}, {EXAMPLE_CONFIG}")
    return EXAMPLE_CONFIG, (
        f"未找到本地配置 {DEFAULT_CONFIG}；正在使用示例 {EXAMPLE_CONFIG}。\n"
        f"请复制后编辑: Copy-Item '{EXAMPLE_CONFIG}' '{DEFAULT_CONFIG}'"
    )


def _format_messages(title, messages):
    messages = list(messages)
    return "" if not messages else "\n".join(
        [title] + [f"  - {message}" for message in messages])


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="启动独立 AIClientCenter 管理页面。")
    parser.add_argument(
        "--config", type=Path,
        help=f"AI Client YAML 配置（默认: {DEFAULT_CONFIG}）",
    )
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--allow-remote", action="store_true")
    parser.add_argument("--disable-manual-calls", action="store_true")
    parser.add_argument(
        "--observation-db", type=Path,
        default=PACKAGE_ROOT / "_data" / "ai_client_observations.db",
    )
    parser.add_argument("--observation-retention-days", type=int, default=90)
    parser.add_argument("--disable-observation", action="store_true")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = parse_args(argv)
    if not 1 <= args.port <= 65535:
        print("错误: port 必须在 1..65535。", file=sys.stderr)
        return 2
    if args.host not in {"127.0.0.1", "localhost", "::1"} and not args.allow_remote:
        print("错误: 非本机监听必须显式传入 --allow-remote。", file=sys.stderr)
        return 2
    if args.observation_retention_days < 1:
        print("错误: observation-retention-days 必须为正整数。", file=sys.stderr)
        return 2

    try:
        config_path, notice = resolve_config_path(args.config)
        if notice:
            print(f"配置提示: {notice}", file=sys.stderr)
        clients, group_limits = load_config(config_path)
        errors, warnings = validate_config(clients, group_limits)
    except (OSError, ValueError) as exc:
        print(f"配置加载失败: {exc}", file=sys.stderr)
        return 2

    if warnings:
        print(_format_messages("配置警告:", warnings))
    if errors:
        print(_format_messages("配置错误:", errors), file=sys.stderr)
        return 2
    print(f"配置有效: {len(clients)} 个 Client, {len(group_limits)} 个分组限制。")
    if args.validate_only:
        return 0

    observer = None
    if not args.disable_observation:
        observer = CallObservationSQLiteLogger(
            str(args.observation_db),
            retention_days=args.observation_retention_days,
        )
    manager = build_manager(clients, group_limits, call_observer=observer)
    manager.start_monitoring()
    dashboard = AIDashboardService(
        manager, enable_manual_calls=not args.disable_manual_calls)
    try:
        print(f"AIClientCenter 已启动: http://{args.host}:{args.port}/")
        dashboard.run_standalone(host=args.host, port=args.port)
    except KeyboardInterrupt:
        logger.info("收到退出信号，正在停止 AIClientCenter。")
    finally:
        manager.stop_monitoring()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
