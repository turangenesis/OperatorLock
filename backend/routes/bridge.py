"""
Bridge routes — Rithmic VPS bridge communication endpoints.

These endpoints are called by the C# VPS bridge, not by the browser.
All require the BRIDGE_HEARTBEAT_SECRET for authentication.
"""

import time

from flask import Blueprint, jsonify, request

from config import BRIDGE_HEARTBEAT_SECRET
from services.state_manager import state, append_log
from services.behavioral_engine import (
    ensure_daily_stop_day_initialized,
    maybe_trigger_daily_stop,
    update_daily_stop_metrics,
    compute_zone,
)
from services.exit_engine import maybe_points_take_profit_exit, maybe_protect_exit
from stores.state_store import save_asset_state
from utils.type_helpers import safe_float

bp = Blueprint("bridge", __name__)


@bp.route("/api/rithmic/pnl-snapshot", methods=["POST"])
def rithmic_pnl_snapshot():
    data = request.get_json(force=True, silent=True) or {}

    if data.get("secret") != BRIDGE_HEARTBEAT_SECRET:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    now = time.time()
    state["global"]["rithmic_last_ts"] = now

    state["global"]["rithmic_realized"] = safe_float(data.get("realized"), default=0.0, nonfinite_to=0.0)
    state["global"]["rithmic_unrealized"] = safe_float(data.get("unrealized"), default=0.0, nonfinite_to=0.0)

    bal = data.get("accountBalance")
    if bal is None:
        state["global"]["rithmic_account_balance"] = None
    else:
        state["global"]["rithmic_account_balance"] = safe_float(bal, default=None, nonfinite_to=None)

    if state["global"]["rithmic_account_balance"] is not None:
        state["global"]["equity"] = state["global"]["rithmic_account_balance"]

    state["global"]["open_pnl"] = safe_float(
        state["global"].get("rithmic_unrealized"), default=0.0, nonfinite_to=0.0
    )

    # Update per-asset position/PnL from Rithmic
    from config import CONTRACT_TO_UI, resolve_rithmic_exchange, resolve_rithmic_symbol
    from services.behavioral_engine import point_value_for_contract

    symbols = data.get("symbols", []) or []
    for p in symbols:
        contract = (p.get("symbol") or "").upper()
        ui_sym = CONTRACT_TO_UI.get(contract)
        if not ui_sym or ui_sym not in state["assets"]:
            continue

        a = state["assets"][ui_sym]
        open_pnl_usd = safe_float(p.get("openPnl"), default=0.0, nonfinite_to=0.0)
        position = safe_float(p.get("position"), default=0.0, nonfinite_to=0.0)

        a["pnl"] = open_pnl_usd
        a["rithmic_symbol"] = contract

        try:
            pos_int = int(position)
        except Exception:
            pos_int = 0

        point_val = point_value_for_contract(contract, ui_sym)
        a["rithmic_point_value"] = point_val

        if pos_int != 0:
            open_points = open_pnl_usd / (abs(pos_int) * point_val) if point_val > 0 else 0.0
        else:
            open_points = 0.0
        a["rithmic_open_points"] = open_points

    # Daily stop / metrics
    ensure_daily_stop_day_initialized()
    update_daily_stop_metrics()
    maybe_trigger_daily_stop()

    # Auto exits
    for sym in list(state["assets"].keys()):
        maybe_points_take_profit_exit(sym)
        maybe_protect_exit(sym)

    append_log("rithmic", {"ts": now, "type": "pnl_snapshot", "symbol": "RITHMIC", "payload": data})
    return jsonify({"ok": True})


@bp.route("/api/bridge/heartbeat", methods=["POST"])
def rithmic_bridge_heartbeat():
    data = request.get_json(force=True, silent=True) or {}

    if data.get("secret") != BRIDGE_HEARTBEAT_SECRET:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    now = time.time()
    state["global"]["rithmic_last_ts"] = now
    state["global"]["rithmic_bridge"] = {
        "tsConnected": bool(data.get("tsConnected")),
        "mdConnected": bool(data.get("mdConnected")),
        "repoOk": bool(data.get("repoOk")),
        "account": data.get("account"),
        "tradeRoute": data.get("tradeRoute"),
        "exchange": data.get("exchange"),
        "lastRapiEventUtc": data.get("lastRapiEventUtc"),
        "lastOrdersPollUtc": data.get("lastOrdersPollUtc"),
        "lastOrdersOkUtc": data.get("lastOrdersOkUtc"),
        "lastPnlMsgUtc": data.get("lastPnlMsgUtc"),
        "note": data.get("note"),
        "server_received_ts": now,
    }

    append_log("rithmic", {"ts": now, "type": "heartbeat", "symbol": "RITHMIC", "payload": data})
    return jsonify({"ok": True})


@bp.route("/rithmic/md-ticks", methods=["POST"])
def rithmic_md_ticks():
    data = request.get_json(force=True, silent=True) or {}
    now = time.time()
    append_log("rithmic", {"ts": now, "type": "md_ticks", "symbol": "RITHMIC", "payload": data})
    return jsonify({"ok": True})
