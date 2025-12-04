import logging
import json
from typing import List, Dict, Any, Optional, Union
from typing_extensions import override

# Try importing the official SDK
try:
    from zai import ZhipuAiClient
    # Also need to import Zhipu's specific exceptions
    from zai.exceptions import ZhipuAiError, RateLimitError, APIError, AuthenticationError, InvalidRequestError
except ImportError:
    ZhipuAiClient = None


    # Define placeholder exceptions if SDK is missing to prevent NameErrors in type hints/catches
    class ZhipuAiError(Exception):
        pass


    class RateLimitError(ZhipuAiError):
        pass


    class APIError(ZhipuAiError):
        pass


    class AuthenticationError(ZhipuAiError):
        pass


    class InvalidRequestError(ZhipuAiError):
        pass

logger = logging.getLogger(__name__)

# --- Standardized Result Structure Type Hint (from your previous class) ---
APIResult = Dict[str, Union[bool, Optional[Dict[str, Any]]]]


# --- Helper Function for Structured Error (re-used from your previous class) ---
def _make_error_result(error_type: str, error_code: str, message: str) -> APIResult:
    """Helper to create a standardized failure dictionary."""
    return {
        "success": False,
        "data": None,
        "error": {
            "type": error_type,
            "code": error_code,
            "message": message
        }
    }


class ZhipuSDKAdapter:
    """
    Adapter pattern implementation.
    Wraps the official 'zai' SDK to look like 'OpenAICompatibleAPI'.

    This allows StandardOpenAIClient to use Zhipu SDK without any code changes,
    and returns results in the standardized APIResult format.
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
        # Note: If this method fails, the calling code needs to handle it.
        # For simplicity, we keep the original static return.
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
    ) -> APIResult:  # <-- Changed return type to APIResult
        """
        The Core Adapter Method.
        Translates StandardOpenAIClient's request -> Zai SDK request -> Standard Response (APIResult).
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

            # 4. Return Standard OpenAI Format Dictionary wrapped in APIResult (SUCCESS)
            completion_data = {
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
            return {"success": True, "data": completion_data, "error": None}

        # 5. UNIFIED ERROR HANDLING
        except AuthenticationError as e:
            # Corresponds to HTTP 401/403: Permanent Error
            logger.error(f"Zhipu SDK Auth Error: {e}")
            message = str(e)
            return _make_error_result("PERMANENT", "HTTP_401", f"Authentication Failed: {message}")

        except InvalidRequestError as e:
            # Corresponds to HTTP 400/404: Permanent Error (Bad request format, invalid model)
            logger.error(f"Zhipu SDK Invalid Request Error: {e}")
            message = str(e)
            return _make_error_result("PERMANENT", "HTTP_400", f"Invalid Request: {message}")

        except RateLimitError as e:
            # Corresponds to HTTP 429: Transient Server Error
            logger.warning(f"Zhipu SDK Rate Limit Error: {e}")
            message = str(e)
            return _make_error_result("TRANSIENT_SERVER", "HTTP_429", f"Rate Limited: {message}")

        except APIError as e:
            # Catch other potential Zhipu API-related errors (e.g., HTTP 5xx)
            logger.error(f"Zhipu SDK API Error: {e}")
            # Try to infer if it's a 5xx or transient issue
            message = str(e)
            if "50" in message or "internal" in message.lower():  # Basic check for 5xx/internal errors
                return _make_error_result("TRANSIENT_SERVER", "HTTP_500", f"Internal Server Error: {message}")
            else:
                # Default to permanent if specific transient nature is unknown
                return _make_error_result("PERMANENT", "UNKNOWN_API_ERROR", f"Unknown API Error: {message}")


        except Exception as e:
            # Catch all other unexpected errors (e.g., network issues, system errors)
            logger.critical(f"Unexpected Zhipu SDK/System Error: {type(e).__name__}: {e}")
            message = str(e)
            # Since the Zhipu SDK handles connection retries internally, any remaining generic
            # Exception often points to a serious system/network issue or an unhandled SDK error.
            # We categorize it as an unexpected error for external retry policy.
            return _make_error_result("PERMANENT", "UNEXPECTED_CLIENT_ERROR", f"System Failure: {message}")
