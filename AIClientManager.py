"""
API_CORE: Robust OpenAI-Compatible API Client

This module defines the core client logic, standardized result structure (APIResult),
and the error classification system used across all synchronous and asynchronous API calls.
The client implements intelligent retries (exponential backoff) and session self-healing
for transient errors.
"""
import socket
import time
import logging
import datetime
import traceback
import threading
from enum import Enum
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, List

from AIClientCenter.APIResult import APIResult
from AIClientCenter.ClientStateSQLiteLogger import ClientStateSQLiteLogger

logger = logging.getLogger(__name__)

CLIENT_PRIORITY_MOST_PRECIOUS = 100  # Precious API resource has the lowest using priority.
CLIENT_PRIORITY_EXPENSIVE = 80
CLIENT_PRIORITY_NORMAL = 50
CLIENT_PRIORITY_CONSUMABLES = 20
CLIENT_PRIORITY_FREEBIE = 0  # Prioritize using the regularly reset free quota

CLIENT_PRIORITY_HIGHER = -5
CLIENT_PRIORITY_LOWER = 5

CLIENT_PRIORITY_MORE_PRECIOUS = CLIENT_PRIORITY_LOWER
CLIENT_PRIORITY_LESS_PRECIOUS = CLIENT_PRIORITY_HIGHER


# 设置全局默认 Socket 超时
# 防止 DNS 解析或 SSL 握手无限期卡死（requests 的 timeout 有时管不到这里）
# 实践中来看，硅基流动的API频繁会触发这个timeout，设置短一些以提高效率。一般对话不会超过3分钟。
socket.setdefaulttimeout(300)


class ClientStatus(Enum):
    """Status of AI client"""
    UNKNOWN = "unknown"
    AVAILABLE = "available"
    ERROR = "error"
    UNAVAILABLE = "unavailable"

    @classmethod
    def _missing_(cls, value):
        return cls.ERROR


class ClientVisibility(Enum):
    PUBLIC = "public"          # 正常参与自动分配
    PRIVATE = "private"        # 默认不参与，除非调用者允许/指定
    NAME_ONLY = "name_only"    # 只允许按 target_client_name 精确获取


class BaseAIClient(ABC):
    """
    Base class for all AI clients.

    Capabilities:
    - Abstract interface for API calls.
    - Status management (Available/Busy/Error).
    - Unified error handling leveraging the structured APIResult from the underlying client.

    Usage Tracking & Quotas:
    - This base class does NOT track token usage or limits.
    - To enable these features, your client class must inherit from `ClientMetricsMixin`.

    Example:
        class OpenAIClient(ClientMetricsMixin, BaseAIClient):
            def __init__(self, ...):
                ClientMetricsMixin.__init__(self, quota_config=...)
                BaseAIClient.__init__(self, ...)
    """

    def __init__(self,
                 name: str,
                 api_token: str,
                 priority: int = CLIENT_PRIORITY_NORMAL,
                 group_id: str = "default",
                 visibility: ClientVisibility = ClientVisibility.PUBLIC):
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
        self.group_id = group_id
        self.visibility = visibility

        self.event_sink: Optional[ClientStateSQLiteLogger] = None

        self._lock = threading.RLock()
        self._status = {
            'status': ClientStatus.UNKNOWN,
            'status_last_updated': 0.0,
            'last_acquired': 0.0,
            'last_released': 0.0,
            'last_chat': 0.0,
            'last_test': 0.0,
            'acquire_count': 0,
            'chat_count': 0,
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
             max_tokens: int = 4096,
             is_health_check: bool = False) -> Dict[str, Any]:
        """
        Executes a chat completion request.

        This method is the primary entry point for consumers and handles:
        1. Client status check (busy/unavailable).
        2. Locking/unlocking the client.
        3. Calling the subclass's API execution method (`_chat_completion_sync`).
        4. Translating the unified `APIResult` into a final status/response.
        """

        with self._lock:
            if self._status['status'] == ClientStatus.UNAVAILABLE:
                return {'error': 'client_unavailable', 'message': 'Client is marked as unavailable.'}
            if self._status['in_use']:
                return {'error': 'client_busy', 'message': 'Client is busy (in use).'}
            self._status['in_use'] = True
            self._status['chat_count'] += 1

        start_ts = time.time()
        chosen_model = model or (self.get_current_model() if hasattr(self, "get_current_model") else None)

        self._emit_event({
            "type": "chat_start",
            "ts": start_ts,
            "client_name": self.name,
            "model": chosen_model,
            "is_health_check": bool(is_health_check),
        })

        final_success = False
        final_error = None

        try:
            # Subclass implements this, returning the structured APIResult from API_CORE
            result: APIResult = self._chat_completion_sync(messages, model, temperature, max_tokens, is_health_check)

            # --- New Logic: Handle based on APIResult structure ---

            # 1. Successful response (API_CORE handled all retries/connections)
            if result.get('success', False):
                final_success = True
                # result['data'] is the LLM response JSON
                return self._handle_llm_response(result['data'], messages)

            # 2. Failed response (error reported by API_CORE)
            error_data = result.get('error')

            final_success = False
            final_error = error_data

            if error_data:
                # Delegate to the unified error handler
                return self._handle_unified_error(error_data)

            # 3. Unknown failure mode (should not happen if API_CORE is working)
            logger.error(f"Unknown APIResult structure: {result}")
            return self._handle_exception(ValueError("API client returned an ambiguous result."))

        except Exception as e:
            final_error = {"type": "EXCEPTION", "code": '', "message": str(e)}
            # Catches errors happening outside of the API call (e.g., locking issue, subclass error)
            return self._handle_exception(e)

        finally:
            with self._lock:
                self._status['in_use'] = False
                # 重要：无论成功或失败，只要尝试了 chat，都应该更新 last_chat
                self._status['last_chat'] = time.time()

            end_ts = time.time()

            self._emit_event({
                "type": "chat_end",
                "ts": end_ts,
                "client_name": self.name,
                "model": chosen_model,
                "is_health_check": bool(is_health_check),
                "success": bool(final_success),
                "error": final_error,  # may be None or dict
                "_client_obj": self,
            })

    def get_status(self, key: Optional[str] = None) -> Any:
        with self._lock:
            return self._status.get(key, None) if key else self._status.copy()

    def validate_response(self, response: Dict[str, Any], expected_content: Optional[str] = None) -> Optional[str]:
        """
        Validates the content of the Chat response for business logic checks or health checks.

        Usage:
            if error := client.validate_response(response, expected_content="JSON"):
                client.complain_error(error)
                print(f"Client {client.name} failed: {error}")
                # Trigger retry logic...
            else:
                print("Success:", response['choices'][0]['message']['content'])

        Args:
            response: The dictionary returned by the chat() method (or the 'data' field of APIResult).
            expected_content: (Optional) Expected string to appear in the response for simple keyword validation.

        Returns:
            Optional[str]: Returns None if validation passes; otherwise, returns the error reason string.
                           The returned value can be passed directly to complain_error().
        """
        # 1. Check if the input is already an error response from chat()
        if 'error' in response:
            # Note: error_count is handled by chat() / _handle_unified_error
            return f"API Internal Error: {response['message']}"

        # 2. Check basic structure (choices)
        choices = response.get('choices', [])
        if not choices:
            return "Invalid response structure: 'choices' is empty"

        # 3. Check for empty content
        first_content = choices[0].get('message', {}).get('content', '')
        if not first_content:
            return "Response content is empty"

        # 4. (Optional) Check business keywords
        if expected_content and expected_content not in first_content:
            return f"Content validation failed: '{expected_content}' not found in response"

        # 5. (Optional) Could check finish_reason here if necessary (e.g., if 'length' is an error)
        # finish_reason = choices[0].get('finish_reason')
        # if finish_reason == 'length':
        #     return "Response truncated (length limit)"

        return None  # All good

    def complain_error(self, reason: str = "Unspecified external error"):
        """
        Interface for external parties to manually report an error.
        Used when the API call succeeded (HTTP 200) but the returned content
        did not meet expectations (e.g., logic error, format error).
        """
        logger.warning(f"Client {self.name} received external complaint: {reason}")

        # Increase error count and mark as ERROR/UNAVAILABLE based on severity (usually ERROR for external)
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

    def set_event_sink(self, sink_callable):
        """Inject an external event sink: sink(event_dict) -> None."""
        self.event_sink = sink_callable

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
            # 1. Initiate test chat
            # chat() internally handles status switching based on APIResult
            result = self.chat(
                messages=[{"role": "user", "content": self.test_prompt}],
                max_tokens=100
            )

            # chat() returns a final response dictionary
            if 'error' in result:
                # Error count and status update are handled by chat's error logic.
                return False

            # 2. Validate response logic
            error_reason = self.validate_response(result, expected_content=self.expected_response)

            if error_reason:
                # 3. If validation fails, manually complain
                # This increases error_count and sets status to ERROR
                self.complain_error(f"Self-test failed: {error_reason}")
                return False

            # 4. Validation passed
            # Status should already be AVAILABLE from _handle_llm_response,
            # but we explicitly reset the counter for a clean slate.
            self._reset_error_count()
            self._update_client_status(ClientStatus.AVAILABLE)
            return True

        except Exception as e:
            # Catches errors in the testing routine itself, not the API call
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
                self._emit_event({
                    "type": "status_change",
                    "ts": time.time(),
                    "client_name": self.name,
                    "old_status": str(old_status),
                    "new_status": str(new_status),
                    # Optional: pass self so logger can derive idle state accurately
                    "_client_obj": self
                })
                logger.info(f"Client {self.name} status changed from {old_status} to {new_status}")

    def _emit_event(self, event: Dict[str, Any]):
        """Best-effort event emission; never break core logic."""
        try:
            if callable(self.event_sink):
                self.event_sink(event)
        except Exception:
            pass

    # ---------------------------------------- Error Handling (Unified) ----------------------------------------

    def _handle_unified_error(self, error: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handles the structured error result from the underlying API_CORE.
        """
        error_type = error.get('type')
        error_code = error.get('code', '')  # 确保默认为空字符串以便进行 in 判断
        message = error.get('message', 'No detail message provided.')

        # 默认归类为 recoverable (可重试)，但在下面会被修正
        error_category = 'recoverable'

        # --- Map API_CORE's classification to BaseAIClient's state update strategy ---

        if error_type == "PERMANENT":
            error_category = 'fatal'  # 永久错误，上层业务应当停止重试

            # 关键修改：区分 "内容错误(400)" 和 "账号/配置错误(401/403/404)"
            if "HTTP_400" in str(error_code):
                # 情况 A: 客户端没挂，是我的 Prompt 违规了
                # 动作: 记录日志，但【不要】把 Client 设为 UNAVAILABLE
                logger.error(
                    f"Input/Param Error ({error_code}). Client {self.name} remains active. Message: {message[:100]}...")
                # 这里不调用 _update_client_status，保持原样 (AVAILABLE)
                # 也不增加错误计数 (因为不是 Client 的错)

            else:
                # 情况 B: 账号废了、找不到资源等 (401, 403, 404)
                # 动作: 这种情况下 Client 是真的不能用了
                self._update_client_status(ClientStatus.UNAVAILABLE)
                self._increase_error_count()  # 只有 Client 真的出错才计数
                logger.error(f"Permanent API Error ({error_code}): {message}")

        elif error_type in ["TRANSIENT_SERVER", "TRANSIENT_NETWORK"]:
            # 瞬时错误 (5xx, Network)
            error_category = 'recoverable'
            # 动作: 暂时标记为 ERROR 或 UNAVAILABLE，等待恢复
            self._update_client_status(ClientStatus.ERROR)
            self._increase_error_count()
            logger.warning(f"Transient API Error ({error_code}): {message}")

        else:
            # 未知错误，保守处理
            error_category = 'recoverable'
            self._update_client_status(ClientStatus.ERROR)
            self._increase_error_count()
            logger.error(f"Unknown API Error Type ({error_type}): {message}")

        return {
            'error': 'unified_api_error',
            'error_type': error_category,  # fatal: 告诉上层别试了; recoverable: 告诉上层换个 Client 试试
            'api_error_code': error_code,
            'api_error_type': error_type,
            'message': message
        }

    def _handle_exception(self, exception: Exception) -> Dict[str, Any]:
        """
        Handles unexpected exceptions occurring outside of the API call process
        (e.g., internal logic error, state locking failure).

        Note: Network/HTTP exceptions are now handled by API_CORE and mapped via _handle_unified_error.
        """
        error_message = str(exception)

        # Classify the exception (mainly looking for internal logic errors now)
        if isinstance(exception, (ValueError, TypeError)):
            # Parameter error or structure mismatch - potential programming error
            error_type = 'fatal'
            logger.error(f"Parameter error (fatal): {error_message}")
            self._update_client_status(ClientStatus.UNAVAILABLE)  # Assume fatal if internal structure fails

        else:
            # Other unknown exceptions
            error_type = 'recoverable'
            logger.error(f"Unexpected internal error: {error_message}")
            self._update_client_status(ClientStatus.ERROR)

        self._increase_error_count()

        return {
            'error': 'internal_exception',
            'error_type': error_type,
            'exception_type': type(exception).__name__,
            'message': error_message
        }

    def _handle_llm_response(self, response: Dict[str, Any], original_messages: List[Dict[str, str]]) -> Dict[str, Any]:
        """
        Handles successful LLM response (status 200/success=True in APIResult),
        checks for business logic errors, and records usage.
        """
        try:
            choices = response.get('choices', [])
            if not choices:
                # Protocol error: API claimed success but returned empty choices.
                self._increase_error_count()
                self._update_client_status(ClientStatus.ERROR)
                return {
                    'error': 'empty_response',
                    'error_type': 'recoverable',
                    'message': 'API returned empty choices array despite success status'
                }

            first_choice = choices[0]
            finish_reason = first_choice.get('finish_reason')

            # 'length' and 'content_filter' are normal operational outcomes, not client errors.
            if finish_reason == 'length':
                logger.warning(f"Client {self.name}: Response truncated due to length.")
            elif finish_reason == 'content_filter':
                logger.warning(f"Client {self.name}: Response triggered content filter.")

            try:
                # Record token usage
                if usage_data := response.get('usage', {}):
                    usage_data['request_count'] = 1
                    self.record_usage(usage_data)
            except Exception:
                pass  # Ignore usage recording failures

            # If the response was successfully processed, the client is healthy
            self._reset_error_count()
            self._update_client_status(ClientStatus.AVAILABLE)

            return response

        except Exception as e:
            logger.error(f"Error processing LLM response: {e}")
            # Failure in the processing logic itself
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
    def get_api_base_url(self) -> str:
        pass

    @abstractmethod
    def _chat_completion_sync(self,
                              messages: List[Dict[str, str]],
                              model: Optional[str] = None,
                              temperature: float = 0.7,
                              max_tokens: int = 4096,
                              is_health_check: bool = False) -> APIResult:  # <-- Changed return type to APIResult
        """
        Subclass must implement this, calling the API_CORE and returning its structured result.
        """
        pass


# ----------------------------------------------------------------------------------------------------------------------

class AIClientManager:
    """
    Management framework for AI clients with priority-based selection,
    health monitoring, and automatic client management.
    """

    def __init__(self, base_check_interval_sec: int = 60, first_check_delay_sec: int = 10, state_logger=None):
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

        # 分组并发限制配置 { 'group_id': max_concurrent_limit }
        self.group_limits: Dict[str, int] = {}

        self._lock = threading.RLock()
        self.monitor_thread = None
        self.monitor_running = False

        # Configuration for the monitoring loop
        self.reset_fatal_interval = base_check_interval_sec * 30    # Interval to reset fatal to unknown or re-check
        self.check_error_interval = base_check_interval_sec         # Interval when client is in ERROR state
        self.check_stable_interval = base_check_interval_sec * 15   # Interval when client is AVAILABLE
        self.first_check_delay_sec = first_check_delay_sec

        self.state_logger = state_logger

    def register_client(self, client: Any):
        """
        Register a new AI client.
        """
        with self._lock:
            self.clients.append(client)
            # Sort by priority (lower number = higher priority)
            # This ensures get_available_client always picks the best one first.
            self.clients.sort(key=lambda x: x.priority)

            try:
                if self.state_logger:
                    self.state_logger.attach_client(client)
            except Exception:
                pass

            logger.info(f"Registered client: {getattr(client, 'name', 'Unknown')}")

    def set_group_limit(self, group_id: str, limit: int):
        with self._lock:
            self.group_limits[group_id] = limit
            logger.info(f"Set concurrency limit for group '{group_id}' to {limit}")

    def get_available_client(self,
                             user_name: str,
                             request_change: bool = False,
                             target_group_id: str = None,
                             target_client_name: str = None,
                             allow_private: bool = False) -> Optional[BaseAIClient]:
        """
        Selects and acquires an *available* AI client for a given user, with optional
        constraints (by name / by group) and allocation policy support (PUBLIC/PRIVATE/NAME_ONLY).

        This API is designed for resource-managed client pools: it not only *selects* a client,
        but also *acquires* it (client._acquire) and records the allocation in `user_client_map`.

        --------------------------------------------------------------------
        Key Concepts
        --------------------------------------------------------------------
        1) "Available" means ALL of the following:
           - Client is not UNAVAILABLE
           - Client is not in a disqualified ERROR state (error_count threshold)
           - Client health score > 0 (calculate_health)
           - Client is not busy and can be acquired (not _is_busy(), and _acquire() succeeds)
           - Group concurrency limit is not violated (unless swapping within the same group)

        2) Allocation Policy (visibility):
           - PUBLIC: participates in normal auto selection
           - PRIVATE: excluded from auto selection unless explicitly allowed
           - NAME_ONLY: excluded from auto selection; only retrievable by explicit name targeting

           Note: visibility is enforced in the condition-based selection path.
                 When `target_client_name` is provided, it is treated as explicit authorization
                 to retrieve the named client (even if PRIVATE/NAME_ONLY), but the client must
                 still satisfy "available" checks.

        --------------------------------------------------------------------
        Precedence / Selection Rules
        --------------------------------------------------------------------
        Priority of constraints:
        (A) target_client_name (highest precedence, explicit selection)
            - If provided, the manager attempts to acquire *that exact client* by name.
            - In this path, `target_group_id` and `allow_private` are not used for candidate filtering,
              because the candidate is already uniquely identified.
            - However, the named client must still pass availability checks:
              status/health/busy/acquire, and group concurrency limits.
            - If the user already holds that client, it will be returned and its last_used refreshed.

        (B) condition-based selection (normal path)
            - If target_client_name is not provided:
                - Optionally filter by target_group_id
                - Enforce visibility rules (PRIVATE requires allow_private or explicit group targeting)
                - Honor request_change (exclude the current client's reuse as a candidate)
                - Choose the best available client by priority order (lower priority number first)

        request_change behavior:
          - Indicates a preference to switch away from the current client.
          - In the condition-based path, the current client is excluded when request_change=True.
          - In the name-targeted path, explicit `target_client_name` overrides request_change
            (i.e., name targeting wins, since it is an explicit user intent).

        --------------------------------------------------------------------
        Group Concurrency Limits and Swap Semantics
        --------------------------------------------------------------------
        Group limits enforce max concurrent acquisitions per group_id.
        The implementation supports "swap" logic:
          - When replacing a client's allocation within the same group, we do not count the
            current user's held slot against the limit for selecting a replacement, so users can
            switch clients without temporarily exceeding the limit.

        --------------------------------------------------------------------
        Args:
            user_name:
                Identifier for the requester. Used as key in `user_client_map`.
            request_change:
                If True, the user's current client (if any) will not be selected in the condition-based path.
                (Explicit name targeting overrides this preference.)
            target_group_id:
                If set (and target_client_name is not set), only clients in this group are considered.
            target_client_name:
                If set, strictly selects the client with this name (explicit selection path).
            allow_private:
                If True, PRIVATE clients are allowed to participate in the condition-based selection path.
                Does not affect the explicit name path (name targeting is already explicit authorization).

        Returns:
            Optional[BaseAIClient]:
                A client that has been acquired and allocated to the user, or None if no suitable client
                can be found/acquired under the given constraints.

        Thread-Safety:
            This method is protected by the manager's lock to ensure consistent allocation records
            and to prevent races in concurrent acquisition/swap operations.
        """
        if not user_name:
            logger.error("user_name is required to get a client.")
            return None

        with self._lock:
            # 1. Retrieve current allocation
            current_allocation = self.user_client_map.get(user_name)
            current_client = current_allocation['client'] if current_allocation else None

            # 2. Cleanup: If current client is effectively dead, release it first
            if current_client and (current_client not in self.clients or
                                   current_client.get_status('status') in [ClientStatus.ERROR,
                                                                           ClientStatus.UNAVAILABLE]):
                self._release_user_resources(user_name)
                current_client = None

            # 3. Calculate Group Usage
            # If request_change is True, we treat the current user as "not holding a slot"
            # for the purpose of finding a replacement (Swap Logic).
            current_group_usage = {}
            for u_name, info in self.user_client_map.items():
                client_in_use = info['client']

                # Logic adjustment for request_change:
                # If this is the current user AND they want to change, do not count their
                # current client towards the group limit. This allows swapping within a full group.
                if request_change and u_name == user_name:
                    continue

                gid = getattr(client_in_use, 'group_id', None)
                if gid:
                    current_group_usage[gid] = current_group_usage.get(gid, 0) + 1

            if target_client_name:
                self._get_available_client_by_name(user_name, target_client_name, current_client, current_group_usage)
            return self._get_available_client_by_conditions(user_name, request_change, target_group_id, allow_private, current_client, current_group_usage)

    def _get_available_client_by_name(self,
                                      user_name: str,
                                      target_client_name: str,
                                      current_client,
                                      current_group_usage
                                      ) -> Optional[BaseAIClient]:
        client = self.get_client_by_name(target_client_name)
        if not client:
            return None

        client_name = getattr(client, 'name', '')
        gid = getattr(client, 'group_id', None)
        client_status = client.get_status('status')

        # Health checks
        if client_status == ClientStatus.UNAVAILABLE:
            return None
        if client_status == ClientStatus.ERROR and client.get_status('error_count') > 1:
            return None
        if client.calculate_health() <= 0:
            return None

        # If user already holds this client, keep it (even if request_change=True, we treat name as override)
        if client is current_client:
            self.user_client_map[user_name]['last_used'] = time.time()
            return client

        # Group concurrency limit (allow swap within same group)
        if gid in self.group_limits:
            limit = self.group_limits[gid]
            current_count = current_group_usage.get(gid, 0)

            # If user already holds a client in the same group, switching won't increase group usage.
            if current_client and getattr(current_client, "group_id", None) == gid:
                current_count -= 1

            if current_count >= limit:
                return None

        # Acquire busy check
        if client._is_busy():
            return None
        if not client._acquire():
            return None

        # Switch from old client if exists
        if current_client:
            self._release_client_core(current_client)

        self.user_client_map[user_name] = {"client": client, "last_used": time.time()}
        logger.info(f"User {user_name} acquired client by name: {client_name}")
        return client

    def _get_available_client_by_conditions(self,
                                            user_name: str,
                                            request_change: bool,
                                            target_group_id: str,
                                            allow_private: bool,
                                            current_client,
                                            current_group_usage
                                            ) -> Optional[BaseAIClient]:
        # Iterate through clients (Priority: High -> Low)
        for client in self.clients:
            client_name = getattr(client, 'name', '')
            client_status = client.get_status('status')
            gid = getattr(client, 'group_id', None)

            # --- FILTER: Visibility / Allocation Policy ---
            vis = getattr(client, "visibility", None)

            if vis == ClientVisibility.NAME_ONLY:
                # 没有 target_client_name 的情况下，NAME_ONLY 永远不参与自动分配
                continue
            if vis == ClientVisibility.PRIVATE and not allow_private and not target_group_id:
                # PRIVATE 默认不参与；除非 allow_private=True 或者你认为“指定 group 也算显式选择”
                continue

            # --- FILTER: Target Group ---
            if target_group_id and gid != target_group_id:
                continue

            # --- FILTER: Request Change (Exclude Current) ---
            # If user explicitly wants a change, the current client is not a candidate.
            if request_change and client is current_client:
                continue

            # --- Standard Health Checks ---
            if client_status == ClientStatus.UNAVAILABLE:
                continue

            # Error threshold check
            if client_status == ClientStatus.ERROR and client.get_status('error_count') > 1:
                continue

            # Dynamic health check
            if client.calculate_health() <= 0:
                continue

            # --- FILTER: Group Concurrency Limits ---
            # Logic: If it is the current user's client, they already hold the slot (limit ignored).
            # BUT if request_change is True, we already skipped 'current_client' above,
            # so we will treat every candidate as a 'new' acquisition subject to limits.
            is_current_users_client = (client is current_client)

            if not is_current_users_client and gid in self.group_limits:
                limit = self.group_limits[gid]
                current_count = current_group_usage.get(gid, 0)

                if current_count >= limit:
                    logger.debug(
                        f"Skipping client {client_name}: Group '{gid}' limit reached ({current_count}/{limit})")
                    continue

            # --- SELECTION LOGIC ---

            # Case A: We found the client currently held by this user.
            # (We only reach here if request_change is False, because of the filter above)
            if client is current_client:
                self.user_client_map[user_name]['last_used'] = time.time()
                logger.debug(f"User {user_name} keeps current client: {client.name}")
                return client

            # Case B: We found a free client (or the specific target requested).
            if not client._is_busy():
                # Attempt to acquire
                if client._acquire():
                    # Release old client if exists
                    if current_client:
                        self._release_client_core(current_client)
                        logger.info(f"User {user_name} switching from {current_client.name} to {client.name}")

                    # Update map
                    self.user_client_map[user_name] = {
                        "client": client,
                        "last_used": time.time()
                    }
                    logger.info(f"User {user_name} acquired client: {client.name}")
                    return client

        # End of Loop: No suitable client found.
        # If target_client_name was set, it means that specific client is unavailable.
        # If request_change was True, it means no OTHER client is available.
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
                acquire_count = raw_status.get('acquire_count', 0)
                chat_count = raw_status.get('chat_count', 0)
                err_count = raw_status.get('error_count', 0)
                err_sum = raw_status.get('error_sum', 0)
                err_rate = (err_sum / chat_count * 100) if chat_count > 0 else 0.0

                client_details.append({
                    "meta": {
                        "name": getattr(client, "name", "Unknown"),
                        "type": client.__class__.__name__,
                        "group_id": getattr(client, "group_id", "default"),
                        "priority": client.priority,
                        "visibility": getattr(client, "visibility", "public").value if hasattr(client, "visibility") else "public",
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
                        "acquire_count": acquire_count,
                        "chat_count": chat_count,
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
                    "group_limits": self.group_limits,
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
                # 注册临时用户
                self._set_test_user(client)

                return client._test_and_update_status()
            finally:
                # 注销临时用户
                self._clear_test_user(client)
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

    def get_state_logger(self):
        """Expose state logger for dashboard and external integrations."""
        return getattr(self, "state_logger", None)

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

        try:
            self.state_logger.stop()
        except Exception:
            pass

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
                # Temporary add delay
                time.sleep(60)
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

    def _generate_test_user_name(self, client: BaseAIClient) -> str:
        """生成一个用于显示的测试者名称，包含Client名以防字典Key冲突"""
        # 使用特殊前缀，让前端或日志能一眼看出这是系统行为
        return f"[System Check] {client.name}"

    def _set_test_user(self, client: BaseAIClient):
        """在 user_client_map 中临时注册一个系统测试用户"""
        test_user = self._generate_test_user_name(client)
        with self._lock:
            self.user_client_map[test_user] = {
                "client": client,
                "last_used": time.time()
            }
        logger.debug(f"Assigned test user '{test_user}' to client {client.name}")

    def _clear_test_user(self, client: BaseAIClient):
        """清除该 Client 对应的测试用户"""
        test_user = self._generate_test_user_name(client)
        with self._lock:
            if test_user in self.user_client_map:
                del self.user_client_map[test_user]

    def _check_client_health(self):
        """
        Trigger active health checks for eligible clients using dynamic intervals,
        based on the last successful/unsuccessful chat activity.
        """
        clients_to_check = []
        now = time.time()

        with self._lock:
            for client in self.clients:
                client_status = client.get_status('status')

                # --- 关键修改 1: 使用最近的活动时间 ---
                # last_chat: 上一次用户请求时间 (无论成功失败，都已更新状态)
                # last_test: 上一次系统主动测试时间
                t_last_activity = max(client.get_status('last_chat'), client.get_status('last_test'))
                # ------------------------------------

                # 1. 动态计算检查间隔 (Timeout)
                client_error_count = client.get_status('error_count')
                base_timeout = self.check_error_interval

                if client_status == ClientStatus.AVAILABLE:
                    # 稳定 Client：使用长间隔
                    timeout = self.check_stable_interval
                elif client_status == ClientStatus.UNAVAILABLE:
                    # 永久不可用：超长间隔 (e.g., 30x)
                    timeout = self.reset_fatal_interval
                elif client_status == ClientStatus.UNKNOWN:
                    # 首次检查：立即检查
                    timeout = 0
                else:  # ClientStatus.ERROR
                    # 瞬时错误：使用指数退避，避免连续失败时过度检查
                    exponent = min(client_error_count, 4)
                    timeout = base_timeout * (2 ** exponent)

                # 2. 检查是否达到 timeout
                # 如果 t_last_activity 是 0.0 (UNKNOWN 状态)，则现在 - 0 > 0 (timeout) 也会触发
                if now - t_last_activity > timeout:

                    # 3. 避免检查正在被用户使用的 Client
                    if not client._is_acquired():
                        clients_to_check.append(client)
                    else:
                        logger.debug(f"Skipping health check for {client.name} (Acquired by user).")

        # Perform checks outside the main lock to avoid blocking get_available_client
        for client in clients_to_check:
            client_name = getattr(client, 'name', 'Unknown Client')
            logger.debug(f'Checking connectivity for {client_name}...')

            # This method usually pings the API or checks simple connectivity
            if client._acquire():
                try:
                    # 标记为系统正在使用，让 Dashboard 能显示 "[System Check] ..."
                    self._set_test_user(client)

                    # 执行原有的测试逻辑
                    result = client._test_and_update_status()

                    if not result:
                        logger.error(f"Status check - {client_name}: Unknown error.")
                except Exception as e:
                    logger.error(f"Exception during health check for {client_name}: {e}")
                finally:
                    # [测试结束，移除系统用户
                    self._clear_test_user(client)
                    client._release()
            else:
                # 如果获取不到锁，说明正被真实用户使用，跳过本次检查
                logger.debug(f"Status check - Cannot acquire {client_name} (Busy).")

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
