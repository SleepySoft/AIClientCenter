import requests
from typing_extensions import override
from typing import Dict, List, Optional, Any, Union

# Handle relative/absolute imports
try:
    from .SimpleRotator import SimpleRotator
    from .LimitMixins import ClientMetricsMixin
    from .AIServiceTokenRotator import RotatableClient
    from .OpenAICompatibleAPI import OpenAICompatibleAPI
    from .AIClientManager import BaseAIClient, CLIENT_PRIORITY_NORMAL, ClientStatus
except ImportError:
    from SimpleRotator import SimpleRotator
    from LimitMixins import ClientMetricsMixin
    from AIServiceTokenRotator import RotatableClient
    from OpenAICompatibleAPI import OpenAICompatibleAPI
    from AIClientManager import BaseAIClient, CLIENT_PRIORITY_NORMAL, ClientStatus


class StandardOpenAIClient(ClientMetricsMixin, BaseAIClient):
    """
    Base Implementation for OpenAI-compatible Clients.

    Responsibilities:
    1. Metrics Tracking: Inherits `ClientMetricsMixin` for quota and usage tracking.
    2. Basic API Execution: Inherits `BaseAIClient` for status management.
    3. Model Rotation: Manages model rotation logic (common for all subclasses).

    Note:
        This class does NOT handle token rotation strategies.
        Token logic is delegated to subclasses via the `_prepare_token` hook.
    """

    def __init__(
            self,
            name: str,
            openai_api: OpenAICompatibleAPI,
            priority: int = CLIENT_PRIORITY_NORMAL,
            group_id: str = 'default',
            default_available: bool = False,
            quota_config: dict = None,
            balance_config: dict = None,
            state_file_path: Optional[str] = None
    ):
        """
        Initialize the Standard Client.

        Args:
            name: Unique identifier for the client.
            openai_api: An instance of the low-level API wrapper.
            priority: Scheduling priority (lower is better).
            default_available: Whether to mark client as AVAILABLE immediately.
            quota_config: Configuration for usage limits (e.g., max tokens).
            balance_config: Configuration for balance tracking.
            state_file_path: Path to persist usage state.
        """

        super().__init__(
            # Initialize BaseAIClient
            name=name,
            api_token=openai_api.get_api_token(),
            priority=priority,
            group_id=group_id,

            # Initialize ClientMetricsMixin
            quota_config=quota_config,
            balance_config=balance_config,
            state_file_path=state_file_path
        )

        self.api = openai_api

        if default_available:
            self._status['status'] = ClientStatus.AVAILABLE

    # ------------------ Overrides ------------------

    @override
    def get_model_list(self) -> Dict[str, Any]:
        return self.api.get_model_list()

    @override
    def get_current_model(self) -> str:
        return self.api.get_using_model()

    @override
    def _chat_completion_sync(self,
                              messages: List[Dict[str, str]],
                              model: Optional[str] = None,
                              temperature: float = 0.7,
                              max_tokens: int = 4096,
                              is_health_check: bool = False) -> Union[Dict[str, Any], requests.Response]:
        """
        Executes the synchronous chat completion.

        Flow:
        1. Call `_prepare_token()` to allow subclasses to set the correct API key.
        2. Determine the model (Rotation Priority > Argument Priority).
        3. Execute the API call.
        """
        return self.api.create_chat_completion_sync(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            is_health_check=is_health_check
        )


class SelfRotatingOpenAIClient(StandardOpenAIClient):
    """
    A Client that manages its own pool of tokens internally.

    Usage:
        Use this class when you have a static list of API keys and want
        simple Round-Robin rotation without external management.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Model Rotator: Handles the logic for switching between models (e.g., gpt-3.5/gpt-4)
        # This is kept in the base class as it is independent of token management.
        self.model_rotator = SimpleRotator[str]()

        # Initialize an internal rotator for tokens
        self.token_rotator = SimpleRotator[str]()

    # ------------------ Configuration Methods ------------------

    def set_rotation_models(self, models: List[str], rotate_per_times: int = 1):
        """Configure the list of models to rotate through."""
        self.model_rotator.set_items(models, rotate_per_times)

    def set_rotation_tokens(self, tokens: List[str], rotate_per_times: int = 1):
        """Configure the internal list of tokens to rotate."""
        self.token_rotator.set_items(tokens, rotate_per_times)

    # ------------------------------------------------------------

    def _prepare_token(self):
        """
        Internal Rotation Logic:
        Before every request, fetch the next token from the internal list
        and update the low-level API instance.
        """
        if next_token := self.token_rotator.get_next():
            self.api_token = next_token
            self.api.set_api_token(next_token)

    @override
    def _chat_completion_sync(self,
                              messages: List[Dict[str, str]],
                              model: Optional[str] = None,
                              temperature: float = 0.7,
                              max_tokens: int = 4096,
                              is_health_check: bool = False) -> Union[Dict[str, Any], requests.Response]:

        # 1. Token Strategy (Delegated to subclass)
        self._prepare_token()

        # 2. Model Strategy
        # If the rotator yields a model, use it; otherwise, fall back to the argument.
        target_model = self.model_rotator.get_next() or model

        # 3. Execution
        return self.api.create_chat_completion_sync(
            messages=messages,
            model=target_model,
            temperature=temperature,
            max_tokens=max_tokens,
            is_health_check=is_health_check
        )


class OuterTokenRotatingOpenAIClient(StandardOpenAIClient, RotatableClient):
    """
    A Client controlled by an external manager.

    Usage:
        Use this class when tokens are managed by an external service (e.g.,
        `AIServiceTokenRotator`) that checks balances or health and pushes
        updates to this client.
    """

    def __init__(self, *args, **kwargs):
        # Initialize the base standard client
        StandardOpenAIClient.__init__(self, *args, **kwargs)
        # Initialize the interface for external rotation
        RotatableClient.__init__(self)

    # ------------------ RotatableClient Interface ------------------

    @override
    def update_api_token(self, token: str):
        """
        Called by an external manager to force a token switch.
        """
        self.api_token = token
        self.api.set_api_token(self.api_token)

        # Reset status to UNKNOWN so the manager re-checks health if necessary
        self._update_client_status(ClientStatus.UNKNOWN)

    @override
    def update_token_balance(self, token: str, balance: float):
        """
        Called by an external manager to update the balance of a specific token.

        Safety Check:
            Only updates the balance if the provided token matches the
            currently active token to avoid race conditions.
        """
        if token == self.api_token:
            self.update_balance(balance)
