import logging
import json
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
        Fetches model list via HTTP v1alpha.
        """
        url = f"{self.base_url}?key={self.api_key}"
        try:
            resp = requests.get(url, proxies=self._get_proxies(), timeout=10)
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
            logger.error(f"Failed to fetch models: {e}")
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

        # 使用 v1alpha 接口
        url = f"https://generativelanguage.googleapis.com/v1alpha/models/{target_model}:streamGenerateContent?key={self.api_key}"

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

        try:
            response = requests.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
                proxies=self._get_proxies(),
                stream=True,
                timeout=60
            )
            response.raise_for_status()

            full_content = ""
            finish_reason = "stop"
            usage_info = {}

            # === 新版解析器：支持多行 JSON ===
            buffer = ""

            for line in response.iter_lines():
                if not line: continue
                # 注意：这里不要 strip()，保留空格以防拼坏字符串，但要 decode
                decoded_part = line.decode('utf-8')

                # 过滤掉最外层的数组括号（通常单独一行）
                stripped = decoded_part.strip()
                if stripped == '[' or stripped == ']':
                    continue

                buffer += decoded_part

                # 尝试解析 Buffer
                try:
                    # 预处理：去掉 buffer 结尾可能的逗号，否则 json.loads 会报错
                    text_to_check = buffer.strip()
                    if text_to_check.startswith(','):
                        text_to_check = text_to_check[1:]
                    if text_to_check.endswith(','):
                        text_to_check = text_to_check[:-1]

                    chunk = json.loads(text_to_check)

                    # --- 如果解析成功，说明凑齐了一个完整对象 ---

                    # 1. 处理数据
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

                    # 2. 清空缓冲区，准备接收下一个对象
                    buffer = ""

                except json.JSONDecodeError:
                    # 解析失败，说明 JSON 还没接收全，继续循环读下一行
                    continue

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

        except Exception as e:
            logger.error(f"Gemini REST API Error: {e}")
            raise e


