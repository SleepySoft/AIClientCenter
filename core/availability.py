"""AI Client 的每日时间窗调度策略。

策略只由外部 ``AIClientManager`` 检查，不修改 ClientStatus，也不会中断已经开始的调用。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class AvailabilityDecision:
    allowed: bool
    reason: str | None = None
    next_change_at: float | None = None


@dataclass(frozen=True)
class DailyPeriod:
    start: time
    end: time

    def bounds_for(self, start_date: date, target_timezone: tzinfo) -> tuple[datetime, datetime]:
        start_at = datetime.combine(start_date, self.start, tzinfo=target_timezone)
        end_date = start_date + timedelta(days=1) if self.end <= self.start else start_date
        end_at = datetime.combine(end_date, self.end, tzinfo=target_timezone)
        return start_at, end_at

    def contains(self, instant: datetime) -> bool:
        for start_date in (instant.date(), instant.date() - timedelta(days=1)):
            start_at, end_at = self.bounds_for(start_date, instant.tzinfo)
            if start_at <= instant < end_at:
                return True
        return False


def resolve_local_timezone() -> tuple[tzinfo, str]:
    """解析系统本地时区，并尽量保留可确认的 IANA 名称。"""
    candidates = []
    env_timezone = str(os.getenv("TZ") or "").strip().lstrip(":")
    if env_timezone:
        candidates.append(env_timezone)
    try:
        configured = Path("/etc/timezone").read_text(encoding="utf-8").strip()
        if configured:
            candidates.append(configured)
    except OSError:
        pass
    try:
        resolved = str(Path("/etc/localtime").resolve())
        marker = "/zoneinfo/"
        if marker in resolved:
            candidates.append(resolved.split(marker, 1)[1])
    except OSError:
        pass

    for name in candidates:
        try:
            return ZoneInfo(name), name
        except (ZoneInfoNotFoundError, ValueError):
            continue

    local = datetime.now().astimezone().tzinfo or timezone.utc
    name = getattr(local, "key", None) or str(local) or "UTC"
    return local, name


class ClientAvailabilitySchedule:
    """按指定或系统本地时区计算的每日可用/不可用时间段。"""

    def __init__(self, *, mode: str, target_timezone: tzinfo,
                 timezone_name: str, periods: Iterable[DailyPeriod],
                 timezone_explicit: bool):
        if mode not in {"available", "unavailable"}:
            raise ValueError("availability 必须配置 available 或 unavailable。")
        self.mode = mode
        self.timezone = target_timezone
        self.timezone_name = timezone_name
        self.timezone_explicit = timezone_explicit
        self.periods = tuple(periods)
        self.enabled = True
        if not self.periods:
            raise ValueError(f"availability.{mode} 不能为空。")

    @classmethod
    def from_config(cls, config: dict[str, Any] | None) -> "ClientAvailabilitySchedule | None":
        if config is None:
            return None
        if not isinstance(config, dict):
            raise ValueError("availability 必须是对象。")
        unknown = set(config).difference({"timezone", "available", "unavailable"})
        if unknown:
            raise ValueError("availability 含未知字段: " + ", ".join(sorted(unknown)))

        configured_modes = [key for key in ("available", "unavailable") if key in config]
        if len(configured_modes) != 1:
            raise ValueError("availability 必须且只能配置 available 或 unavailable 之一。")
        mode = configured_modes[0]
        periods = cls._parse_periods(config[mode], f"availability.{mode}")

        raw_timezone = config.get("timezone")
        if raw_timezone is None:
            target_timezone, timezone_name = resolve_local_timezone()
            timezone_explicit = False
        else:
            if not isinstance(raw_timezone, str) or not raw_timezone.strip():
                raise ValueError("availability.timezone 必须是 IANA 时区名称。")
            timezone_name = raw_timezone.strip()
            try:
                target_timezone = ZoneInfo(timezone_name)
            except ZoneInfoNotFoundError as exc:
                raise ValueError(
                    f"availability.timezone 不存在: {timezone_name!r}。") from exc
            timezone_explicit = True

        return cls(
            mode=mode,
            target_timezone=target_timezone,
            timezone_name=timezone_name,
            periods=periods,
            timezone_explicit=timezone_explicit,
        )

    @classmethod
    def _parse_periods(cls, raw: Any, location: str) -> list[DailyPeriod]:
        values = [raw] if isinstance(raw, str) else raw
        if not isinstance(values, list) or not values:
            raise ValueError(f"{location} 必须是时间段字符串或非空列表。")
        return [cls._parse_period(value, f"{location}[{index}]")
                for index, value in enumerate(values)]

    @staticmethod
    def _parse_period(raw: Any, location: str) -> DailyPeriod:
        if not isinstance(raw, str):
            raise ValueError(f"{location} 必须是 HH:MM-HH:MM。")
        match = re.fullmatch(r"(\d{2}:\d{2})-(\d{2}:\d{2})", raw.strip())
        if match is None:
            raise ValueError(f"{location} 必须是 HH:MM-HH:MM。")
        start = ClientAvailabilitySchedule._parse_time(match.group(1), location)
        end = ClientAvailabilitySchedule._parse_time(match.group(2), location)
        if start == end:
            raise ValueError(f"{location} 的开始和结束时间不能相同。")
        return DailyPeriod(start, end)

    @staticmethod
    def _parse_time(raw: str, location: str) -> time:
        try:
            parsed = time.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError(f"{location} 必须是有效的 HH:MM-HH:MM。") from exc
        if parsed.second or parsed.microsecond or parsed.tzinfo is not None:
            raise ValueError(f"{location} 只允许分钟精度。")
        return parsed

    def evaluate(self, now: datetime | float | None = None) -> AvailabilityDecision:
        instant = self._as_local_datetime(now)
        inside = any(period.contains(instant) for period in self.periods)
        allowed = inside if self.mode == "available" else not inside
        reason = None if allowed else (
            "outside_available_schedule"
            if self.mode == "available" else "inside_unavailable_schedule"
        )
        return AvailabilityDecision(
            allowed=allowed,
            reason=reason,
            next_change_at=self._next_change_at(instant, allowed),
        )

    def _as_local_datetime(self, now: datetime | float | None) -> datetime:
        if now is None:
            return datetime.now(self.timezone)
        if isinstance(now, (int, float)):
            return datetime.fromtimestamp(float(now), self.timezone)
        if not isinstance(now, datetime):
            raise TypeError("now 必须是 datetime、Unix 时间戳或 None。")
        if now.tzinfo is None:
            raise ValueError("now datetime 必须带时区。")
        return now.astimezone(self.timezone)

    def _next_change_at(self, instant: datetime, current_allowed: bool) -> float | None:
        candidates: set[datetime] = set()
        for offset in range(-1, 3):
            start_date = instant.date() + timedelta(days=offset)
            for period in self.periods:
                candidates.update(period.bounds_for(start_date, self.timezone))
        for boundary in sorted(candidate for candidate in candidates if candidate > instant):
            after = boundary + timedelta(microseconds=1)
            inside = any(period.contains(after) for period in self.periods)
            allowed = inside if self.mode == "available" else not inside
            if allowed != current_allowed:
                return boundary.timestamp()
        return None

    def seconds_until_change(self, now: datetime | float | None = None) -> float | None:
        instant = self._as_local_datetime(now)
        decision = self.evaluate(instant)
        if decision.next_change_at is None:
            return None
        return max(0.0, decision.next_change_at - instant.timestamp())

    def describe(self, now: datetime | float | None = None) -> dict[str, Any]:
        decision = self.evaluate(now)
        return {
            "enabled": True,
            "mode": self.mode,
            "timezone": self.timezone_name,
            "timezone_explicit": self.timezone_explicit,
            "allowed": decision.allowed,
            "reason": decision.reason,
            "next_change_at": decision.next_change_at,
        }
