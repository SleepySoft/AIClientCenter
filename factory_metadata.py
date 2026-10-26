"""AI Client 工厂配置说明契约。

公共字段在这里统一定义；工厂显式参数由签名发现，Provider 可通过
``set_factory_metadata`` 补充 ``**kwargs`` 参数或覆盖说明。Web/CLI 只消费本模块输出。
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from typing import Any


METADATA_VERSION = 1

COMMON_FIELDS = {
    "clients": "Configured AI client entries. Each entry selects one factory preset.",
    "group_limits": "Maximum concurrent leases for each scheduling group.",
    "factory": "Factory preset name or an import reference in module:callable form.",
    "name": "Unique display name of this configured AI client.",
    "group": "Scheduling group used for concurrency limits and task routing.",
    "enable": "Whether this client entry is loaded. Disabled entries remain in YAML.",
    "tags": "Capability tags used for client selection, such as low_sensitivity or strong.",
    "availability": "Optional daily available or unavailable time windows and timezone.",
    "factory_parameters": "Parameters accepted by the selected factory preset. They are validated by the real factory before saving.",
}

KNOWN_FACTORY_FIELDS = {
    "api_base_url": "Base URL of the provider's OpenAI-compatible API.",
    "model": "Provider model identifier used by this client.",
    "models": "Ordered provider model identifiers used by a rotating client.",
    "api_key": "Provider API key stored directly in YAML. Prefer an environment variable when possible.",
    "api_key_env": "Name of the environment variable containing the provider API key.",
    "tokens": "Provider tokens stored directly in YAML. Prefer token_envs when possible.",
    "token_envs": "Environment variable names containing provider tokens.",
    "proxies": "HTTP and HTTPS proxy mapping passed to the provider adapter.",
    "request_overrides": "Additional request fields merged into every provider API call.",
    "model_rotate_per_times": "Number of calls made before rotating to the next model.",
    "token_rotate_per_times": "Number of calls made before rotating to the next token.",
    "enable_thinking": "Enable the provider's extended thinking or reasoning mode.",
    "gateway_token_env": "Environment variable containing the OpenClaw gateway token.",
}


def set_factory_metadata(factory: Callable, *, description: str = "",
                         fields: Mapping[str, str] | None = None) -> Callable:
    """给工厂附加可被配置 UI 和 CLI 读取的说明。"""

    factory.__ai_config_metadata__ = {
        "description": str(description or inspect.getdoc(factory) or "").strip(),
        "fields": {str(key): str(value) for key, value in (fields or {}).items()},
    }
    return factory


def inherit_factory_metadata(factory: Callable, delegate: Callable) -> Callable:
    """让只接收 ``**kwargs`` 的 preset 复用其底层 builder 参数说明。"""

    delegate_metadata = factory_config_metadata(delegate)
    return set_factory_metadata(
        factory,
        description=str(inspect.getdoc(factory) or inspect.getdoc(delegate) or ""),
        fields={
            name: item["description"]
            for name, item in delegate_metadata["fields"].items()
        },
    )


def factory_config_metadata(factory: Callable) -> dict[str, Any]:
    declared = dict(getattr(factory, "__ai_config_metadata__", {}) or {})
    overrides = dict(declared.get("fields") or {})
    fields: dict[str, dict[str, Any]] = {}
    try:
        parameters = inspect.signature(factory).parameters.values()
    except (TypeError, ValueError):
        parameters = ()
    for parameter in parameters:
        if parameter.name in {"name", "group"} or parameter.kind in {
                inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}:
            continue
        default = None if parameter.default is inspect.Parameter.empty else parameter.default
        if default is not None and not isinstance(
                default, (str, int, float, bool, list, dict, tuple)):
            default = repr(default)
        fields[parameter.name] = {
            "description": overrides.pop(
                parameter.name,
                KNOWN_FACTORY_FIELDS.get(
                    parameter.name,
                    f"Parameter passed to the {getattr(factory, '__name__', 'selected')} factory.")),
            "required": parameter.default is inspect.Parameter.empty,
            "default": default,
        }
    for name, description in overrides.items():
        fields[name] = {"description": description, "required": False, "default": None}
    return {
        "description": str(
            declared.get("description") or inspect.getdoc(factory) or "").strip(),
        "fields": fields,
    }


def configuration_metadata(factories: Mapping[str, Callable]) -> dict[str, Any]:
    return {
        "version": METADATA_VERSION,
        "common_fields": dict(COMMON_FIELDS),
        "factories": {
            name: factory_config_metadata(factory)
            for name, factory in sorted(factories.items())
        },
    }
