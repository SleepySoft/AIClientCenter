"""AI Client 的周时间窗调度策略。

策略只由外部 ``AIClientManager`` 检查，不修改 ClientStatus，也不会中断已经开始的调用。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import re
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


WEEKDAYS = {
    "mon": 0, "monday": 0,
    "tue": 1, "tuesday": 1,
    "wed": 2, "wednesday": 2,
    "thu": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}
@dataclass(frozen=True)
class AvailabilityDecision:
    allowed: bool
    reason: str | None = None
    next_change_at: float | None = None


@dataclass(frozen=True)
class WeeklyPeriod:
    days: frozenset[int]
    start: time
    end: time

    def bounds_for(self, start_date: date, timezone: ZoneInfo) -> tuple[datetime, datetime]:
        start_at = datetime.combine(start_date, self.start, tzinfo=timezone)
        end_date = start_date + timedelta(days=1) if self.end <= self.start else start_date
        end_at = datetime.combine(end_date, self.end, tzinfo=timezone)
        return start_at, end_at

    def contains(self, instant: datetime) -> bool:
        # 跨午夜窗口属于其开始日，例如 mon 22:00→07:00 覆盖周二凌晨。
        for start_date in (instant.date(), instant.date() - timedelta(days=1)):
            if start_date.weekday() not in self.days:
                continue
            start_at, end_at = self.bounds_for(start_date, instant.tzinfo)
            if start_at <= instant < end_at:
                return True
        return False


class ClientAvailabilitySchedule:
    """按 IANA 时区计算的每周可用或不可用时间段。"""

    def __init__(self, *, mode: str, timezone: ZoneInfo,
                 periods: Iterable[WeeklyPeriod], enabled: bool = True):
        normalized_mode = str(mode).strip().lower()
        if normalized_mode not in {"available", "unavailable"}:
            raise ValueError("availability.mode 必须是 available 或 unavailable。")
        self.mode = normalized_mode
        self.timezone = timezone
        self.periods = tuple(periods)
        self.enabled = bool(enabled)
        if self.enabled and not self.periods:
            raise ValueError("availability.periods 启用时不能为空。")

    @classmethod
    def from_config(cls, config: dict[str, Any] | None) -> "ClientAvailabilitySchedule | None":
        if config is None:
            return None
        if not isinstance(config, dict):
            raise ValueError("availability 必须是对象。")
        unknown = set(config).difference({"enabled", "timezone", "mode", "periods"})
        if unknown:
            raise ValueError("availability 含未知字段: " + ", ".join(sorted(unknown)))

        enabled = config.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("availability.enabled 必须是布尔值。")
        if not enabled:
            return None
        timezone_name = config.get("timezone")
        if not isinstance(timezone_name, str) or not timezone_name.strip():
            raise ValueError("availability.timezone 必须是显式 IANA 时区名称。")
        try:
            timezone = ZoneInfo(timezone_name.strip())
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"availability.timezone 不存在: {timezone_name!r}。") from exc

        mode = config.get("mode")
        if not isinstance(mode, str):
            raise ValueError("availability.mode 必须是 available 或 unavailable。")
        raw_periods = config.get("periods")
        if not isinstance(raw_periods, list):
            raise ValueError("availability.periods 必须是列表。")
        periods = [cls._parse_period(item, index) for index, item in enumerate(raw_periods)]
        return cls(mode=mode, timezone=timezone, periods=periods, enabled=enabled)

    @staticmethod
    def _parse_period(raw: Any, index: int) -> WeeklyPeriod:
        location = f"availability.periods[{index}]"
        if not isinstance(raw, dict):
            raise ValueError(f"{location} 必须是对象。")
        unknown = set(raw).difference({"days", "start", "end"})
        if unknown:
            raise ValueError(f"{location} 含未知字段: " + ", ".join(sorted(unknown)))
        raw_days = raw.get("days", ["mon", "tue", "wed", "thu", "fri", "sat", "sun"])
        if not isinstance(raw_days, list) or not raw_days:
            raise ValueError(f"{location}.days 必须是非空列表。")
        days = set()
        for raw_day in raw_days:
            day = WEEKDAYS.get(str(raw_day).strip().lower())
            if day is None:
                raise ValueError(f"{location}.days 含无效星期: {raw_day!r}。")
            days.add(day)
        start = ClientAvailabilitySchedule._parse_time(raw.get("start"), f"{location}.start")
        end = ClientAvailabilitySchedule._parse_time(raw.get("end"), f"{location}.end")
        if start == end:
            raise ValueError(f"{location} 的 start 与 end 不能相同。")
        return WeeklyPeriod(frozenset(days), start, end)

    @staticmethod
    def _parse_time(raw: Any, location: str) -> time:
        if not isinstance(raw, str):
            raise ValueError(f"{location} 必须是 HH:MM。")
        if re.fullmatch(r"\d{2}:\d{2}", raw.strip()) is None:
            raise ValueError(f"{location} 必须是 HH:MM。")
        try:
            parsed = time.fromisoformat(raw.strip())
        except ValueError as exc:
            raise ValueError(f"{location} 必须是 HH:MM。") from exc
        if parsed.second or parsed.microsecond or parsed.tzinfo is not None:
            raise ValueError(f"{location} 只允许 HH:MM。")
        return parsed

    def evaluate(self, now: datetime | float | None = None) -> AvailabilityDecision:
        if not self.enabled:
            return AvailabilityDecision(True)
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
        for offset in range(-1, 9):
            start_date = instant.date() + timedelta(days=offset)
            for period in self.periods:
                if start_date.weekday() not in period.days:
                    continue
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
            "enabled": self.enabled,
            "mode": self.mode,
            "timezone": self.timezone.key,
            "allowed": decision.allowed,
            "reason": decision.reason,
            "next_change_at": decision.next_change_at,
        }
