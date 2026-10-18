"""Gemini AI Studio 免费层推荐配置。"""

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
from AIClientCenter.providers.openai_compatible import create_gemini_client


GEMINI_FREE_GROUP_ID = "gemini_flash_80"
DEFAULT_SAFETY_RATIO = 0.8


@dataclass(frozen=True)
class GeminiModelLimit:
    rpm: int
    tpm: int
    rpd: int | None
    tier: str


# AI Studio 免费层原始额度；限额器再应用安全比例。
GEMINI_FREE_MODEL_LIMITS: dict[str, GeminiModelLimit] = {
    "gemini-3.5-flash-lite": GeminiModelLimit(15, 250_000, 500, "primary"),
    "gemini-3.1-flash-lite": GeminiModelLimit(15, 250_000, 500, "primary"),
    "gemini-2.5-flash-lite": GeminiModelLimit(10, 250_000, 20, "reserve"),
    "gemini-3.8-flash": GeminiModelLimit(5, 250_000, 20, "reserve"),
    "gemini-3.7-flash": GeminiModelLimit(5, 250_000, 20, "reserve"),
    "gemini-3.6-flash": GeminiModelLimit(5, 250_000, 20, "reserve"),
    "gemini-3.5-flash": GeminiModelLimit(5, 250_000, 20, "reserve"),
    "gemini-3-flash": GeminiModelLimit(5, 250_000, 20, "reserve"),
    "gemini-2.5-flash": GeminiModelLimit(5, 250_000, 20, "reserve"),
}


def _positive_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value < 1:
        raise ValueError(f"{name} 必须为正整数")
    return value


def _ratio(configured: Optional[float] = None) -> float:
    value = float(
        configured if configured is not None
        else os.getenv("GEMINI_QUOTA_SAFETY_RATIO", str(DEFAULT_SAFETY_RATIO))
    )
    if not 0 < value <= 1:
        raise ValueError("Gemini quota safety ratio 必须在 (0, 1] 范围内")
    return value


def _selected_models(configured: Optional[list[str] | tuple[str, ...]]) -> list[str]:
    raw = os.getenv("GEMINI_MODELS", "").strip()
    models = list(configured) if configured is not None else (
        [item.strip() for item in raw.split(",") if item.strip()]
        if raw else list(GEMINI_FREE_MODEL_LIMITS)
    )
    unknown = [model for model in models if model not in GEMINI_FREE_MODEL_LIMITS]
    if unknown:
        raise ValueError(f"Gemini models 缺少免费限额定义: {', '.join(unknown)}")
    return models


def _rate_limit_db_path(configured: Optional[str] = None) -> Path:
    value = (configured or os.getenv("GEMINI_RATE_LIMIT_DB", "")).strip()
    if value:
        return Path(value).expanduser().resolve()
    return Path(__file__).resolve().parents[2] / "_data" / "ai_rate_limits.sqlite3"


def build_gemini_free_clients(
        api_keys: APIKeyConfig = None,
        *,
        models: Optional[list[str] | tuple[str, ...]] = None,
        primary_slots: Optional[int] = None,
        reserve_slots: Optional[int] = None,
        safety_ratio: Optional[float] = None,
        storage_path: Optional[str] = None,
        proxies: Optional[dict[str, str]] = None,
) -> dict[str, BaseAIClient]:
    credentials = resolve_api_keys(api_keys, env_name="GEMINI_API_KEY")
    if not credentials:
        return {}

    ratio = _ratio(safety_ratio)
    slots = {
        "primary": (
            primary_slots if primary_slots is not None
            else _positive_int("GEMINI_PRIMARY_SLOTS", 2)
        ),
        "reserve": (
            reserve_slots if reserve_slots is not None
            else _positive_int("GEMINI_RESERVE_SLOTS", 1)
        ),
    }
    if any(value < 1 for value in slots.values()):
        raise ValueError("Gemini slots 必须为正整数")
    priorities = {
        "primary": CLIENT_PRIORITY_FREEBIE,
        "reserve": CLIENT_PRIORITY_CONSUMABLES,
    }
    database = _rate_limit_db_path(storage_path)
    clients: dict[str, BaseAIClient] = {}
    for credential in credentials:
        for model in _selected_models(models):
            spec = GEMINI_FREE_MODEL_LIMITS[model]
            limiter = EstimatedRateLimiter(
                rpm=spec.rpm,
                tpm=spec.tpm,
                rpd=spec.rpd,
                utilization_ratio=ratio,
                storage_path=database,
                storage_namespace=(
                    f"google-generative-language:{credential.quota_id}:{model}"
                ),
            )
            model_key = model.removeprefix("gemini-").replace(".", "_").replace("-", "_")
            for slot in range(1, slots[spec.tier] + 1):
                key = f"gemini_{credential.alias}_{model_key}_{slot}"
                clients[key] = RateLimitedOpenAIClient(
                    name=f"Gemini [{credential.alias}] {model} #{slot}",
                    openai_api=create_gemini_client(
                        token=credential.api_key,
                        model=model,
                        proxies=proxies,
                    ),
                    rate_limiter=limiter,
                    priority=priorities[spec.tier],
                    group_id=GEMINI_FREE_GROUP_ID,
                    default_available=True,
                    observation_context={
                        "provider": "gemini",
                        "account_alias": credential.alias,
                        "preset": "gemini_free",
                    },
                )
    return clients

def create_gemini_free_preset(
        *, name: str, group: str, **kwargs,
) -> dict[str, BaseAIClient]:
    """供配置工厂引擎调用的 Gemini 免费层预置。"""

    clients = build_gemini_free_clients(**kwargs)
    for client in clients.values():
        client.group_id = group
        client.name = name if len(clients) == 1 else f"{name} / {client.name}"
    return clients
