import logging
import json
import time

import requests
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)


class GoogleGeminiAdapter:
    """
    Adapter implementation using DIRECT REST API (HTTP).
    Features:
    - Supports per-instance Proxy.
    - Uses 'v1alpha' endpoint to support the latest Gemini 2.5 models.
    - No external SDK dependencies (requests only).
    """

    def __init__(self, api_key: str, model: str = "gemini-2.0-flash-exp", proxy: Optional[str] = None):
        self.api_key = api_key
        self.model = model
        self.proxy = proxy

        # Use v1alpha to support the latest experimental models (like gemini-2.5-flash-lite)
        self.base_url = "https://generativelanguage.googleapis.com/v1alpha/models"

    def _get_proxies(self) -> Dict[str, str]:
        if not self.proxy:
            return {}
        return {
            "http": self.proxy,
            "https": self.proxy
        }

    def get_api_token(self) -> str:
        return self.api_key

    def set_api_token(self, token: str):
        self.api_key = token

    def get_using_model(self) -> str:
        return self.model

    def get_model_list(self) -> Dict[str, Any]:
        """
        Fetches model list with retry logic.
        """
        url = f"{self.base_url}?key={self.api_key}"
        # 简单重试 3 次
        for attempt in range(3):
            try:
                resp = requests.get(
                    url,
                    proxies=self._get_proxies(),
                    timeout=10,
                    headers={"Connection": "close"}  # 关键：防止复用死连接
                )
                resp.raise_for_status()
                data = resp.json()

                models = []
                for m in data.get("models", []):
                    if "generateContent" in m.get("supportedGenerationMethods", []):
                        models.append({
                            "id": m["name"].replace("models/", ""),
                            "object": "model"
                        })
                return {"data": models}
            except Exception as e:
                logger.warning(f"Fetch models failed (Attempt {attempt + 1}): {e}")
                time.sleep(1)

        # 如果全失败，返回空
        return {"data": []}

    def _convert_messages(self, messages: List[Dict[str, str]]) -> tuple[Optional[Dict], List[Dict]]:
        system_instruction = None
        contents = []

        for msg in messages:
            role = msg.get("role")
            content = msg.get("content")

            if role == "system":
                system_instruction = {"parts": [{"text": content}]}
            elif role == "user":
                contents.append({"role": "user", "parts": [{"text": content}]})
            elif role == "assistant":
                contents.append({"role": "model", "parts": [{"text": content}]})
            else:
                contents.append({"role": "user", "parts": [{"text": f"[{role}]: {content}"}]})

        return system_instruction, contents

    def create_chat_completion_sync(
            self,
            messages: List[Dict[str, str]],
            model: Optional[str] = None,
            temperature: float = 1.0,
            max_tokens: int = 4096
    ) -> Dict[str, Any]:

        target_model = model or self.model
        target_model = target_model.replace("models/", "")

        url = f"{self.base_url}/{target_model}:streamGenerateContent?key={self.api_key}"

        system_instruction, contents = self._convert_messages(messages)

        payload = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens
            },
            "safetySettings": [
                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
            ]
        }
        if system_instruction:
            payload["system_instruction"] = system_instruction

        # ==========================
        # 🛡️ 增强部分：重试循环
        # ==========================
        max_retries = 3
        last_exception = None

        for attempt in range(max_retries):
            try:
                # 显式创建 Session 以应用 Connection: close
                with requests.Session() as s:
                    response = s.post(
                        url,
                        json=payload,
                        headers={
                            "Content-Type": "application/json",
                            "Connection": "close"  # 关键：禁用 Keep-Alive，防止 SSL EOF
                        },
                        proxies=self._get_proxies(),
                        stream=True,
                        timeout=60
                    )

                    # 遇到 429 也进行重试
                    if response.status_code == 429:
                        wait = 2 * (attempt + 1)
                        logger.warning(f"Rate Limit 429. Sleeping {wait}s...")
                        time.sleep(wait)
                        continue

                    response.raise_for_status()

                    full_content = ""
                    finish_reason = "stop"
                    usage_info = {}
                    buffer = ""

                    for line in response.iter_lines():
                        if not line: continue
                        decoded_part = line.decode('utf-8')

                        stripped = decoded_part.strip()
                        if stripped == '[' or stripped == ']': continue

                        buffer += decoded_part
                        try:
                            # 去掉尾部逗号
                            text_to_check = buffer.strip()
                            if text_to_check.startswith(','): text_to_check = text_to_check[1:]
                            if text_to_check.endswith(','): text_to_check = text_to_check[:-1]

                            chunk = json.loads(text_to_check)

                            # 解析成功，提取数据
                            candidates = chunk.get("candidates", [])
                            if candidates:
                                candidate = candidates[0]
                                parts = candidate.get("content", {}).get("parts", [])
                                if parts:
                                    full_content += parts[0].get("text", "")
                                if "finishReason" in candidate:
                                    finish_reason = candidate["finishReason"]

                            if "usageMetadata" in chunk:
                                usage_info = chunk["usageMetadata"]

                            buffer = ""  # 清空
                        except json.JSONDecodeError:
                            continue

                    # 成功获取完整流，直接返回
                    return {
                        "id": "gen-google-rest",
                        "object": "chat.completion",
                        "created": 0,
                        "model": target_model,
                        "choices": [{
                            "index": 0,
                            "message": {"role": "assistant", "content": full_content},
                            "finish_reason": finish_reason
                        }],
                        "usage": {
                            "prompt_tokens": usage_info.get("promptTokenCount", 0),
                            "completion_tokens": usage_info.get("candidatesTokenCount", 0),
                            "total_tokens": usage_info.get("totalTokenCount", 0)
                        }
                    }

            except (requests.exceptions.SSLError, requests.exceptions.ChunkedEncodingError,
                    requests.exceptions.ConnectionError) as e:
                # 捕获 SSL/网络 断开错误
                logger.warning(f"Network Error (Attempt {attempt + 1}/{max_retries}): {e}")
                last_exception = e
                time.sleep(1 + attempt)  # 稍微睡一会再试
                continue

            except Exception as e:
                logger.error(f"Unrecoverable Error: {e}")
                raise e

        # 如果循环结束还没返回，说明重试次数用尽
        raise last_exception or Exception("Max retries exceeded")

