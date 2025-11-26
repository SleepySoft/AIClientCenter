import time
import logging
import requests
import datetime
import traceback
import threading
from enum import Enum
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, List, Union


logger = logging.getLogger(__name__)


CLIENT_PRIORITY_MOST_PRECIOUS = 100     # Precious API resource has the lowest using priority.
CLIENT_PRIORITY_EXPENSIVE = 80
CLIENT_PRIORITY_NORMAL = 50
CLIENT_PRIORITY_CONSUMABLES = 20
CLIENT_PRIORITY_FREEBIE = 0             # Prioritize using the regularly reset free quota

CLIENT_PRIORITY_HIGHER = -5
CLIENT_PRIORITY_LOWER = 5

CLIENT_PRIORITY_MORE_PRECIOUS = CLIENT_PRIORITY_LOWER
CLIENT_PRIORITY_LESS_PRECIOUS = CLIENT_PRIORITY_HIGHER


class ClientStatus(Enum):
    """Status of AI client"""
    UNKNOWN = "unknown"
    AVAILABLE = "available"
    ERROR = "error"
    UNAVAILABLE = "unavailable"

    @classmethod
    def _missing_(cls, value):
        return cls.ERROR


class BaseAIClient(ABC):
    """
    Base class for all AI clients.

    Capabilities:
    - Abstract interface for API calls.
    - Status management (Available/Busy/Error).

    Usage Tracking & Quotas:
    - This base class does NOT track token usage or limits.
    - To enable these features, your client class must inherit from `ClientMetricsMixin`.

    Example:
        class OpenAIClient(ClientMetricsMixin, BaseAIClient):
            def __init__(self, ...):
                ClientMetricsMixin.__init__(self, quota_config=...)
                BaseAIClient.__init__(self, ...)
    """

    def __init__(self, name: str, api_token: str, priority: int = CLIENT_PRIORITY_NORMAL):
        """
        Initialize AI client with token and priority.

        Args:
            name: The name of AI Client
            api_token: API token for authentication
            priority: Client priority (lower number = higher priority)
        """
        self.name = name
        self.api_token = api_token
        self.priority = priority

        self._lock = threading.RLock()
        self._status = {
            'status': ClientStatus.UNKNOWN,
            'status_last_updated': 0.0,
            'last_acquired': 0.0,
            'last_released': 0.0,
            'last_chat': 0.0,
            'last_test': 0.0,
            'acquire_count': 0,
            'error_count': 0,
            'error_sum': 0,
            'in_use': False,
            'acquired': False
        }

        self.test_prompt = "If you are working, please respond with 'OK'."
        self.expected_response = "OK"

    # -------------------------------------- User interface --------------------------------------

    def chat(self,
             messages: List[Dict[str, str]],
             model: Optional[str] = None,
             temperature: float = 0.7,
             max_tokens: int = 4096) -> Dict[str, Any]:

        with self._lock:
            if self._status['status'] == ClientStatus.UNAVAILABLE:
                return {'error': 'client_unavailable', 'message': 'Client is marked as unavailable.'}
            if self._status['in_use']:
                return {'error': 'client_busy', 'message': 'Client is busy (in use).'}
            self._status['in_use'] = True

        try:
            response = self._chat_completion_sync(messages, model, temperature, max_tokens)

            # 处理HTTP错误响应
            if hasattr(response, 'status_code') and response.status_code != 200:
                return self._handle_http_error(response)

            # 处理API响应错误
            if isinstance(response, dict) and 'error' in response:
                return self._handle_api_error(response)

            # 处理成功的LLM响应，检查业务逻辑错误
            if isinstance(response, dict) and 'choices' in response:
                return self._handle_llm_response(response, messages)

            # 未知响应格式
            logger.error(f"Unknown response format: {type(response)}")
            return {'error': 'unknown_response_format', 'message': 'Received unexpected response format'}

        except Exception as e:
            return self._handle_exception(e)

        finally:
            with self._lock:
                self._status['in_use'] = False

    def get_status(self, key: Optional[str] = None) -> Any:
        with self._lock:
            return self._status.get(key, None) if key else self._status.copy()

    def validate_response(self, response: Dict[str, Any], expected_content: Optional[str] = None) -> Optional[str]:
        """
        验证 Chat 响应的内容是否合法。
        用于业务层检查，或者 _test_and_update_status 内部检查。

        Usage:
            if error := client.validate_response(response, expected_content="JSON"):
                client.complain_error(error)
                print(f"Client {client.name} failed: {error}")
                # 触发重试逻辑...
            else:
                print("Success:", response['choices'][0]['message']['content'])

        Args:
            response: chat() 方法返回的字典
            expected_content: (可选) 期望在响应中出现的字符串，用于简单的关键词验证

        Returns:
            Optional[str]: 如果验证通过返回 None；如果不通过，返回错误原因字符串。
                           返回值可以直接传给 complain_error()。
        """
        # 1. 检查底层是否已经报错 (由 chat 内部捕获的)
        if 'error' in response:
            # 注意：chat 内部已经计过一次 error_count 了
            # 但如果用户显式调用 validate 并 complain，说明这对业务影响很大，再次计分也是允许的
            return f"API Internal Error: {response['message']}"

        # 2. 检查基本结构 (choices)
        choices = response.get('choices', [])
        if not choices:
            return "Invalid response structure: 'choices' is empty"

        # 3. 检查内容是否为空
        first_content = choices[0].get('message', {}).get('content', '')
        if not first_content:
            return "Response content is empty"

        # 4. (可选) 检查业务关键词
        if expected_content and expected_content not in first_content:
            return f"Content validation failed: '{expected_content}' not found in response"

        # 5. (可选) 可以在这里检查 finish_reason，如果业务非常严格的话
        # finish_reason = choices[0].get('finish_reason')
        # if finish_reason == 'length':
        #     return "Response truncated (length limit)"

        return None  # 一切正常

    def complain_error(self, reason: str = "Unspecified external error"):
        """
        外部主动报错接口。
        用于当 API 调用成功(HTTP 200)，但返回内容不符合预期（如逻辑错误、格式错误）时，
        由上层调用者手动标记此 Client 为不健康。
        """
        logger.warning(f"Client {self.name} received external complaint: {reason}")

        # 既然外部认为由于这个 Client 的原因导致任务失败，
        # 我们就应当增加错误计数，并将其标记为 ERROR，使其暂停接客或降低权重。
        self._increase_error_count()
        self._update_client_status(ClientStatus.ERROR)

    # =========================================================================
    # Metrics & Health Interface (Stubs)
    # =========================================================================
    # Note: The BaseAIClient provides NO built-in statistics tracking.
    # To enable usage tracking, quotas, and balance checks, your subclass
    # must inherit from 'ClientMetricsMixin' alongside this base class.
    # =========================================================================

    def record_usage(self, usage_data: Dict[str, Any]):
        """
        Records usage statistics (e.g., tokens, cost) for this request.

        [STUB IMPLEMENTATION]
        By default, this method does nothing.

        To enable functionality:
            Inherit from `ClientMetricsMixin`. It will override this method to
            accumulate stats (using Counter) and trigger quota checks.

        Args:
            usage_data (Dict[str, Any]): A dictionary of usage deltas.
                Standard keys used by the Mixin include:
                - 'prompt_tokens' (int)
                - 'completion_tokens' (int)
                - 'total_tokens' (int)
                - 'cost_usd' (float)
        """
        # Intentionally left empty to serve as an interface.
        pass

    def calculate_health(self) -> float:
        """
        Calculates the abstract health score of the client (0.0 to 100.0).

        [STUB IMPLEMENTATION]
        Returns 100.0 (Fully Healthy) by default.

        To enable functionality:
            Inherit from `ClientMetricsMixin`. It will implement logic to return
            lower scores based on exhausted quotas or low balances.

        Returns:
            float: Always 100.0 unless overridden.
        """
        return 100.0

    def get_standardized_metrics(self) -> List[Dict[str, Any]]:
        """
        Retrieves standardized metric details for reporting and health calculation.
        Used by the Manager to display quota progress bars or balance alerts.

        [STUB IMPLEMENTATION]
        Returns an empty list by default.

        To enable functionality:
            Inherit from `ClientMetricsMixin`. It will return structured data like:
            [{'type': 'USAGE_LIMIT', 'current': 500, 'target': 1000}, ...]

        Returns:
            List[Dict]: Empty list unless overridden.
        """
        return []

    # ---------------------------------------- Not for user ----------------------------------------

    def _is_busy(self) -> bool:
        """Check if client is currently in use."""
        with self._lock:
            return self._status['in_use']

    def _acquire(self) -> bool:
        """
        Attempt to acquire the client for use.

        Returns:
            bool: True if acquired successfully
        """
        with self._lock:
            if self._status['acquired'] or self._status['status'] == ClientStatus.UNAVAILABLE:
                return False

            self._status['acquired'] = True
            self._status['acquire_count'] += 1
            self._status['last_acquired'] = time.time()

            return True

    def _release(self):
        """Release the client after use."""
        with self._lock:
            self._status['acquired'] = False
            self._status['last_released'] = time.time()

    def _is_acquired(self) -> bool:
        with self._lock:
            return self._status['acquired']

    def _test_and_update_status(self) -> bool:
        """
        Refactored: Uses validate_response to standardize health checks.
        """
        try:
            # 1. 发起测试对话
            # chat() 内部处理网络层面的 Available/Error 状态切换
            result = self.chat(
                messages=[{"role": "user", "content": self.test_prompt}],
                max_tokens=100
            )

            if 'error' in result:
                # Error count is handled by chat.
                return False

            # 2. 验证响应逻辑
            # 直接使用公共的验证函数
            error_reason = self.validate_response(result, expected_content=self.expected_response)

            if error_reason:
                # 3. 如果有错，进行投诉
                # 这会增加 error_count 并将状态设为 ERROR
                self.complain_error(f"Self-test failed: {error_reason}")
                return False

            # 4. 验证通过
            # chat() 成功时会重置 count，但这里显式确认一下也好，或者完全依赖 chat 的重置
            # 建议：如果 validate_response 没问题，说明这是一个高质量的 Available
            self._reset_error_count()
            self._update_client_status(ClientStatus.AVAILABLE)
            return True

        except Exception as e:
            self.complain_error(f"Exception during self-test: {e}")
            return False
        finally:
            with self._lock:
                self._status['last_test'] = time.time()

    def _reset_error_count(self):
        with self._lock:
            self._status['error_count'] = 0

    def _increase_error_count(self):
        with self._lock:
            self._status['error_count'] += 1
            self._status['error_sum'] += 1

    def _update_client_status(self, new_status: ClientStatus):
        """Update client status with thread safety."""
        with self._lock:
            old_status = self._status['status']
            self._status['status'] = new_status
            self._status['status_last_updated'] = 0.0 if new_status == ClientStatus.UNKNOWN else time.time()

            if old_status != new_status:
                logger.info(f"Client {self.name} status changed from {old_status} to {new_status}")

    # ---------------------------------------- Error Handling ----------------------------------------

    def _handle_http_error(self, response) -> Dict[str, Any]:
        """处理HTTP错误状态码"""

        # --- 新增逻辑：尝试从 Body 中通过 API Error 逻辑判断是否 Fatal ---
        try:
            error_json = response.json()
            # 如果能解析出 JSON，且里面有 error 字段，直接转交给 handle_api_error 处理
            # 因为 handle_api_error 里的分类更精准 (比如区分了 Quota 和 RateLimit)
            if isinstance(error_json, dict) and 'error' in error_json:
                logger.info(f"Delegating HTTP {response.status_code} to API error handler")
                return self._handle_api_error(error_json)
        except Exception:
            # 解析失败，说明不是标准的 JSON 错误响应，继续走下面的 HTTP 状态码判断
            pass

        # 根据状态码分类错误类型
        if response.status_code in [400, 422]:
            # 错误请求 - 通常是参数错误，可能是可恢复的
            error_type = 'recoverable'
            logger.warning(f"Bad request error (recoverable): {response.status_code}")

        # Because of the API token rotation. We don't think these error are fatal.
        elif response.status_code == 401:
            # 认证失败 - 通常是不可恢复的致命错误
            error_type = 'fatal'
            logger.error("Authentication failed - invalid API token")

        elif response.status_code == 403:
            # 权限不足 - 可能是不可恢复的
            error_type = 'fatal'
            logger.error("Permission denied - check API permissions")

        elif response.status_code == 429:
            # 速率限制 - 可恢复错误，需要延迟重试
            error_type = 'recoverable'
            retry_after = response.headers.get('Retry-After', 60)
            logger.warning(f"Rate limit exceeded, retry after {retry_after}s")
            # 可以在这里实现延迟重试逻辑

        elif response.status_code >= 500:
            # 服务器错误 - 通常是临时的，可恢复
            error_type = 'recoverable'
            logger.warning(f"Server error {response.status_code}, may be temporary")

        else:
            # 其他HTTP错误
            error_type = 'recoverable'
            logger.warning(f"HTTP error {response.status_code}")

        if error_type == 'recoverable':
            self._update_client_status(ClientStatus.ERROR)
        elif error_type == 'fatal':
            self._update_client_status(ClientStatus.UNAVAILABLE)
        self._increase_error_count()

        logger.warning(f"Error reason: {response.text}")

        return {
            'error': 'http_error',
            'error_type': error_type,
            'status_code': response.status_code,
            'message': f"HTTP error {response.status_code}: {getattr(response, 'reason', 'Unknown')}"
        }

    def _handle_api_error(self, response: Dict[str, Any]) -> Dict[str, Any]:
        """处理API返回的业务错误"""
        error_data = response.get('error', {})
        error_message = error_data.get('message', 'Unknown API error') if isinstance(error_data, dict) else str(
            error_data)
        error_type = error_data.get('type', 'unknown') if isinstance(error_data, dict) else 'unknown'

        # 根据错误类型分类
        fatal_errors = {'invalid_request_error', 'insufficient_quota', 'billing_hard_limit_reached'}
        recoverable_errors = {'rate_limit_exceeded', 'server_error', 'timeout'}

        if error_type in fatal_errors:
            error_category = 'fatal'
            logger.error(f"Fatal API error: {error_message} (type: {error_type})")
        elif error_type in recoverable_errors:
            error_category = 'recoverable'
            logger.warning(f"Recoverable API error: {error_message}")
        else:
            error_category = 'recoverable'
            logger.warning(f"Unknown API error type: {error_type}, message: {error_message}")

        if error_category == 'recoverable':
            self._update_client_status(ClientStatus.ERROR)
        elif error_category == 'fatal':
            self._update_client_status(ClientStatus.UNAVAILABLE)
        self._increase_error_count()

        return {
            'error': 'api_error',
            'error_type': error_category,
            'api_error_type': error_type,
            'message': error_message
        }

    def _handle_exception(self, exception: Exception) -> Dict[str, Any]:
        """处理异常情况"""
        error_message = str(exception)

        # 根据异常类型分类
        if isinstance(exception, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
            # 网络相关异常 - 通常是可恢复的
            error_type = 'recoverable'
            logger.warning(f"Network error (recoverable): {error_message}")

        elif isinstance(exception, requests.exceptions.RequestException):
            # 其他请求异常
            error_type = 'recoverable'
            logger.warning(f"Request exception: {error_message}")

        elif isinstance(exception, (ValueError, TypeError)):
            # 参数错误 - 可能是不可恢复的编程错误
            error_type = 'fatal'
            logger.error(f"Parameter error (fatal): {error_message}")

        else:
            # 其他未知异常
            error_type = 'recoverable'
            logger.error(f"Unexpected error: {error_message}")

        if error_type == 'recoverable':
            self._update_client_status(ClientStatus.ERROR)
        elif error_type == 'fatal':
            self._update_client_status(ClientStatus.UNAVAILABLE)
        self._increase_error_count()

        return {
            'error': 'exception',
            'error_type': error_type,
            'exception_type': type(exception).__name__,
            'message': error_message
        }

    def _handle_llm_response(self, response: Dict[str, Any], original_messages: List[Dict[str, str]]) -> Dict[str, Any]:
        """处理成功的LLM响应，检查业务逻辑错误并统计使用情况"""
        try:
            choices = response.get('choices', [])
            if not choices:
                # 这是一个真正的 API 错误（协议层）
                self._increase_error_count()
                self._update_client_status(ClientStatus.ERROR)
                return {
                    'error': 'empty_response',
                    'error_type': 'recoverable',
                    'message': 'API returned empty choices array'
                }

            first_choice = choices[0]
            finish_reason = first_choice.get('finish_reason')

            # 'length' (达到max_tokens) 和 'content_filter' (内容审查)
            # 是 API 正常工作的表现，不应计入 Client 的 error_count。
            if finish_reason == 'length':
                logger.warning(f"Client {self.name}: Response truncated due to length.")
                # 这是一个 Warning，不是 Error，不要调用 _increase_error_count

            elif finish_reason == 'content_filter':
                logger.warning(f"Client {self.name}: Response triggered content filter.")

            try:
                # 统计token使用量
                if usage_data := response.get('usage', {}):
                    usage_data['request_count'] = 1
                    self.record_usage(usage_data)
            except Exception:
                pass

            # 只要能正常解析出 choices，就认为 Client 是健康的
            self._reset_error_count()
            self._update_client_status(ClientStatus.AVAILABLE)

            return response

        except Exception as e:
            logger.error(f"Error processing LLM response: {e}")
            # 解析过程崩了，可能是数据结构变了，算作错误
            self._increase_error_count()
            self._update_client_status(ClientStatus.ERROR)
            return {
                'error': 'response_processing_error',
                'error_type': 'recoverable',
                'message': f'Failed to process LLM response: {str(e)}'
            }

    # ---------------------------------------- Abstractmethod ----------------------------------------

    @abstractmethod
    def get_model_list(self) -> Dict[str, Any]:
        pass

    @abstractmethod
    def get_current_model(self) -> str:
        """
        Return the name of the model currently being used.
        Subclasses should override this if they support multiple models or rotation.
        """
        pass

    @abstractmethod
    def _chat_completion_sync(self,
                              messages: List[Dict[str, str]],
                              model: Optional[str] = None,
                              temperature: float = 0.7,
                              max_tokens: int = 4096) -> Union[Dict[str, Any], requests.Response]:
        pass


# ----------------------------------------------------------------------------------------------------------------------

class AIClientManager:
    """
    Management framework for AI clients with priority-based selection,
    health monitoring, and automatic client management.
    """

    def __init__(self, base_check_interval_sec: int = 60, first_check_delay_sec: int = 10):
        """
        Initialize client manager.

        Args:
            base_check_interval_sec: Base interval for health checks.
            first_check_delay_sec: Delay before the first check loop starts.
        """
        self.clients = []  # List of BaseAIClient instances

        # Map user_name to their acquired client info
        # Structure: { "user_name": {"client": client_obj, "last_used": timestamp} }
        self.user_client_map: Dict[str, Dict[str, Any]] = {}

        self._lock = threading.RLock()
        self.monitor_thread = None
        self.monitor_running = False

        # Configuration for the monitoring loop
        self.reset_fatal_interval = base_check_interval_sec * 30    # Interval to reset fatal to unknown or re-check
        self.check_error_interval = base_check_interval_sec         # Interval when client is in ERROR state
        self.check_stable_interval = base_check_interval_sec * 15   # Interval when client is AVAILABLE
        self.first_check_delay_sec = first_check_delay_sec

    def register_client(self, client: Any):
        """
        Register a new AI client.
        """
        with self._lock:
            self.clients.append(client)
            # Sort by priority (lower number = higher priority)
            # This ensures get_available_client always picks the best one first.
            self.clients.sort(key=lambda x: x.priority)
            logger.info(f"Registered client: {getattr(client, 'name', 'Unknown')}")

    def get_available_client(self, user_name: str) -> Optional[Any]:
        """
        Get an available client for a specific user.

        Logic:
        1. If user already holds a client:
           - If a higher priority client is free, release old and grab new.
           - If no higher priority client is free, keep the current one (refresh timestamp).
        2. If user holds no client:
           - Acquire the highest priority free client.

        Args:
            user_name: The identifier for the user requesting the client.

        Returns:
            BaseAIClient or None if no clients are available/healthy.
        """
        if not user_name:
            logger.error("user_name is required to get a client.")
            return None

        with self._lock:
            # Retrieve current allocation for this user
            current_allocation = self.user_client_map.get(user_name)
            current_client = current_allocation['client'] if current_allocation else None

            # If the current client is effectively dead/removed, treat user as having no client
            if current_client and (current_client not in self.clients or
                                   current_client.get_status('status') in [ClientStatus.ERROR, ClientStatus.UNAVAILABLE]):
                self._release_user_resources(user_name)
                current_client = None

            # Iterate through clients (already sorted by priority: High -> Low)
            for client in self.clients:
                client_status = client.get_status('status')

                # 1. Filter out permanently dead clients
                if client_status == ClientStatus.UNAVAILABLE: continue
                # TODO: Set an error threshold to not select this client.
                if client_status == ClientStatus.ERROR and client.get_status('error_count') > 2: continue

                # 2. Check dynamic health (Optional optimization)
                if client.calculate_health() <= 0:
                    continue

                # 3. Logic for selection
                # Case A: We found the client currently held by this user.
                # Since we iterate by priority, if we reached here, it means
                # no *higher* priority client was free. So we keep this one.
                if client is current_client:
                    self.user_client_map[user_name]['last_used'] = time.time()
                    logger.debug(f"User {user_name} keeps client: {client.name}")
                    return client

                # Case B: We found a free client (not busy).
                # Since this appears *before* the current_client in the loop (or user has no client),
                # this client has higher priority. We should take it.
                if not client._is_busy():
                    # Try to acquire the lock/token for the new client
                    if client._acquire():
                        # If user had an old client, release it first
                        if current_client:
                            self._release_client_core(current_client)
                            logger.info(
                                f"User {user_name} switching from {current_client.name} to better client {client.name}")

                        # Update map with new client
                        self.user_client_map[user_name] = {
                            "client": client,
                            "last_used": time.time()
                        }
                        logger.info(f"User {user_name} acquired client: {client.name}")
                        return client

            # If we exit loop and user had a client but it wasn't found (should be covered by initial check)
            # or no suitable client found at all.
            return None

    def release_client(self, client: BaseAIClient | str):
        """
        Release the client currently held by the specified user.
        This should be called when the user session ends or they want to free resources.
        """
        with self._lock:
            keys_to_remove = [k for k, v in self.user_client_map.items() if v['client'] == client] \
                if isinstance(client, BaseAIClient) else [str(client)]
            for key in keys_to_remove:
                self._release_user_resources(key)

    def _release_client_core(self, client: Any):
        """Internal helper to release the physical client lock."""
        if hasattr(client, '_release'):
            client._release()

    def _release_user_resources(self, user_name: str):
        """Internal helper to clean up user map entries without double-releasing if client is dead."""
        if user_name in self.user_client_map:
            # We might want to attempt release just in case, usually safe
            client = self.user_client_map[user_name]['client']
            self._release_client_core(client)
            del self.user_client_map[user_name]

    def get_client_stats(self) -> Dict[str, Any]:
        """
        Get comprehensive statistics about all clients.
        Enhancements: Added error rates, hold durations, and detailed timing.
        """
        with self._lock:
            now = time.time()

            # 1. User Allocation Lookup
            client_to_user_info = {}
            for u_name, info in self.user_client_map.items():
                client_to_user_info[info['client']] = {
                    "user": u_name,
                    "start_time": info['last_used']  # 假设这个key存的是分配时间
                }

            # 2. Categorize Clients
            # 使用 getattr 避免 AttributeError，如果没有 status 属性则默认 UNKNOWN
            available_cnt = sum(1 for c in self.clients if c.get_status('status') == 'AVAILABLE')  # 假设是字符串或枚举
            busy_cnt = sum(1 for c in self.clients if c._is_busy())
            error_cnt_clients = sum(1 for c in self.clients if c.get_status('error_count') > 0)

            client_details = []
            for client in self.clients:
                # --- Extract Data ---
                health_score = client.calculate_health() if hasattr(client, 'calculate_health') else 100
                metrics_detail = client.get_standardized_metrics() if hasattr(client,
                                                                              'get_standardized_metrics') else {}

                # Access internal status directly for raw counters
                raw_status = client._status

                # User Info
                allocation = client_to_user_info.get(client, None)

                # --- Derived Metrics ---
                # 1. Duration (How long held or how long idle)
                duration = 0.0
                if client._is_busy() and raw_status.get('last_acquired'):
                    duration = now - raw_status['last_acquired']
                elif raw_status.get('last_released'):
                    duration = now - raw_status['last_released']

                # 2. Error Rate
                total_ops = raw_status.get('acquire_count', 0)
                err_count = raw_status.get('error_count', 0)
                err_sum = raw_status.get('error_sum', 0)
                err_rate = (err_sum / total_ops * 100) if total_ops > 0 else 0.0

                client_details.append({
                    "meta": {
                        "name": getattr(client, "name", "Unknown"),
                        "type": client.__class__.__name__,
                        "priority": client.priority,
                    },
                    "state": {
                        "status": client.get_status('status'),
                        "is_busy": client._is_busy(),
                        "health_score": health_score,
                        "last_active_ts": raw_status.get('status_last_updated', 0.0),
                    },
                    "allocation": {
                        "held_by": allocation['user'] if allocation else None,
                        "held_since": allocation['start_time'] if allocation else None,
                        "duration_seconds": duration if allocation else 0
                    },
                    "runtime_stats": {
                        "acquire_count": total_ops,
                        "error_count": err_count,
                        "error_sum": err_sum,
                        "error_rate_percent": round(err_rate, 1),
                        "last_chat_ts": raw_status.get('last_chat', 0),
                    },
                    "metrics": metrics_detail  # Token limits, RPM, etc.
                })

            # Sort: 1. By Priority (asc), 2. By Busy Status (busy first), 3. By Health (desc)
            client_details.sort(key=lambda x: (
                x['meta']['priority'],
                not x['state']['is_busy'],
                -x['state']['health_score']
            ))

            return {
                "summary": {
                    "timestamp": now,
                    "total_clients": len(self.clients),
                    "available": available_cnt,
                    "busy": busy_cnt,
                    "clients_with_errors": error_cnt_clients,
                    "active_users": len(self.user_client_map),
                    "system_load": f"{(busy_cnt / len(self.clients) * 100):.1f}%" if self.clients else "0%"
                },
                "clients": client_details
            }

    def get_client_by_name(self, name: str) -> Optional[BaseAIClient]:
        """Helper to find a client by name."""
        with self._lock:
            return next((c for c in self.clients if getattr(c, 'name', '') == name), None)

    def trigger_manual_check(self, client_name: str) -> bool:
        """
        Manually trigger a health check for a specific client.
        Thread-safe wrapper around the private check logic.
        """
        client = self.get_client_by_name(client_name)
        if not client:
            return False

        logger.info(f"Manual check triggered for {client_name}")
        # 尝试获取锁并执行检查
        if client._acquire():
            try:
                return client._test_and_update_status()
            finally:
                client._release()
        else:
            logger.warning(f"Could not acquire lock for {client_name} manual check")
            return False

    def set_client_status(self, client_name: str, status: ClientStatus) -> bool:
        """
        Manually force a status change for a client.
        """
        client = self.get_client_by_name(client_name)
        if not client:
            return False

        client._update_client_status(status)
        if status == ClientStatus.AVAILABLE:
            client._reset_error_count()
        return True

    @staticmethod
    def format_stats_report(stats_data: Dict[str, Any]) -> str:
        """
        Formats the dict returned by get_client_stats into a readable dashboard string.
        """
        summary = stats_data.get('summary', {})
        clients = stats_data.get('clients', [])
        now = time.time()

        # --- Helpers ---
        def _time_ago(ts):
            if not ts or ts == 0: return "-"
            diff = now - ts
            if diff < 60: return f"{int(diff)}s ago"
            if diff < 3600: return f"{int(diff / 60)}m ago"
            return f"{int(diff / 3600)}h ago"

        def _progress_bar(val, max_val=100, width=10):
            percent = val / max_val
            fill = int(width * percent)
            # Visual indicator: High health = Green-ish (using characters)
            return f"[{'#' * fill}{'.' * (width - fill)}]"

        # --- Header Section ---
        lines = ["=" * 80,
                 f" AI CLIENT MANAGER DASHBOARD | {datetime.datetime.fromtimestamp(summary['timestamp']).strftime('%Y-%m-%d %H:%M:%S')}",
                 "-" * 80, f" Clients: {summary['total_clients']} | "
                           f"Avail: {summary['available']} | "
                           f"Busy: {summary['busy']} | "
                           f"Users: {summary['active_users']} | "
                           f"Load: {summary['system_load']}", "=" * 80]

        # KPIs

        # --- Table Header ---
        # Col widths: Name(15) Prio(4) Stat(10) Health(16) User/Duration(20) Stats(12)
        header = f"{'CLIENT NAME':<18} {'PRIO':<5} {'STATUS':<10} {'HEALTH':<14} {'USER / DURATION':<22} {'STATS (Acq/Err)'}"
        lines.append(header)
        lines.append("-" * 80)

        # --- Rows ---
        for c in clients:
            meta = c['meta']
            state = c['state']
            alloc = c['allocation']
            run = c['runtime_stats']

            # 1. Name & Priority
            name_str = meta['name'][:17]
            prio_str = str(meta['priority'])

            # 2. Status & Icon
            status_raw = str(state['status']).split('.')[-1]  # Get 'AVAILABLE' from enum
            status_icon = "🟢"
            if state['is_busy']: status_icon = "🟡"  # Busy
            if status_raw in ['UNAVAILABLE', 'ERROR']: status_icon = "🔴"
            status_str = f"{status_icon} {status_raw[:7]}"

            # 3. Health Bar
            health_val = state['health_score']
            health_str = f"{_progress_bar(health_val)} {int(health_val)}%"

            # 4. Allocation info
            if state['is_busy'] and alloc['held_by']:
                user_str = f"{alloc['held_by'][:10]}"
                dur_str = f"{int(alloc['duration_seconds'])}s"
                alloc_str = f"👤 {user_str:<10} ({dur_str})"
            elif state['is_busy']:
                alloc_str = "🟡 System/Busy"
            else:
                alloc_str = "⚪ Idle"

            # 5. Stats (Acquire Count / Error Count)
            stats_str = f"Use:{run['acquire_count']:<3} Err:{run['error_count']}"

            # Combine
            row = f"{name_str:<18} {prio_str:<5} {status_str:<10} {health_str:<14} {alloc_str:<22} {stats_str}"
            lines.append(row)

            # Optional: Add error detail line if health is low
            if health_val < 60:
                lines.append(f"   ↳ ⚠️ Low Health Warning. Last Active: {_time_ago(state['last_active_ts'])}")

        lines.append("=" * 80)
        return "\n".join(lines)

    def start_monitoring(self):
        """Start background monitoring of client health."""
        if self.monitor_running:
            return

        self.monitor_running = True
        self.monitor_thread = threading.Thread(name='AIClientManager Monitoring', target=self._monitor_loop, daemon=True)
        self.monitor_thread.start()
        logger.info("Started AI client monitoring")

    def stop_monitoring(self):
        """Stop background monitoring."""
        self.monitor_running = False
        if self.monitor_thread:
            self.monitor_thread.join(timeout=10)
        logger.info("Stopped AI client monitoring")

    def _monitor_loop(self):
        """Background monitoring loop."""
        logger.info("Monitor loop started.")
        while self.monitor_running:
            # Initial startup delay
            if self.first_check_delay_sec > 0:
                self.first_check_delay_sec -= 1
                time.sleep(1)
                continue

            try:
                self._check_client_health()
                # - Do not clean up the unavailable clients.
                # - Because the limit will be reset by time or by changing token.
                # self._cleanup_unavailable_clients()
                # - Optional: Auto-release idle clients held by users for too long?
                # self._cleanup_idle_user_sessions()
            except Exception as e:
                # Prevent monitor thread from crashing entirely
                print(traceback.format_exc())
                logger.error(f"Error in monitor loop: {e}")

            # Sleep with a small deviation to avoid thundering herd if multiple managers exist
            time.sleep(self.check_error_interval)

    def _check_client_health(self):
        """
        Trigger active health checks (connectivity/latency) for eligible clients.
        This does NOT recalculate quota/balance logic (handled by Mixin internally).
        """
        clients_to_check = []

        with self._lock:
            for client in self.clients:
                client_status = client.get_status('status')
                status_last_updated = client.get_status('status_last_updated')

                if (client_error_count := client.get_status('error_count')) > 0:
                    adjusted_error_interval = min(self.check_error_interval * client_error_count,
                                                  self.reset_fatal_interval)
                else:
                    adjusted_error_interval = self.check_error_interval

                # Determine timeout based on current status
                timeout = {
                    ClientStatus.UNKNOWN.value :    0,
                    ClientStatus.AVAILABLE.value :  self.check_stable_interval,
                    # Just treat error and fatal as the same.
                    ClientStatus.ERROR.value:       adjusted_error_interval,
                    ClientStatus.UNAVAILABLE.value: adjusted_error_interval,
                }.get(client_status.value, adjusted_error_interval)

                # print(f"client_status 的类 ID: {id(client_status.__class__)}")
                # print(f"ClientStatus 类的 ID: {id(ClientStatus)}")
                # print(f"是否同一个类: {client_status.__class__ is ClientStatus}")
                #
                # print(f"client_status 所属的模块: {client_status.__class__.__module__}")
                # print(f"ClientStatus 所属的模块: {ClientStatus.__module__}")

                if time.time() - status_last_updated > timeout:
                    clients_to_check.append(client)

        # Perform checks outside the main lock to avoid blocking get_available_client
        for client in clients_to_check:
            client_name = getattr(client, 'name', 'Unknown Client')
            logger.debug(f'Checking connectivity for {client_name}...')

            # This method usually pings the API or checks simple connectivity
            if client._acquire():
                result = client._test_and_update_status()
                client._release()

                if not result:
                    logger.error(f"Status check - {client_name}: Unknown error.")
            else:
                logger.debug(f"Status check - Cannot acquire {client_name}.")

    def _cleanup_unavailable_clients(self):
        """
        Remove clients that are marked as UNAVAILABLE (permanently dead).
        Also cleans up user mappings if their held client is removed.
        """
        with self._lock:
            initial_count = len(self.clients)

            # Identify clients to remove
            clients_to_remove = [
                c for c in self.clients
                if c.get_status('status') == ClientStatus.UNAVAILABLE
            ]

            if not clients_to_remove:
                return

            # Remove from main list
            self.clients = [c for c in self.clients if c not in clients_to_remove]

            # Clean up user mappings that refer to removed clients
            users_to_clear = []
            for user, info in self.user_client_map.items():
                if info['client'] in clients_to_remove:
                    users_to_clear.append(user)

            for user in users_to_clear:
                # Note: No need to call _release() as client is dead/unavailable
                del self.user_client_map[user]
                logger.info(f"Removed allocation for user {user} (Client became unavailable)")

            removed = initial_count - len(self.clients)
            if removed > 0:
                logger.info(f"Cleaned up {removed} unavailable clients.")
