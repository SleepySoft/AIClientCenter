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

# --- Optional Dependencies ---
try:
    import aiohttp
    from aiohttp import TCPConnector
except ImportError:
    aiohttp = None
    TCPConnector = None

logger = logging.getLogger(__name__)

"""
Siliconflow Reply example:
{
  "id": "0196f08d74220b683a08ca3630683a51",         # 唯一标识符，用于追踪API调用记录
  "object": "chat.completion",                      # 标识响应类型，chat.completion表示这是聊天补全类型的响应
  "created": 1747792524,                            # Unix时间戳，表示API请求处理完成的时间（示例值1747792524对应北京时间2025-05-21 15:55:24）
  "model": "Qwen/Qwen3-235B-A22B",                  # 实际使用的模型标识，示例中Qwen/Qwen3-235B-A22B表明调用了第三方适配的千问模型
  "choices": [                                      # 包含生成结果的容器，常规场景下仅有一个元素
    {
      "index": 0,                                   # 候选结果的序号（多候选时有效）
      "message": {                                  # 生成的对话消息对象
        "role": "assistant",                        # 消息来源标识（assistant表示AI生成）
        "content": ""                               # 实际生成的文本内容 <-- **需要关注该内容**
		},
      "finish_reason": "stop"                       # 生成终止原因，"stop"表示自然结束
    }
  ],
  "usage": {                                        # 资源消耗统计
    "prompt_tokens": 22,                            # 输入消耗的token数
    "completion_tokens": 254,                       # 输出消耗的token数
    "total_tokens": 276,                            # 总token数
    "completion_tokens_details": {                  # 扩展字段
      "reasoning_tokens": 187                       # 推理过程消耗的token数
    }
  },
  "system_fingerprint": ""                          # 系统指纹标识，用于追踪模型版本信息（示例为空说明未启用该功能）
}
"""

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
    # Synchronous Core Logic (Auto-Healing)
    # --------------------------------------------------------------------------

    def _create_sync_session(self) -> requests.Session:
        """Configures a new requests.Session with connection pooling and basic retries."""
        session = requests.Session()

        # Configure connection pool size to handle multithreaded usage
        adapter = HTTPAdapter(
            pool_connections=20,
            pool_maxsize=20,
            max_retries=Retry(
                total=3,  # Low-level TCP retries
                backoff_factor=1,
                status_forcelist=list(RETRYABLE_STATUS_CODES),
                allowed_methods=["GET", "POST"],
                respect_retry_after_header=True
            )
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)

        session.headers.update(self._get_dynamic_header())
        session.proxies = self.proxies
        return session

    def _reset_sync_session(self):
        """
        CRITICAL: Forcefully closes and recreates the synchronous session.

        This is used to recover from 'zombie' connections where the client believes
        a TCP connection is open, but the server/proxy has dropped it.
        """
        with self.lock:
            try:
                logger.warning("Reseting synchronous session to recover from connection error...")
                self.sync_session.close()
            except Exception:
                pass  # Ignore errors during cleanup
            self.sync_session = self._create_sync_session()

    def _post_sync(self, endpoint: str, data: dict) -> Union[Dict[str, Any], requests.Response]:
        """
        Internal wrapper for synchronous POST requests.
        Handles URL construction, error logging, and triggers session reset on failure.
        """
        url = self._construct_url(endpoint)

        try:
            # Attempt request with standard timeout
            response = self.sync_session.post(url, json=data, timeout=(5, LLM_DEFAULT_TIMEOUT_S))

            if response.status_code == 200:
                return response.json()

            logger.error(f"Sync request to {endpoint} failed ({response.status_code}): {response.text[:200]}")
            return response

        except requests.exceptions.RequestException as e:
            # On any connection error (timeout, DNS, proxy fail), assume the session is tainted.
            logger.error(f"Critical sync connection error: {e}. Triggering session reset.")
            self._reset_sync_session()
            return {'error': f'Connection failed and session reset. Last error: {str(e)}'}

    # --------------------------------------------------------------------------
    # Asynchronous Core Logic (Shared Session)
    # --------------------------------------------------------------------------

    async def _get_async_session(self):
        """
        Retrieves the existing async session or creates a new one if it's closed/missing.
        Uses a shared session to preserve TCP connections (Keep-Alive).
        """
        if self._async_session is not None and not self._async_session.closed:
            # Ensure the session belongs to the current running loop
            if self._async_session.loop is asyncio.get_running_loop():
                return self._async_session
            await self._async_session.close()

        # Configure connector limits to prevent ephemeral port exhaustion
        connector = TCPConnector(limit=100, limit_per_host=20) if TCPConnector else None

        timeout = aiohttp.ClientTimeout(total=LLM_DEFAULT_TIMEOUT_S, connect=10, sock_connect=10)

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
    async def _post_async(self, endpoint: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Internal wrapper for asynchronous POST requests.
        Handles Backoff retries, Rate Limits (429), and JSON parsing.
        """
        if not self._api_token: return {'error': 'Missing API token'}
        if not aiohttp: return {'error': 'aiohttp library not installed'}

        session = await self._get_async_session()
        url = self._construct_url(endpoint)

        try:
            async with session.post(
                    url,
                    json=data,
                    headers=self._get_dynamic_header(),  # Inject latest token
                    proxy=self._get_url_proxy(url)
            ) as response:

                if response.status == 429:
                    logger.warning(f"Async Rate Limit (429) hit for {endpoint}. Backoff will retry...")

                # Check for HTTP errors; this triggers the @backoff decorator
                response.raise_for_status()
                return await response.json()

        except aiohttp.ClientConnectorError as e:
            logger.warning(f"Async connection error to {url}: {e}")
            raise  # Re-raise to trigger backoff

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
                                    temperature: float = 0.7, max_tokens: int = 4096):
        """Creates a chat completion (Synchronous)."""
        if not self._api_token: return {'error': 'Missing API token'}
        data = self._prepare_request_data(model=model, messages=messages,
                                          temperature=temperature, max_tokens=max_tokens)
        return self._post_sync("chat/completions", data)

    def create_completion_sync(self, prompt: str, model: str = None,
                               temperature: float = 0.7, max_tokens: int = 4096):
        """Creates a text completion (Synchronous)."""
        if not self._api_token: return {'error': 'Missing API token'}
        data = self._prepare_request_data(model=model, prompt=prompt,
                                          temperature=temperature, max_tokens=max_tokens)
        return self._post_sync("completions", data)

    async def create_chat_completion_async(self, messages: List[Dict], model: str = None,
                                           temperature: float = 0.7, max_tokens: int = 4096):
        """Creates a chat completion (Asynchronous)."""
        data = self._prepare_request_data(model=model, messages=messages,
                                          temperature=temperature, max_tokens=max_tokens)
        return await self._post_async("chat/completions", data)

    async def create_completion_async(self, prompt: str, model: str = None,
                                      temperature: float = 0.7, max_tokens: int = 4096):
        """Creates a text completion (Asynchronous)."""
        data = self._prepare_request_data(model=model, prompt=prompt,
                                          temperature=temperature, max_tokens=max_tokens)
        return await self._post_async("completions", data)

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


def main():
    try:
        from MyPythonUtility.DictTools import DictPrinter
    except Exception as e:
        print(str(e))
        DictPrinter = None
    finally:
        pass

    # Initialize the client - token can be passed directly or will be fetched from environment
    client = create_gemini_client()

    model_list = client.get_model_list()
    print(f'Model list of {client.api_base_url}')

    if isinstance(model_list, dict) and DictPrinter:
        print(DictPrinter.pretty_print(model_list))
    else:
        print(model_list)

    # Example synchronous chat completion
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "你是谁?"}
    ]

    response = client.create_chat_completion_sync(messages=messages)
    print("Synchronous chat completion response:")
    print(json.dumps(response, indent=2, ensure_ascii=False))

    # # Example synchronous text completion
    # prompt = "Once upon a time in a land far away,"
    # response = client.create_completion_sync(prompt=prompt)
    # print("\nSynchronous text completion response:")
    # print(json.dumps(response, indent=2))

    # Example asynchronous usage requires asyncio
    import asyncio

    async def async_demo():
        # Example async chat completion
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "What is the meaning of life?"}
        ]

        response = await client.create_chat_completion_async(messages=messages)
        print("\nAsynchronous chat completion response:")
        print(json.dumps(response, indent=2))

        # Example async text completion
        prompt = "The capital of France is"
        response = await client.create_completion_async(prompt=prompt)
        print("\nAsynchronous text completion response:")
        print(json.dumps(response, indent=2))

    # Run the async demo
    asyncio.run(async_demo())


# Example usage
if __name__ == "__main__":
    main()
