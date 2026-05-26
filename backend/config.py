import os


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(
            f"Required environment variable '{name}' is not set. "
            f"See .env.example for setup instructions."
        )
    return val


# ----------------------------------------------------------------
#  Webhook / bridge authentication
# ----------------------------------------------------------------
TV_WEBHOOK_SECRET = os.environ.get("TV_WEBHOOK_SECRET", "dev-secret")
BRIDGE_HEARTBEAT_SECRET = os.environ.get("BRIDGE_HEARTBEAT_SECRET", "dev-bridge-secret")

# ----------------------------------------------------------------
#  Flask
# ----------------------------------------------------------------
FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "dev-flask-secret-change-in-prod")

# ----------------------------------------------------------------
#  Dashboard auth
# ----------------------------------------------------------------
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")

# ----------------------------------------------------------------
#  Execution constants
# ----------------------------------------------------------------
LOG_MAX = 100
FIXED_ENTRY_QTY = 1
SCALE_IN_QTY = 5

# ----------------------------------------------------------------
#  Asset configuration
# ----------------------------------------------------------------
ASSETS = [
    {"symbol": "ES", "name": "S&P 500 Futures"},
]

ASSET_CONFIG = {
    "ES": {"size": 1, "stop_points": 15.0, "breakeven_trigger": 15.0},
}

POINT_VALUE_USD = {
    "ES": 50.0,
}

RITHMIC_EXCHANGES = {
    "ES": "CME",
}

RITHMIC_SYMBOLS = {
    "ES": "MESM6",
}

CONTRACT_TO_UI = {
    "MESM6": "ES",
}


def resolve_rithmic_exchange(asset_key: str) -> str:
    return RITHMIC_EXCHANGES.get(asset_key, "CME")


def resolve_rithmic_symbol(asset_key: str) -> str:
    return RITHMIC_SYMBOLS.get(asset_key, asset_key)
