"""AIClientCenter 独立配置引擎。"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import yaml

from AIClientCenter.core.manager import AIClientManager, BaseAIClient
from AIClientCenter.core.preset import ClientPreset


Factory = Callable[..., BaseAIClient | Mapping[str, BaseAIClient] | ClientPreset]
RESERVED_FIELDS = frozenset({"factory", "name", "group"})


class ClientFactoryRegistry:
    def __init__(self, factories: Mapping[str, Factory] | None = None):
        self._factories: dict[str, Factory] = {}
        for name, factory in (factories or {}).items():
            self.register(name, factory)

    def register(self, name: str, factory: Factory) -> None:
        name = str(name or "").strip()
        if not name or not callable(factory):
            raise ValueError("AI Client 工厂必须具有非空名称且可调用。")
        if name in self._factories:
            raise ValueError(f"AI Client 工厂重复注册: {name}")
        self._factories[name] = factory

    def get(self, name: str) -> Factory:
        if name not in self._factories:
            available = ", ".join(sorted(self._factories)) or "（无）"
            raise ValueError(f"未知 AI Client 工厂: {name}；可用工厂: {available}")
        return self._factories[name]


def default_factory_registry() -> ClientFactoryRegistry:
    from AIClientCenter.presets import FACTORIES
    return ClientFactoryRegistry(FACTORIES)


def load_config(config_path, *, registry: ClientFactoryRegistry | None = None):
    """加载 YAML 工厂配置；高级定制可提供完整 Python 配置。"""
    path = Path(config_path).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"AI Client 配置文件不存在: {path}")
    if path.suffix.lower() in {".yaml", ".yml"}:
        return _load_yaml(path, registry or default_factory_registry())
    if path.suffix.lower() == ".py":
        return _load_python(path)
    raise ValueError("AI Client 配置仅支持 .yaml/.yml 或自定义 .py 文件。")


def _load_yaml(path: Path, registry: ClientFactoryRegistry):
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
    for index, entry in enumerate(entries):
        built = _build_entry(entry, index, registry)
        duplicate = clients.keys() & built.keys()
        if duplicate:
            raise ValueError("AI Client 名称重复: " + ", ".join(sorted(duplicate)))
        clients.update(built)
    return clients, _group_limits(raw.get("group_limits") or {})


def _build_entry(entry: Any, index: int, registry: ClientFactoryRegistry):
    location = f"clients[{index}]"
    if not isinstance(entry, dict):
        raise ValueError(f"{location} 必须是对象。")
    factory_name = _required(entry, "factory", location)
    name = _required(entry, "name", location)
    group = _required(entry, "group", location)
    kwargs = {key: value for key, value in entry.items() if key not in RESERVED_FIELDS}
    try:
        result = registry.get(factory_name)(name=name, group=group, **kwargs)
    except Exception as exc:
        raise ValueError(
            f"{location} 工厂 {factory_name!r} 构造失败: {type(exc).__name__}: {exc}"
        ) from exc
    if isinstance(result, ClientPreset):
        result = result.clients
    if isinstance(result, BaseAIClient):
        result = {name: result}
    if not isinstance(result, Mapping):
        raise ValueError(f"{location} 工厂返回值不是 Client、Client 映射或 ClientPreset。")
    values = dict(result)
    built: dict[str, BaseAIClient] = {}
    for key, client in values.items():
        if not isinstance(client, BaseAIClient):
            raise ValueError(f"{location} 工厂结果 {key!r} 不是 BaseAIClient。")
        if client.group_id != group:
            raise ValueError(
                f"{location} 生成的 {key!r} 分组为 {client.group_id!r}，应为 {group!r}。")
        output_key = name if len(values) == 1 else f"{name}:{key}"
        built[output_key] = client
    return built


def _load_python(path: Path):
    module_name = "_ai_client_center_custom_config"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if not spec or not spec.loader:
        raise ValueError(f"无法加载 AI Client Python 配置: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        builder = getattr(module, "build_ai_client_config", None)
        if not callable(builder):
            raise ValueError("自定义配置必须定义 build_ai_client_config()。")
        result = builder()
    finally:
        sys.modules.pop(module_name, None)
    if isinstance(result, ClientPreset):
        return dict(result.clients), _group_limits(result.group_limits)
    if isinstance(result, tuple) and len(result) == 2:
        return dict(result[0]), _group_limits(result[1])
    raise ValueError("build_ai_client_config() 必须返回 ClientPreset 或二元组。")


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


def build_manager_from_config(config_path, **manager_kwargs):
    clients, group_limits = load_config(config_path)
    errors, _warnings = validate_config(clients, group_limits)
    if errors:
        raise ValueError("AI Client 配置无效: " + "; ".join(errors))
    return build_manager(clients, group_limits, **manager_kwargs)
