"""AI Client 配置的安全编辑、校验和历史版本管理。

本模块属于 AIClientCenter，不依赖 IIS 的配置模型。Web 层和 CLI 都只通过这里
读写配置，校验始终复用 ``factory_engine`` 的真实工厂构造流程。
"""

from __future__ import annotations

import os
import re
import hashlib
import json
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml

from AIClientCenter.core.manager import AIClientManager
from AIClientCenter.factory_engine import RESERVED_FIELDS, load_config, validate_config


KEEP_SECRET = "__KEEP_EXISTING_SECRET__"
_SECRET_KEY = re.compile(r"(?:password|passwd|secret|token|api[_-]?key)$", re.I)
REVIEWED_AI_CONFIG_SCHEMA_SHA256 = (
    "a72233cffb2a7ffae3d5f242ac3b7af8d34293d1f441c6c8191a1b1da76bd052")


def schema_ui_contract() -> dict[str, Any]:
    contract = {
        "top_level": ["clients", "group_limits"],
        "reserved_fields": sorted(RESERVED_FIELDS),
    }
    encoded = json.dumps(
        contract, sort_keys=True, separators=(",", ":")).encode("utf-8")
    actual = hashlib.sha256(encoded).hexdigest()
    compatible = actual == REVIEWED_AI_CONFIG_SCHEMA_SHA256
    return {
        "compatible": compatible,
        "reviewed_sha256": REVIEWED_AI_CONFIG_SCHEMA_SHA256,
        "actual_sha256": actual,
        "warning": None if compatible else "schema_ui_review_required",
    }


def _load_yaml_text(text: str) -> dict[str, Any]:
    try:
        value = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"YAML 解析失败: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("YAML 顶层必须是对象。")
    return value


def _redact(value: Any, key: str = "") -> Any:
    if _SECRET_KEY.search(key) and value not in (None, ""):
        return KEEP_SECRET
    if isinstance(value, dict):
        return {str(k): _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _restore_secrets(candidate: Any, current: Any) -> Any:
    if candidate == KEEP_SECRET:
        if current is None:
            raise ValueError("隐藏值没有可保留的现有配置。")
        return current
    if isinstance(candidate, dict):
        old = current if isinstance(current, dict) else {}
        return {key: _restore_secrets(value, old.get(key))
                for key, value in candidate.items()}
    if isinstance(candidate, list):
        old = current if isinstance(current, list) else []
        return [_restore_secrets(value, old[index] if index < len(old) else None)
                for index, value in enumerate(candidate)]
    return candidate


class AIClientConfigurationService:
    """管理单个 AI Client YAML；所有写入先验证、备份，再原子替换。"""

    def __init__(self, config_path, *, fallback_path=None, factories=None,
                 include_builtins: bool = True, history_limit: int = 30):
        self.config_path = Path(config_path).expanduser().resolve()
        self.fallback_path = (Path(fallback_path).expanduser().resolve()
                              if fallback_path else None)
        self.factories = factories
        self.include_builtins = include_builtins
        self.history_limit = max(1, int(history_limit))
        self._lock = threading.RLock()

    @property
    def history_dir(self) -> Path:
        return self.config_path.parent / ".config-history" / self.config_path.stem

    def read(self) -> dict[str, Any]:
        with self._lock:
            raw = self._read_current()
        public = _redact(raw)
        explicit = os.environ.get("AI_CLIENT_CONFIG", "").strip()
        selected_by = (
            "AI_CLIENT_CONFIG"
            if explicit and Path(explicit).expanduser().resolve() == self.config_path
            else "explicit_argument")
        return {
            "path": str(self.config_path),
            "selected_by": selected_by,
            "data": public,
            "yaml": yaml.safe_dump(public, allow_unicode=True, sort_keys=False),
            "exists": self.config_path.is_file(),
            "schema_ui_contract": schema_ui_contract(),
            "keep_secret_sentinel": KEEP_SECRET,
        }

    def validate_data(self, data: Mapping[str, Any]) -> dict[str, Any]:
        raw = dict(data)
        with self._lock:
            merged = _restore_secrets(raw, self._read_current())
        return self._validate_raw(merged)

    def validate_text(self, text: str) -> dict[str, Any]:
        return self.validate_data(_load_yaml_text(text))

    def save_data(self, data: Mapping[str, Any]) -> dict[str, Any]:
        return self._save(dict(data))

    def save_text(self, text: str) -> dict[str, Any]:
        return self._save(_load_yaml_text(text))

    def _save(self, candidate: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            current = self._read_current()
            merged = _restore_secrets(candidate, current)
            result = self._validate_raw(merged)
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            if self.config_path.is_file():
                self._backup_current()
            self._atomic_write(yaml.safe_dump(
                merged, allow_unicode=True, sort_keys=False))
            self._prune_history()
        return {**result, "saved": True, "path": str(self.config_path)}

    def history(self) -> list[dict[str, Any]]:
        if not self.history_dir.is_dir():
            return []
        return [{"name": path.name, "size": path.stat().st_size,
                 "modified_at": datetime.fromtimestamp(
                     path.stat().st_mtime, timezone.utc).isoformat()}
                for path in sorted(self.history_dir.glob("*.yaml"), reverse=True)]

    def restore(self, name: str) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+\.yaml", str(name or "")):
            raise ValueError("无效的历史版本名称。")
        source = (self.history_dir / name).resolve()
        if source.parent != self.history_dir.resolve() or not source.is_file():
            raise ValueError("历史版本不存在。")
        return self.save_text(source.read_text(encoding="utf-8"))

    def _read_current(self) -> dict[str, Any]:
        source = self.config_path
        if not source.is_file() and self.fallback_path and self.fallback_path.is_file():
            source = self.fallback_path
        if not source.is_file():
            return {"clients": [], "group_limits": {}}
        return _load_yaml_text(source.read_text(encoding="utf-8"))

    def _validate_raw(self, raw: dict[str, Any]) -> dict[str, Any]:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(
            prefix=f".{self.config_path.stem}.validate-", suffix=".yaml",
            dir=self.config_path.parent)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                yaml.safe_dump(raw, stream, allow_unicode=True, sort_keys=False)
            clients, limits = load_config(
                temporary, factories=self.factories,
                include_builtins=self.include_builtins)
            try:
                errors, warnings = validate_config(clients, limits)
                if errors:
                    raise ValueError("; ".join(errors))
                return {"valid": True, "warnings": warnings,
                        "client_count": len(clients), "group_count": len(limits)}
            finally:
                for client in clients.values():
                    AIClientManager._dispose_client(client)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _backup_current(self) -> None:
        self.history_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        shutil.copy2(self.config_path, self.history_dir / f"{stamp}.yaml")

    def _atomic_write(self, text: str) -> None:
        mode = self.config_path.stat().st_mode & 0o777 if self.config_path.exists() else 0o600
        handle, temporary = tempfile.mkstemp(
            prefix=f".{self.config_path.name}.", suffix=".tmp",
            dir=self.config_path.parent)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, self.config_path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _prune_history(self) -> None:
        for path in sorted(self.history_dir.glob("*.yaml"), reverse=True)[self.history_limit:]:
            path.unlink()
