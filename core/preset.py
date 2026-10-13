"""可组合的 AI Client 推荐配置。"""

from __future__ import annotations

from dataclasses import dataclass, field

from AIClientCenter.core.manager import BaseAIClient


@dataclass(frozen=True)
class ClientPreset:
    """一个服务商预置生成的客户端及其并发限制。"""

    clients: dict[str, BaseAIClient] = field(default_factory=dict)
    group_limits: dict[str, int] = field(default_factory=dict)


def compose_presets(
        *presets: ClientPreset,
) -> tuple[dict[str, BaseAIClient], dict[str, int]]:
    """合并多个预置，并拒绝会产生静默覆盖的命名冲突。"""

    clients: dict[str, BaseAIClient] = {}
    group_limits: dict[str, int] = {}
    for preset in presets:
        duplicate_clients = clients.keys() & preset.clients.keys()
        if duplicate_clients:
            raise ValueError(
                "Client preset 名称重复: " + ", ".join(sorted(duplicate_clients))
            )
        for group_id, limit in preset.group_limits.items():
            if group_id in group_limits and group_limits[group_id] != limit:
                raise ValueError(f"Client preset 分组限额冲突: {group_id}")
            if int(limit) < 1:
                raise ValueError(f"Client preset 分组限额必须为正整数: {group_id}")
        clients.update(preset.clients)
        group_limits.update(preset.group_limits)
    return clients, group_limits
