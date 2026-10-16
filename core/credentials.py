"""API Key 配置归一化工具。"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


APIKeyConfig = Mapping[str, str] | Sequence[str] | None


@dataclass(frozen=True)
class APIKeyCredential:
    """不泄露密钥的运行时凭据描述。"""

    alias: str
    api_key: str
    quota_id: str


def _safe_alias(value: str) -> str:
    alias = re.sub(r"[^a-zA-Z0-9_-]+", "_", value.strip()).strip("_").lower()
    if not alias:
        raise ValueError("API Key 别名不能为空或只包含特殊字符")
    return alias


def _fingerprint(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def resolve_api_keys(
        configured: APIKeyConfig,
        *,
        env_name: str,
) -> tuple[APIKeyCredential, ...]:
    """解析配置中的多个 Key；未配置时回退到单个环境变量。

    Mapping 的键是只用于展示的稳定别名，值是 API Key。Sequence 会自动生成
    ``key_1``、``key_2`` 别名。额度身份由密钥指纹确定，明文不会进入日志或账本。
    """

    if configured:
        if isinstance(configured, Mapping):
            entries = list(configured.items())
        elif isinstance(configured, Sequence) and not isinstance(configured, (str, bytes)):
            entries = [(f"key_{index}", value) for index, value in enumerate(configured, 1)]
        else:
            raise TypeError("API Key 配置必须是 alias -> key 映射或 key 序列")
    else:
        env_value = os.getenv(env_name, "").strip()
        entries = [("env", env_value)] if env_value else []

    credentials: list[APIKeyCredential] = []
    aliases: set[str] = set()
    fingerprints: set[str] = set()
    for raw_alias, raw_key in entries:
        alias = _safe_alias(str(raw_alias))
        api_key = str(raw_key).strip()
        if not api_key:
            raise ValueError(f"API Key {raw_alias!r} 不能为空")
        quota_id = _fingerprint(api_key)
        if alias in aliases:
            raise ValueError(f"API Key 别名规范化后重复: {alias}")
        if quota_id in fingerprints:
            raise ValueError("同一个 API Key 不能重复配置为多份独立额度")
        aliases.add(alias)
        fingerprints.add(quota_id)
        credentials.append(APIKeyCredential(alias, api_key, quota_id))
    return tuple(credentials)
