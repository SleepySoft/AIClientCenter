"""AI Client 配置页：既可独立发布，也可作为 Blueprint 嵌入宿主。"""

from __future__ import annotations

import secrets
from typing import Callable, Optional

from flask import Blueprint, Flask, jsonify, render_template, request, session

from AIClientCenter.configuration import AIClientConfigurationService
from AIClientCenter.factory_engine import ReloadInProgressError


class AIClientConfigurationWeb:
    def __init__(self, service: AIClientConfigurationService, *, reloader=None):
        self.service = service
        self.reloader = reloader
        self._registered_apps: set[int] = set()

    def create_blueprint(self, wrapper: Optional[Callable] = None) -> Blueprint:
        bp = Blueprint(
            "ai_client_configuration", __name__,
            template_folder="../templates",
        )

        def protected(function):
            return wrapper(function) if wrapper else function

        def require_csrf():
            expected = session.get("ai_config_csrf")
            supplied = request.headers.get("X-Config-CSRF")
            if not expected or not secrets.compare_digest(expected, supplied or ""):
                return jsonify({"error": "Invalid or missing CSRF token."}), 403
            return None

        @bp.get("/")
        @protected
        def page():
            token = secrets.token_urlsafe(32)
            session["ai_config_csrf"] = token
            return render_template(
                "ai_client_config.html", csrf_token=token,
                apply_enabled=self.reloader is not None,
            )

        @bp.get("/api/config")
        @protected
        def get_config():
            payload = self.service.read()
            payload["configuration_metadata"] = self.service.metadata()
            payload["factories"] = sorted(
                payload["configuration_metadata"]["factories"])
            payload["apply_enabled"] = self.reloader is not None
            return jsonify(payload)

        @bp.get("/api/history")
        @protected
        def get_history():
            return jsonify({"items": self.service.history()})

        @bp.post("/api/validate")
        @protected
        def validate():
            denied = require_csrf()
            if denied:
                return denied
            try:
                body = request.get_json(silent=True) or {}
                result = (self.service.validate_text(body["yaml"])
                          if "yaml" in body else self.service.validate_data(body["data"]))
                return jsonify(result)
            except (KeyError, OSError, TypeError, ValueError) as exc:
                return jsonify({"valid": False, "error": str(exc)}), 400

        @bp.post("/api/save")
        @protected
        def save():
            denied = require_csrf()
            if denied:
                return denied
            try:
                body = request.get_json(silent=True) or {}
                result = (self.service.save_text(body["yaml"])
                          if "yaml" in body else self.service.save_data(body["data"]))
                return jsonify(result)
            except (KeyError, OSError, TypeError, ValueError) as exc:
                return jsonify({"saved": False, "error": str(exc)}), 400

        @bp.post("/api/apply")
        @protected
        def apply():
            denied = require_csrf()
            if denied:
                return denied
            if self.reloader is None:
                return jsonify({"error": "Runtime reload is not available."}), 501
            try:
                return jsonify(self.reloader.reload())
            except ReloadInProgressError as exc:
                return jsonify({"error": str(exc)}), 409
            except (OSError, ValueError) as exc:
                return jsonify({"error": str(exc)}), 400

        @bp.post("/api/restore")
        @protected
        def restore():
            denied = require_csrf()
            if denied:
                return denied
            try:
                result = self.service.restore((request.get_json(silent=True) or {})["name"])
                return jsonify(result)
            except (KeyError, OSError, ValueError) as exc:
                return jsonify({"restored": False, "error": str(exc)}), 400

        return bp

    def mount_to_app(self, app: Flask, *, url_prefix="/config",
                     wrapper: Optional[Callable] = None) -> bool:
        identity = id(app)
        if identity in self._registered_apps:
            return False
        try:
            from WebI18n import ensure_web_i18n
            ensure_web_i18n(app)
        except ImportError:
            # AIClientCenter 独立发布时不强依赖 IIS；模板退回英文 identity gettext。
            app.jinja_env.globals.setdefault("_", lambda value: value)
            app.jinja_env.globals.setdefault("gettext", lambda value: value)
            app.context_processor(lambda: {"ui_locale": "en"})
        app.register_blueprint(
            self.create_blueprint(wrapper), url_prefix=url_prefix)
        self._registered_apps.add(identity)
        return True
