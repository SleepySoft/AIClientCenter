"""NVIDIA Build 免费原型端点的大模型客户端配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from AIClientCenter.core.credentials import APIKeyConfig, resolve_api_keys
from AIClientCenter.core.manager import (
    BaseAIClient,
    CLIENT_PRIORITY_CONSUMABLES,
    CLIENT_PRIORITY_FREEBIE,
)
from AIClientCenter.core.rate_limit import EstimatedRateLimiter
from AIClientCenter.providers.openai_clients import RateLimitedOpenAIClient
from AIClientCenter.providers.openai_compatible import create_nvidia_client


NVIDIA_GROUP_PREFIX = "nvidia_free:"
DEFAULT_ACCOUNT_RPM = 40
DEFAULT_SAFETY_RATIO = 0.6


@dataclass(frozen=True)
class NvidiaModelSpec:
    priority: int
    note: str


# 仅登记当前 NVIDIA Build 标记为 Free Endpoint，且达到 IIS 大模型质量门槛的模型。
# 模型上下线频繁；Deprecated 模型不得继续留在默认列表。
NVIDIA_FREE_MODEL_SPECS: dict[str, NvidiaModelSpec] = {
    "nvidia/nemotron-3-ultra-550b-a55b": NvidiaModelSpec(
        CLIENT_PRIORITY_FREEBIE,
        "561B / 1M context; primary Event V4 extraction model",
    ),
    "z-ai/glm-5.3": NvidiaModelSpec(
        CLIENT_PRIORITY_FREEBIE,
        "753B MoE / 1M context; primary multilingual model",
    ),
    "moonshotai/kimi-k3": NvidiaModelSpec(
        CLIENT_PRIORITY_CONSUMABLES,
        "2.8T MoE / 1M context; experimental multilingual reserve",
    ),
    "z-ai/glm-5.3-flash": NvidiaModelSpec(
        CLIENT_PRIORITY_CONSUMABLES,
        "320B MoE; throughput-oriented reserve",
    ),
    "deepseek-ai/deepseek-v4.1-flash": NvidiaModelSpec(
        CLIENT_PRIORITY_CONSUMABLES,
        "552B MoE / 8B active; experimental throughput reserve",
    ),
}

DEFAULT_MODELS = (
    "nvidia/nemotron-3-ultra-550b-a55b",
    "z-ai/glm-5.3",
    "moonshotai/kimi-k3",
)


def _positive_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value < 1:
        raise ValueError(f"{name} 必须为正整数")
    return value


def _ratio() -> float:
    value = float(os.getenv("NVIDIA_QUOTA_SAFETY_RATIO", str(DEFAULT_SAFETY_RATIO)))
    if not 0 < value <= 1:
        raise ValueError("NVIDIA_QUOTA_SAFETY_RATIO 必须在 (0, 1] 范围内")
    return value


def _selected_models() -> list[str]:
    raw = os.getenv("NVIDIA_MODELS", "").strip()
    models = [item.strip() for item in raw.split(",") if item.strip()] \
        if raw else list(DEFAULT_MODELS)
    unknown = [model for model in models if model not in NVIDIA_FREE_MODEL_SPECS]
    if unknown:
        raise ValueError(
            "NVIDIA_MODELS 包含未登记、低于质量门槛或已弃用的模型: "
            + ", ".join(unknown)
        )
    return models


def _rate_limit_db_path(configured: Optional[str] = None) -> Path:
    value = (configured or os.getenv("NVIDIA_RATE_LIMIT_DB", "")).strip()
    if value:
        return Path(value).expanduser().resolve()
    return Path(__file__).resolve().parents[2] / "_data" / "ai_rate_limits.sqlite3"


def build_nvidia_clients(
        api_keys: APIKeyConfig = None,
        *,
        storage_path: Optional[str] = None,
) -> dict[str, BaseAIClient]:
    """为每个账号构造模型池；账号内模型共享 RPM 账本并固定单并发。"""

    credentials = resolve_api_keys(api_keys, env_name="NVIDIA_API_KEY")
    if not credentials:
        return {}

    account_rpm = _positive_int("NVIDIA_ACCOUNT_RPM", DEFAULT_ACCOUNT_RPM)
    ratio = _ratio()
    database = _rate_limit_db_path(storage_path)
    models = _selected_models()
    clients: dict[str, BaseAIClient] = {}
    for credential in credentials:
        limiter = EstimatedRateLimiter(
            rpm=account_rpm,
            utilization_ratio=ratio,
            storage_path=database,
            storage_namespace=f"nvidia:{credential.quota_id}",
        )
        group_id = f"{NVIDIA_GROUP_PREFIX}{credential.quota_id}"
        for model in models:
            spec = NVIDIA_FREE_MODEL_SPECS[model]
            model_key = model.replace("/", "_").replace(".", "_").replace("-", "_")
            key = f"nvidia_{credential.alias}_{model_key}"
            clients[key] = RateLimitedOpenAIClient(
                name=f"NVIDIA [{credential.alias}] {model}",
                openai_api=create_nvidia_client(
                    token=credential.api_key,
                    model=model,
                ),
                rate_limiter=limiter,
                priority=spec.priority,
                group_id=group_id,
                default_available=True,
                observation_context={
                    "provider": "nvidia",
                    "account_alias": credential.alias,
                    "preset": "nvidia_free",
                },
            )
    return clients

def create_nvidia_free_preset(
        *, name: str, group: str, **kwargs,
) -> dict[str, BaseAIClient]:
    """供配置工厂引擎调用的 NVIDIA 免费端点预置。"""

    clients = build_nvidia_clients(**kwargs)
    for client in clients.values():
        client.group_id = group
        client.name = name if len(clients) == 1 else f"{name} / {client.name}"
    return clients


def nvidia_group_limits(clients: dict[str, BaseAIClient]) -> dict[str, int]:
    """为直接使用 provider builder 的调用方生成按账号分组限制。"""

    return {
        client.group_id: 1
        for client in clients.values()
        if client.group_id.startswith(NVIDIA_GROUP_PREFIX)
    }
