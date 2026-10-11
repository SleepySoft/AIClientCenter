"""基于本地估算的滚动 RPM/TPM/RPD 限额器。

Provider 返回的精确 token 用量只能在请求完成后取得，因此请求发送前使用保守估算值
预占配额，响应完成后再用 ``prompt_tokens`` 校正。该限额器用于主动留出余量，不能代替
服务商返回的 429 和实际配额控制。
"""

from __future__ import annotations

import math
import threading
import time
import unicodedata
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional


def estimate_text_tokens(text: Any) -> int:
    """粗略估算文本 token 数，兼顾 CJK 和英文，不依赖特定 tokenizer。"""
    value = str(text or "")
    wide = 0
    narrow = 0
    for character in value:
        if character.isspace():
            narrow += 1
        elif unicodedata.east_asian_width(character) in {"W", "F"}:
            wide += 1
        else:
            narrow += 1
    # CJK 通常接近 1 字/token；英文及 JSON 标点按约 4 字符/token。
    return wide + math.ceil(narrow / 4)


def estimate_message_tokens(messages: Iterable[Mapping[str, Any]]) -> int:
    """估算 OpenAI chat messages 的输入 token，包含少量消息封装开销。"""
    total = 2
    for message in messages:
        total += 4
        total += estimate_text_tokens(message.get("role", ""))
        total += estimate_text_tokens(message.get("content", ""))
        if message.get("name"):
            total += estimate_text_tokens(message["name"])
    return max(1, total)


@dataclass
class RateLimitReservation:
    timestamp: float
    prompt_tokens: int


class EstimatedRateLimiter:
    """线程安全的滚动限额器；日限额使用保守的滚动 24 小时窗口。"""

    def __init__(
            self,
            *,
            rpm: Optional[int] = None,
            tpm: Optional[int] = None,
            rpd: Optional[int] = None,
            utilization_ratio: float = 0.8,
            clock: Callable[[], float] = time.monotonic,
    ):
        if not 0 < utilization_ratio <= 1:
            raise ValueError("utilization_ratio 必须在 (0, 1] 范围内")
        for name, value in (("rpm", rpm), ("tpm", tpm), ("rpd", rpd)):
            if value is not None and value <= 0:
                raise ValueError(f"{name} 必须为正整数或 None")
        self.rpm = rpm
        self.tpm = tpm
        self.rpd = rpd
        self.utilization_ratio = float(utilization_ratio)
        self._clock = clock
        self._minute: deque[RateLimitReservation] = deque()
        self._day: deque[float] = deque()
        self._lock = threading.RLock()

    @staticmethod
    def estimate_tokens(messages: Iterable[Mapping[str, Any]]) -> int:
        return estimate_message_tokens(messages)

    def try_reserve(
            self, messages: Optional[Iterable[Mapping[str, Any]]] = None,
            *, estimated_tokens: Optional[int] = None,
    ) -> Optional[RateLimitReservation]:
        estimate = max(1, int(
            estimated_tokens if estimated_tokens is not None
            else estimate_message_tokens(messages or [])
        ))
        with self._lock:
            now = self._clock()
            self._prune(now)
            if not self._can_fit(estimate):
                return None
            reservation = RateLimitReservation(now, estimate)
            self._minute.append(reservation)
            self._day.append(now)
            return reservation

    def reconcile(self, reservation: RateLimitReservation, actual_prompt_tokens: Any) -> None:
        """用响应中的实际输入 token 校正仍位于分钟窗口内的预占值。"""
        try:
            actual = int(actual_prompt_tokens)
        except (TypeError, ValueError):
            return
        if actual <= 0:
            return
        with self._lock:
            self._prune(self._clock())
            if reservation in self._minute:
                reservation.prompt_tokens = actual

    def can_accept(self, estimated_tokens: int = 1) -> bool:
        with self._lock:
            self._prune(self._clock())
            return self._can_fit(max(1, int(estimated_tokens)))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._prune(self._clock())
            used_rpm = len(self._minute)
            used_tpm = sum(item.prompt_tokens for item in self._minute)
            used_rpd = len(self._day)
            effective = {
                "rpm": self._effective_limit(self.rpm),
                "tpm": self._effective_limit(self.tpm),
                "rpd": self._effective_limit(self.rpd),
            }
            used = {"rpm": used_rpm, "tpm": used_tpm, "rpd": used_rpd}
            remaining = {
                key: None if effective[key] is None else max(0, effective[key] - used[key])
                for key in effective
            }
            pressure_values = [
                used[key] / effective[key]
                for key in effective if effective[key] not in (None, 0)
            ]
            return {
                "estimated": True,
                "utilization_ratio": self.utilization_ratio,
                "limits": {"rpm": self.rpm, "tpm": self.tpm, "rpd": self.rpd},
                "effective_limits": effective,
                "used": used,
                "remaining": remaining,
                "pressure": max(pressure_values, default=0.0),
            }

    def _can_fit(self, prompt_tokens: int) -> bool:
        rpm_limit = self._effective_limit(self.rpm)
        tpm_limit = self._effective_limit(self.tpm)
        rpd_limit = self._effective_limit(self.rpd)
        return (
            (rpm_limit is None or len(self._minute) + 1 <= rpm_limit)
            and (tpm_limit is None or sum(x.prompt_tokens for x in self._minute) + prompt_tokens <= tpm_limit)
            and (rpd_limit is None or len(self._day) + 1 <= rpd_limit)
        )

    def _effective_limit(self, limit: Optional[int]) -> Optional[int]:
        return None if limit is None else max(1, math.floor(limit * self.utilization_ratio))

    def _prune(self, now: float) -> None:
        minute_start = now - 60.0
        while self._minute and self._minute[0].timestamp <= minute_start:
            self._minute.popleft()
        day_start = now - 86400.0
        while self._day and self._day[0] <= day_start:
            self._day.popleft()
