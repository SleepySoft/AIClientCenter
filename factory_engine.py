"""从声明式配置组装 AI Client。

内置 ``factory`` 名称直接指向 ``providers/``（以及本机 harness）里的预置构造
函数。本文件只负责读取配置、解析工厂、合并结果和执行公共校验，不包含任何厂商
参数或 Client 实现。

扩展方式有两种，均不需要修改本文件：

1. 在 YAML 中写可导入路径：``factory: my_package.clients:create_client``；
   单文件工厂可写 ``factory: ./custom_clients.py:create_client``，文件路径始终
   相对于当前 YAML，而不是进程工作目录；
2. 在宿主程序中显式注入：
   ``ClientFactoryEngine({"custom": create_client}).load(config_path)``。

自定义工厂必须接受关键字参数 ``name``、``group`` 以及自己的配置参数，并返回一个
``BaseAIClient`` 或 ``Mapping[str, BaseAIClient]``。YAML 中的 ``tags`` 与
``availability`` 由外部调度器消费，不传给工厂；其余非公共字段原样传入工厂。
``enable`` 默认为 ``true``，设为 ``false`` 时保留配置但不创建 Client。
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import sys
import types
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import yaml

from AIClientCenter.core.manager import AIClientManager, BaseAIClient
from AIClientCenter.core.availability import ClientAvailabilitySchedule


ClientFactory = Callable[..., BaseAIClient | Mapping[str, BaseAIClient]]
RESERVED_FIELDS = frozenset({
    "factory", "name", "group", "enable", "tags", "availability",
})


def builtin_factories() -> dict[str, ClientFactory]:
    """返回内置预置的新映射；调用者修改它不会影响其他引擎。"""

    from AIClientCenter.harness.cli import (
        create_codex_preset, create_kimi_preset,
    )
    from AIClientCenter.providers.gemini_free import create_gemini_free_preset
    from AIClientCenter.providers.groq import create_groq_free_preset
    from AIClientCenter.providers.nvidia import create_nvidia_free_preset
    from AIClientCenter.providers.openai_clients import (
        create_openai_compatible_preset, create_self_rotating_openai_preset,
    )
    from AIClientCenter.providers.openclaw import create_openclaw_preset
    from AIClientCenter.providers.zhipu import create_zhipu_preset

    return {
        "codex": create_codex_preset,
        "gemini_free": create_gemini_free_preset,
        "groq_free": create_groq_free_preset,
        "kimi": create_kimi_preset,
        "nvidia_free": create_nvidia_free_preset,
        "openai_compatible": create_openai_compatible_preset,
        "self_rotating_openai": create_self_rotating_openai_preset,
        "openclaw": create_openclaw_preset,
        "zhipu": create_zhipu_preset,
    }


class ClientFactoryEngine:
    """加载 AI Client YAML，并通过预置或外部工厂生成 Client。"""

    def __init__(
            self,
            factories: Mapping[str, ClientFactory] | None = None,
            *,
            include_builtins: bool = True,
    ):
        self._factories = builtin_factories() if include_builtins else {}
        for name, factory in (factories or {}).items():
            self.register_factory(name, factory)

    def register_factory(
            self, name: str, factory: ClientFactory, *, replace: bool = False,
    ) -> None:
        """注册宿主程序提供的短名称工厂。"""

        normalized = str(name or "").strip()
        if not normalized or not callable(factory):
            raise ValueError("AI Client 工厂必须具有非空名称且可调用。")
        if normalized in self._factories and not replace:
            raise ValueError(f"AI Client 工厂重复注册: {normalized}")
        self._factories[normalized] = factory

    def resolve_factory(
            self, reference: str, *, config_dir: Path | None = None,
    ) -> ClientFactory:
        """解析短名称、模块路径或相对于 YAML 的 Python 文件路径。"""

        if reference in self._factories:
            return self._factories[reference]
        if ":" not in reference:
            available = ", ".join(sorted(self._factories)) or "（无）"
            raise ValueError(
                f"未知 AI Client 工厂: {reference}；可用工厂: {available}；"
                "自定义工厂可使用 module:callable。"
            )
        # 从右侧切分，以兼容 ``C:\\path\\factory.py:create_client``。
        source, attribute_path = reference.rsplit(":", 1)
        if not source or not attribute_path:
            raise ValueError(f"无效的 AI Client 工厂导入路径: {reference}")
        try:
            if _is_python_file_reference(source):
                if config_dir is None:
                    raise ValueError("解析 Python 文件工厂时缺少配置目录。")
                module_path = Path(source)
                if not module_path.is_absolute():
                    module_path = config_dir / module_path
                factory: Any = _load_factory_module(module_path.resolve())
            else:
                factory = importlib.import_module(source)
            for attribute in attribute_path.split("."):
                factory = getattr(factory, attribute)
        except (ImportError, AttributeError, OSError, ValueError) as exc:
            raise ValueError(
                f"无法导入 AI Client 工厂 {reference!r}: {exc}") from exc
        if not callable(factory):
            raise ValueError(f"AI Client 工厂 {reference!r} 不可调用。")
        return factory

    def load(self, config_path) -> tuple[dict[str, BaseAIClient], dict[str, int]]:
        path = Path(config_path).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"AI Client 配置文件不存在: {path}")
        if path.suffix.lower() not in {".yaml", ".yml"}:
            raise ValueError("AI Client 配置仅支持 .yaml/.yml 文件。")
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise ValueError(f"AI Client YAML 读取失败: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError("AI Client YAML 顶层必须是对象。")
        unknown = set(raw).difference({"clients", "group_limits"})
        if unknown:
            raise ValueError("未知顶层字段: " + ", ".join(sorted(unknown)))
        entries = raw.get("clients") or []
        if not isinstance(entries, list):
            raise ValueError("clients 必须是列表。")

        clients: dict[str, BaseAIClient] = {}
        built: dict[str, BaseAIClient] = {}
        try:
            for index, entry in enumerate(entries):
                built = self._build_entry(entry, index, config_dir=path.parent)
                duplicate = clients.keys() & built.keys()
                if duplicate:
                    raise ValueError("AI Client 名称重复: " + ", ".join(sorted(duplicate)))
                clients.update(built)
                built = {}
            group_limits = _group_limits(raw.get("group_limits") or {})
        except Exception:
            # 热重载在构造完整配置前失败时，这批对象永远不会进入 Manager；
            # 及时关闭已创建的 Session/连接，且不触碰当前生产池。
            for client in [*clients.values(), *built.values()]:
                AIClientManager._dispose_client(client)
            raise
        return clients, group_limits

    def _build_entry(
            self, entry: Any, index: int, *, config_dir: Path,
    ) -> dict[str, BaseAIClient]:
        location = f"clients[{index}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{location} 必须是对象。")
        enabled = entry.get("enable", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"{location}.enable 必须是布尔值。")
        if not enabled:
            return {}
        factory_name = _required(entry, "factory", location)
        name = _required(entry, "name", location)
        group = _required(entry, "group", location)
        kwargs = {
            key: value for key, value in entry.items()
            if key not in RESERVED_FIELDS
        }
        try:
            result = self.resolve_factory(
                factory_name, config_dir=config_dir)(
                name=name, group=group, **kwargs)
        except Exception as exc:
            raise ValueError(
                f"{location} 工厂 {factory_name!r} 构造失败: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if isinstance(result, BaseAIClient):
            result = {name: result}
        if not isinstance(result, Mapping):
            raise ValueError(
                f"{location} 工厂返回值不是 BaseAIClient 或 Client 映射。")

        # tags：能力/敏感性标签（如 low_sensitivity/strong），不属于工厂构造参数，
        # 由引擎在构造后直接挂载，供调度侧按标签过滤。
        tags = entry.get("tags") or []
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            raise ValueError(f"{location}.tags 必须是字符串列表。")
        try:
            availability = ClientAvailabilitySchedule.from_config(
                entry.get("availability"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{location}.{exc}") from exc

        values = dict(result)
        built: dict[str, BaseAIClient] = {}
        for key, client in values.items():
            if not isinstance(client, BaseAIClient):
                raise ValueError(f"{location} 工厂结果 {key!r} 不是 BaseAIClient。")
            if tags:
                client.tags = frozenset(t.strip() for t in tags if t.strip())
            if availability is not None:
                client.availability_schedule = availability
            if client.group_id != group:
                raise ValueError(
                    f"{location} 生成的 {key!r} 分组为 {client.group_id!r}，"
                    f"应为 {group!r}。"
                )
            output_key = name if len(values) == 1 else f"{name}:{key}"
            built[output_key] = client
        return built


def load_config(config_path, *, factories=None, include_builtins=True):
    """使用内置预置及可选外部工厂加载配置。"""

    return ClientFactoryEngine(
        factories, include_builtins=include_builtins).load(config_path)


def _is_python_file_reference(source: str) -> bool:
    return source.endswith(".py") or source.startswith(("./", "../", ".\\", "..\\"))


def _load_factory_module(path: Path):
    if not path.is_file():
        raise ValueError(f"自定义工厂文件不存在: {path}")
    directory_digest = hashlib.sha256(
        str(path.parent).encode("utf-8")).hexdigest()[:16]
    file_digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
    package_name = f"_ai_client_factories_{directory_digest}"
    module_name = f"{package_name}.factory_{file_digest}"
    package = sys.modules.get(package_name)
    if package is None:
        package = types.ModuleType(package_name)
        package.__package__ = package_name
        package.__path__ = [str(path.parent)]
        sys.modules[package_name] = package
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"无法创建自定义工厂模块: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def _group_limits(raw):
    if not isinstance(raw, dict):
        raise ValueError("group_limits 必须是对象。")
    result = {}
    for group, limit in raw.items():
        if not isinstance(group, str) or not group.strip():
            raise ValueError("group_limits 的分组名称必须是非空字符串。")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError(f"group_limits[{group!r}] 必须是正整数。")
        result[group] = limit
    return result


def _required(mapping, key, location):
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{location}.{key} 必须是非空字符串。")
    return value.strip()


def validate_config(clients, group_limits):
    errors, warnings, names, groups = [], [], set(), set()
    for key, client in clients.items():
        if not isinstance(client, BaseAIClient):
            errors.append(f"Client {key!r} 不是 BaseAIClient 实例。")
            continue
        if not client.name or client.name in names:
            errors.append(f"Client name 为空或重复: {client.name!r}。")
        names.add(client.name)
        if not isinstance(client.group_id, str) or not client.group_id:
            errors.append(f"Client {client.name!r} 的 group_id 无效。")
        else:
            groups.add(client.group_id)
        try:
            decision = client.get_budget_decision()
            if not decision.allowed:
                warnings.append(f"Client {client.name!r} 被预算阻断: {decision.reason}")
        except Exception as exc:
            errors.append(f"Client {client.name!r} 的预算策略无效: {exc}")
        schedule = getattr(client, "availability_schedule", None)
        if schedule is not None and not getattr(schedule, "timezone_explicit", True):
            warnings.append(
                f"Client {client.name!r} 的 availability 未配置 timezone，"
                f"使用系统本地时区 {schedule.timezone_name!r}。")
    for group in sorted(groups.difference(group_limits)):
        errors.append(f"Client 分组 {group!r} 未配置并发限制。")
    for group in sorted(set(group_limits).difference(groups)):
        warnings.append(f"分组 {group!r} 当前没有 Client 使用。")
    if not clients:
        warnings.append("配置没有生成任何 AI Client。")
    return errors, warnings


def build_manager(clients, group_limits, **manager_kwargs):
    manager = AIClientManager(**manager_kwargs)
    for client in clients.values():
        manager.register_client(client)
    for group, limit in group_limits.items():
        manager.set_group_limit(group, limit)
    return manager


def build_manager_from_config(
        config_path, *, factories=None, **manager_kwargs,
):
    clients, group_limits = load_config(config_path, factories=factories)
    errors, _warnings = validate_config(clients, group_limits)
    if errors:
        raise ValueError("AI Client 配置无效: " + "; ".join(errors))
    return build_manager(clients, group_limits, **manager_kwargs)


class ReloadInProgressError(RuntimeError):
    """同一 Manager 已有配置重载正在执行。"""


class AIClientConfigReloader:
    """从 YAML 重建全部 Client，并在成功后原子切换 Manager 的 Client 池。"""

    def __init__(self, manager, config_path, *, factories=None,
                 allow_empty: bool = False, include_builtins: bool = True):
        self.manager = manager
        self.config_path = Path(config_path).expanduser().resolve()
        self.factories = factories
        self.allow_empty = allow_empty
        self.include_builtins = include_builtins
        self._reload_lock = threading.Lock()

    def reload(self) -> dict[str, Any]:
        # 防止用户连点导致并行构造两套 Client（部分自定义工厂可能启动线程）。
        if not self._reload_lock.acquire(blocking=False):
            raise ReloadInProgressError("AI Client 配置正在重新加载，请稍后重试。")
        clients = None
        try:
            # 所有可能失败的 IO、YAML 解析、工厂构造和校验均在 Manager 锁外完成。
            # 任何异常都会保留当前池，不影响 IIS 的现有调用。
            clients, group_limits = load_config(
                self.config_path,
                factories=self.factories,
                include_builtins=self.include_builtins,
            )
            errors, warnings = validate_config(clients, group_limits)
            if errors:
                for client in clients.values():
                    self.manager._dispose_client(client)
                raise ValueError("AI Client 配置无效: " + "; ".join(errors))
            if not clients and not self.allow_empty:
                raise ValueError(
                    "AI Client 配置没有生成任何 Client；已保留当前 Client 池。")

            result = self.manager.replace_client_pool(clients, group_limits)
            result.update({
                "config_path": str(self.config_path),
                "warnings": warnings,
            })
            return result
        finally:
            self._reload_lock.release()
