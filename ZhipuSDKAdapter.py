import logging
import json
from typing import List, Dict, Any, Optional, Union
from typing_extensions import override

# --- 1. Import Official ZhipuAI SDK (V2) ---
# --- 1. Import Official ZhipuAI SDK (Robust Import) ---
try:
    import zhipuai
    from zhipuai import ZhipuAI

    # 【核心修复】：动态获取异常类，如果找不到则使用 Exception 作为兜底
    # 智谱不同版本 SDK 的异常命名可能不同（例如 RateLimitError 有时叫 APIReachLimitError）

    # 1. 基类错误 (Base Error)
    ZhipuBaseError = getattr(zhipuai, "ZhipuAIError", Exception)  # 部分旧版

    # 2. HTTP 状态错误 (APIError / APIStatusError)
    APIError = getattr(zhipuai, "APIError", None) or \
               getattr(zhipuai, "APIStatusError", None) or \
               getattr(zhipuai, "APIRequestFailedError", ZhipuBaseError)

    # 3. 认证错误 (AuthenticationError / APIAuthenticationError)
    AuthenticationError = getattr(zhipuai, "AuthenticationError", None) or \
                          getattr(zhipuai, "APIAuthenticationError", APIError)

    # 4. 限流错误 (RateLimitError / APIReachLimitError)
    RateLimitError = getattr(zhipuai, "RateLimitError", None) or \
                     getattr(zhipuai, "APIReachLimitError", APIError)

    # 5. 连接/超时错误
    APIConnectionError = getattr(zhipuai, "APIConnectionError", APIError)
    APITimeoutError = getattr(zhipuai, "APITimeoutError", APIError)

    # 6. 请求内容错误
    BadRequestError = getattr(zhipuai, "BadRequestError", None) or \
                      getattr(zhipuai, "InvalidRequestError", APIError)

    # 7. 资源不存在
    NotFoundError = getattr(zhipuai, "NotFoundError", APIError)

    # 8. 服务端错误
    InternalServerError = getattr(zhipuai, "InternalServerError", APIError)

except ImportError:
    # 如果完全没有安装 zhipuai
    ZhipuAI = None


    # 定义空异常以防止代码语法报错
    class APIError(Exception):
        pass


    class RateLimitError(APIError):
        pass


    class AuthenticationError(APIError):
        pass


    class APIConnectionError(APIError):
        pass


    class APITimeoutError(APIError):
        pass


    class BadRequestError(APIError):
        pass


    class NotFoundError(APIError):
        pass


    class InternalServerError(APIError):
        pass


logger = logging.getLogger(__name__)

# --- Standardized Result Structure Type Hint ---
APIResult = Dict[str, Union[bool, Optional[Dict[str, Any]]]]


# --- Helper Function for Structured Error ---
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
    Wraps the official 'zhipuai' SDK (V2) to look like 'OpenAICompatibleAPI'.
    """

    def __init__(self, api_key: str, model: str = "glm-4", enable_thinking: bool = True):
        if ZhipuAI is None:
            raise ImportError("Please install the official zhipuai SDK: pip install zhipuai")

        self.api_token = api_key
        self.model = model
        self.enable_thinking = enable_thinking

        # Initialize the official SDK Client
        self._client = ZhipuAI(api_key=api_key)

    # ------------------ Interface Implementation ------------------

    def get_api_token(self) -> str:
        return self.api_token

    def set_api_token(self, token: str):
        """Allows StandardOpenAIClient to rotate tokens."""
        if token != self.api_token:
            self.api_token = token
            # Re-initialize SDK client with new token
            self._client = ZhipuAI(api_key=token)

    def get_using_model(self) -> str:
        return self.model

    def get_model_list(self) -> Dict[str, Any]:
        """
        Mock implementation. Zhipu SDK doesn't always expose a direct list method
        that matches OpenAI strictly, so we return a static list for compatibility.
        """
        return {
            "data": [
                {"id": "glm-4"},
                {"id": "glm-4-plus"},
                {"id": "glm-4-flash"},
                {"id": "glm-4-air"},
            ]
        }

    def create_chat_completion_sync(
            self,
            messages: List[Dict[str, str]],
            model: Optional[str] = None,
            temperature: float = 0.95,
            max_tokens: int = 4096
    ) -> APIResult:
        """
        The Core Adapter Method.
        Translates Request -> ZhipuAI SDK (V2) -> Standard Response (APIResult).
        """

        # 1. Prepare Parameters
        target_model = model or self.model
        params = {
            "model": target_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,  # Force stream to capture usage and thinking content reliably
        }

        # 智谱目前部分模型支持 formatting 参数或 thinking 参数，视具体模型而定
        # 此处保留你原本的 enable_thinking 逻辑
        # 注意：ZhipuAI V2 的 thinking 通常在 content 中返回或 specific field，具体取决于 API 版本
        # 下面的流式处理逻辑会尝试捕获 reasoning_content

        try:
            # 2. Call SDK
            response_generator = self._client.chat.completions.create(**params)

            # 3. Aggregate Stream (Convert Stream -> Sync Dict)
            full_content = ""
            full_reasoning = ""
            finish_reason = "stop"
            usage_info = {}

            for chunk in response_generator:
                if not chunk.choices:
                    # Usage stats often appear in the last chunk which has no choices
                    if hasattr(chunk, 'usage') and chunk.usage:
                        usage_info = chunk.usage
                    continue

                delta = chunk.choices[0].delta

                # Capture Thinking/Reasoning (If supported by model/SDK version)
                if hasattr(delta, 'reasoning_content') and delta.reasoning_content:
                    full_reasoning += delta.reasoning_content

                # Capture Content
                if hasattr(delta, 'content') and delta.content:
                    full_content += delta.content

                if chunk.choices[0].finish_reason:
                    finish_reason = chunk.choices[0].finish_reason

                # Accumulate usage if present in choice chunks
                if hasattr(chunk, 'usage') and chunk.usage:
                    usage_info = chunk.usage

            # 4. Return Standard OpenAI Format Dictionary wrapped in APIResult (SUCCESS)
            # If reasoning exists, we might want to prepend/append it or store it separately.
            # Here we just stick to standard content.

            completion_data = {
                "id": "gen-zhipu-adapter-v2",
                "object": "chat.completion",
                "created": 0,
                "model": target_model,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": full_content,
                            # Optional: exclude reasoning from main content if needed,
                            # currently Zhipu convention is often mixed or separate.
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

        # ==============================================================================
        # Error Mapping Strategy (Strictly following provided table)
        # ==============================================================================

        # --- Case 1: PERMANENT (Auth, Permission, NotFound) ---
        except AuthenticationError as e:
            # HTTP 401: Invalid API Key
            logger.error(f"Zhipu SDK Auth Error: {e}")
            return _make_error_result("PERMANENT", "HTTP_401", f"Authentication Failed: {e}")

        except NotFoundError as e:
            # HTTP 404: Model not found or API endpoint wrong
            logger.error(f"Zhipu SDK Not Found Error: {e}")
            return _make_error_result("PERMANENT", "HTTP_404", f"Resource Not Found: {e}")

        # --- Case 2: BAD_REQUEST (Content issues) ---
        except BadRequestError as e:
            # HTTP 400: Invalid JSON, Sensitive Content, Context too long
            logger.error(f"Zhipu SDK Bad Request: {e}")
            # Client strategy: Do not retry (status unchanged)
            return _make_error_result("BAD_REQUEST", "HTTP_400", f"Bad Request: {e}")

        # --- Case 3: TRANSIENT_SERVER (Rate Limit, Overload) ---
        except RateLimitError as e:
            # HTTP 429
            logger.warning(f"Zhipu SDK Rate Limit: {e}")
            return _make_error_result("TRANSIENT_SERVER", "HTTP_429", f"Rate Limited: {e}")

        except InternalServerError as e:
            # HTTP 500+
            logger.error(f"Zhipu SDK Internal Server Error: {e}")
            return _make_error_result("TRANSIENT_SERVER", "HTTP_500", f"Server Error: {e}")

        # --- Case 4: TRANSIENT_NETWORK (Connection, Timeout) ---
        except (APIConnectionError, APITimeoutError) as e:
            # Network level failures
            logger.warning(f"Zhipu SDK Network Error: {type(e).__name__}: {e}")
            return _make_error_result("TRANSIENT_NETWORK", "CONNECTION_TIMEOUT", f"Network Error: {e}")

        # --- Case 5: Fallback for generic APIError ---
        except APIError as e:
            # Catch-all for other SDK errors not caught above
            logger.error(f"Zhipu SDK Generic API Error: {e}")
            # Try to distinguish based on message if possible, otherwise default to server error
            msg = str(e).lower()
            if "50" in msg or "internal" in msg:
                return _make_error_result("TRANSIENT_SERVER", "HTTP_500", f"API Error: {e}")
            return _make_error_result("PERMANENT", "UNKNOWN_API_ERROR", f"Unknown API Error: {e}")

        # --- Case 6: Unexpected System Errors ---
        except Exception as e:
            logger.critical(f"Unexpected Zhipu SDK System Error: {type(e).__name__}: {e}")
            return _make_error_result("PERMANENT", "UNEXPECTED_CLIENT_ERROR", f"System Failure: {str(e)}")
