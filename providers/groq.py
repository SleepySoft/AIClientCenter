"""Groq 免费层大模型客户端配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from AIClientCenter.core.manager import BaseAIClient, CLIENT_PRIORITY_FREEBIE
from AIClientCenter.core.rate_limit import EstimatedRateLimiter
from AIClientCenter.providers.openai_clients import RateLimitedOpenAIClient
from AIClientCenter.providers.openai_compatible import create_groq_client


GROQ_GROUP_ID = "groq_free_large"
DEFAULT_SAFETY_RATIO = 0.8
DEFAULT_COMPLETION_RESERVE_TOKENS = 2_048


@dataclass(frozen=True)
class GroqModelLimit:
    rpm: int
    rpd: int
    tpm: int
    tpd: int


# Groq 官方免费层，核对日期 2026-10-12。仅允许达到本项目质量门槛的模型；
# 20B/27B/7B 等模型即使账号可用也不会由该构建器启用。
GROQ_LARGE_MODEL_LIMITS: dict[str, GroqModelLimit] = {
    "openai/gpt-oss-120b": GroqModelLimit(
        rpm=30,
        rpd=1_000,
        tpm=8_000,
        tpd=200_000,
    ),
}


def _ratio() -> float:
    value = float(os.getenv("GROQ_QUOTA_SAFETY_RATIO", str(DEFAULT_SAFETY_RATIO)))
    if not 0 < value <= 1:
        raise ValueError("GROQ_QUOTA_SAFETY_RATIO 必须在 (0, 1] 范围内")
    return value


def _completion_reserve() -> int:
    value = int(os.getenv(
        "GROQ_COMPLETION_RESERVE_TOKENS",
        str(DEFAULT_COMPLETION_RESERVE_TOKENS),
    ))
    if value < 0:
        raise ValueError("GROQ_COMPLETION_RESERVE_TOKENS 不能为负数")
    return value


def _selected_models() -> list[str]:
    raw = os.getenv("GROQ_MODELS", "").strip()
    models = [item.strip() for item in raw.split(",") if item.strip()] \
        if raw else list(GROQ_LARGE_MODEL_LIMITS)
    rejected = [model for model in models if model not in GROQ_LARGE_MODEL_LIMITS]
    if rejected:
        raise ValueError(
            "GROQ_MODELS 包含未达到大模型质量门槛或缺少限额定义的模型: "
            + ", ".join(rejected)
        )
    return models


def _rate_limit_db_path(configured: Optional[str] = None) -> Path:
    value = (configured or os.getenv("GROQ_RATE_LIMIT_DB", "")).strip()
    if value:
        return Path(value).expanduser().resolve()
    return Path(__file__).resolve().parents[2] / "_data" / "ai_rate_limits.sqlite3"


def build_groq_clients(
        api_key: Optional[str] = None,
        *,
        storage_path: Optional[str] = None,
) -> dict[str, BaseAIClient]:
    """构造单并发 Groq 免费客户端；没有 Key 时安静跳过。"""

    token = (api_key or os.getenv("GROQ_API_KEY", "")).strip()
    if not token:
        return {}

    ratio = _ratio()
    completion_reserve = _completion_reserve()
    database = _rate_limit_db_path(storage_path)
    clients: dict[str, BaseAIClient] = {}
    for model in _selected_models():
        limits = GROQ_LARGE_MODEL_LIMITS[model]
        limiter = EstimatedRateLimiter(
            rpm=limits.rpm,
            rpd=limits.rpd,
            tpm=limits.tpm,
            tpd=limits.tpd,
            utilization_ratio=ratio,
            storage_path=database,
            storage_namespace=f"groq:{model}",
        )
        key = "groq_" + model.replace("/", "_").replace("-", "_")
        clients[key] = RateLimitedOpenAIClient(
            name=f"Groq {model}",
            openai_api=create_groq_client(token=token, model=model),
            rate_limiter=limiter,
            # Groq 的 TPM/TPD 是 token 总量口径，成功后按 total_tokens 校正。
            token_usage_field="total_tokens",
            completion_reserve_tokens=completion_reserve,
            priority=CLIENT_PRIORITY_FREEBIE,
            group_id=GROQ_GROUP_ID,
            default_available=True,
        )
    return clients
