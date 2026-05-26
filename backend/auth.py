"""
Authentication middleware.

Password-protected session auth for the operator dashboard.
If DASHBOARD_PASSWORD is not set, all routes pass through (dev mode).
"""

import os
from functools import wraps

from flask import Flask, jsonify, request, session


def init_auth(app: Flask) -> None:
    """Register login/logout routes on the Flask app."""

    @app.route("/api/login", methods=["POST"])
    def api_login():
        password = os.environ.get("DASHBOARD_PASSWORD", "")
        if not password:
            session["authenticated"] = True
            return jsonify({"ok": True})

        data = request.get_json(force=True, silent=True) or {}
        if data.get("password") == password:
            session["authenticated"] = True
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "Invalid password"}), 401

    @app.route("/api/logout", methods=["POST"])
    def api_logout():
        session.clear()
        return jsonify({"ok": True})

    @app.route("/api/auth/status", methods=["GET"])
    def api_auth_status():
        password = os.environ.get("DASHBOARD_PASSWORD", "")
        if not password:
            return jsonify({"authenticated": True, "password_required": False})
        return jsonify({
            "authenticated": bool(session.get("authenticated")),
            "password_required": True,
        })


def login_required(f):
    """Decorator that requires an authenticated session (or no password configured)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        password = os.environ.get("DASHBOARD_PASSWORD", "")
        if not password or session.get("authenticated"):
            return f(*args, **kwargs)
        return jsonify({"ok": False, "error": "Authentication required"}), 401
    return decorated
