"""推荐 Client preset，以及 YAML 配置引擎使用的内置工厂。"""

from __future__ import annotations

import os
from collections.abc import Mapping

from AIClientCenter.core.manager import BaseAIClient
from AIClientCenter.core.preset import ClientPreset, compose_presets
from AIClientCenter.harness.cli import create_codex_client, create_kimi_client
from AIClientCenter.providers.gemini_free import build_gemini_free_clients, gemini_free
from AIClientCenter.providers.groq import build_groq_clients, groq_free
from AIClientCenter.providers.nvidia import build_nvidia_clients, nvidia_free
from AIClientCenter.providers.openai_clients import StandardOpenAIClient
from AIClientCenter.providers.openai_compatible import OpenAICompatibleAPI
from AIClientCenter.providers.openclaw import OpenClawClient
from AIClientCenter.providers.zhipu import ZhipuSDKAdapter


def _retarget(clients, *, name: str, group: str):
    clients = dict(clients)
    for client in clients.values():
        client.group_id = group
    if len(clients) == 1:
        next(iter(clients.values())).name = name
    else:
        for client in clients.values():
            client.name = f"{name} / {client.name}"
    return clients


def factory_gemini_free(*, name: str, group: str, **kwargs):
    return _retarget(build_gemini_free_clients(**kwargs), name=name, group=group)


def factory_groq_free(*, name: str, group: str, **kwargs):
    return _retarget(build_groq_clients(**kwargs), name=name, group=group)


def factory_nvidia_free(*, name: str, group: str, **kwargs):
    return _retarget(build_nvidia_clients(**kwargs), name=name, group=group)


def factory_codex(*, name: str, group: str, **kwargs):
    return create_codex_client(name=name, group_id=group, **kwargs)


def factory_kimi(*, name: str, group: str, **kwargs):
    return create_kimi_client(name=name, group_id=group, **kwargs)


def factory_openclaw(
        *, name: str, group: str, gateway_token_env: str | None = None,
        **kwargs):
    if gateway_token_env:
        kwargs["gateway_token"] = os.getenv(gateway_token_env)
    return OpenClawClient(name=name, group_id=group, **kwargs)


def factory_openai_compatible(
        *, name: str, group: str, api_base_url: str, model: str,
        api_key: str | None = None, api_key_env: str | None = None,
        proxies: Mapping[str, str] | None = None, **kwargs):
    if api_key is not None and api_key_env is not None:
        raise ValueError("api_key 与 api_key_env 不能同时配置。")
    token = api_key if api_key is not None else (
        os.getenv(api_key_env) if api_key_env else None)
    if not token:
        raise ValueError("缺少 API Key；请配置 api_key 或 api_key_env。")
    api = OpenAICompatibleAPI(
        api_base_url=api_base_url,
        token=token,
        default_model=model,
        proxies=dict(proxies or {}),
    )
    return StandardOpenAIClient(
        name=name, group_id=group, openai_api=api, **kwargs)


def factory_zhipu(
        *, name: str, group: str, api_key: str | None = None,
        api_key_env: str = "ZHIPU_API_KEY", model: str = "glm-4",
        enable_thinking: bool = True, **kwargs):
    token = api_key or os.getenv(api_key_env)
    if not token:
        raise ValueError(f"环境变量 {api_key_env} 未设置。")
    adapter = ZhipuSDKAdapter(
        api_key=token, model=model, enable_thinking=enable_thinking)
    return StandardOpenAIClient(
        name=name, group_id=group, openai_api=adapter, **kwargs)


FACTORIES = {
    "codex": factory_codex,
    "gemini_free": factory_gemini_free,
    "groq_free": factory_groq_free,
    "kimi": factory_kimi,
    "nvidia_free": factory_nvidia_free,
    "openai_compatible": factory_openai_compatible,
    "openclaw": factory_openclaw,
    "zhipu": factory_zhipu,
}

__all__ = [
    "ClientPreset",
    "compose_presets",
    "gemini_free",
    "groq_free",
    "nvidia_free",
    "FACTORIES",
]
