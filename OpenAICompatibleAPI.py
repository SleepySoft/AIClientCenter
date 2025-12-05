import os
import uuid
import socket
import backoff
import logging
import asyncio
import requests
import threading
from typing import Optional, Dict, Any, Union, List
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from requests.exceptions import (RequestException, Timeout, ConnectionError,
                                 ConnectTimeout, ReadTimeout, ProxyError, SSLError)

try:
    from APIResult import APIResult
except ImportError:
    from .APIResult import APIResult


# --- Optional Dependencies ---
try:
    import aiohttp
    from aiohttp import TCPConnector
except ImportError:
    aiohttp = None
    TCPConnector = None

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


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
    if not aiohttp:
        return isinstance(e, asyncio.TimeoutError)

    # 1. 连接相关的错误（网络波动，插拔网线，代理挂了） -> 应该重试
    # 注意：aiohttp 区分 ServerDisconnected (连接断开) 和 Timeout
    if isinstance(e, (aiohttp.ClientConnectionError, aiohttp.ServerDisconnectedError)):
        return True

    # 2. 特别处理 TimeoutError
    if isinstance(e, asyncio.TimeoutError):
        # 如果是 Socket 连接超时 (握手慢)，可以重试
        # 但 Python 的 asyncio.TimeoutError 通常不区分得那么细，
        # 在 backoff 语境下，建议：如果已经等了 5 分钟才超时，就别重试了，用户等不起了。
        # 这里建议返回 False，或者依靠外部逻辑控制。
        # 为了稳妥，我们假设“连接超时”已经被 socket timeout (5s) 捕获并抛出特定错误
        # 而总超时通常意味着生成太慢，重试意义不大。
        return False  # <--- 建议改为 False，超时就报错给用户，不要让用户等 10 分钟

    # 3. HTTP 状态码
    if isinstance(e, aiohttp.ClientResponseError):
        return e.status in RETRYABLE_STATUS_CODES

    return False


def _should_giveup(e):
    # 1. 致命：如果是读取超时（AI生成超过5分钟），直接放弃！
    #    因为重试意味着你会再傻等5分钟。
    if isinstance(e, ReadTimeout):
        logger.error("ReadTimeout detected (AI took too long). Giving up immediately.")
        return True

    # 2. 致命：如果是 SSL 错误或其他非网络错误，放弃。
    if isinstance(e, SSLError):
        return True

    # 3. 重试：如果是连接超时 (ConnectTimeout) 或 网络连接错误 (ConnectionError/ProxyError)
    #    这些通常是瞬时的，值得重试。
    if isinstance(e, (ConnectTimeout, ConnectionError, ProxyError)):
        return False

    # 默认放弃其他未知的 RequestException
    return True


# --- Helper: Concise Log Handler ---
def log_retry_attempt(details):
    """
    Logs a short summary of the retry attempt.
    details is a dict provided by the backoff library.
    """
    exception_type = type(details['exception']).__name__
    # Format: [Retry #1] ConnectTimeout -> Wait 0.5s
    logger.warning(
        f"[Retry #{details['tries']}] {exception_type} -> Wait {details['wait']:.1f}s"
    )


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

            logger.info(f'API key updated. Changed from {token[-8:] if token else "None"} '
                        f'to {old_token[-8:] if old_token else "None"}')

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
            max_retries=0  # <--- 禁用底层重试，完全交由 backoff 接管
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)

        session.headers.update(self._get_dynamic_header())
        session.proxies = self.proxies
        return session

    def _reset_sync_session(self):
        """
        Forcefully recreates the session.
        No lock is used to avoid deadlocks during network freezes.
        """
        logger.warning("Resetting synchronous session...")

        # 1. 先创建新会话（避免中间出现 None 状态）
        new_session = self._create_sync_session()

        # 2. 交换引用
        old_session = self.sync_session
        self.sync_session = new_session

        # 3. 【关键修改】不要在主线程里同步等待 close()，防止卡死
        # 如果 old_session 的 socket 已经坏死，close() 可能会阻塞很久。
        # 我们直接把清理工作扔给线程去做，或者干脆只把它设为 None 让 GC 处理
        def safe_close(s):
            try:
                s.close()
            except Exception:
                pass

        if old_session:
            # 启动一个守护线程去关闭旧连接，不通过主线程等待
            threading.Thread(target=safe_close, args=(old_session,), daemon=True).start()

    def _attempt_sync_post_core(self, url: str, data: dict, timeout_tuple: tuple) -> requests.Response:
        """
        [NEW INTERNAL] The non-retried, core logic for POST request.
        """
        req_id = str(uuid.uuid4())[:8]

        try:
            logger.debug(f"[{req_id}] POST requesting {url}...")

            resp = self.sync_session.post(
                url,
                json=data,
                timeout=timeout_tuple  # 使用传入的 timeout
            )

            logger.debug(f"[{req_id}] POST success: {resp.status_code}")
            return resp

        except RequestException as e:
            logger.error(f"[{req_id}] POST failed/hung: {type(e).__name__} - {e}")
            raise e

    def _attempt_sync_post(self, url: str, data: dict, is_health_check: bool) -> requests.Response:
        """
        Public entry point for sync POST, applying backoff only if not a health check.
        """
        if is_health_check:
            # 健康检查模式：使用短超时 (e.g., 10s total) 且不重试
            # (ConnectTimeout, ReadTimeout)
            short_timeout = (5, 5)  # 5s 连接 + 5s 读取，总共 10s 快速失败
            return self._attempt_sync_post_core(url, data, short_timeout)
        else:
            # 正常模式：应用 Backoff 和长超时

            # 正常模式的超时配置 (保持原有逻辑)
            normal_timeout = (5, LLM_DEFAULT_TIMEOUT_S)

            # 使用 backoff 装饰器封装 core 逻辑
            @backoff.on_exception(
                backoff.expo,
                (RequestException,),
                max_tries=3,
                base=2,
                max_time=30,
                giveup=_should_giveup,
                on_backoff=log_retry_attempt
            )
            def _retry_wrapper():
                return self._attempt_sync_post_core(url, data, normal_timeout)

            return _retry_wrapper()

    def _post_sync_unified(self, endpoint: str, data: dict, is_health_check: bool = False) -> APIResult:
        """
        Internal wrapper for synchronous POST requests, returns a structured APIResult.
        Handles: backoff retries, session reset, and error categorization.
        """
        url = self._construct_url(endpoint)

        try:
            # 1. 执行请求 (Backoff 负责连接层重试，如超时/断网)
            response = self._attempt_sync_post(url, data, is_health_check)
            status = response.status_code

            # 2. 成功
            if status == 200:
                return {"success": True, "data": response.json(), "error": None}

            # -----------------------------------------------------------
            # 3. 关键修改：细分错误类型，不要统统算作 PERMANENT
            # -----------------------------------------------------------

            # A. 客户端侧错误 (400-499) -> 意味着 Prompt 有问题或 Auth 错误
            # 这种错误说明：
            # 1. 链路通畅 (Client是好的)
            # 2. 参数错误/内容违规 (换个 Client 也没用，不要重试)
            if 400 <= status < 500:
                # 特殊处理 429 (Too Many Requests) -> 这属于服务端限流，归类为瞬时错误
                if status == 429:
                    error_code = f"HTTP_{status}"
                    message = f"Rate Limit Hit: {status}"
                    return _make_error_result("TRANSIENT_SERVER", error_code, message)

                # 其他 4xx (400 Bad Request, 401 Unauthorized, 403 Forbidden)
                # 使用 "BAD_REQUEST" 类型，明确告知上层：别重试了，也没必要封禁 Client
                error_code = f"HTTP_{status}"
                # 截取一小段 error message 用于调试，但不依赖它做逻辑
                message = f"Client Side Error: {status} ({response.text[:100]})"
                return _make_error_result("BAD_REQUEST", error_code, message)

            # B. 服务端错误 (500-599) -> 意味着对方服务器挂了
            elif 500 <= status < 600:
                error_code = f"HTTP_{status}"
                message = f"Server Error: {status} ({response.text[:100]})"
                return _make_error_result("TRANSIENT_SERVER", error_code, message)

            # C. 其他未知状态码
            else:
                return _make_error_result("PERMANENT", f"HTTP_{status}", f"Unknown Status: {status}")

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
        if self._async_session is not None and not self._async_session.closed:
            return self._async_session

        if self._async_session is not None and self._async_session.closed:
            self._async_session = None

        # 优化 1: 强制使用 IPv4，极大减少本地代理环境下的连接延迟
        # limit_per_host 适当调大以支持并发
        connector = TCPConnector(
            limit=100,
            limit_per_host=50,
            family=socket.AF_INET,  # <--- 关键：强制 IPv4
            force_close=False
        )

        # 优化 2: 拆分超时
        # sock_connect: 建立连接的时间（握手），设短一点（如 5秒），连不上立刻重试
        # sock_read: 等待数据返回的时间，设长一点（如 300秒），生成慢不要紧
        timeout = aiohttp.ClientTimeout(
            total=LLM_DEFAULT_TIMEOUT_S,
            sock_connect=5,  # <--- 关键：连接超时设短，快速失败
            sock_read=LLM_DEFAULT_TIMEOUT_S
        )

        self._async_session = aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
            headers=self._get_dynamic_header()
        )
        return self._async_session

    @backoff.on_exception(
        backoff.expo,
        RETRYABLE_ASYNC_EXCEPTIONS,
        max_tries=3,    # <--- 降级：由 6 改为 3。对于用户交互，试 3 次连不上基本就是挂了。
        base=2,
        factor=1,       # <--- 加快重试节奏
        max_time=30,    # <--- 总共只花 30 秒尝试连接，连不上就报错。
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

        if not self._api_token or not aiohttp:
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
                                    temperature: float = 0.7, max_tokens: int = 4096,
                                    is_health_check: bool = False) -> APIResult:
        """Creates a chat completion (Synchronous), returning a structured result."""
        if not self._api_token:
            return _make_error_result("PERMANENT", "MISSING_TOKEN", "API token is missing.")

        data = self._prepare_request_data(model=model, messages=messages,
                                          temperature=temperature, max_tokens=max_tokens)
        return self._post_sync_unified("chat/completions", data, is_health_check=is_health_check)

    def create_completion_sync(self, prompt: str, model: str = None,
                               temperature: float = 0.7, max_tokens: int = 4096,
                               is_health_check: bool = False) -> APIResult:
        """Creates a text completion (Synchronous), returning a structured result."""
        if not self._api_token:
            return _make_error_result("PERMANENT", "MISSING_TOKEN", "API token is missing.")

        data = self._prepare_request_data(model=model, prompt=prompt,
                                          temperature=temperature, max_tokens=max_tokens)
        return self._post_sync_unified("completions", data, is_health_check=is_health_check)

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
