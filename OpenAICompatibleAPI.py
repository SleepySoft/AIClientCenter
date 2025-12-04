import os
import json
import backoff
import logging
import asyncio
import requests
import threading
from typing import Optional, Dict, Any, Union, List
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from requests.exceptions import RequestException, Timeout, ConnectionError

# --- Optional Dependencies ---
try:
    import aiohttp
    from aiohttp import TCPConnector
except ImportError:
    aiohttp = None
    TCPConnector = None

logger = logging.getLogger(__name__)


# --- Constants & Helpers ---

# HTTP Status codes that generally indicate a temporary issue safe to retry.

RETRYABLE_STATUS_CODES = {
    429,  # Too Many Requests
    500,  # Internal Server Error
    502,  # Bad Gateway
    503,  # Service Unavailable
    504,  # Gateway Timeout
    524
}

# Construct the tuple of async exceptions to catch during retries.
# This prevents NameError if aiohttp is not installed.
_async_retry_exceptions = [asyncio.TimeoutError]
if aiohttp:
    _async_retry_exceptions.extend([
        aiohttp.ClientConnectionError,
        aiohttp.ClientResponseError,
        aiohttp.ServerDisconnectedError
    ])
RETRYABLE_ASYNC_EXCEPTIONS = tuple(_async_retry_exceptions)

LLM_DEFAULT_TIMEOUT_S = 5 * 60


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


def is_retryable_async_error(e: Exception) -> bool:
    """
    Determines if an asynchronous exception is transient and worth retrying.
    Handles network timeouts, connection drops, and specific HTTP 5xx/429 errors.
    """
    if not aiohttp:
        return isinstance(e, asyncio.TimeoutError)

    # 1. Network-level errors (timeouts, connection reset)
    if isinstance(e, (asyncio.TimeoutError, aiohttp.ClientConnectionError, aiohttp.ServerDisconnectedError)):
        return True

    # 2. HTTP Response errors (check status code)
    if isinstance(e, aiohttp.ClientResponseError):
        return e.status in RETRYABLE_STATUS_CODES

    return False


class OpenAICompatibleAPI:
    """
    A robust client for OpenAI-compatible API services.

    Key Features:
    1. **Dual Mode:** Supports both Synchronous (requests) and Asynchronous (aiohttp) operations.
    2. **Auto-Healing Sync Sessions:** Automatically detects broken connection pools or stale proxies
       in synchronous mode and resets the session to recover connectivity.
    3. **Efficient Async Resource Management:** Uses a shared, lazy-loaded aiohttp session to prevent
       TCP socket exhaustion/port starvation during high concurrency.
    4. **Smart Retries:** Implements exponential backoff for rate limits (429) and server errors.
    """

    def __init__(self, api_base_url: str, token: Optional[str] = None,
                 default_model: str = "gpt-3.5-turbo", proxies: dict = None):
        """
        Args:
            api_base_url: The base endpoint (e.g., 'https://api.openai.com/v1').
            token: API Key. Defaults to OPENAI_API_KEY env var if not provided.
            default_model: The model name to use if not specified in requests.
            proxies: Dictionary for HTTP/HTTPS proxies.
        """
        self.api_base_url = api_base_url.strip()
        self._api_token = token or os.getenv("OPENAI_API_KEY")
        self.default_model = default_model
        self.using_model = default_model
        self.proxies = proxies or {}

        # Thread-safe lock for token updates
        self.lock = threading.Lock()

        # Initialize the synchronous session immediately
        self.sync_session = self._create_sync_session()

        # Placeholder for the asynchronous session (initialized lazily)
        self._async_session: Optional[Any] = None

    # --------------------------------------------------------------------------
    # Authentication Management
    # --------------------------------------------------------------------------

    def get_api_token(self) -> str:
        with self.lock:
            return self._api_token

    def set_api_token(self, token: str):
        """
        Updates the API token at runtime for both sync and async requests.
        """
        with self.lock:
            old_token = self._api_token
            self._api_token = token

            # Immediately update the header in the active sync session
            if self.sync_session:
                self.sync_session.headers["Authorization"] = f"Bearer {self._api_token}"

            logger.info(f'API key updated. (Ends with: ...{token[-4:] if token else "None"})')

    def _get_dynamic_header(self) -> dict:
        """Returns fresh headers (useful for async requests where session is shared)."""
        with self.lock:
            return {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_token}"
            }

        # --------------------------------------------------------------------------
        # Synchronous Core Logic (Auto-Healing & Backoff Integrated)
        # --------------------------------------------------------------------------

    def _create_sync_session(self) -> requests.Session:
        """Configures a new requests.Session with connection pooling and basic retries."""
        session = requests.Session()

        # Low-level TCP Retry configuration remains the same, but now acts as first line of defense
        adapter = HTTPAdapter(
            pool_connections=20,
            pool_maxsize=20,
            max_retries=Retry(
                total=2,  # Lowered internal retry, rely more on external @backoff
                backoff_factor=1,
                status_forcelist=list(RETRYABLE_STATUS_CODES),
                allowed_methods=["POST"],
                respect_retry_after_header=True
            )
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)

        session.headers.update(self._get_dynamic_header())
        session.proxies = self.proxies
        return session

    def _reset_sync_session(self):
        """Forcefully closes and recreates the synchronous session."""
        with self.lock:
            try:
                logger.warning("Reseting synchronous session to recover from connection error...")
                self.sync_session.close()
            except Exception:
                pass
            self.sync_session = self._create_sync_session()

    # The internal posting logic, now with backoff integrated for RequestExceptions
    @backoff.on_exception(
        backoff.expo,
        (RequestException,),  # Catch all connection/timeout errors (client-side)
        max_tries=4,  # Fewer tries, faster ultimate failure (for external handling)
        base=2,
        factor=1,
        max_time=30,  # Don't wait too long on connection failures
        giveup=lambda e: not isinstance(e, (Timeout, ConnectionError))  # Only retry network/connection issues
    )
    def _attempt_sync_post(self, url: str, data: dict) -> requests.Response:
        """Helper function to perform the actual network request, potentially retried by backoff."""
        return self.sync_session.post(url, json=data, timeout=(5, LLM_DEFAULT_TIMEOUT_S))

    def _post_sync_unified(self, endpoint: str, data: dict) -> APIResult:
        """
        Internal wrapper for synchronous POST requests, returns a structured APIResult.
        Handles: backoff retries, session reset, and error categorization.
        """
        url = self._construct_url(endpoint)

        try:
            # 1. 尝试执行请求，内部已集成 backoff 对 RequestException 的重试
            response = self._attempt_sync_post(url, data)

            status = response.status_code

            # 2. 成功响应 (200)
            if status == 200:
                return {"success": True, "data": response.json(), "error": None}

            # 3. HTTP 错误响应 (非 200) -> 归类错误

            # 瞬时服务器错误 (429, 5xx)
            if status in RETRYABLE_STATUS_CODES:
                error_code = f"HTTP_{status}"
                message = f"Transient Server Error: {status} ({response.text[:100]})"
                return _make_error_result("TRANSIENT_SERVER", error_code, message)

            # 永久性错误 (40x)
            else:
                error_code = f"HTTP_{status}"
                message = f"Permanent Error: {status} ({response.text[:100]})"
                return _make_error_result("PERMANENT", error_code, message)

        except RequestException as e:
            # 4. 彻底的网络连接失败 (backoff 内部重试已耗尽)

            # 尝试修复连接池
            try:
                self._reset_sync_session()
            except Exception as reset_e:
                # 如果连重置都会失败
                message = f"Network failure: {type(e).__name__}. Session reset failed: {reset_e}"
                return _make_error_result("TRANSIENT_NETWORK", "SESSION_RESET_FAILED", message)

            # 连接彻底失败，但会话已重置
            error_code = "CONNECTION_TIMEOUT" if isinstance(e, Timeout) else "PROXY_FAIL"
            message = f"Critical Network Failure ({error_code}). Session reset triggered. Last error: {e}"
            return _make_error_result("TRANSIENT_NETWORK", error_code, message)

        except Exception as e:
            # 5. 捕获其他意料之外的系统级错误
            message = f"Unexpected client error: {type(e).__name__}: {str(e)}"
            return _make_error_result("PERMANENT", "UNEXPECTED_CLIENT_ERROR", message)

    # --------------------------------------------------------------------------
    # Asynchronous Core Logic (Shared Session)
    # --------------------------------------------------------------------------

    async def _get_async_session(self):
        # 如果会话已存在且未关闭，直接返回
        if self._async_session is not None and not self._async_session.closed:
            return self._async_session

        # 警告：如果存在但已关闭，意味着上次使用后没有调用 self.close()，可能是资源泄漏
        if self._async_session is not None and self._async_session.closed:
            logger.warning("Existing async session was found closed. Creating a new one.")
            # 此时无需 await close()，因为它已关闭
            self._async_session = None  # 确保旧引用被释放

        # 配置新的连接器和超时
        # 考虑将 limit_per_host 设得更高，例如 50 或 100，以匹配 API QPS
        connector = TCPConnector(limit=100, limit_per_host=50) if TCPConnector else None
        timeout = aiohttp.ClientTimeout(total=LLM_DEFAULT_TIMEOUT_S, connect=10, sock_connect=10)

        # 创建新会话
        self._async_session = aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
            headers=self._get_dynamic_header()
        )
        return self._async_session

    @backoff.on_exception(
        backoff.expo,
        RETRYABLE_ASYNC_EXCEPTIONS,
        max_tries=6,
        base=2,
        factor=3,
        max_time=120,
        giveup=lambda e: not is_retryable_async_error(e)
    )
    async def _attempt_async_post(self, url: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Internal wrapper for asynchronous POST requests with integrated Backoff retries.
        Raises exceptions on failure (after retries are exhausted).
        """
        session = await self._get_async_session()

        async with session.post(
                url,
                json=data,
                headers=self._get_dynamic_header(),  # Inject latest token
                proxy=self._get_url_proxy(url)
        ) as response:
            if response.status == 429:
                logger.warning(f"Async Rate Limit (429) hit for {url}. Backoff will retry...")

            # Check for HTTP errors; this triggers the @backoff decorator or an outer try/except
            response.raise_for_status()
            return await response.json()

    async def _post_async_unified(self, endpoint: str, data: dict) -> APIResult:
        """
        Internal wrapper for asynchronous POST requests, returns a structured APIResult.
        Handles: backoff retries (via _attempt_async_post), and error categorization.
        """
        url = self._construct_url(endpoint)

        if not self._api_token or not aiohttp:··
            # 使用统一的错误处理函数进行检查
            if not self._api_token:
                return _make_error_result("PERMANENT", "MISSING_TOKEN", "API token is missing.")
            if not aiohttp:
                return _make_error_result("PERMANENT", "MISSING_DEPENDENCY",
                                          "aiohttp library not installed for async mode.")
        try:
            # 1. 尝试执行请求 (内部已集成 backoff 重试)
            response_json = await self._attempt_async_post(url, data)
            return {"success": True, "data": response_json, "error": None}

        except aiohttp.ClientResponseError as e:
            # 2. HTTP 错误响应 (非 200, 重试耗尽后抛出)
            status = e.status
            error_code = f"HTTP_{status}"

            if status in RETRYABLE_STATUS_CODES:
                message = f"Transient Server Error (Async): {status} ({e.message})"
                return _make_error_result("TRANSIENT_SERVER", error_code, message)
            else:
                message = f"Permanent Error (Async): {status} ({e.message})"
                return _make_error_result("PERMANENT", error_code, message)

        except RETRYABLE_ASYNC_EXCEPTIONS as e:
            # 3. 彻底的网络连接失败 (重试耗尽后抛出)
            message = f"Critical Network Failure (Async). Last error: {e}"
            return _make_error_result("TRANSIENT_NETWORK", "CONNECTION_TIMEOUT", message)

        except Exception as e:
            # 4. 捕获其他意料之外的系统级错误
            message = f"Unexpected client error (Async): {type(e).__name__}: {str(e)}"
            return _make_error_result("PERMANENT", "UNEXPECTED_CLIENT_ERROR", message)

    # --------------------------------------------------------------------------
    # Public API Methods
    # --------------------------------------------------------------------------

    def get_using_model(self) -> str:
        return self.using_model

    def get_model_list(self) -> Union[Dict[str, Any], requests.Response]:
        """Synchronously retrieves the list of available models."""
        if not self._api_token: return {'error': 'Missing API token'}

        url = self._construct_url("models")
        try:
            response = self.sync_session.get(url, timeout=LLM_DEFAULT_TIMEOUT_S)
            return response.json() if response.status_code == 200 else response
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to get model list: {e}")
            self._reset_sync_session()
            return {'error': str(e)}

    def create_chat_completion_sync(self, messages: List[Dict], model: str = None,
                                    temperature: float = 0.7, max_tokens: int = 4096) -> APIResult:
        """Creates a chat completion (Synchronous), returning a structured result."""
        if not self._api_token:
            return _make_error_result("PERMANENT", "MISSING_TOKEN", "API token is missing.")

        data = self._prepare_request_data(model=model, messages=messages,
                                          temperature=temperature, max_tokens=max_tokens)
        return self._post_sync_unified("chat/completions", data)

    def create_completion_sync(self, prompt: str, model: str = None,
                               temperature: float = 0.7, max_tokens: int = 4096) -> APIResult:
        """Creates a text completion (Synchronous), returning a structured result."""
        if not self._api_token:
            return _make_error_result("PERMANENT", "MISSING_TOKEN", "API token is missing.")

        data = self._prepare_request_data(model=model, prompt=prompt,
                                          temperature=temperature, max_tokens=max_tokens)
        return self._post_sync_unified("completions", data)

    async def create_chat_completion_async(self, messages: List[Dict], model: str = None,
                                           temperature: float = 0.7,
                                           max_tokens: int = 4096) -> APIResult:  # <--- 注意返回类型
        """Creates a chat completion (Asynchronous), returning a structured result."""
        data = self._prepare_request_data(model=model, messages=messages,
                                          temperature=temperature, max_tokens=max_tokens)
        return await self._post_async_unified("chat/completions", data)

    async def create_completion_async(self, prompt: str, model: str = None,
                                      temperature: float = 0.7, max_tokens: int = 4096) -> APIResult:  # <--- 注意返回类型
        """Creates a text completion (Asynchronous), returning a structured result."""
        data = self._prepare_request_data(model=model, prompt=prompt,
                                          temperature=temperature, max_tokens=max_tokens)
        return await self._post_async_unified("completions", data)

    async def close(self):
        """
        Gracefully closes both synchronous and asynchronous sessions.
        Should be called when the application is shutting down.
        """
        if self.sync_session:
            self.sync_session.close()

        if self._async_session and not self._async_session.closed:
            await self._async_session.close()

    # --------------------------------------------------------------------------
    # Internal Utilities
    # --------------------------------------------------------------------------

    def _construct_url(self, endpoint: str) -> str:
        """Joins the base URL with the endpoint."""
        base = self.api_base_url.rstrip('/')
        return f"{base}/{endpoint}"

    def _get_url_proxy(self, url: str) -> Optional[str]:
        """Selects the correct proxy based on the URL scheme (http vs https)."""
        if not self.proxies: return None
        return self.proxies.get("https") if url.startswith("https") else self.proxies.get("http")

    def _prepare_request_data(self, model=None, messages=None, **kwargs) -> Dict[str, Any]:
        """Standardizes the payload for OpenAI-compatible endpoints."""
        self.using_model = model or self.default_model
        request_data = {"model": self.using_model, **kwargs}
        if messages:
            request_data["messages"] = messages
        return request_data


# ----------------------------------------------------------------------------------------------------------------------

def create_ollama_client(model: Optional[str] = None):
    client = OpenAICompatibleAPI(
        api_base_url='http://localhost:11434/v1',
        token='x',
        default_model=model or 'qwen3:14b'
    )
    return client


def create_siliconflow_client(token: Optional[str] = None, model: Optional[str] = None):
    client = OpenAICompatibleAPI(
        api_base_url='https://api.siliconflow.cn/v1',
        token=token or os.getenv("SILICON_API_KEY"),
        default_model=model or 'Qwen/Qwen3-235B-A22B'
    )
    return client


def create_modelscope_client(token: Optional[str] = None, model: Optional[str] = None):
    client = OpenAICompatibleAPI(
        api_base_url='https://api-inference.modelscope.cn/v1',
        token=token or os.getenv("MODELSCOPE_API_KEY"),
        default_model=model or 'deepseek-ai/DeepSeek-V3.2-Exp'
    )
    return client


def create_gemini_client(token: Optional[str] = None, model: Optional[str] = None):
    client = OpenAICompatibleAPI(
        api_base_url='https://generativelanguage.googleapis.com/v1beta/openai',
        token=token or os.getenv("GEMINI_API_KEY"),
        default_model=model or 'models/gemini-pro-latest',
        proxies={
            "http": "http://127.0.0.1:10809",
            "https": "http://127.0.0.1:10809"
        }
    )
    return client
