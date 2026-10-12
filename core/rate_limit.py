"""基于本地估算的滚动 RPM/TPM/RPD 限额器。

Provider 返回的精确 token 用量只能在请求完成后取得，因此请求发送前使用保守估算值
预占配额，响应完成后再用 ``prompt_tokens`` 校正。该限额器用于主动留出余量，不能代替
服务商返回的 429 和实际配额控制。
"""

from __future__ import annotations

import math
import os
import sqlite3
import threading
import time
import unicodedata
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional
from uuid import uuid4


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
    reservation_id: Optional[str] = None


class SQLiteRateLimitLedger:
    """跨进程保存滚动请求记录，并在每次访问时删除 24 小时窗口外的数据。"""

    def __init__(self, path: os.PathLike[str] | str):
        self.path = str(Path(path).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS rate_limit_usage (
                    namespace TEXT NOT NULL,
                    reservation_id TEXT NOT NULL,
                    requested_at REAL NOT NULL,
                    prompt_tokens INTEGER NOT NULL,
                    PRIMARY KEY (namespace, reservation_id)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_rate_limit_usage_requested_at
                ON rate_limit_usage (requested_at)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_rate_limit_usage_namespace_time
                ON rate_limit_usage (namespace, requested_at)
                """
            )

    @staticmethod
    def _cleanup(connection: sqlite3.Connection, now: float) -> int:
        cursor = connection.execute(
            "DELETE FROM rate_limit_usage WHERE requested_at <= ?",
            (now - 86400.0,),
        )
        return max(0, int(cursor.rowcount))

    @staticmethod
    def _usage(
            connection: sqlite3.Connection, namespace: str, now: float,
    ) -> tuple[int, int, int, int]:
        minute_count, minute_tokens = connection.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(prompt_tokens), 0)
            FROM rate_limit_usage
            WHERE namespace = ? AND requested_at > ?
            """,
            (namespace, now - 60.0),
        ).fetchone()
        day_count, day_tokens = connection.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(prompt_tokens), 0)
            FROM rate_limit_usage
            WHERE namespace = ?
            """,
            (namespace,),
        ).fetchone()
        return int(minute_count), int(minute_tokens), int(day_count), int(day_tokens)

    def try_reserve(
            self, *, namespace: str, now: float, prompt_tokens: int,
            rpm_limit: Optional[int], tpm_limit: Optional[int], rpd_limit: Optional[int],
            tpd_limit: Optional[int],
    ) -> Optional[RateLimitReservation]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._cleanup(connection, now)
            used_rpm, used_tpm, used_rpd, used_tpd = self._usage(connection, namespace, now)
            if not (
                (rpm_limit is None or used_rpm + 1 <= rpm_limit)
                and (tpm_limit is None or used_tpm + prompt_tokens <= tpm_limit)
                and (rpd_limit is None or used_rpd + 1 <= rpd_limit)
                and (tpd_limit is None or used_tpd + prompt_tokens <= tpd_limit)
            ):
                return None
            reservation_id = uuid4().hex
            connection.execute(
                """
                INSERT INTO rate_limit_usage
                    (namespace, reservation_id, requested_at, prompt_tokens)
                VALUES (?, ?, ?, ?)
                """,
                (namespace, reservation_id, now, prompt_tokens),
            )
        return RateLimitReservation(now, prompt_tokens, reservation_id)

    def reconcile(self, namespace: str, reservation_id: str, prompt_tokens: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE rate_limit_usage SET prompt_tokens = ?
                WHERE namespace = ? AND reservation_id = ?
                """,
                (prompt_tokens, namespace, reservation_id),
            )

    def usage(self, namespace: str, now: float) -> tuple[int, int, int, int]:
        with self._connect() as connection:
            self._cleanup(connection, now)
            return self._usage(connection, namespace, now)

    def cleanup(self, now: float) -> int:
        with self._connect() as connection:
            return self._cleanup(connection, now)


class EstimatedRateLimiter:
    """线程安全的滚动限额器；日限额使用保守的滚动 24 小时窗口。"""

    def __init__(
            self,
            *,
            rpm: Optional[int] = None,
            tpm: Optional[int] = None,
            rpd: Optional[int] = None,
            tpd: Optional[int] = None,
            utilization_ratio: float = 0.8,
            clock: Callable[[], float] = time.time,
            storage_path: Optional[os.PathLike[str] | str] = None,
            storage_namespace: Optional[str] = None,
    ):
        if not 0 < utilization_ratio <= 1:
            raise ValueError("utilization_ratio 必须在 (0, 1] 范围内")
        for name, value in (("rpm", rpm), ("tpm", tpm), ("rpd", rpd), ("tpd", tpd)):
            if value is not None and value <= 0:
                raise ValueError(f"{name} 必须为正整数或 None")
        self.rpm = rpm
        self.tpm = tpm
        self.rpd = rpd
        self.tpd = tpd
        self.utilization_ratio = float(utilization_ratio)
        self._clock = clock
        self._ledger = SQLiteRateLimitLedger(storage_path) if storage_path is not None else None
        if self._ledger is not None and not storage_namespace:
            raise ValueError("启用 SQLite 限额账本时必须提供 storage_namespace")
        self.storage_namespace = str(storage_namespace) if storage_namespace else None
        self._minute: deque[RateLimitReservation] = deque()
        self._day: deque[RateLimitReservation] = deque()
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
            if self._ledger is not None:
                return self._ledger.try_reserve(
                    namespace=self.storage_namespace,
                    now=now,
                    prompt_tokens=estimate,
                    rpm_limit=self._effective_limit(self.rpm),
                    tpm_limit=self._effective_limit(self.tpm),
                    rpd_limit=self._effective_limit(self.rpd),
                    tpd_limit=self._effective_limit(self.tpd),
                )
            self._prune(now)
            if not self._can_fit(estimate):
                return None
            reservation = RateLimitReservation(now, estimate)
            self._minute.append(reservation)
            self._day.append(reservation)
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
            if self._ledger is not None:
                if reservation.reservation_id:
                    self._ledger.reconcile(
                        self.storage_namespace, reservation.reservation_id, actual)
                return
            self._prune(self._clock())
            if reservation in self._minute:
                reservation.prompt_tokens = actual

    def can_accept(self, estimated_tokens: int = 1) -> bool:
        with self._lock:
            now = self._clock()
            estimate = max(1, int(estimated_tokens))
            if self._ledger is not None:
                used_rpm, used_tpm, used_rpd, used_tpd = self._ledger.usage(
                    self.storage_namespace, now)
                return self._usage_can_fit(
                    used_rpm, used_tpm, used_rpd, used_tpd, estimate)
            self._prune(now)
            return self._can_fit(estimate)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = self._clock()
            if self._ledger is not None:
                used_rpm, used_tpm, used_rpd, used_tpd = self._ledger.usage(
                    self.storage_namespace, now)
            else:
                self._prune(now)
                used_rpm = len(self._minute)
                used_tpm = sum(item.prompt_tokens for item in self._minute)
                used_rpd = len(self._day)
                used_tpd = sum(item.prompt_tokens for item in self._day)
            effective = {
                "rpm": self._effective_limit(self.rpm),
                "tpm": self._effective_limit(self.tpm),
                "rpd": self._effective_limit(self.rpd),
                "tpd": self._effective_limit(self.tpd),
            }
            used = {"rpm": used_rpm, "tpm": used_tpm, "rpd": used_rpd, "tpd": used_tpd}
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
                "persistent": self._ledger is not None,
                "storage_namespace": self.storage_namespace,
                "utilization_ratio": self.utilization_ratio,
                "limits": {
                    "rpm": self.rpm, "tpm": self.tpm,
                    "rpd": self.rpd, "tpd": self.tpd,
                },
                "effective_limits": effective,
                "used": used,
                "remaining": remaining,
                "pressure": max(pressure_values, default=0.0),
            }

    def _can_fit(self, prompt_tokens: int) -> bool:
        return self._usage_can_fit(
            len(self._minute),
            sum(x.prompt_tokens for x in self._minute),
            len(self._day),
            sum(x.prompt_tokens for x in self._day),
            prompt_tokens,
        )

    def _usage_can_fit(
            self, used_rpm: int, used_tpm: int, used_rpd: int,
            used_tpd: int, prompt_tokens: int,
    ) -> bool:
        rpm_limit = self._effective_limit(self.rpm)
        tpm_limit = self._effective_limit(self.tpm)
        rpd_limit = self._effective_limit(self.rpd)
        tpd_limit = self._effective_limit(self.tpd)
        return (
            (rpm_limit is None or used_rpm + 1 <= rpm_limit)
            and (tpm_limit is None or used_tpm + prompt_tokens <= tpm_limit)
            and (rpd_limit is None or used_rpd + 1 <= rpd_limit)
            and (tpd_limit is None or used_tpd + prompt_tokens <= tpd_limit)
        )

    def _effective_limit(self, limit: Optional[int]) -> Optional[int]:
        return None if limit is None else max(1, math.floor(limit * self.utilization_ratio))

    def _prune(self, now: float) -> None:
        minute_start = now - 60.0
        while self._minute and self._minute[0].timestamp <= minute_start:
            self._minute.popleft()
        day_start = now - 86400.0
        while self._day and self._day[0].timestamp <= day_start:
            self._day.popleft()
