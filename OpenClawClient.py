"""
OpenClaw Client for AIClientCenter

Provides an interface to communicate with OpenClaw agents via the CLI.
This client adapter allows seamless integration with the AIClientCenter's
client management system, supporting health checks, error handling, and timeouts.
"""

import json
import subprocess
import threading
import logging
import time
from typing import Dict, List, Optional, Any, Union

# Handle relative/absolute imports
try:
    from .AIClientManager import BaseAIClient, CLIENT_PRIORITY_NORMAL, ClientStatus, ClientVisibility
    from .APIResult import APIResult
except ImportError:
    from AIClientManager import BaseAIClient, CLIENT_PRIORITY_NORMAL, ClientStatus, ClientVisibility
    from APIResult import APIResult

logger = logging.getLogger(__name__)


class OpenClawClient(BaseAIClient):
    """
    A client that communicates with OpenClaw agents via the `openclaw agent` CLI command.

    Features:
    - Subprocess-based communication with OpenClaw Gateway
    - Configurable timeout to prevent hanging
    - Structured error handling compatible with APIResult format
    - Health check support via test prompts

    Usage:
        client = OpenClawClient(
            name='openclaw-asuka',
            agent_id='asuka',
            priority=CLIENT_PRIORITY_NORMAL,
            timeout=60
        )
    """

    # Class-level lock to prevent concurrent CLI calls which could overwhelm the gateway
    _cli_lock = threading.RLock()

    def __init__(
        self,
        name: str,
        agent_id: str = "main",
        priority: int = CLIENT_PRIORITY_NORMAL,
        group_id: str = 'openclaw',
        visibility: ClientVisibility = ClientVisibility.PUBLIC,
        default_available: bool = True,
        timeout: int = 60,
        thinking: Optional[str] = None,
        verbose: Optional[bool] = None
    ):
        """
        Initialize the OpenClaw client.

        Args:
            name: Unique identifier for this client instance
            agent_id: OpenClaw agent ID to target (e.g., 'main', 'asuka', 'kaori')
            priority: Scheduling priority (lower is better)
            group_id: Client group for concurrency limits
            visibility: PUBLIC/PRIVATE/NAME_ONLY visibility setting
            default_available: Whether to mark as AVAILABLE immediately
            timeout: Maximum seconds to wait for OpenClaw CLI response
            thinking: Thinking level override (off/minimal/low/medium/high/xhigh)
            verbose: Verbose mode override
        """
        # Use a placeholder token since OpenClaw uses its own auth
        super().__init__(
            name=name,
            api_token="openclaw-cli",
            priority=priority,
            group_id=group_id,
            visibility=visibility
        )

        self.agent_id = agent_id
        self.timeout = max(10, timeout)  # Minimum 10s timeout
        self.thinking = thinking
        self.verbose = verbose
        self._model_name = f"openclaw/{agent_id}"

        if default_available:
            self._status['status'] = ClientStatus.AVAILABLE

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
        """Return a pseudo API base URL."""
        return f"openclaw://agent/{self.agent_id}"

    def _chat_completion_sync(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        is_health_check: bool = False
    ) -> APIResult:
        """
        Execute a chat completion via OpenClaw CLI.

        Flow:
        1. Convert messages to a single prompt string
        2. Build the openclaw agent command
        3. Execute with timeout protection
        4. Parse JSON response
        5. Convert to OpenAI-compatible format
        """
        # Build prompt from messages (take the last user message, or concatenate)
        prompt = self._extract_prompt(messages)
        if not prompt.strip():
            return self._make_error_result("BAD_REQUEST", "EMPTY_PROMPT", "No user message found in the conversation")

        # Build CLI command
        cmd = self._build_command(prompt, is_health_check)

        # Execute with lock and timeout
        try:
            result = self._execute_cli(cmd)
            return self._parse_response(result, is_health_check)
        except subprocess.TimeoutExpired:
            logger.warning(f"OpenClaw client {self.name} timed out after {self.timeout}s")
            return self._make_error_result(
                "TRANSIENT_NETWORK",
                "CLI_TIMEOUT",
                f"OpenClaw CLI command timed out after {self.timeout} seconds"
            )
        except Exception as e:
            logger.error(f"OpenClaw client {self.name} execution error: {e}")
            return self._make_error_result(
                "PERMANENT",
                "CLI_EXECUTION_ERROR",
                f"Failed to execute OpenClaw CLI: {str(e)}"
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

    def _extract_clean_error(self, stderr: str, stdout: str) -> str:
        """
        Extract a clean error message from CLI output, removing plugin noise.

        OpenClaw outputs plugin registration lines and other noise to stderr.
        This method extracts the actual error message.
        """
        # Prefer stderr for error messages
        source = stderr if stderr else stdout
        lines = source.split('\n')

        # Filter out plugin noise lines
        noise_prefixes = [
            '[plugins]',
            '[qqbot-',
            'Registered QQ',
            'Set plugins.allow',
            'discovered non-bundled plugins',
        ]

        clean_lines = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            # Skip noise lines
            if any(line.startswith(prefix) for prefix in noise_prefixes):
                continue
            clean_lines.append(line)

        if clean_lines:
            return ' '.join(clean_lines)

        # Fallback: return truncated original
        return (stderr or stdout)[:500]

    def _build_command(self, prompt: str, is_health_check: bool) -> List[str]:
        """Build the openclaw agent CLI command."""
        cmd = [
            "openclaw", "agent",
            "--agent", self.agent_id,
            "--message", prompt,
            "--json",
            "--timeout", str(self.timeout)
        ]

        if is_health_check:
            # Use shorter timeout for health checks
            cmd[-1] = str(min(15, self.timeout))

        if self.thinking:
            cmd.extend(["--thinking", self.thinking])

        if self.verbose is not None:
            cmd.extend(["--verbose", "on" if self.verbose else "off"])

        return cmd

    def _execute_cli(self, cmd: List[str]) -> Dict[str, Any]:
        """
        Execute the CLI command with proper timeout and error handling.

        Uses a class-level lock to prevent concurrent CLI calls that could
        overwhelm the OpenClaw gateway.
        """
        with self._cli_lock:
            logger.debug(f"[{self.name}] Executing: {' '.join(cmd)}")
            start_time = time.time()

            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout + 5,  # Add buffer for subprocess overhead
                    check=False  # Don't raise on non-zero exit
                )

                elapsed = time.time() - start_time
                logger.debug(f"[{self.name}] CLI completed in {elapsed:.2f}s (exit={result.returncode})")

                if result.returncode != 0:
                    stderr = result.stderr.strip() if result.stderr else ""
                    stdout = result.stdout.strip() if result.stdout else ""

                    # Check for specific error patterns
                    if "timeout" in stderr.lower() or "timeout" in stdout.lower():
                        raise subprocess.TimeoutExpired(cmd, self.timeout)

                    clean_error = self._extract_clean_error(stderr, stdout)
                    raise RuntimeError(f"OpenClaw CLI failed (exit={result.returncode}): {clean_error}")

                # Parse JSON output
                output = result.stdout.strip()
                if not output:
                    raise ValueError("OpenClaw CLI returned empty output")

                return json.loads(output)

            except json.JSONDecodeError as e:
                # Try to extract JSON from partial output
                output = result.stdout.strip() if 'result' in dir() else ""
                parsed = self._extract_json_from_output(output)
                if parsed:
                    return parsed
                raise ValueError(f"Failed to parse OpenClaw output as JSON: {e}")

    def _parse_response(self, result: Dict[str, Any], is_health_check: bool) -> APIResult:
        """
        Parse the OpenClaw CLI JSON response into APIResult format.

        OpenClaw response format:
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
        status = result.get("status", "unknown")

        if status != "ok":
            summary = result.get("summary", "Unknown error")
            return self._make_error_result(
                "TRANSIENT_SERVER",
                "OPENCLAW_ERROR",
                f"OpenClaw returned status '{status}': {summary}"
            )

        result_data = result.get("result", {})
        payloads = result_data.get("payloads", [])

        if not payloads:
            return self._make_error_result(
                "TRANSIENT_SERVER",
                "EMPTY_RESPONSE",
                "OpenClaw returned no response payloads"
            )

        # Extract text from payloads
        texts = []
        for payload in payloads:
            text = payload.get("text", "")
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
            "id": result.get("runId", "openclaw-" + str(int(time.time()))),
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
        """Extract token usage from OpenClaw response metadata."""
        meta = result_data.get("meta", {})
        agent_meta = meta.get("agentMeta", {})
        usage = agent_meta.get("usage", {})
        last_call = agent_meta.get("lastCallUsage", {})

        # Prefer lastCallUsage if available, fallback to usage
        prompt_tokens = last_call.get("input", usage.get("input", 0))
        completion_tokens = last_call.get("output", usage.get("output", 0))
        total_tokens = last_call.get("total", usage.get("total", prompt_tokens + completion_tokens))

        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens
        }

    def _extract_json_from_output(self, output: str) -> Optional[Dict[str, Any]]:
        """
        Try to extract JSON from mixed output (handles plugin log lines before JSON).

        OpenClaw may output plugin registration lines before the JSON response:
        [plugins] plugins.allow is empty...
        [qqbot-channel-api] Registered...
        {"status": "ok", ...}
        """
        # Find the first '{' that starts a JSON object
        brace_idx = output.find('{')
        if brace_idx == -1:
            return None

        json_part = output[brace_idx:]

        # Try to find matching braces
        depth = 0
        end_idx = 0
        for i, char in enumerate(json_part):
            if char == '{':
                depth += 1
            elif char == '}':
                depth -= 1
                if depth == 0:
                    end_idx = i + 1
                    break

        if end_idx == 0:
            # Didn't find matching close, try the whole thing
            end_idx = len(json_part)

        try:
            return json.loads(json_part[:end_idx])
        except json.JSONDecodeError:
            return None

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
        try:
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


# ------------------ Factory Functions ------------------

def create_openclaw_client(
    name: str,
    agent_id: str = "main",
    priority: int = CLIENT_PRIORITY_NORMAL,
    timeout: int = 60,
    **kwargs
) -> OpenClawClient:
    """
    Factory function to create an OpenClaw client with common defaults.

    Args:
        name: Client name
        agent_id: OpenClaw agent ID
        priority: Client priority
        timeout: CLI timeout in seconds
        **kwargs: Additional arguments passed to OpenClawClient

    Returns:
        Configured OpenClawClient instance
    """
    return OpenClawClient(
        name=name,
        agent_id=agent_id,
        priority=priority,
        timeout=timeout,
        **kwargs
    )
