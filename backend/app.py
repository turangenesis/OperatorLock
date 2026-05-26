"""
OperatorLock Flask application factory.

Creates the Flask app, registers all blueprints, and hydrates state from
Firestore on startup. This is the entry point for both gunicorn (Heroku)
and `flask run` (local development).
"""

import os

from flask import Flask

from config import FLASK_SECRET_KEY
from auth import init_auth
from services.state_manager import hydrate_state_from_firestore

from routes.webhooks import bp as webhooks_bp
from routes.orders import bp as orders_bp
from routes.bridge import bp as bridge_bp
from routes.controls import bp as controls_bp
from routes.state import bp as state_bp


def create_app() -> Flask:
    app = Flask(__name__, static_folder="static")
    app.secret_key = FLASK_SECRET_KEY

    init_auth(app)

    app.register_blueprint(webhooks_bp)
    app.register_blueprint(orders_bp)
    app.register_blueprint(bridge_bp)
    app.register_blueprint(controls_bp)
    app.register_blueprint(state_bp)

    hydrate_state_from_firestore()

    return app


app = create_app()
