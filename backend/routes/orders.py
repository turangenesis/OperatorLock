"""
Order routes — order creation, VPS bridge polling, execution reports.
"""

import time

from flask import Blueprint, jsonify, request

from config import (
    BRIDGE_HEARTBEAT_SECRET,
    FIXED_ENTRY_QTY, SCALE_IN_QTY,
    resolve_rithmic_symbol, resolve_rithmic_exchange, CONTRACT_TO_UI,
)
from services.state_manager import state, ORDER_INDEX
from services.behavioral_engine import (
    ensure_daily_stop_day_initialized,
    ensure_five_min_lock_initialized,
    ensure_post_exit_lock_initialized,
    activate_five_min_lock,
    compute_zone,
    entry_direction_allowed,
    clear_intent, clear_preorder,
    point_value_for_contract,
)
from services.execution_engine import handle_execution_report, close_trade
from stores.orders_store import create_order, get_pending_orders, update_order_status, get_order
from stores.state_store import save_asset_state
from utils.type_helpers import safe_float, safe_int, scrub_nonfinite
from utils.direction import parse_direction
from auth import login_required

bp = Blueprint("orders", __name__)


@bp.route("/api/orders", methods=["POST"])
@login_required
def api_create_order():
    payload = request.get_json(force=True) or {}

    order_type = (payload.get("order_type") or "market").lower().strip()
    if order_type not in ("market", "preorder"):
        return jsonify({"ok": False, "error": "Invalid order_type"}), 400

    asset = payload.get("asset")
    mode = "A"
    trade_mode = (payload.get("trade_mode") or "").upper().strip()
    exit_tf = "main"

    if asset:
        sym = str(asset).upper().strip()
        if sym not in state["assets"]:
            return jsonify({"ok": False, "error": "Unknown asset"}), 400

        if trade_mode not in ("SCALP", "RUNNER"):
            return jsonify({"ok": False, "error": "Invalid trade_mode"}), 400

        a = state["assets"][sym]
        g = state["global"]

        if bool(g.get("daily_stop_triggered")):
            return jsonify({"ok": False, "error": "Daily stop TRIGGERED — entries blocked for today"}), 403

        ensure_five_min_lock_initialized()
        ensure_post_exit_lock_initialized()

        if bool(g.get("post_exit_lock_active")):
            remaining = safe_int(g.get("post_exit_lock_remaining_s"), default=0)
            mm, ss = remaining // 60, remaining % 60
            return jsonify({"ok": False, "error": f"POST-EXIT 5 MIN LOCK ACTIVE ({mm:02d}:{ss:02d} remaining)"}), 403

        if bool(g.get("five_min_trade_lock_active")):
            remaining = safe_int(g.get("five_min_trade_lock_remaining_s"), default=0)
            mm, ss = remaining // 60, remaining % 60
            return jsonify({"ok": False, "error": f"5 MIN LOCK ACTIVE — one trade already used this candle ({mm:02d}:{ss:02d} remaining)"}), 403

        if g.get("trade_lock"):
            return jsonify({"ok": False, "error": "Global trade lock active"}), 403

        if a.get("position", 0) != 0:
            return jsonify({"ok": False, "error": "Asset already in a trade"}), 403

        if a.get("pending_order_id"):
            return jsonify({"ok": False, "error": "Pending order already exists"}), 403

        now = time.time()
        hb_ts = a.get("last_heartbeat_ts")
        if hb_ts is None or (now - hb_ts) > 10 * 60:
            return jsonify({"ok": False, "error": "Heartbeat stale or missing – cannot trade"}), 403

        if not bool(a.get("tempo_ready")):
            return jsonify({"ok": False, "error": "Tempo token WAIT — need a new 6pt bar"}), 403

        entry_override = bool(
            payload.get("entry_override") or payload.get("override")
            or payload.get("override_entry") or payload.get("manual_entry_override")
        )
        direction_in = payload.get("direction") or payload.get("side") or payload.get("direction_override")
        chosen_direction = parse_direction(direction_in)

        a["auto_exit_renko"] = "6pt"

        high = (a.get("high_renko_color") or "neutral").lower()
        macro = (a.get("macro_renko_color") or "neutral").lower()
        zone_type = compute_zone(a)
        a["zone_type"] = zone_type
        a["conflict_mode"] = (zone_type == "FREE")
        conflict_mode = (zone_type == "FREE")

        if entry_override:
            if chosen_direction is None:
                return jsonify({"ok": False, "error": "Override entry requires direction=BUY/SELL"}), 403
            direction = chosen_direction
        elif order_type == "market":
            if high not in ("green", "red") or macro not in ("green", "red"):
                return jsonify({"ok": False, "error": "8pt/12pt structure unavailable – cannot trade"}), 403
            if not conflict_mode:
                direction = "BUY" if high == "green" else "SELL"
            else:
                if chosen_direction is None:
                    return jsonify({"ok": False, "error": "Free zone: choose direction=BUY/SELL"}), 403
                direction = chosen_direction
        elif order_type == "preorder":
            if chosen_direction is None:
                return jsonify({"ok": False, "error": "Pre-order requires direction=BUY/SELL"}), 403

        if order_type == "market":
            allowed, reason = entry_direction_allowed(a, direction)
            if not allowed:
                return jsonify({
                    "ok": False, "error": reason or "Entry blocked by structure/2.5pt filter",
                    "zone_type": compute_zone(a),
                    "main_renko_color": a.get("main_renko_color"),
                    "high_renko_color": a.get("high_renko_color"),
                    "macro_renko_color": a.get("macro_renko_color"),
                    "two_half_renko_color": a.get("two_half_renko_color"),
                }), 409

        entry_size_mode = payload.get("entry_size_mode", "6pt")
        if entry_size_mode == "9pt":
            size, rith_sym, exit_tf = 3, "MESM6", "high"
        else:
            size, rith_sym, exit_tf = 5, "MESM6", "high"

        if order_type == "preorder":
            if a.get("preorder_active"):
                return jsonify({"ok": False, "error": "Pre-order already active"}), 403

            a["preorder_active"] = True
            a["preorder_direction"] = chosen_direction
            a["preorder_qty"] = size
            a["preorder_entry_size_mode"] = entry_size_mode
            a["preorder_trade_mode"] = trade_mode
            a["preorder_created_ts"] = time.time()
            a["preorder_bar_base_ts"] = a.get("main_renko_ts")
            a["preorder_status"] = "PENDING"
            a["intent_active"] = False
            a["intent_created_ts"] = None
            a["intent_bar_base_ts"] = None
            a["intent_ready_bar_ts"] = None
            a["intent_status"] = None
            try:
                save_asset_state(sym, a)
            except Exception as e:
                print(f"Error saving state (create preorder) for {sym}:", e)
            return jsonify({"ok": True, "preorder": True, "state": state}), 201

        env = (g.get("env") or "DEMO").upper()
        rith_exch = resolve_rithmic_exchange(sym)

        try:
            order = create_order(
                symbol=rith_sym, exchange=rith_exch, side=direction,
                qty=size, source="UI", mode=mode, kind="ENTRY", env=env,
            )
        except Exception as e:
            print("Error creating Firestore order:", e)
            return jsonify({"ok": False, "error": "Failed to create order"}), 500

        a["pending_order_id"] = order.get("id")
        a["pending_side"] = direction
        a["pending_qty"] = size
        a["pending_mode"] = mode
        a["pending_trade_mode"] = trade_mode
        a["exit_tf"] = exit_tf
        a["rithmic_symbol"] = rith_sym
        a["main_flip_exit_enabled"] = False
        activate_five_min_lock()

        a["tempo_ready"] = False
        a["tempo_spent_ts"] = time.time()
        a["tempo_last_bar_ts"] = a.get("tempo_ts")
        a["tp_count"] = 0
        a["tp_armed"] = False
        a["tp_target"] = 3
        a["intent_active"] = False
        a["intent_created_ts"] = None
        a["intent_bar_base_ts"] = None
        a["intent_ready_bar_ts"] = None
        a["intent_status"] = None
        a["points_tp_enabled"] = False
        a["points_tp_target"] = 10.0
        a["rithmic_open_points"] = 0.0
        a["points_tp_hit_ts"] = None
        a["protect_enabled"] = False
        a["protect_threshold_points"] = -2.0
        a["protect_hit_ts"] = None
        a["two_half_tp_lock_enabled"] = False
        a["two_half_tp_lock_base_color"] = None
        a["two_half_tp_lock_released"] = False
        a["two_half_tp_lock_started_ts"] = None
        a["two_half_tp_lock_released_ts"] = None

        ORDER_INDEX[order["id"]] = {
            "symbol": sym, "side": direction, "qty": size,
            "mode": mode, "trade_mode": trade_mode, "env": env, "kind": "ENTRY",
        }

        try:
            save_asset_state(sym, a)
        except Exception as e:
            print(f"Error saving state (create_order UI) for {sym}:", e)

        return jsonify({"ok": True, "order": order, "state": state}), 201

    # --- Raw generic branch (direct symbol/side/qty) ---
    symbol = payload.get("symbol")
    side = payload.get("side")
    qty = payload.get("qty", 1)
    mode = "A"
    trade_mode = (payload.get("trade_mode") or "").upper().strip()
    source = payload.get("source") or "UI"

    if not symbol or side not in ("BUY", "SELL"):
        return jsonify({"error": "symbol and side are required"}), 400

    kind = str(payload.get("kind") or "ENTRY").upper().strip()
    if kind == "ENTRY" and bool(state["global"].get("daily_stop_triggered")):
        return jsonify({"ok": False, "error": "Daily stop TRIGGERED — entries blocked for today"}), 403

    env = (payload.get("env") or state["global"].get("env", "DEMO"))
    exchange = payload.get("exchange") or "CME"

    order = create_order(
        symbol=symbol, exchange=exchange, side=side, qty=qty,
        source=source, mode=mode, kind=kind, env=env,
    )

    ui_sym = CONTRACT_TO_UI.get(str(symbol).upper())
    if ui_sym and ui_sym in state["assets"]:
        a = state["assets"][ui_sym]
        if not a.get("pending_order_id"):
            a["pending_order_id"] = order.get("id")
            a["pending_side"] = side
            a["pending_qty"] = qty
            a["pending_mode"] = mode
            a["pending_trade_mode"] = trade_mode if trade_mode in ("SCALP", "RUNNER") else None

        ORDER_INDEX[order["id"]] = {
            "symbol": ui_sym, "side": side, "qty": qty, "mode": mode,
            "trade_mode": trade_mode if trade_mode in ("SCALP", "RUNNER") else None,
            "env": env, "kind": kind,
        }
        try:
            save_asset_state(ui_sym, a)
        except Exception as e:
            print(f"Error saving state (create_order RAW) for {ui_sym}:", e)

    return jsonify(order), 201


@bp.route("/api/orders/pending", methods=["GET"])
def api_pending_orders():
    secret = request.args.get("secret")
    if secret != BRIDGE_HEARTBEAT_SECRET:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    max_items = int(request.args.get("limit", 20))
    owner = request.args.get("owner") or "bridge"
    raw_orders = get_pending_orders(limit=max_items, owner=owner)

    converted = []
    for o in raw_orders:
        converted.append({
            "id": o["id"],
            "asset": o.get("symbol"),
            "exchange": o.get("exchange") or "CME",
            "side": o.get("side"),
            "qty": o.get("qty"),
            "mode": o.get("mode"),
            "env": (o.get("env") or state["global"].get("env", "DEMO")),
            "kind": (o.get("kind") or "ENTRY"),
            "source": o.get("source"),
        })

    return jsonify({"ok": True, "orders": converted})


@bp.route("/api/orders/<order_id>/execution-report", methods=["POST"])
def api_execution_report(order_id):
    payload = request.get_json(force=True) or {}
    status = payload.get("status")
    extra = payload.get("extra", {}) or {}

    if status not in ("WORKING", "FILLED", "REJECTED", "CANCELLED"):
        return jsonify({"error": "invalid status"}), 400

    result = handle_execution_report(order_id, status, extra)
    return jsonify(result)


@bp.route("/api/scale-in", methods=["POST"])
@login_required
def api_scale_in():
    payload = request.get_json(force=True, silent=True) or {}
    asset_key = (payload.get("asset") or "ES").upper().strip()

    if asset_key not in state["assets"]:
        return jsonify({"ok": False, "error": "Unknown asset"}), 400

    a = state["assets"][asset_key]
    pos = int(a.get("position", 0) or 0)

    if pos == 0:
        return jsonify({"ok": False, "error": "No open position"}), 409
    if a.get("pending_order_id"):
        return jsonify({"ok": False, "error": "Pending order already exists"}), 409
    if not bool(a.get("scale_in_available", False)):
        return jsonify({"ok": False, "error": "Scale-in not available"}), 409

    side = "BUY" if pos > 0 else "SELL"
    qty = SCALE_IN_QTY
    env = (a.get("env") or state["global"].get("env") or "DEMO").upper().strip()
    rith_sym = a.get("rithmic_symbol") or resolve_rithmic_symbol(asset_key)
    rith_exch = resolve_rithmic_exchange(asset_key)
    mode = "A"

    try:
        order = create_order(
            symbol=rith_sym, exchange=rith_exch, side=side, qty=qty,
            source="SCALE_IN", mode=mode, kind="ENTRY", env=env,
        )
    except Exception as e:
        print("Scale-in create_order failed:", e)
        return jsonify({"ok": False, "error": "Failed to create scale-in order"}), 500

    a["pending_order_id"] = order.get("id")
    a["pending_side"] = side
    a["pending_qty"] = qty
    a["pending_mode"] = mode
    a["pending_trade_mode"] = None
    a["scale_in_available"] = False
    a["scale_in_used"] = True
    a["scale_in_stage"] = None
    a["scale_in_last_ts"] = time.time()

    ORDER_INDEX[order["id"]] = {
        "symbol": asset_key, "side": side, "qty": qty,
        "mode": mode, "env": env, "kind": "ENTRY", "source": "SCALE_IN",
    }

    try:
        save_asset_state(asset_key, a)
    except Exception as e:
        print(f"Error saving state (scale-in) for {asset_key}:", e)

    return jsonify({"ok": True, "order": order, "state": scrub_nonfinite(state)}), 201
