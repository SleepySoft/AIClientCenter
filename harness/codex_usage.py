"""Codex subscription-window usage guard.

The official Codex app-server exposes account rate-limit percentages and reset
timestamps without starting a model turn.  Session token_count events are kept
as a local fallback.  Only the small quota snapshot is persisted; prompts,
responses, and authentication material are never stored.
"""

from __future__ import annotations

import json
import queue
import sqlite3
import subprocess
import threading
from datetime import datetime
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from AIClientCenter.core.budget import BudgetDecision


DEFAULT_STORAGE_PATH = (
    Path(__file__).resolve().parents[2] / "_data" / "codex_usage.sqlite3"
)


@dataclass(frozen=True)
class CodexUsageWindow:
    used_percent: float
    window_minutes: int
    resets_at: float


@dataclass(frozen=True)
class CodexUsageSnapshot:
    observed_at: float
    primary: Optional[CodexUsageWindow]
    secondary: Optional[CodexUsageWindow]
    plan_type: Optional[str] = None
    rate_limit_reached_type: Optional[str] = None


class CodexUsageManager:
    """Persist and enforce Codex's primary and secondary usage windows."""

    def __init__(
            self,
            *,
            reserve_percent: float = 20.0,
            primary_max_percent: float | None = None,
            secondary_max_percent: float | None = None,
            storage_path: str | Path = DEFAULT_STORAGE_PATH,
            namespace: str = "default",
            refresh_seconds: float = 30.0,
            reset_grace_seconds: float = 30.0,
            app_server_enabled: bool = True,
            app_server_timeout: float = 5.0,
            codex_command: str = "codex",
            bootstrap_from_codex_sessions: bool = True,
            session_refresh_seconds: float | None = None,
            codex_home: str | Path | None = None,
            clock: Callable[[], float] = time.time,
    ):
        self.reserve_percent = self._validate_percentage(
            "reserve_percent", reserve_percent, allow_100=False)
        default_max = 100.0 - self.reserve_percent
        self.primary_max_percent = self._validate_percentage(
            "primary_max_percent",
            default_max if primary_max_percent is None else primary_max_percent,
        )
        self.secondary_max_percent = self._validate_percentage(
            "secondary_max_percent",
            default_max if secondary_max_percent is None else secondary_max_percent,
        )
        self.storage_path = str(Path(storage_path).expanduser().resolve())
        self.namespace = str(namespace or "default").strip()
        if not self.namespace:
            raise ValueError("Codex usage namespace 不能为空")
        self.codex_home = Path(codex_home or Path.home() / ".codex").expanduser()
        if session_refresh_seconds is not None:
            refresh_seconds = session_refresh_seconds
        self.refresh_seconds = max(0.0, float(refresh_seconds))
        self.reset_grace_seconds = max(0.0, float(reset_grace_seconds))
        self.app_server_enabled = bool(app_server_enabled)
        self.app_server_timeout = max(0.5, float(app_server_timeout))
        self.codex_command = str(codex_command)
        self.bootstrap_from_codex_sessions = bool(bootstrap_from_codex_sessions)
        self._clock = clock
        self._lock = threading.RLock()
        self._snapshot: Optional[CodexUsageSnapshot] = None
        self._next_refresh_at = 0.0
        self._initialize()
        self._snapshot = self._load()
        self.refresh(force=True)

    @staticmethod
    def _validate_percentage(
            name: str, value: Any, *, allow_100: bool = True,
    ) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} 必须是 0 到 100 之间的数字") from exc
        upper_valid = result <= 100 if allow_100 else result < 100
        if result < 0 or not upper_valid:
            interval = "[0, 100]" if allow_100 else "[0, 100)"
            raise ValueError(f"{name} 必须在 {interval} 范围内")
        return result

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.storage_path, timeout=30.0)
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        Path(self.storage_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS codex_usage_snapshot (
                    namespace TEXT PRIMARY KEY,
                    observed_at REAL NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )

    def _load(self) -> Optional[CodexUsageSnapshot]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT observed_at, payload_json FROM codex_usage_snapshot "
                "WHERE namespace = ?", (self.namespace,),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row[1])
            return self.parse_snapshot(payload, observed_at=float(row[0]))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    @classmethod
    def parse_snapshot(
            cls, payload: Any, *, observed_at: Optional[float] = None,
    ) -> Optional[CodexUsageSnapshot]:
        if not isinstance(payload, dict):
            return None
        primary = cls._parse_window(payload.get("primary"))
        secondary = cls._parse_window(payload.get("secondary"))
        if primary is None and secondary is None:
            return None
        return CodexUsageSnapshot(
            observed_at=float(observed_at if observed_at is not None else time.time()),
            primary=primary,
            secondary=secondary,
            plan_type=_optional_text(payload.get("plan_type")),
            rate_limit_reached_type=_optional_text(
                payload.get("rate_limit_reached_type")),
        )

    @staticmethod
    def _parse_window(payload: Any) -> Optional[CodexUsageWindow]:
        if not isinstance(payload, dict):
            return None
        try:
            used_percent = max(0.0, min(100.0, float(payload["used_percent"])))
            window_minutes = max(0, int(payload.get("window_minutes", 0)))
            resets_at = max(0.0, float(payload.get("resets_at", 0)))
        except (KeyError, TypeError, ValueError):
            return None
        return CodexUsageWindow(used_percent, window_minutes, resets_at)

    def update_from_event(
            self, rate_limits: Any, *, observed_at: Optional[float] = None,
    ) -> Optional[CodexUsageSnapshot]:
        snapshot = self.parse_snapshot(rate_limits, observed_at=observed_at)
        if snapshot is not None:
            self.update_snapshot(snapshot)
        return snapshot

    def update_snapshot(self, snapshot: CodexUsageSnapshot) -> None:
        payload = {
            "primary": asdict(snapshot.primary) if snapshot.primary else None,
            "secondary": asdict(snapshot.secondary) if snapshot.secondary else None,
            "plan_type": snapshot.plan_type,
            "rate_limit_reached_type": snapshot.rate_limit_reached_type,
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO codex_usage_snapshot(namespace, observed_at, payload_json)
                VALUES (?, ?, ?)
                ON CONFLICT(namespace) DO UPDATE SET
                    observed_at = excluded.observed_at,
                    payload_json = excluded.payload_json
                WHERE excluded.observed_at >= codex_usage_snapshot.observed_at
                """,
                (self.namespace, snapshot.observed_at, encoded),
            )
            if self._snapshot is None or snapshot.observed_at >= self._snapshot.observed_at:
                self._snapshot = snapshot

    def get_snapshot(self) -> Optional[CodexUsageSnapshot]:
        with self._lock:
            return self._snapshot

    def refresh(self, *, force: bool = False) -> None:
        """Refresh from app-server, falling back to local Codex sessions."""
        current = float(self._clock())
        with self._lock:
            if not force and current < self._next_refresh_at:
                return
            # Reserve the slot before I/O so scheduler threads cannot all spawn
            # app-server or traverse the session directory simultaneously.
            self._next_refresh_at = current + self.refresh_seconds
        snapshot = self._load_app_server_snapshot() if self.app_server_enabled else None
        if snapshot is None and self.bootstrap_from_codex_sessions:
            snapshot = self._load_latest_session_snapshot()
        if snapshot is not None:
            self.update_snapshot(snapshot)

    def refresh_from_codex_sessions(self, *, force: bool = False) -> None:
        """Compatibility hook; refresh now prefers the official app-server API."""
        self.refresh(force=force)

    def evaluate(self, *, now: Optional[float] = None) -> BudgetDecision:
        self.refresh()
        snapshot = self.get_snapshot()
        if snapshot is None:
            # The first real call obtains an authoritative snapshot. Blocking here
            # would deadlock a fresh installation forever.
            return BudgetDecision(True, reason="codex_usage_snapshot_missing")
        current = float(now if now is not None else self._clock())
        checks = (
            ("primary", snapshot.primary, self.primary_max_percent),
            ("secondary", snapshot.secondary, self.secondary_max_percent),
        )
        window_names = {"primary": "5小时窗口", "secondary": "周窗口"}
        for name, window, limit in checks:
            if window is None or self._window_expired(window, current):
                continue
            if window.used_percent >= limit:
                resume_at = datetime.fromtimestamp(
                    window.resets_at + self.reset_grace_seconds).strftime("%m-%d %H:%M")
                return BudgetDecision(
                    False,
                    0.0,
                    # 前缀保持机器可解析（codex_*_limit:），后随人读文本
                    f"codex_{name}_limit: {window_names.get(name, name)}已用 "
                    f"{window.used_percent:.1f}%（上限 {limit:.1f}%），{resume_at} 恢复",
                )
        return BudgetDecision(True)

    def standardized_metrics(self, *, now: Optional[float] = None) -> list[dict[str, Any]]:
        self.refresh()
        snapshot = self.get_snapshot()
        if snapshot is None:
            return []
        current = float(now if now is not None else self._clock())
        result = []
        for key, window, limit in (
            ("codex_primary_percent", snapshot.primary, self.primary_max_percent),
            ("codex_secondary_percent", snapshot.secondary, self.secondary_max_percent),
        ):
            if window is None or self._window_expired(window, current):
                continue
            result.append({
                "type": "USAGE_LIMIT",
                "key": key,
                "current": window.used_percent,
                "target": limit,
                "window_minutes": window.window_minutes,
                "resets_at": window.resets_at,
                "resume_after": window.resets_at + self.reset_grace_seconds,
                "observed_at": snapshot.observed_at,
            })
        return result

    def _window_expired(self, window: CodexUsageWindow, current: float) -> bool:
        return window.resets_at + self.reset_grace_seconds <= current

    def _load_app_server_snapshot(self) -> Optional[CodexUsageSnapshot]:
        """Read account/rateLimits/read without creating a Codex model turn."""
        process: Optional[subprocess.Popen] = None
        messages: queue.Queue = queue.Queue()
        try:
            process = subprocess.Popen(
                [self.codex_command, "app-server"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            if process.stdin is None or process.stdout is None:
                return None

            def read_messages() -> None:
                assert process is not None and process.stdout is not None
                for line in process.stdout:
                    try:
                        messages.put(json.loads(line))
                    except (TypeError, json.JSONDecodeError):
                        continue
                messages.put(None)

            threading.Thread(target=read_messages, daemon=True).start()
            self._send_app_server(process, {
                "method": "initialize",
                "id": 0,
                "params": {
                    "clientInfo": {
                        "name": "iis_usage_guard",
                        "title": "IIS Codex Usage Guard",
                        "version": "1.0",
                    },
                },
            })
            initialized = self._wait_for_response(messages, 0)
            if not initialized or initialized.get("error"):
                return None
            self._send_app_server(
                process, {"method": "initialized", "params": {}})
            self._send_app_server(
                process, {"method": "account/rateLimits/read", "id": 1})
            response = self._wait_for_response(messages, 1)
            if not response or response.get("error"):
                return None
            result = response.get("result")
            if not isinstance(result, dict):
                return None
            buckets = result.get("rateLimitsByLimitId")
            bucket = buckets.get("codex") if isinstance(buckets, dict) else None
            if not isinstance(bucket, dict):
                bucket = result.get("rateLimits")
            return self._parse_app_server_bucket(bucket, observed_at=self._clock())
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
        finally:
            if process is not None:
                _close_process(process)

    def _wait_for_response(self, messages: queue.Queue, request_id: int) -> Any:
        deadline = time.monotonic() + self.app_server_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                message = messages.get(timeout=remaining)
            except queue.Empty:
                return None
            if message is None:
                return None
            if isinstance(message, dict) and message.get("id") == request_id:
                return message

    @staticmethod
    def _send_app_server(process: subprocess.Popen, message: dict[str, Any]) -> None:
        if process.stdin is None:
            raise OSError("Codex app-server stdin is unavailable")
        process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        process.stdin.flush()

    @classmethod
    def _parse_app_server_bucket(
            cls, bucket: Any, *, observed_at: float,
    ) -> Optional[CodexUsageSnapshot]:
        if not isinstance(bucket, dict):
            return None

        def normalize_window(window: Any) -> Any:
            if not isinstance(window, dict):
                return None
            return {
                "used_percent": window.get("usedPercent", window.get("used_percent")),
                "window_minutes": window.get(
                    "windowDurationMins", window.get("window_minutes", 0)),
                "resets_at": window.get("resetsAt", window.get("resets_at", 0)),
            }

        return cls.parse_snapshot({
            "primary": normalize_window(bucket.get("primary")),
            "secondary": normalize_window(bucket.get("secondary")),
            "plan_type": bucket.get("planType", bucket.get("plan_type")),
            "rate_limit_reached_type": bucket.get(
                "rateLimitReachedType", bucket.get("rate_limit_reached_type")),
        }, observed_at=observed_at)

    def _load_latest_session_snapshot(self) -> Optional[CodexUsageSnapshot]:
        session_root = self.codex_home / "sessions"
        if not session_root.is_dir():
            return None
        try:
            files = sorted(
                session_root.rglob("*.jsonl"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )[:20]
        except OSError:
            return None
        latest: Optional[CodexUsageSnapshot] = None
        for path in files:
            for line in _tail_lines(path):
                try:
                    event = json.loads(line)
                except (TypeError, json.JSONDecodeError):
                    continue
                payload = event.get("payload") if isinstance(event, dict) else None
                if not isinstance(payload, dict) or payload.get("type") != "token_count":
                    continue
                observed_at = _parse_timestamp(event.get("timestamp"))
                snapshot = self.parse_snapshot(
                    payload.get("rate_limits"), observed_at=observed_at)
                if snapshot and (latest is None or snapshot.observed_at > latest.observed_at):
                    latest = snapshot
            if latest is not None:
                break
        return latest


def _tail_lines(path: Path, max_bytes: int = 2 * 1024 * 1024) -> list[str]:
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - max_bytes))
            data = stream.read()
        if size > max_bytes:
            data = data.split(b"\n", 1)[-1]
        return data.decode("utf-8", errors="ignore").splitlines()
    except OSError:
        return []


def _close_process(process: subprocess.Popen) -> None:
    try:
        if process.stdin is not None:
            process.stdin.close()
        process.wait(timeout=1.0)
    except (OSError, subprocess.SubprocessError):
        try:
            process.terminate()
        except OSError:
            pass
        try:
            process.wait(timeout=1.0)
        except (OSError, subprocess.SubprocessError):
            try:
                process.kill()
            except OSError:
                pass


def _parse_timestamp(value: Any) -> float:
    if not value:
        return time.time()
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return time.time()


def _optional_text(value: Any) -> Optional[str]:
    text = str(value).strip() if value is not None else ""
    return text or None
