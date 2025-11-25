# AI clients that matches BaseAIClient

import requests
from typing_extensions import override
from typing import Dict, List, Optional, Any, Union

try:
    from .LimitMixins import ClientMetricsMixin
    from .AIServiceTokenRotator import RotatableClient
    from .OpenAICompatibleAPI import OpenAICompatibleAPI
    from .AIClientManager import BaseAIClient, CLIENT_PRIORITY_NORMAL, ClientStatus

except ImportError:
    from LimitMixins import ClientMetricsMixin
    from AIServiceTokenRotator import RotatableClient
    from OpenAICompatibleAPI import OpenAICompatibleAPI
    from AIClientManager import BaseAIClient, CLIENT_PRIORITY_NORMAL, ClientStatus


class OpenAIRotationClient(ClientMetricsMixin, BaseAIClient, RotatableClient):
    def __init__(
            self,
            name: str,
            openai_api: OpenAICompatibleAPI,
            priority: int = CLIENT_PRIORITY_NORMAL,
            default_available: bool = False,
            quota_config: dict = None,
            balance_config: dict = None,
            state_file_path: Optional[str] = None
    ):
        super().__init__(
            # 1. BaseAIClient
            name=name,
            api_token=openai_api.get_api_token(),
            priority=priority,

            # 2. ClientMetricsMixin
            quota_config=quota_config,
            balance_config=balance_config,
            state_file_path=state_file_path

            # 3. RotatableClient
        )

        self.api = openai_api

        self._rotation_models = []
        self._rotate_per_times = 1
        self._current_model_idx = 0
        self._current_model_uses = 0

        if default_available:
            self._status['status'] = ClientStatus.AVAILABLE


    # ------------------------------------------------- Overrides -------------------------------------------------

    # ------------------ BaseAIClient ------------------

    @override
    def get_model_list(self) -> Dict[str, Any]:
        return self.api.get_model_list()

    @override
    def _chat_completion_sync(self,
                              messages: List[Dict[str, str]],
                              model: Optional[str] = None,
                              temperature: float = 0.7,
                              max_tokens: int = 4096) -> Union[Dict[str, Any], requests.Response]:
        return self.api.create_chat_completion_sync(
            messages=messages,
            model=self._get_next_model() or model,
            temperature=temperature,
            max_tokens=max_tokens
        )

    # ------------------ RotatableClient ------------------

    @override
    def update_api_token(self, token: str):
        self.api_token = token
        self.api.set_api_token(self.api_token)
        self._update_client_status(ClientStatus.UNKNOWN)

    @override
    def update_token_balance(self, token: str, balance: float):
        if token == self.api_token:
            self.update_balance(balance)

    # ------------------ Model Rotation ------------------

    def set_rotation_models(self, models: List[str], rotate_per_times: int = 1):
        self._rotation_models = models
        self._rotate_per_times = rotate_per_times

    def _get_next_model(self) -> Optional[str]:
        if not self._rotation_models:
            # Return None to use API default module
            return None
        if self._current_model_uses >= self._rotate_per_times:
            self._current_model_idx += 1
            self._current_model_idx %= len(self._rotation_models)
            self._current_model_uses = 0
        self._current_model_uses += 1
        return self._rotation_models[self._current_model_idx]
