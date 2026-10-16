"""Advanced configuration escape hatch for clients without a registered factory."""

from AIClientCenter.core.preset import ClientPreset


def build_ai_client_config() -> ClientPreset:
    # Construct custom BaseAIClient instances here.
    return ClientPreset(clients={}, group_limits={})
