"""AIClientCenter 的独立启动入口。

默认加载本模块 config/config.py；首次使用尚未创建该文件时，临时加载
config/example.py，并提示复制样例。启动时只验证 Python 配置和本机可用的 CLI
Harness，不会自动发送模型请求。启动后访问仪表盘的 Manual Call 页面，可选择
指定 Client 进行真实对话测试。

示例：
    python -m AIClientCenter --validate-only
    python -m AIClientCenter.cli.launcher --port 8010
"""

import argparse
import importlib.util
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


# launcher 位于 AIClientCenter/cli/。独立模块配置随包保存，避免依赖 IIS 的
# `_config`；父项目根目录仅用于让直接执行 launcher 文件时也能找到包。
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
DEFAULT_CONFIG = PACKAGE_ROOT / "config" / "config.py"
EXAMPLE_CONFIG = PACKAGE_ROOT / "config" / "example.py"

# 允许直接执行文件和 python -m 两种方式。
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from AIClientCenter.core.manager import AIClientManager, BaseAIClient  # noqa: E402
from AIClientCenter.web.dashboard import AIDashboardService  # noqa: E402


logger = logging.getLogger("AIClientCenterLauncher")


def resolve_config_path(config_path: Optional[Path]) -> Tuple[Path, Optional[str]]:
    """选择显式配置，或选择本模块配置及其首次使用的样例回退。"""
    if config_path is not None:
        return config_path, None
    if DEFAULT_CONFIG.is_file():
        return DEFAULT_CONFIG, None
    if not EXAMPLE_CONFIG.is_file():
        raise ValueError(f"默认配置和样例均不存在：{DEFAULT_CONFIG}，{EXAMPLE_CONFIG}")
    return EXAMPLE_CONFIG, (
        f"未找到本地配置 {DEFAULT_CONFIG}；正在使用样例 {EXAMPLE_CONFIG}。\n"
        f"请复制样例后编辑：Copy-Item '{EXAMPLE_CONFIG}' '{DEFAULT_CONFIG}'"
    )


def load_config(config_path: Path) -> Tuple[Dict[str, BaseAIClient], Dict[str, int]]:
    """加载一个 ai_client_config.py 文件并读取两个公开配置对象。"""
    config_path = config_path.resolve()
    if not config_path.is_file():
        raise ValueError(f"配置文件不存在：{config_path}")

    module_name = "_ai_client_center_runtime_config"
    spec = importlib.util.spec_from_file_location(module_name, config_path)
    if not spec or not spec.loader:
        raise ValueError(f"无法加载配置文件：{config_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise ValueError(f"配置文件执行失败：{type(exc).__name__}: {exc}") from exc
    finally:
        sys.modules.pop(module_name, None)

    clients = getattr(module, "AI_CLIENTS", None)
    group_limits = getattr(module, "AI_CLIENT_LIMIT", None)
    if not isinstance(clients, dict):
        raise ValueError("配置必须定义 AI_CLIENTS 字典。")
    if not isinstance(group_limits, dict):
        raise ValueError("配置必须定义 AI_CLIENT_LIMIT 字典。")
    return clients, group_limits


def validate_config(clients: Dict[str, BaseAIClient],
                    group_limits: Dict[str, int]) -> Tuple[List[str], List[str]]:
    """进行不触网的结构和本机 Harness 可执行文件校验。"""
    errors: List[str] = []
    warnings: List[str] = []
    seen_names = set()

    for key, client in clients.items():
        if not isinstance(key, str) or not key.strip():
            errors.append("AI_CLIENTS 的键必须是非空字符串。")
        if not isinstance(client, BaseAIClient):
            errors.append(f"Client {key!r} 不是 BaseAIClient 实例。")
            continue
        if not client.name:
            errors.append(f"Client {key!r} 的 name 不能为空。")
        elif client.name in seen_names:
            errors.append(f"Client name 重复：{client.name!r}。")
        seen_names.add(client.name)
        if not isinstance(client.group_id, str) or not client.group_id:
            errors.append(f"Client {client.name!r} 的 group_id 必须是非空字符串。")

        # AgentCLIClient 使用 cli_command；只做本机可执行性检查，不发起 CLI 调用。
        cli_command = getattr(client, "cli_command", None)
        if cli_command:
            is_path = os.path.sep in cli_command or (os.path.altsep and os.path.altsep in cli_command)
            found = os.path.isfile(cli_command) if is_path else shutil.which(cli_command)
            if not found:
                warnings.append(
                    f"Harness {client.name!r} 的 CLI 不在当前 PATH：{cli_command!r}；"
                    "启动仍可继续，但手动调用会失败。")

        try:
            decision = client.get_budget_decision()
            if not decision.allowed:
                warnings.append(f"Client {client.name!r} 当前被本地预算阻断：{decision.reason}")
        except Exception as exc:
            errors.append(f"Client {client.name!r} 的预算策略无效：{exc}")

    for group_id, limit in group_limits.items():
        if not isinstance(group_id, str) or not group_id:
            errors.append("AI_CLIENT_LIMIT 的 group_id 必须是非空字符串。")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            errors.append(f"AI_CLIENT_LIMIT[{group_id!r}] 必须是正整数，当前为 {limit!r}。")

    if not clients:
        warnings.append("AI_CLIENTS 为空；仪表盘可启动，但不能执行对话测试。")
    return errors, warnings


def build_manager(clients: Dict[str, BaseAIClient],
                  group_limits: Dict[str, int]) -> AIClientManager:
    """把已验证的配置组装为独立运行的 manager。"""
    manager = AIClientManager()
    for client in clients.values():
        manager.register_client(client)
    for group_id, limit in group_limits.items():
        manager.set_group_limit(group_id, limit)
    return manager


def _format_messages(title: str, messages: Iterable[str]) -> str:
    messages = list(messages)
    if not messages:
        return ""
    return "\n".join([title] + [f"  - {message}" for message in messages])


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="启动独立的 AIClientCenter 管理与手动测试页。")
    parser.add_argument("--config", type=Path,
                        help=f"AI Client Python 配置文件（默认：{DEFAULT_CONFIG}；不存在时使用样例）")
    parser.add_argument("--validate-only", action="store_true",
                        help="仅加载并验证配置，不启动 HTTP 服务。")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认仅本机。")
    parser.add_argument("--port", type=int, default=8000, help="监听端口，默认 8000。")
    parser.add_argument("--allow-remote", action="store_true",
                        help="允许非本机监听地址；手动调用页会消耗额度，请自行做好访问控制。")
    parser.add_argument("--disable-manual-calls", action="store_true",
                        help="只启动监控页，禁用手动对话 API。")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = parse_args(argv)
    if args.port < 1 or args.port > 65535:
        print("错误：port 必须在 1..65535。", file=sys.stderr)
        return 2
    if args.host not in {"127.0.0.1", "localhost", "::1"} and not args.allow_remote:
        print("错误：非本机监听必须显式传入 --allow-remote。", file=sys.stderr)
        return 2

    try:
        config_path, config_notice = resolve_config_path(args.config)
        if config_notice:
            print(f"配置提示：{config_notice}", file=sys.stderr)
        clients, group_limits = load_config(config_path)
    except ValueError as exc:
        print(f"配置加载失败：{exc}", file=sys.stderr)
        return 2

    errors, warnings = validate_config(clients, group_limits)
    warning_text = _format_messages("配置警告：", warnings)
    if warning_text:
        print(warning_text)
    if errors:
        print(_format_messages("配置错误：", errors), file=sys.stderr)
        return 2

    print(f"配置有效：{len(clients)} 个 Client，{len(group_limits)} 个分组限额。")
    if args.validate_only:
        return 0

    manager = build_manager(clients, group_limits)
    manager.start_monitoring()
    dashboard = AIDashboardService(manager, enable_manual_calls=not args.disable_manual_calls)
    try:
        suffix = "（手动调用已禁用）" if args.disable_manual_calls else ""
        print(f"AIClientCenter 已启动：http://{args.host}:{args.port}/ {suffix}")
        dashboard.run_standalone(host=args.host, port=args.port)
    except KeyboardInterrupt:
        logger.info("收到退出信号，正在停止 AIClientCenter。")
    finally:
        manager.stop_monitoring()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
