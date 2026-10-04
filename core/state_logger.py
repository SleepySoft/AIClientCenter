# AIClientCenter/ClientStateSQLiteLogger.py
import os
import json
import time
import uuid
import socket
import sqlite3
import threading
from typing import Optional, Dict, Any


class ClientStateSQLiteLogger:
    """
    Persist client state intervals into sqlite with run isolation + heartbeat self-healing.

    Design goals:
    - Interval-based logging: [ts_start, ts_end)
    - run_id isolation: avoid mixing across program launches
    - Heartbeat + reconcile: close dangling intervals even if no graceful shutdown
    """

    def __init__(self,
                 sqlite_db_path: str = "./ai_client_state_log.sqlite",
                 run_id: Optional[str] = None,
                 heartbeat_interval_sec: int = 30,
                 heartbeat_grace_sec: int = 120):
        self.sqlite_db_path = sqlite_db_path
        self.run_id = run_id or self._gen_run_id()
        self.heartbeat_interval_sec = heartbeat_interval_sec
        self.heartbeat_grace_sec = heartbeat_grace_sec

        self._db_lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None

        # Open intervals in memory: { client_name: {"row_id": int, "state": str, "model": str} }
        self._open: Dict[str, Dict[str, Any]] = {}

        # Heartbeat thread
        self._hb_running = False
        self._hb_thread: Optional[threading.Thread] = None

        self._init_db()
        self._register_run_meta()
        # Heal old runs on each start (important when last run crashed)
        self.reconcile_stale_runs()

    # -------------------------- Public API --------------------------

    def start(self):
        """Start heartbeat thread (daemon). Safe to call multiple times."""
        if self._hb_running:
            return
        self._hb_running = True
        self._hb_thread = threading.Thread(target=self._heartbeat_loop, name="ClientStateSQLiteLoggerHeartbeat", daemon=True)
        self._hb_thread.start()

    def stop(self):
        """
        Best-effort stop:
        - mark run end_ts
        - close all open intervals with end_ts = now
        Note: If caller never invokes stop(), reconcile will fix on next start.
        """
        now = time.time()
        try:
            # Close all open intervals
            for cname in list(self._open.keys()):
                self._close_interval(cname, now)
        except Exception:
            pass

        try:
            self._set_run_end_ts(now)
        except Exception:
            pass

        self._hb_running = False

    def attach_client(self, client_obj: Any):
        """
        Attach event sink to a client object (BaseAIClient) without coupling to client implementation.
        The client must implement set_event_sink(callable).
        """
        if hasattr(client_obj, "set_event_sink"):
            client_obj.set_event_sink(self.handle_event)

        # Create an initial idle interval for timeline baseline
        try:
            cname = getattr(client_obj, "name", "Unknown")
            model = client_obj.get_current_model() if hasattr(client_obj, "get_current_model") else None
            idle_state = self._derive_idle_state(client_obj)
            ts = time.time()

            # Close any unexpected open interval in memory
            if cname in self._open:
                self._close_interval(cname, ts)

            self._open_interval(cname, idle_state, model, ts, is_health_check=False, extra={"event": "register"})
        except Exception:
            pass

    def handle_event(self, event: Dict[str, Any]):
        """
        Receive events from BaseAIClient and persist intervals.
        Expected event types:
        - chat_start, chat_end, status_change
        """
        et = event.get("type")
        ts = float(event.get("ts", time.time()))
        cname = event.get("client_name", "Unknown")
        model = event.get("model")
        is_hc = bool(event.get("is_health_check", False))

        # Update run heartbeat as well (cheap)
        self._touch_run_heartbeat(ts)

        if et == "chat_start":
            # Transition to RUNNING interval
            self._ensure_state(cname, "RUNNING", model, ts, is_health_check=is_hc, extra={"event": "chat_start"})
            return

        if et == "chat_end":
            # Close RUNNING and finalize outcome
            success = bool(event.get("success", False))
            final_state = "RUN_SUCCESS" if success else "RUN_FAIL"

            err = event.get("error") or {}
            err_type = str(err.get("type")) if (not success and isinstance(err, dict) and err.get("type") is not None) else None
            err_code = str(err.get("code")) if (not success and isinstance(err, dict) and err.get("code") is not None) else None

            # Close current open interval and finalize it
            self._close_interval(cname, ts, final_state=final_state, error_type=err_type, error_code=err_code,
                                 extra_patch={"event": "chat_end", "success": success})

            # After chat ends, open an idle interval based on client's current status (if provided)
            client_obj = event.get("_client_obj")  # optional injection; usually not provided
            if client_obj is not None:
                idle_state = self._derive_idle_state(client_obj)
            else:
                # If caller doesn't pass client object, default to IDLE_OK. status_change event will correct later.
                idle_state = "IDLE_OK"

            self._open_interval(cname, idle_state, model, ts, is_health_check=is_hc, extra={"event": "idle_after_chat"})
            return

        if et == "status_change":
            # Do not override RUNNING; status changes during running are noisy
            open_info = self._open.get(cname)
            if open_info and open_info.get("state") == "RUNNING":
                return

            client_obj = event.get("_client_obj")
            if client_obj is not None:
                idle_state = self._derive_idle_state(client_obj)
            else:
                # Fallback mapping if client object is absent
                new_status = str(event.get("new_status", "")).split(".")[-1]
                if new_status == "UNAVAILABLE":
                    idle_state = "UNAVAILABLE"
                elif new_status in ("ERROR", "UNKNOWN"):
                    idle_state = "IDLE_ERROR"
                else:
                    idle_state = "IDLE_OK"

            self._ensure_state(cname, idle_state, model_name=None, ts=ts, is_health_check=False,
                               extra={"event": "status_change", "old": event.get("old_status"), "new": event.get("new_status")})
            return

    def reconcile_stale_runs(self):
        """
        Close runs that never wrote end_ts (crash/kill) by using last_heartbeat_ts.
        Also close dangling client intervals (ts_end IS NULL) for those runs.
        """
        now = time.time()
        with self._db_lock:
            cur = self._conn.cursor()

            # Find stale runs without end_ts and heartbeat expired
            cur.execute("""
                SELECT run_id, last_heartbeat_ts
                FROM run_meta
                WHERE end_ts IS NULL AND last_heartbeat_ts IS NOT NULL AND last_heartbeat_ts < ?
            """, (now - float(self.heartbeat_grace_sec),))
            rows = cur.fetchall()

            for rid, last_hb in rows:
                # Mark run end
                cur.execute("UPDATE run_meta SET end_ts=? WHERE run_id=? AND end_ts IS NULL", (float(last_hb), rid))
                # Close all dangling intervals for that run
                cur.execute("""
                    UPDATE client_state_log
                    SET ts_end=?
                    WHERE run_id=? AND ts_end IS NULL AND ts_start <= ?
                """, (float(last_hb), rid, float(last_hb)))

            self._conn.commit()

    def get_run_list(self, limit: int = 50) -> Dict[str, Any]:
        """
        Return recent run list for UI selection.
        Dashboard must not access DB directly; it calls this method.
        """
        with self._db_lock:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT run_id, start_ts, COALESCE(end_ts, 0), COALESCE(last_heartbeat_ts, 0), pid, host "
                "FROM run_meta ORDER BY start_ts DESC LIMIT ?",
                (int(limit),)
            )
            rows = cur.fetchall()

        runs = []
        for run_id, start_ts, end_ts, hb_ts, pid, host in rows:
            runs.append({
                "run_id": run_id,
                "start_ts": float(start_ts),
                "end_ts": float(end_ts) if end_ts else None,
                "last_heartbeat_ts": float(hb_ts) if hb_ts else None,
                "pid": pid,
                "host": host
            })
        return {"runs": runs}

    def query_timeline(self,
                       run_id: str,
                       from_ts: float,
                       to_ts: float,
                       client_name: Optional[str] = None,
                       limit: int = 200000) -> Dict[str, Any]:
        """
        Query interval logs for timeline plotting. Clip intervals into [from_ts, to_ts].
        """
        # Keep data sane
        from_ts = float(from_ts)
        to_ts = float(to_ts)
        if to_ts <= from_ts:
            to_ts = from_ts + 1.0

        # Optional: reconcile stale runs before serving data (cheap safety)
        try:
            self.reconcile_stale_runs()
        except Exception:
            pass

        legend = {
            "RUN_SUCCESS": "#22c55e",
            "RUN_FAIL": "#ef4444",
            "RUNNING": "#f59e0b",
            "IDLE_OK": "#e5e7eb",
            "IDLE_ERROR": "#fb923c",
            "UNAVAILABLE": "#6b7280",
            "UNKNOWN": "#93c5fd"
        }

        sql = (
            "SELECT client_name, COALESCE(model_name, ''), state, ts_start, COALESCE(ts_end, ?) "
            "FROM client_state_log "
            "WHERE run_id = ? "
            "  AND ts_start <= ? "
            "  AND COALESCE(ts_end, ?) >= ? "
        )
        params = [to_ts, run_id, to_ts, to_ts, from_ts]

        if client_name:
            sql += " AND client_name = ? "
            params.append(str(client_name))

        sql += " ORDER BY client_name, ts_start ASC LIMIT ?"
        params.append(int(limit))

        with self._db_lock:
            cur = self._conn.cursor()
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()

        items = []
        clients = set()
        for cname, model, st, s, e in rows:
            s = float(s)
            e = float(e)

            # Clip to window for drawing
            s2 = max(s, from_ts)
            e2 = min(e, to_ts)
            if e2 <= s2:
                continue

            clients.add(cname)
            items.append({
                "client": cname,
                "model": model or None,
                "state": st or "UNKNOWN",
                "start": s2,
                "end": e2
            })

        return {
            "run_id": run_id,
            "window": {"from": from_ts, "to": to_ts},
            "clients": sorted(list(clients)),
            "items": items,
            "legend": legend
        }

    # -------------------------- Internal: DB schema --------------------------

    def _init_db(self):
        os.makedirs(os.path.dirname(self.sqlite_db_path) or ".", exist_ok=True)
        with self._db_lock:
            self._conn = sqlite3.connect(self.sqlite_db_path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")

            self._conn.execute("""
            CREATE TABLE IF NOT EXISTS run_meta (
                run_id TEXT PRIMARY KEY,
                start_ts REAL NOT NULL,
                last_heartbeat_ts REAL,
                end_ts REAL,
                pid INTEGER,
                host TEXT
            );
            """)

            self._conn.execute("""
            CREATE TABLE IF NOT EXISTS client_state_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                ts_start REAL NOT NULL,
                ts_end REAL,
                client_name TEXT NOT NULL,
                model_name TEXT,
                state TEXT NOT NULL,
                is_health_check INTEGER DEFAULT 0,
                error_code TEXT,
                error_type TEXT,
                extra_json TEXT
            );
            """)

            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_log_run_client_ts ON client_state_log(run_id, client_name, ts_start);")
            self._conn.commit()

    def _register_run_meta(self):
        now = time.time()
        with self._db_lock:
            self._conn.execute("""
                INSERT OR IGNORE INTO run_meta(run_id, start_ts, last_heartbeat_ts, end_ts, pid, host)
                VALUES(?, ?, ?, NULL, ?, ?)
            """, (self.run_id, now, now, os.getpid(), socket.gethostname()))
            self._conn.commit()

    def _touch_run_heartbeat(self, ts: float):
        with self._db_lock:
            self._conn.execute("UPDATE run_meta SET last_heartbeat_ts=? WHERE run_id=?", (float(ts), self.run_id))
            self._conn.commit()

    def _set_run_end_ts(self, ts: float):
        with self._db_lock:
            self._conn.execute("UPDATE run_meta SET end_ts=? WHERE run_id=? AND end_ts IS NULL", (float(ts), self.run_id))
            self._conn.commit()

    # -------------------------- Internal: Heartbeat --------------------------

    def _heartbeat_loop(self):
        while self._hb_running:
            try:
                self._touch_run_heartbeat(time.time())
            except Exception:
                pass
            time.sleep(max(1, int(self.heartbeat_interval_sec)))

    # -------------------------- Internal: Interval state machine --------------------------

    def _derive_idle_state(self, client_obj: Any) -> str:
        """
        Derive idle state from client status (log-level state, not ClientStatus).
        """
        try:
            st = client_obj.get_status("status")
        except Exception:
            st = None
        st_str = str(st).split(".")[-1] if st is not None else "UNKNOWN"

        if st_str == "UNAVAILABLE":
            return "UNAVAILABLE"
        if st_str in ("ERROR", "UNKNOWN"):
            return "IDLE_ERROR"
        return "IDLE_OK"

    def _ensure_state(self, client_name: str, desired_state: str, model_name: Optional[str], ts: float,
                      is_health_check: bool = False, extra: Optional[Dict[str, Any]] = None):
        open_info = self._open.get(client_name)
        if open_info and open_info.get("state") == desired_state and open_info.get("model") == model_name:
            return
        if open_info:
            self._close_interval(client_name, ts)
        self._open_interval(client_name, desired_state, model_name, ts, is_health_check=is_health_check, extra=extra)

    def _open_interval(self, client_name: str, state: str, model_name: Optional[str], start_ts: float,
                       is_health_check: bool = False, extra: Optional[Dict[str, Any]] = None):
        extra_json = json.dumps(extra or {}, ensure_ascii=False)
        with self._db_lock:
            cur = self._conn.cursor()
            cur.execute("""
                INSERT INTO client_state_log(run_id, ts_start, ts_end, client_name, model_name, state, is_health_check, extra_json)
                VALUES(?, ?, NULL, ?, ?, ?, ?, ?)
            """, (self.run_id, float(start_ts), client_name, model_name, state, 1 if is_health_check else 0, extra_json))
            self._conn.commit()
            row_id = cur.lastrowid

        self._open[client_name] = {"row_id": row_id, "state": state, "model": model_name}

    def _close_interval(self, client_name: str, end_ts: float, final_state: Optional[str] = None,
                        error_type: Optional[str] = None, error_code: Optional[str] = None,
                        extra_patch: Optional[Dict[str, Any]] = None):
        open_info = self._open.get(client_name)
        if not open_info:
            return
        row_id = open_info["row_id"]
        new_state = final_state or open_info["state"]

        # Merge extra_json if patch provided
        extra_json = None
        if extra_patch is not None:
            try:
                with self._db_lock:
                    cur = self._conn.cursor()
                    cur.execute("SELECT extra_json FROM client_state_log WHERE id=?", (row_id,))
                    row = cur.fetchone()
                    base = json.loads(row[0]) if row and row[0] else {}
                    base.update(extra_patch)
                    extra_json = json.dumps(base, ensure_ascii=False)
            except Exception:
                extra_json = json.dumps(extra_patch, ensure_ascii=False)

        with self._db_lock:
            self._conn.execute("""
                UPDATE client_state_log
                SET ts_end=?, state=?, error_type=?, error_code=?, extra_json=COALESCE(?, extra_json)
                WHERE id=?
            """, (float(end_ts), new_state, error_type, error_code, extra_json, row_id))
            self._conn.commit()

        self._open.pop(client_name, None)

    # -------------------------- Internal: run_id --------------------------

    @staticmethod
    def _gen_run_id() -> str:
        return f"{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}_{uuid.uuid4().hex[:8]}"
