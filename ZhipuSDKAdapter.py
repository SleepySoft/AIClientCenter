import logging
from typing import List, Dict, Any, Optional, Union
from typing_extensions import override

# Try importing the official SDK
try:
    from zai import ZhipuAiClient
except ImportError:
    ZhipuAiClient = None

logger = logging.getLogger(__name__)


class ZhipuSDKAdapter:
    """
    Adapter pattern implementation.
    Wraps the official 'zai' SDK to look like 'OpenAICompatibleAPI'.

    This allows StandardOpenAIClient to use Zhipu SDK without any code changes.
    """

    def __init__(self, api_key: str, model: str = "glm-4.6", enable_thinking: bool = True):
        if ZhipuAiClient is None:
            raise ImportError("Please install zai-sdk: pip install zai-sdk")

        self.api_token = api_key
        self.model = model
        self.enable_thinking = enable_thinking

        # Initialize the official SDK
        self._client = ZhipuAiClient(api_key=api_key)

        # Cache for model list (optional, mimics OpenAICompatibleAPI behavior)
        self._cached_models = []

    # ------------------ Interface Implementation ------------------

    def get_api_token(self) -> str:
        return self.api_token

    def set_api_token(self, token: str):
        """Allows StandardOpenAIClient to rotate tokens."""
        if token != self.api_token:
            self.api_token = token
            # Re-initialize SDK client with new token
            self._client = ZhipuAiClient(api_key=token)

    def get_using_model(self) -> str:
        # Placeholder: This adapter doesn't hold state of "current model"
        # The Client passes the model in each request.
        return "glm-4"

    def get_model_list(self) -> Dict[str, Any]:
        """
        Mock implementation since Zhipu SDK might not have a direct list_models equivalent
        that matches OpenAI format perfectly, or we just return a static list.
        """
        return {
            "data": [
                {"id": "glm-4.6"},
                {"id": "glm-4-plus"},
                {"id": "glm-4-flash"},
            ]
        }

    def create_chat_completion_sync(
            self,
            messages: List[Dict[str, str]],
            model: Optional[str] = None,
            temperature: float = 1.0,
            max_tokens: int = 4096
    ) -> Dict[str, Any]:
        """
        The Core Adapter Method.
        Translates StandardOpenAIClient's request -> Zai SDK request -> Standard Response.
        """

        # 1. Prepare Parameters for Zai SDK
        params = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,  # Force stream to capture thinking content
        }

        if self.enable_thinking:
            params["thinking"] = {"type": "enabled"}

        try:
            # 2. Call SDK
            response_generator = self._client.chat.completions.create(**params)

            # 3. Aggregate Stream (Convert Stream -> Sync Dict)
            # This logic mimics what we did in the previous subclass, but now inside the Adapter.
            full_content = ""
            full_reasoning = ""
            finish_reason = "stop"
            usage_info = {}

            for chunk in response_generator:
                if not chunk.choices:
                    if hasattr(chunk, 'usage') and chunk.usage:
                        usage_info = chunk.usage
                    continue

                delta = chunk.choices[0].delta

                # Capture Thinking
                if hasattr(delta, 'reasoning_content') and delta.reasoning_content:
                    full_reasoning += delta.reasoning_content

                # Capture Content
                if hasattr(delta, 'content') and delta.content:
                    full_content += delta.content

                if chunk.choices[0].finish_reason:
                    finish_reason = chunk.choices[0].finish_reason

                if hasattr(chunk, 'usage') and chunk.usage:
                    usage_info = chunk.usage

            # 4. Return Standard OpenAI Format Dictionary
            return {
                "id": "gen-zhipu-adapter",
                "object": "chat.completion",
                "created": 0,
                "model": params["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": full_content,
                            # Optionally attach reasoning here if your parser supports it
                            # Or append it to content:
                            # "content": f"<thinking>{full_reasoning}</thinking>\n{full_content}"
                        },
                        "finish_reason": finish_reason
                    }
                ],
                "usage": {
                    "prompt_tokens": getattr(usage_info, 'prompt_tokens', 0),
                    "completion_tokens": getattr(usage_info, 'completion_tokens', 0),
                    "total_tokens": getattr(usage_info, 'total_tokens', 0)
                } if usage_info else {}
            }

        except Exception as e:
            # StandardOpenAIClient expects exceptions to be raised so it can count errors
            logger.error(f"Zhipu SDK Error: {e}")
            raise e
