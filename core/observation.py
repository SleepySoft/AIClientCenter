"""轻量、通用的 AI 调用观测与切片统计。"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional


_SLICE_FIELDS = {
    "provider": "provider",
    "model": "model",
    "client_name": "client_name",
    "account_alias": "account_alias",
    "group_id": "group_id",
}


class CallObservationSQLiteLogger:
    """按调用追加观测记录；不保存 Prompt 或响应正文。"""

    def __init__(self, sqlite_db_path: str, *, retention_days: int = 90):
        if retention_days < 1:
            raise ValueError("retention_days 必须为正整数")
        self.sqlite_db_path = str(Path(sqlite_db_path).resolve())
        self.retention_days = int(retention_days)
        self._lock = threading.RLock()
        self._write_count = 0
        Path(self.sqlite_db_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.sqlite_db_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ai_calls (
                    request_id TEXT PRIMARY KEY,
                    started_at REAL NOT NULL,
                    finished_at REAL NOT NULL,
                    provider TEXT,
                    model TEXT,
                    client_name TEXT NOT NULL,
                    account_alias TEXT,
                    group_id TEXT,
                    success INTEGER NOT NULL,
                    is_health_check INTEGER NOT NULL DEFAULT 0,
                    elapsed_ms REAL NOT NULL,
                    logical_input_bytes INTEGER NOT NULL DEFAULT 0,
                    logical_output_bytes INTEGER NOT NULL DEFAULT 0,
                    estimated_input_tokens INTEGER,
                    estimated_output_tokens INTEGER,
                    reported_input_tokens INTEGER,
                    reported_output_tokens INTEGER,
                    reported_total_tokens INTEGER,
                    cached_input_tokens INTEGER,
                    uncached_input_tokens INTEGER,
                    reasoning_output_tokens INTEGER,
                    token_source TEXT NOT NULL,
                    finish_reason TEXT,
                    error_type TEXT,
                    error_code TEXT,
                    feedback_valid INTEGER,
                    feedback_score REAL,
                    feedback_json TEXT,
                    feedback_at REAL
                )
                """
            )
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(ai_calls)")
            }
            for name in (
                "cached_input_tokens", "uncached_input_tokens",
                "reasoning_output_tokens",
            ):
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE ai_calls ADD COLUMN {name} INTEGER")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_ai_calls_time ON ai_calls(started_at)"
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_ai_calls_provider_model_time
                ON ai_calls(provider, model, started_at)
                """
            )

    def attach_client(self, client_obj: Any) -> None:
        """订阅 Client 事件，不替换已经存在的状态记录器。"""

        if hasattr(client_obj, "add_event_sink"):
            client_obj.add_event_sink(self.handle_event)
        elif hasattr(client_obj, "set_event_sink"):
            client_obj.set_event_sink(self.handle_event)

    def handle_event(self, event: dict[str, Any]) -> None:
        if event.get("type") != "chat_end":
            return
        try:
            self.record(event)
        except Exception:
            # 观测永远不能影响调用主链。
            return

    def record(self, event: dict[str, Any]) -> None:
        request_id = str(event.get("request_id") or "").strip()
        if not request_id:
            raise ValueError("chat_end 事件缺少 request_id")
        error = event.get("error") if isinstance(event.get("error"), dict) else {}
        row = (
            request_id,
            float(event.get("started_at", event.get("ts", time.time()))),
            float(event.get("ts", time.time())),
            _optional_text(event.get("provider")),
            _optional_text(event.get("model")),
            str(event.get("client_name") or "Unknown"),
            _optional_text(event.get("account_alias")),
            _optional_text(event.get("group_id")),
            int(bool(event.get("success"))),
            int(bool(event.get("is_health_check"))),
            max(0.0, float(event.get("elapsed_ms", 0.0))),
            _non_negative_int(event.get("logical_input_bytes"), 0),
            _non_negative_int(event.get("logical_output_bytes"), 0),
            _optional_non_negative_int(event.get("estimated_input_tokens")),
            _optional_non_negative_int(event.get("estimated_output_tokens")),
            _optional_non_negative_int(event.get("reported_input_tokens")),
            _optional_non_negative_int(event.get("reported_output_tokens")),
            _optional_non_negative_int(event.get("reported_total_tokens")),
            _optional_non_negative_int(event.get("cached_input_tokens")),
            _optional_non_negative_int(event.get("uncached_input_tokens")),
            _optional_non_negative_int(event.get("reasoning_output_tokens")),
            str(event.get("token_source") or "estimated"),
            _optional_text(event.get("finish_reason")),
            _optional_text(error.get("type") or event.get("error_type")),
            _optional_text(error.get("code") or event.get("error_code")),
        )
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO ai_calls (
                    request_id, started_at, finished_at, provider, model,
                    client_name, account_alias, group_id, success, is_health_check,
                    elapsed_ms, logical_input_bytes, logical_output_bytes,
                    estimated_input_tokens, estimated_output_tokens,
                    reported_input_tokens, reported_output_tokens,
                    reported_total_tokens, cached_input_tokens,
                    uncached_input_tokens, reasoning_output_tokens,
                    token_source, finish_reason,
                    error_type, error_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                row,
            )
            self._write_count += 1
            if self._write_count % 100 == 0:
                cutoff = time.time() - self.retention_days * 86400
                connection.execute("DELETE FROM ai_calls WHERE started_at < ?", (cutoff,))

    def feedback(
            self,
            request_id: str,
            *,
            valid: Optional[bool] = None,
            score: Optional[float] = None,
            labels: Optional[dict[str, Any]] = None,
    ) -> bool:
        """可选回填通用质量反馈；未回填不会影响基础统计。"""

        if score is not None and not math.isfinite(float(score)):
            raise ValueError("feedback score 必须是有限数值")
        payload = json.dumps(
            labels or {}, ensure_ascii=False, separators=(",", ":"), default=str
        )
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE ai_calls SET
                    feedback_valid = ?, feedback_score = ?,
                    feedback_json = ?, feedback_at = ?
                WHERE request_id = ?
                """,
                (
                    None if valid is None else int(bool(valid)),
                    None if score is None else float(score),
                    payload,
                    time.time(),
                    str(request_id),
                ),
            )
            return cursor.rowcount > 0

    def query_calls(
            self,
            *,
            since_ts: Optional[float] = None,
            until_ts: Optional[float] = None,
            limit: int = 100,
    ) -> list[dict[str, Any]]:
        """读取最近调用；主要用于调试和管理页面。"""

        if limit < 1 or limit > 10_000:
            raise ValueError("limit 必须在 1..10000 范围内")
        where, params = _time_filter(since_ts, until_ts)
        query = "SELECT * FROM ai_calls" + where + " ORDER BY started_at DESC LIMIT ?"
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, (*params, limit)).fetchall()
        return [_decode_row(row) for row in rows]

    def summarize(
            self,
            *,
            since_ts: Optional[float] = None,
            until_ts: Optional[float] = None,
            group_by: Iterable[str] = ("provider", "model"),
            time_bucket: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """按白名单维度切片汇总调用量、Token、字节、延迟和反馈。"""

        dimensions = tuple(group_by)
        unknown = [item for item in dimensions if item not in _SLICE_FIELDS]
        if unknown:
            raise ValueError("不支持的统计维度: " + ", ".join(unknown))
        columns = [_SLICE_FIELDS[item] for item in dimensions]
        if time_bucket not in (None, "hour", "day"):
            raise ValueError("time_bucket 只支持 hour、day 或 None")
        bucket_expression = None
        if time_bucket == "hour":
            bucket_expression = (
                "strftime('%Y-%m-%dT%H:00:00Z', started_at, 'unixepoch')"
            )
        elif time_bucket == "day":
            bucket_expression = "strftime('%Y-%m-%d', started_at, 'unixepoch')"
        select_columns = list(columns)
        group_columns = list(columns)
        if bucket_expression:
            select_columns.insert(0, f"{bucket_expression} AS time_bucket")
            group_columns.insert(0, bucket_expression)
        select_prefix = ", ".join(select_columns) + ", " if select_columns else ""
        group_clause = (
            " GROUP BY " + ", ".join(group_columns) if group_columns else ""
        )
        where, params = _time_filter(since_ts, until_ts)
        query = f"""
            SELECT {select_prefix}
                COUNT(*) AS request_count,
                SUM(success) AS success_count,
                SUM(logical_input_bytes) AS logical_input_bytes,
                SUM(logical_output_bytes) AS logical_output_bytes,
                SUM(COALESCE(reported_input_tokens, estimated_input_tokens, 0))
                    AS input_tokens,
                SUM(COALESCE(reported_output_tokens, estimated_output_tokens, 0))
                    AS output_tokens,
                SUM(COALESCE(cached_input_tokens, 0)) AS cached_input_tokens,
                SUM(COALESCE(uncached_input_tokens, 0)) AS uncached_input_tokens,
                SUM(COALESCE(reasoning_output_tokens, 0)) AS reasoning_output_tokens,
                AVG(elapsed_ms) AS average_elapsed_ms,
                SUM(CASE WHEN token_source = 'reported' THEN 1 ELSE 0 END)
                    AS reported_token_calls,
                SUM(is_health_check) AS health_check_count,
                SUM(CASE WHEN feedback_valid IS NOT NULL THEN 1 ELSE 0 END)
                    AS feedback_count,
                SUM(COALESCE(feedback_valid, 0)) AS feedback_valid_count,
                AVG(feedback_score) AS average_feedback_score
            FROM ai_calls{where}{group_clause}
            ORDER BY request_count DESC
        """
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            count = int(item["request_count"] or 0)
            success = int(item["success_count"] or 0)
            feedback_count = int(item["feedback_count"] or 0)
            feedback_valid = int(item["feedback_valid_count"] or 0)
            item["success_rate"] = success / count if count else None
            item["feedback_valid_rate"] = (
                feedback_valid / feedback_count if feedback_count else None
            )
            result.append(item)
        return result


def _time_filter(
        since_ts: Optional[float], until_ts: Optional[float],
) -> tuple[str, tuple[float, ...]]:
    clauses: list[str] = []
    params: list[float] = []
    if since_ts is not None:
        clauses.append("started_at >= ?")
        params.append(float(since_ts))
    if until_ts is not None:
        clauses.append("started_at < ?")
        params.append(float(until_ts))
    return (" WHERE " + " AND ".join(clauses) if clauses else "", tuple(params))


def _optional_text(value: Any) -> Optional[str]:
    return None if value is None else str(value)


def _non_negative_int(value: Any, default: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _optional_non_negative_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    return _non_negative_int(value, 0)


def _decode_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["success"] = bool(item["success"])
    item["is_health_check"] = bool(item["is_health_check"])
    if item["feedback_valid"] is not None:
        item["feedback_valid"] = bool(item["feedback_valid"])
    try:
        item["feedback"] = json.loads(item.pop("feedback_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        item["feedback"] = {}
    return item
