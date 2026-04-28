"""
OpenClaw Client for AIClientCenter

Provides an interface to communicate with OpenClaw agents via the Gateway WebSocket API.
This client adapter allows seamless integration with the AIClientCenter's
client management system, supporting health checks, error handling, and timeouts.

------------------------------------------------------------------------------
获取 Gateway Token 的方法
------------------------------------------------------------------------------
由于本程序与 OpenClaw Gateway 通常运行在不同机器上，token 必须显式传入，
无法自动读取本地配置文件。以下是获取 token 的几种方式：

1. 从 OpenClaw 配置文件读取（在 Gateway 所在机器执行）：
   $ cat ~/.openclaw/openclaw.json | grep -o '"token": "[^"]*"'
   或
   $ cat ~/.openclaw/openclaw.json | python3 -c "import sys,json; print(json.load(sys.stdin).get('gateway',{}).get('auth',{}).get('token',''))"

2. 通过 openclaw gateway 命令查看（在 Gateway 所在机器执行）：
   $ openclaw gateway status    # 查看 gateway 配置摘要
   $ openclaw config get        # 查看完整配置

3. 如果是 systemd/user 服务运行，token 通常在启动时由 OpenClaw 自动生成，
   路径固定为 ~/.openclaw/openclaw.json，字段路径为 gateway.auth.token

4. 若使用远程模式 (gateway.mode=remote)，token 可能通过环境变量
   OPENCLAW_GATEWAY_TOKEN 或 OPENCLAW_AUTH_TOKEN 注入，需咨询部署者。

注意：token 是 gateway 的访问凭证，请妥善保管，不要在日志中明文打印。
------------------------------------------------------------------------------
"""

import json
import threading
import logging
import time
import uuid
import os
import base64
from typing import Dict, List, Optional, Any, Union

# Handle relative/absolute imports
try:
    from .AIClientManager import BaseAIClient, CLIENT_PRIORITY_NORMAL, ClientStatus, ClientVisibility
    from .APIResult import APIResult
except ImportError:
    from AIClientManager import BaseAIClient, CLIENT_PRIORITY_NORMAL, ClientStatus, ClientVisibility
    from APIResult import APIResult

logger = logging.getLogger(__name__)

# Optional WebSocket dependency
try:
    import websocket
    WEBSOCKET_AVAILABLE = True
except ImportError:
    WEBSOCKET_AVAILABLE = False
    logger.warning("websocket-client not installed. OpenClawClient will not function.")

# Optional cryptography for device identity signing
try:
    from cryptography.hazmat.primitives import serialization
    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False


class OpenClawClient(BaseAIClient):
    """
    A client that communicates with OpenClaw agents via the Gateway WebSocket API.

    Features:
    - Direct WebSocket connection to OpenClaw Gateway (no CLI subprocess)
    - Concurrent-friendly: multiple requests can share one connection
    - Configurable timeout with per-request tracking
    - Structured error handling compatible with APIResult format
    - Health check support via test prompts
    - Auto-reconnect on connection loss

    Usage:
        client = OpenClawClient(
            name='openclaw-asuka',
            agent_id='asuka',
            gateway_url='ws://openclaw-host:18789',
            gateway_token='<从Gateway机器获取的token>',
            priority=CLIENT_PRIORITY_NORMAL,
            timeout=60
        )
    """

    def __init__(
        self,
        name: str,
        agent_id: str = "main",
        gateway_url: str = "ws://127.0.0.1:18789",
        gateway_token: Optional[str] = None,
        priority: int = CLIENT_PRIORITY_NORMAL,
        group_id: str = 'openclaw',
        visibility: ClientVisibility = ClientVisibility.PUBLIC,
        default_available: bool = True,
        timeout: int = 60,
        thinking: Optional[str] = None,
        verbose: Optional[bool] = None,
        auto_reconnect: bool = True,
        max_reconnect_attempts: int = 3
    ):
        """
        Initialize the OpenClaw client.

        Args:
            name: Unique identifier for this client instance
            agent_id: OpenClaw agent ID to target (e.g., 'main', 'asuka', 'kaori')
            gateway_url: WebSocket URL of the OpenClaw Gateway
                         示例: ws://192.168.1.100:18789 或 wss://remote.example.com:18789
            gateway_token: Gateway 认证 token (必需)。获取方式见模块顶部注释。
            priority: Scheduling priority (lower is better)
            group_id: Client group for concurrency limits
            visibility: PUBLIC/PRIVATE/NAME_ONLY visibility setting
            default_available: Whether to mark as AVAILABLE immediately
            timeout: Maximum seconds to wait for Gateway response
            thinking: Thinking level override (off/minimal/low/medium/high/xhigh)
            verbose: Verbose mode override
            auto_reconnect: Whether to auto-reconnect on connection loss
            max_reconnect_attempts: Max reconnection attempts before giving up
        """
        super().__init__(
            name=name,
            api_token="openclaw-ws",
            priority=priority,
            group_id=group_id,
            visibility=visibility
        )

        if not WEBSOCKET_AVAILABLE:
            logger.error("websocket-client is required for OpenClawClient. Install: pip install websocket-client")
            self._status['status'] = ClientStatus.ERROR
            return

        self.agent_id = agent_id
        self.timeout = max(10, timeout)
        self.thinking = thinking
        self.verbose = verbose
        self._model_name = f"openclaw/{agent_id}"

        # Gateway configuration
        self.gateway_url = gateway_url
        if not gateway_token:
            logger.warning(
                "[OpenClawClient] gateway_token is empty! "
                "This client will fail to connect. "
                "Please provide a valid token (see module docstring for how to obtain one)."
            )
        self.gateway_token = gateway_token
        self.auto_reconnect = auto_reconnect
        self.max_reconnect_attempts = max_reconnect_attempts

        # Connection state
        self._ws: Optional[websocket.WebSocket] = None
        self._connected = False
        self._lock = threading.RLock()
        self._pending_requests: Dict[str, threading.Event] = {}
        self._responses: Dict[str, Any] = {}
        self._req_counter = 0
        self._req_counter_lock = threading.Lock()

        # Chat event storage: runId -> final chat event (for retrieving agent responses)
        self._chat_events: Dict[str, Any] = {}

        # Background thread for connection maintenance
        self._connect()

        if default_available:
            self._status['status'] = ClientStatus.AVAILABLE

    # ------------------ Connection Management ------------------

    def _connect(self) -> bool:
        """Establish WebSocket connection to Gateway."""
        with self._lock:
            if self._connected and self._ws:
                return True

            try:
                logger.debug(f"[{self.name}] Connecting to {self.gateway_url}")
                # Temporarily disable proxy to prevent websocket-client from using HTTP_PROXY
                # (local proxies often don't support WebSocket upgrade correctly)
                import os as _os
                _proxy_keys = ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy']
                _proxy_backup = {k: _os.environ.pop(k, None) for k in _proxy_keys}
                try:
                    self._ws = websocket.create_connection(
                        self.gateway_url,
                        timeout=10,
                        enable_multithread=True
                    )
                finally:
                    for k, v in _proxy_backup.items():
                        if v is not None:
                            _os.environ[k] = v

                # 1. Receive connect.challenge
                raw_challenge = self._ws.recv()
                challenge_msg = json.loads(raw_challenge)
                if challenge_msg.get("event") != "connect.challenge":
                    logger.error(f"[{self.name}] Expected connect.challenge, got: {challenge_msg}")
                    self._ws.close()
                    self._ws = None
                    return False

                nonce = challenge_msg.get("payload", {}).get("nonce", "")
                logger.debug(f"[{self.name}] Received connect.challenge nonce={nonce[:16]}...")

                # 2. Build connect handshake (with optional device identity)
                device = self._build_device_signature(nonce) if CRYPTO_AVAILABLE else None
                connect_params = {
                    "minProtocol": 3,
                    "maxProtocol": 3,
                    "role": "operator",
                    "scopes": ["operator.write"],
                    "client": {
                        "id": "gateway-client",
                        "version": "1.0.0",
                        "platform": "python",
                        "mode": "backend"
                    },
                    "auth": {
                        "token": self.gateway_token
                    } if self.gateway_token else {}
                }
                # If we have a device identity, use its metadata and attach device field
                if device:
                    meta = device.get("_meta", {})
                    connect_params["client"]["id"] = meta.get("clientId", "gateway-client")
                    connect_params["client"]["mode"] = meta.get("clientMode", "backend")
                    connect_params["client"]["platform"] = meta.get("platform", "python")
                    connect_params["device"] = {
                        "id": device["id"],
                        "publicKey": device["publicKey"],
                        "signature": device["signature"],
                        "signedAt": device["signedAt"],
                        "nonce": device["nonce"]
                    }

                connect_req = {
                    "type": "req",
                    "id": self._next_req_id(),
                    "method": "connect",
                    "params": connect_params
                }
                self._ws.send(json.dumps(connect_req))

                # 3. Receive connect response (ignore health/tick events that may arrive first)
                start = time.time()
                resp = None
                while time.time() - start < 10:
                    raw_resp = self._ws.recv()
                    if not raw_resp.strip():
                        continue
                    resp = json.loads(raw_resp)
                    if resp.get("type") == "res" and resp.get("id") == connect_req["id"]:
                        break
                    # Ignore server-push events
                    if resp.get("type") == "event":
                        continue
                    logger.debug(f"[{self.name}] Unexpected frame during connect: {resp.get('type')}")

                if resp is None:
                    logger.error(f"[{self.name}] Connect response timeout")
                    self._ws.close()
                    self._ws = None
                    return False

                if resp.get("ok"):
                    self._connected = True
                    logger.info(f"[{self.name}] Connected to OpenClaw Gateway")
                    # Start a background thread to listen for events/responses
                    threading.Thread(target=self._receive_loop, daemon=True).start()
                    return True
                else:
                    error = resp.get("error", {}).get("message", "Unknown error")
                    logger.error(f"[{self.name}] Gateway connect failed: {error}")
                    self._ws.close()
                    self._ws = None
                    return False

            except Exception as e:
                logger.error(f"[{self.name}] Connection error: {e}")
                if self._ws:
                    self._ws.close()
                    self._ws = None
                return False

    def _build_device_signature(self, nonce: str) -> Optional[Dict[str, Any]]:
        """Build device identity payload for connect handshake.

        Reads local device identity from ~/.openclaw/identity/device.json and
        ~/.openclaw/devices/paired.json, then signs a v3 payload using Ed25519.
        Returns None if no device identity is available or cryptography is missing.
        """
        if not CRYPTO_AVAILABLE:
            return None
        try:
            device_json_path = os.path.expanduser("~/.openclaw/identity/device.json")
            paired_json_path = os.path.expanduser("~/.openclaw/devices/paired.json")
            if not os.path.exists(device_json_path) or not os.path.exists(paired_json_path):
                return None

            with open(device_json_path) as f:
                device_identity = json.load(f)
            with open(paired_json_path) as f:
                paired_devices = json.load(f)

            device_id = device_identity.get("deviceId")
            private_key_pem = device_identity.get("privateKeyPem")
            if not device_id or not private_key_pem:
                return None

            paired = paired_devices.get(device_id, {})
            public_key_b64url = paired.get("publicKey")
            if not public_key_b64url:
                return None

            # Use paired device metadata so the server accepts it without re-pairing
            client_id = paired.get("clientId", "gateway-client")
            client_mode = paired.get("clientMode", "backend")
            platform = paired.get("platform", "python")
            role = paired.get("role", "operator")
            scopes = paired.get("scopes", ["operator.write"])
            device_family = paired.get("deviceFamily", "")

            signed_at_ms = int(time.time() * 1000)
            token = self.gateway_token or ""

            # Build v3 payload: v3|deviceId|clientId|clientMode|role|scopes|signedAtMs|token|nonce|platform|deviceFamily
            scopes_str = ",".join(scopes)
            platform_norm = platform.strip().lower() if platform else ""
            family_norm = device_family.strip().lower() if device_family else ""
            payload = "|".join([
                "v3", device_id, client_id, client_mode, role, scopes_str,
                str(signed_at_ms), token, nonce, platform_norm, family_norm
            ])

            # Sign with Ed25519 private key
            private_key = serialization.load_pem_private_key(
                private_key_pem.encode("utf-8"), password=None
            )
            signature = private_key.sign(payload.encode("utf-8"))
            signature_b64url = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")

            return {
                "id": device_id,
                "publicKey": public_key_b64url,
                "signature": signature_b64url,
                "signedAt": signed_at_ms,
                "nonce": nonce,
                "_meta": {
                    "clientId": client_id,
                    "clientMode": client_mode,
                    "platform": platform,
                    "role": role,
                    "scopes": scopes,
                }
            }
        except Exception as e:
            logger.warning(f"[{self.name}] Failed to build device signature: {e}")
            return None

    def _disconnect(self):
        """Close WebSocket connection."""
        with self._lock:
            self._connected = False
            if self._ws:
                try:
                    self._ws.close()
                except Exception:
                    pass
                self._ws = None

    def _reconnect(self) -> bool:
        """Attempt to reconnect with backoff."""
        self._disconnect()
        for attempt in range(self.max_reconnect_attempts):
            logger.debug(f"[{self.name}] Reconnect attempt {attempt + 1}/{self.max_reconnect_attempts}")
            if self._connect():
                return True
            time.sleep(min(2 ** attempt, 10))  # Exponential backoff, max 10s
        logger.error(f"[{self.name}] Failed to reconnect after {self.max_reconnect_attempts} attempts")
        return False

    def _receive_loop(self):
        """Background thread: receive responses and route to pending requests."""
        while self._connected and self._ws:
            try:
                raw = self._ws.recv()
                if not raw:
                    continue
                msg = json.loads(raw)

                if msg.get("type") == "res":
                    req_id = msg.get("id")
                    if req_id in self._pending_requests:
                        self._responses[req_id] = msg
                        self._pending_requests[req_id].set()
                    else:
                        # Response for unknown request (e.g., server push)
                        logger.debug(f"[{self.name}] Unsolicited response: {msg}")

                elif msg.get("type") == "event":
                    event_type = msg.get("event")
                    payload = msg.get("payload", {})
                    logger.debug(f"[{self.name}] Event: {event_type}")

                    # Collect chat events for response retrieval
                    if event_type == "chat":
                        run_id = payload.get("runId")
                        if run_id:
                            with self._lock:
                                self._chat_events[run_id] = payload
                            logger.debug(f"[{self.name}] Chat event for runId={run_id}: state={payload.get('state')}")

            except websocket.WebSocketConnectionClosedException:
                logger.warning(f"[{self.name}] Connection closed")
                self._connected = False
                break
            except json.JSONDecodeError as e:
                logger.warning(f"[{self.name}] Invalid JSON received: {e}")
            except Exception as e:
                logger.error(f"[{self.name}] Receive error: {e}")
                self._connected = False
                break

        # If auto-reconnect is enabled, try to reconnect
        if self.auto_reconnect:
            logger.info(f"[{self.name}] Connection lost, attempting reconnect...")
            self._reconnect()

    def _next_req_id(self) -> str:
        """Generate unique request ID."""
        with self._req_counter_lock:
            self._req_counter += 1
            return f"{self.name}-{self._req_counter}-{uuid.uuid4().hex[:8]}"

    def _send_request(self, method: str, params: Dict[str, Any], timeout_ms: Optional[int] = None) -> Dict[str, Any]:
        """
        Send a request via WebSocket and wait for response.
        Thread-safe; multiple callers can share the same connection.
        """
        if not WEBSOCKET_AVAILABLE:
            raise RuntimeError("websocket-client not installed")

        # Ensure connection
        if not self._connected or not self._ws:
            if not self._reconnect():
                raise ConnectionError("Not connected to OpenClaw Gateway")

        req_id = self._next_req_id()
        req = {
            "type": "req",
            "id": req_id,
            "method": method,
            "params": params
        }

        # Setup pending request tracking
        event = threading.Event()
        with self._lock:
            self._pending_requests[req_id] = event
            self._responses.pop(req_id, None)  # Clear any stale response

        try:
            # Send the request
            with self._lock:
                if not self._ws:
                    raise ConnectionError("WebSocket not available")
                self._ws.send(json.dumps(req))

            # Wait for response
            timeout_sec = (timeout_ms or self.timeout * 1000) / 1000.0
            if not event.wait(timeout=timeout_sec):
                raise TimeoutError(f"Request {req_id} timed out after {timeout_sec}s")

            # Retrieve response
            resp = self._responses.pop(req_id, None)
            if not resp:
                raise RuntimeError("Response was set but not found")

            if not resp.get("ok"):
                error = resp.get("error", {})
                raise RuntimeError(f"Gateway error: {error.get('message', 'Unknown')}")

            return resp.get("payload", {})

        finally:
            # Cleanup
            with self._lock:
                self._pending_requests.pop(req_id, None)
                self._responses.pop(req_id, None)

    # ------------------ BaseAIClient Interface ------------------

    def get_model_list(self) -> Dict[str, Any]:
        """Return a pseudo model list for compatibility."""
        return {
            "object": "list",
            "data": [
                {
                    "id": self._model_name,
                    "object": "model",
                    "owned_by": "openclaw"
                }
            ]
        }

    def get_current_model(self) -> str:
        """Return the current model identifier."""
        return self._model_name

    def get_api_base_url(self) -> str:
        """Return the WebSocket URL of the Gateway."""
        return self.gateway_url

    def _chat_completion_sync(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        is_health_check: bool = False
    ) -> APIResult:
        """
        Execute a chat completion via OpenClaw Gateway WebSocket API.

        Flow:
        1. Convert messages to a single prompt string
        2. Send chat.send request via WebSocket
        3. Wait for response
        4. Convert to OpenAI-compatible format
        """
        if not WEBSOCKET_AVAILABLE:
            return self._make_error_result(
                "PERMANENT",
                "MISSING_DEPENDENCY",
                "websocket-client not installed. Run: pip install websocket-client"
            )

        # Build prompt from messages
        prompt = self._extract_prompt(messages)
        if not prompt.strip():
            return self._make_error_result("BAD_REQUEST", "EMPTY_PROMPT", "No user message found in the conversation")

        # Build request parameters
        params = {
            "sessionKey": f"agent:{self.agent_id}:default",
            "message": prompt,
            "idempotencyKey": str(uuid.uuid4()),
            "timeoutMs": min(15000, self.timeout * 1000) if is_health_check else self.timeout * 1000
        }

        if self.thinking:
            params["thinking"] = self.thinking

        try:
            # Send via WebSocket - chat.send returns an ack, not the actual reply
            ack = self._send_request("chat.send", params)
            run_id = ack.get("runId")
            if not run_id:
                return self._make_error_result(
                    "TRANSIENT_SERVER",
                    "NO_RUN_ID",
                    "Gateway did not return a runId"
                )

            # Wait for the chat event with the actual agent response
            logger.debug(f"[{self.name}] Waiting for chat event, runId={run_id}")
            start_time = time.time()
            final_event = None
            while time.time() - start_time < self.timeout:
                with self._lock:
                    event = self._chat_events.get(run_id)
                if event and event.get("state") in ("final", "error", "aborted"):
                    final_event = event
                    break
                time.sleep(0.2)

            if not final_event:
                return self._make_error_result(
                    "TRANSIENT_SERVER",
                    "TIMEOUT",
                    f"No chat event received for runId={run_id} within {self.timeout}s"
                )

            # Clean up
            with self._lock:
                self._chat_events.pop(run_id, None)

            if final_event.get("state") == "error":
                return self._make_error_result(
                    "TRANSIENT_SERVER",
                    "AGENT_ERROR",
                    final_event.get("errorMessage", "Agent returned error state")
                )

            # Parse the actual response from the chat event
            message_data = final_event.get("message", {})
            content_parts = message_data.get("content", [])
            texts = []
            for part in content_parts:
                if part.get("type") == "text":
                    texts.append(part.get("text", ""))

            response_text = "".join(texts)

            if not response_text.strip():
                return self._make_error_result(
                    "TRANSIENT_SERVER",
                    "EMPTY_CONTENT",
                    "Agent returned empty response text"
                )

            # Build OpenAI-compatible response
            openai_response = {
                "id": run_id,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": self._model_name,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": response_text
                        },
                        "finish_reason": "stop"
                    }
                ],
                "usage": self._extract_usage(final_event)
            }

            return {
                "success": True,
                "data": openai_response,
                "error": None
            }

        except TimeoutError:
            logger.warning(f"[{self.name}] Gateway request timed out")
            return self._make_error_result(
                "TRANSIENT_NETWORK",
                "GATEWAY_TIMEOUT",
                f"OpenClaw Gateway request timed out after {self.timeout}s"
            )
        except ConnectionError as e:
            logger.error(f"[{self.name}] Connection error: {e}")
            return self._make_error_result(
                "TRANSIENT_NETWORK",
                "CONNECTION_ERROR",
                f"Failed to connect to OpenClaw Gateway: {str(e)}"
            )
        except Exception as e:
            logger.error(f"[{self.name}] Request error: {e}")
            return self._make_error_result(
                "TRANSIENT_SERVER",
                "GATEWAY_ERROR",
                f"OpenClaw Gateway error: {str(e)}"
            )

    # ------------------ Internal Helpers ------------------

    def _extract_prompt(self, messages: List[Dict[str, str]]) -> str:
        """Extract the prompt from the message list."""
        if not messages:
            return ""

        # Try to find the last user message
        user_messages = [m for m in messages if m.get("role") == "user"]
        if user_messages:
            return user_messages[-1].get("content", "")

        # Fallback: concatenate all messages
        parts = []
        for m in messages:
            role = m.get("role", "unknown")
            content = m.get("content", "")
            if content:
                parts.append(f"[{role}] {content}")
        return "\n".join(parts)

    def _parse_response(self, payload: Dict[str, Any], is_health_check: bool) -> APIResult:
        """
        Parse the Gateway response payload into APIResult format.

        Expected payload format from chat.send:
        {
            "runId": "...",
            "status": "ok|error",
            "summary": "...",
            "result": {
                "payloads": [{"text": "...", "mediaUrl": null}],
                "meta": {...}
            }
        }
        """
        status = payload.get("status", "unknown")

        if status != "ok":
            summary = payload.get("summary", "Unknown error")
            return self._make_error_result(
                "TRANSIENT_SERVER",
                "OPENCLAW_ERROR",
                f"OpenClaw returned status '{status}': {summary}"
            )

        result_data = payload.get("result", {})
        payloads = result_data.get("payloads", [])

        if not payloads:
            return self._make_error_result(
                "TRANSIENT_SERVER",
                "EMPTY_RESPONSE",
                "OpenClaw returned no response payloads"
            )

        # Extract text from payloads
        texts = []
        for p in payloads:
            text = p.get("text", "")
            if text:
                texts.append(text)

        response_text = "\n".join(texts) if texts else ""

        if not response_text.strip():
            return self._make_error_result(
                "TRANSIENT_SERVER",
                "EMPTY_CONTENT",
                "OpenClaw returned empty response text"
            )

        # Convert to OpenAI-compatible format
        openai_response = {
            "id": payload.get("runId", f"openclaw-{int(time.time())}"),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": self._model_name,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": response_text
                    },
                    "finish_reason": "stop"
                }
            ],
            "usage": self._extract_usage(result_data)
        }

        return {
            "success": True,
            "data": openai_response,
            "error": None
        }

    def _extract_usage(self, result_data: Dict[str, Any]) -> Dict[str, int]:
        """Extract token usage from OpenClaw response (chat event or legacy format)."""
        # Try chat event format first (usage at top level)
        usage = result_data.get("usage", {})
        if not usage:
            # Fallback to legacy nested format
            meta = result_data.get("meta", {})
            agent_meta = meta.get("agentMeta", {})
            usage = agent_meta.get("usage", {})
            last_call = agent_meta.get("lastCallUsage", {})
        else:
            last_call = usage

        prompt_tokens = last_call.get("input", usage.get("input", 0))
        completion_tokens = last_call.get("output", usage.get("output", 0))
        total_tokens = last_call.get("total", usage.get("total", prompt_tokens + completion_tokens))

        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens
        }

    def _make_error_result(self, error_type: str, error_code: str, message: str) -> APIResult:
        """Create a standardized error result."""
        return {
            "success": False,
            "data": None,
            "error": {
                "type": error_type,
                "code": error_code,
                "message": message
            }
        }

    # ------------------ Health Check Helpers ------------------

    def _test_and_update_status(self) -> bool:
        """
        Override health check to use OpenClaw-specific test.
        """
        if not WEBSOCKET_AVAILABLE:
            self.complain_error("websocket-client not installed")
            return False

        try:
            # Ensure connection before test
            if not self._connected:
                if not self._reconnect():
                    self.complain_error("Cannot connect to Gateway for health check")
                    return False

            result = self.chat(
                messages=[{"role": "user", "content": self.test_prompt}],
                max_tokens=100
            )

            if 'error' in result:
                return False

            error_reason = self.validate_response(result, expected_content=self.expected_response)
            if error_reason:
                self.complain_error(f"Self-test failed: {error_reason}")
                return False

            self._reset_error_count()
            self._update_client_status(ClientStatus.AVAILABLE)
            return True

        except Exception as e:
            self.complain_error(f"Exception during self-test: {e}")
            return False
        finally:
            with self._lock:
                self._status['last_test'] = time.time()

    def close(self):
        """Clean up resources. Call this when done with the client."""
        self._disconnect()

    def __del__(self):
        """Destructor to ensure connection is closed."""
        try:
            self.close()
        except Exception:
            pass


# ------------------ Factory Functions ------------------

def create_openclaw_client(
    name: str,
    agent_id: str = "main",
    gateway_url: str = "ws://127.0.0.1:18789",
    gateway_token: str = "",
    priority: int = CLIENT_PRIORITY_NORMAL,
    timeout: int = 60,
    **kwargs
) -> OpenClawClient:
    """
    Factory function to create an OpenClaw client with common defaults.

    由于程序与 OpenClaw 通常不在同一台机器上，token 必须从 Gateway 所在机器获取后
    显式传入。获取方式见模块顶部注释。

    Args:
        name: Client name
        agent_id: OpenClaw agent ID
        gateway_url: WebSocket URL of the OpenClaw Gateway
                     示例: ws://192.168.1.100:18789
        gateway_token: Gateway 认证 token (必需)
        priority: Client priority
        timeout: Request timeout in seconds
        **kwargs: Additional arguments passed to OpenClawClient

    Returns:
        Configured OpenClawClient instance
    """
    return OpenClawClient(
        name=name,
        agent_id=agent_id,
        gateway_url=gateway_url,
        gateway_token=gateway_token,
        priority=priority,
        timeout=timeout,
        **kwargs
    )
