"""
================================================================================
Standardized API Result Structure (APIResult)
================================================================================
All API calls in this client (e.g., create_chat_completion_sync) return a dictionary
of the following standardized format:

Type: Dict[str, Union[bool, Optional[Dict[str, Any]]]]

| Key     | Type                          | Description                                                                                      |
|---------|-------------------------------|--------------------------------------------------------------------------------------------------|
| success | bool                          | True if the API call was successful (HTTP 200) after all retries.                                |
| data    | Optional[Dict[str, Any]]      | The parsed JSON response from the API (e.g., LLM completion data). Present if 'success' is True. |
| error   | Optional[Dict[str, Any]]      | Detailed error object. Present if 'success' is False.                                            |

================================================================================
Error Object Format (When success=False)
================================================================================
If 'success' is False, the 'error' field contains a dictionary with the following structure:

| Key     | Type      | Description                                                                                     |
|---------|-----------|-------------------------------------------------------------------------------------------------|
| type    | str       | The error category, one of: 'PERMANENT', 'TRANSIENT_SERVER', or 'TRANSIENT_NETWORK'.            |
| code    | str       | Specific error identifier (e.g., 'HTTP_404', 'CONNECTION_TIMEOUT', 'UNEXPECTED_CLIENT_ERROR').  |
| message | str       | A detailed description of the error cause, including relevant status codes or exceptions.       |

================================================================================
Error Classification and External Strategy
================================================================================

+===================+======================================================+=====================================+================================================================================================================+
|   错误类型 (type)   |                               错误码 (code)                           |                          描述                      | 上层 Client Manager 策略 (BaseAIClient 行为) | 外部调用者策略 (Tenacity 行为)          |
+===================+======================================================+=====================================+================================================================================================================+
| PERMANENT         | HTTP_401, HTTP_403, HTTP_404, MISSING_TOKEN, UNEXPECTED_CLIENT_ERROR | 认证失败、权限不足、资源丢失或客户端系统级致命错误。         | Client → UNAVAILABLE。                     | 立即放弃（不可重试）。                   |
+-------------------+------------------------------------------------------+-------------------------------------+----------------------------------------------------------------------------------------------------------------+
| BAD_REQUEST       | HTTP_400                                                             | 请求内容或格式错误（如敏感词、JSON 结构错误）。            | Client 状态不变，错误计数不变。Client 仍健康。   | 立即放弃（不可重试，或需修改请求内容）。     |
+-------------------+------------------------------------------------------+-------------------------------------+----------------------------------------------------------------------------------------------------------------+
| TRANSIENT_SERVER  | HTTP_429, HTTP_500, HTTP_502, HTTP_503, HTTP_504                     | 服务端瞬时问题（限速、过载、网关错误）。底层重试已耗尽。      | Client → ERROR（计数 ↑）。                  | 冷却后重试。 客户端内部重试已耗尽。         |
+-------------------+------------------------------------------------------+-------------------------------------+----------------------------------------------------------------------------------------------------------------+
| TRANSIENT_NETWORK | CONNECTION_TIMEOUT, PROXY_FAIL, SESSION_RESET_FAILED                 | 网络连接彻底失败（代理、连接超时），底层重试和会话重置均失败。 | Client → ERROR（计数 ↑）。                  | 延迟重试。 待网络环境稳定后，延迟较长时间重试。|
+===================+======================================================+=====================================+================================================================================================================+
"""
from typing import Dict, Any, Union, Optional

# --- Standardized Result Structure Type Hint from API_CORE ---
# APIResult = Dict[str, Union[bool, Optional[Dict[str, Any]]]]
# For this class, we define the structure locally for clarity (or rely on import)
# We will assume APIResult is available from the context where OpenAICompatibleAPI is defined.
# For simplicity, we use Dict[str, Any] as the return type in the new base class.
APIResult = Dict[str, Union[bool, Optional[Dict[str, Any]]]]
