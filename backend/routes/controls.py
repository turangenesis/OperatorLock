"""
Control routes — operator UI control APIs.

All endpoints are login-protected and allow the operator to manage
open positions, toggle behavioral constraints, and adjust system settings.
"""

import time

from flask import Blueprint, jsonify, request

from config import resolve_rithmic_symbol, resolve_rithmic_exchange
from services.state_manager import state, ORDER_INDEX
from services.behavioral_engine import (
    ensure_daily_stop_day_initialized,
    ensure_five_min_lock_initialized,
    ensure_post_exit_lock_initialized,
    consume_trade_limit_on_fill,
    clear_intent,
    clear_preorder,
    six_next_bar_exit_allowed,
    eight_next_bar_exit_allowed,
)
from services.exit_engine import maybe_protect_exit, maybe_points_take_profit_exit
from stores.orders_store import create_order
from stores.state_store import save_asset_state, save_global_state
from utils.type_helpers import safe_int, scrub_nonfinite
from auth import login_required

bp = Blueprint("controls", __name__)


def _check_rithmic_connection():
    return True, "Rithmic connection OK (handled by C# bridge)."


@bp.route("/api/trade-limit/consume", methods=["POST"])
@login_required
def api_trade_limit_consume():
    g = state["global"]
    ensure_daily_stop_day_initialized()
    ok = consume_trade_limit_on_fill()
    if not ok and safe_int(g.get("trades_remaining_today"), default=0) <= 0:
        return jsonify({"ok": False, "error": "Daily trade limit already reached"}), 409
    if not ok:
        return jsonify({"ok": False, "error": "Failed to persist trade limit"}), 500

    return jsonify({
        "ok": True,
        "max_trades_per_day": g.get("max_trades_per_day"),
        "trades_taken_today": g.get("trades_taken_today"),
        "trades_remaining_today": g.get("trades_remaining_today"),
        "daily_trade_limit_date": g.get("daily_trade_limit_date"),
    })


@bp.route("/api/exit-all", methods=["POST"])
@login_required
def api_manual_exit_all():
    payload = request.get_json(force=True, silent=True) or {}
    force = bool(payload.get("force", True))

    created_orders = []
    skipped = []

    for asset_key, a in state["assets"].items():
        pos = int(a.get("position", 0) or 0)

        if pos == 0:
            skipped.append({"asset": asset_key, "reason": "no_open_position"})
            continue

        if a.get("pending_order_id"):
            skipped.append({"asset": asset_key, "reason": "pending_order_exists"})
            continue

        if bool(a.get("initial_exit_lock_active", False)) and not force:
            skipped.append({"asset": asset_key, "reason": "initial_exit_lock_active"})
            continue

        side = "SELL" if pos > 0 else "BUY"
        qty = abs(pos)
        env = (a.get("env") or state["global"].get("env") or "DEMO").upper().strip()
        rith_sym = a.get("rithmic_symbol") or resolve_rithmic_symbol(asset_key)
        rith_exch = resolve_rithmic_exchange(asset_key)

        try:
            order = create_order(
                symbol=rith_sym, exchange=rith_exch, side=side, qty=qty,
                source=("MANUAL_EXIT_ALL_FORCE" if force else "MANUAL_EXIT_ALL"),
                mode=a.get("exit_mode") or "A", kind="EXIT", env=env,
            )
        except Exception as e:
            print(f"Manual exit-all create_order failed for {asset_key}:", e)
            skipped.append({"asset": asset_key, "reason": "create_order_failed"})
            continue

        a["pending_order_id"] = order.get("id")
        a["pending_side"] = side
        a["pending_qty"] = qty
        a["pending_mode"] = a.get("exit_mode") or "A"
        a["pending_trade_mode"] = None

        ORDER_INDEX[order["id"]] = {
            "symbol": asset_key, "side": side, "qty": qty,
            "mode": a.get("exit_mode") or "A", "env": env, "kind": "EXIT",
        }

        try:
            save_asset_state(asset_key, a)
        except Exception as e:
            print(f"Error saving state (manual exit all) for {asset_key}:", e)

        created_orders.append({
            "asset": asset_key, "order_id": order.get("id"), "side": side, "qty": qty,
        })

    if not created_orders:
        return jsonify({
            "ok": False, "error": "No open positions available to exit", "skipped": skipped,
        }), 409

    return jsonify({"ok": True, "orders": created_orders, "skipped": skipped}), 201


@bp.route("/api/env", methods=["POST"])
@login_required
def set_env():
    data = request.get_json() or {}
    env = (data.get("env") or "").upper()

    if env not in ("DEMO", "LIVE"):
        return jsonify({"ok": False, "error": "Invalid env value"}), 400

    if env == "DEMO":
        state["global"]["env"] = "DEMO"
        message = "Demo mode enabled. All trades are simulated."
    else:
        ok, msg = _check_rithmic_connection()
        if not ok:
            return jsonify({"ok": False, "error": msg or "Rithmic connection failed – cannot enable LIVE."}), 503
        state["global"]["env"] = "LIVE"
        message = msg or "Live mode enabled (execution via C# Rithmic bridge)."

    try:
        save_global_state(state["global"])
    except Exception as e:
        print("Error saving global state:", e)

    return jsonify({
        "ok": True,
        "env": state["global"]["env"],
        "rithmic_connected": state["global"].get("rithmic_connected"),
        "message": message,
    })


@bp.route("/api/two-half-tp-lock", methods=["POST"])
@login_required
def api_two_half_tp_lock():
    return jsonify({
        "ok": False,
        "error": "Manual 2.5 lock is disabled. 2.5 lock is automatic only."
    }), 423


@bp.route("/api/protect", methods=["POST"])
@login_required
def api_set_protect():
    payload = request.get_json(force=True, silent=True) or {}
    asset_key = (payload.get("asset") or "").upper().strip()

    if asset_key not in state["assets"]:
        return jsonify({"ok": False, "error": "Unknown asset"}), 400

    a = state["assets"][asset_key]
    pos = int(a.get("position", 0) or 0)

    if pos == 0:
        return jsonify({"ok": False, "error": "No open position"}), 409
    if a.get("pending_order_id"):
        return jsonify({"ok": False, "error": "Pending order already exists"}), 409

    enabled = payload.get("enabled", None)
    if enabled is None:
        enabled = not bool(a.get("protect_enabled", False))
    a["protect_enabled"] = bool(enabled)

    threshold = payload.get("threshold", payload.get("protect_threshold_points", None))
    if threshold is not None:
        try:
            threshold = float(threshold)
        except Exception:
            return jsonify({"ok": False, "error": "Invalid protect threshold"}), 400

        allowed_thresholds = (-2.0, 0.5)
        if threshold not in allowed_thresholds:
            return jsonify({
                "ok": False,
                "error": "Protect threshold must be either -2.0 or 0.5 points"
            }), 400
        a["protect_threshold_points"] = threshold
    else:
        if a.get("protect_threshold_points") is None:
            a["protect_threshold_points"] = -2.0

    try:
        save_asset_state(asset_key, a)
    except Exception as e:
        print(f"Error saving state (protect toggle) for {asset_key}:", e)
        return jsonify({"ok": False, "error": "Failed to save protect state"}), 500

    maybe_protect_exit(asset_key)

    return jsonify({
        "ok": True,
        "asset": asset_key,
        "protect_enabled": bool(a.get("protect_enabled", False)),
        "protect_threshold_points": a.get("protect_threshold_points"),
        "rithmic_open_points": a.get("rithmic_open_points"),
        "pending_order_id": a.get("pending_order_id"),
        "state": scrub_nonfinite(state),
    })


@bp.route("/api/points-take-profit", methods=["POST"])
@login_required
def api_set_points_take_profit():
    payload = request.get_json(force=True) or {}
    asset_key = (payload.get("asset") or "").upper().strip()

    if asset_key not in state["assets"]:
        return jsonify({"ok": False, "error": "Unknown asset"}), 400

    a = state["assets"][asset_key]
    pos = int(a.get("position", 0) or 0)

    if pos == 0:
        return jsonify({"ok": False, "error": "No open position"}), 409

    enabled = payload.get("enabled", a.get("points_tp_enabled", False))
    value = payload.get("target", payload.get("points_tp_target", None))

    if value is not None and bool(a.get("two_half_tp_lock_enabled", False)):
        return jsonify({
            "ok": False,
            "error": "TP target changes locked until 2.5pt management unlocks"
        }), 423

    a["points_tp_enabled"] = bool(enabled)
    if value is not None:
        try:
            value = float(value)
        except Exception:
            return jsonify({"ok": False, "error": "Invalid target"}), 400
        if value <= 0 or value > 200:
            return jsonify({"ok": False, "error": "Target must be between 0 and 200 points"}), 400
        a["points_tp_target"] = value

    try:
        save_asset_state(asset_key, a)
    except Exception as e:
        print(f"Error saving state (points TP set) for {asset_key}:", e)
        return jsonify({"ok": False, "error": "Failed to save points TP"}), 500

    maybe_points_take_profit_exit(asset_key)

    return jsonify({
        "ok": True,
        "asset": asset_key,
        "points_tp_enabled": bool(a.get("points_tp_enabled", False)),
        "points_tp_target": a.get("points_tp_target"),
        "rithmic_open_points": a.get("rithmic_open_points"),
        "pending_order_id": a.get("pending_order_id"),
        "state": scrub_nonfinite(state),
    })


@bp.route("/api/high-next-bar-exit", methods=["POST"])
@login_required
def api_high_next_bar_exit():
    payload = request.get_json(force=True, silent=True) or {}
    asset_key = (payload.get("asset") or "").upper().strip()

    if asset_key not in state["assets"]:
        return jsonify({"ok": False, "error": "Unknown asset"}), 400

    a = state["assets"][asset_key]
    pos = int(a.get("position", 0) or 0)

    if pos == 0:
        return jsonify({"ok": False, "error": "No open position"}), 409
    if a.get("pending_order_id"):
        return jsonify({"ok": False, "error": "Pending order already exists"}), 409

    new_val = not bool(a.get("high_next_bar_exit_enabled", False))

    if new_val and not eight_next_bar_exit_allowed(a):
        return jsonify({
            "ok": False,
            "error": "8pt next-bar exit locked until 2.5pt management unlocks"
        }), 423

    a["high_next_bar_exit_enabled"] = new_val
    if new_val:
        a["high_next_bar_exit_started_ts"] = time.time()
        a["high_next_bar_exit_base_ts"] = a.get("high_renko_ts")
    else:
        a["high_next_bar_exit_started_ts"] = None
        a["high_next_bar_exit_base_ts"] = None

    try:
        save_asset_state(asset_key, a)
    except Exception as e:
        print(f"Error saving state (8pt next-bar exit toggle) for {asset_key}:", e)
        return jsonify({"ok": False, "error": "Failed to save 8pt next-bar exit"}), 500

    return jsonify({
        "ok": True,
        "asset": asset_key,
        "high_next_bar_exit_enabled": bool(a.get("high_next_bar_exit_enabled", False)),
        "high_next_bar_exit_base_ts": a.get("high_next_bar_exit_base_ts"),
        "next_bar_exit_allowed": six_next_bar_exit_allowed(a),
        "high_next_bar_exit_allowed": eight_next_bar_exit_allowed(a),
        "state": scrub_nonfinite(state),
    })


@bp.route("/api/take-profit", methods=["POST"])
@login_required
def api_set_take_profit():
    payload = request.get_json(force=True) or {}
    asset_key = (payload.get("asset") or "").upper().strip()

    if asset_key not in state["assets"]:
        return jsonify({"ok": False, "error": "Unknown asset"}), 400

    a = state["assets"][asset_key]
    pos = int(a.get("position", 0) or 0)
    if pos == 0:
        return jsonify({"ok": False, "error": "No open position"}), 409

    if "tp_armed" not in a or a.get("tp_armed") is None:
        a["tp_armed"] = False
    if "tp_target" not in a or a.get("tp_target") is None:
        a["tp_target"] = 3
    if "tp_count" not in a or a.get("tp_count") is None:
        a["tp_count"] = 0

    if payload.get("tp_arm") == "toggle":
        new_val = not bool(a.get("tp_armed", False))

        if new_val and not six_next_bar_exit_allowed(a):
            return jsonify({
                "ok": False,
                "error": "6pt next-bar exit locked until 2.5pt management unlocks"
            }), 423

        a["tp_armed"] = new_val
        if new_val:
            a["tp_target"] = 1
            a["tp_count"] = 0
        else:
            a["tp_count"] = 0

        try:
            save_asset_state(asset_key, a)
        except Exception as e:
            print(f"Error saving state (tp toggle) for {asset_key}:", e)

        return jsonify({
            "ok": True,
            "asset": asset_key,
            "tp_armed": a["tp_armed"],
            "tp_target": a["tp_target"],
            "tp_count": a["tp_count"],
        })

    value = payload.get("tp_target", payload.get("value", None))
    if value is None:
        return jsonify({"ok": False, "error": "No tp_arm or tp_target provided"}), 400

    try:
        value = int(value)
    except Exception:
        return jsonify({"ok": False, "error": "Invalid value"}), 400

    if value < 1 or value > 50:
        return jsonify({"ok": False, "error": "TP target must be between 1 and 50"}), 400

    a["tp_target"] = value
    if bool(payload.get("reset")):
        a["tp_count"] = 0

    try:
        save_asset_state(asset_key, a)
    except Exception as e:
        print(f"Error saving state (take-profit set) for {asset_key}:", e)

    return jsonify({
        "ok": True,
        "asset": asset_key,
        "tp_armed": bool(a.get("tp_armed", False)),
        "tp_target": a["tp_target"],
        "tp_count": a.get("tp_count", 0),
    })


@bp.route("/api/auto-exit", methods=["POST"])
@login_required
def api_set_auto_exit():
    payload = request.get_json(force=True) or {}
    asset_key = (payload.get("asset") or "").upper().strip()

    if asset_key not in state["assets"]:
        return jsonify({"ok": False, "error": "Unknown asset"}), 400

    a = state["assets"][asset_key]
    if int(a.get("position", 0) or 0) == 0:
        return jsonify({"ok": False, "error": "No open position"}), 409

    a["auto_exit_renko"] = "6pt"

    try:
        save_asset_state(asset_key, a)
    except Exception as e:
        print(f"Error saving state (auto-exit) for {asset_key}:", e)

    return jsonify({"ok": True, "asset": asset_key, "auto_exit_renko": "6pt"})


@bp.route("/api/intent", methods=["POST"])
@login_required
def api_create_intent():
    payload = request.get_json(force=True) or {}
    asset_key = (payload.get("asset") or "").upper().strip()

    if asset_key not in state["assets"]:
        return jsonify({"ok": False, "error": "Unknown asset"}), 400

    a = state["assets"][asset_key]
    g = state["global"]

    if bool(g.get("daily_stop_triggered")):
        return jsonify({"ok": False, "error": "Daily stop TRIGGERED — entries blocked for today"}), 403

    ensure_five_min_lock_initialized()
    ensure_post_exit_lock_initialized()

    if bool(g.get("post_exit_lock_active")):
        remaining = safe_int(g.get("post_exit_lock_remaining_s"), default=0)
        mm, ss = remaining // 60, remaining % 60
        return jsonify({
            "ok": False,
            "error": f"POST-EXIT 5 MIN LOCK ACTIVE ({mm:02d}:{ss:02d} remaining)"
        }), 403

    if g.get("trade_lock"):
        return jsonify({"ok": False, "error": "Global trade lock active"}), 403

    if int(a.get("position", 0) or 0) != 0:
        return jsonify({"ok": False, "error": "Asset already in a trade"}), 403

    if a.get("pending_order_id"):
        return jsonify({"ok": False, "error": "Pending order already exists"}), 403

    if bool(a.get("preorder_active")):
        return jsonify({"ok": False, "error": "Pre-order already active"}), 403

    if bool(a.get("intent_active")):
        return jsonify({"ok": False, "error": "Intent already active"}), 403

    now = time.time()
    hb_ts = a.get("last_heartbeat_ts")
    if hb_ts is None or (now - hb_ts) > 10 * 60:
        return jsonify({"ok": False, "error": "Heartbeat stale or missing – cannot set intent"}), 403

    a["intent_active"] = True
    a["intent_created_ts"] = now
    a["intent_bar_base_ts"] = a.get("main_renko_ts")
    a["intent_ready_bar_ts"] = None
    a["intent_status"] = "PENDING"

    try:
        save_asset_state(asset_key, a)
    except Exception as e:
        print(f"Error saving state (create intent) for {asset_key}:", e)
        return jsonify({"ok": False, "error": "Failed to save intent"}), 500

    return jsonify({"ok": True}), 201


@bp.route("/api/intent/cancel", methods=["POST"])
@login_required
def api_cancel_intent():
    payload = request.get_json(force=True) or {}
    asset_key = (payload.get("asset") or "").upper().strip()

    if asset_key not in state["assets"]:
        return jsonify({"ok": False, "error": "Unknown asset"}), 400

    a = state["assets"][asset_key]
    if not bool(a.get("intent_active")):
        return jsonify({"ok": False, "error": "No active intent"}), 409

    clear_intent(asset_key, status="CANCELLED_BY_USER")
    return jsonify({"ok": True})


@bp.route("/api/preorder/cancel", methods=["POST"])
@login_required
def api_cancel_preorder():
    payload = request.get_json(force=True) or {}
    asset_key = (payload.get("asset") or "").upper().strip()

    if asset_key not in state["assets"]:
        return jsonify({"ok": False, "error": "Unknown asset"}), 400

    a = state["assets"][asset_key]
    if not bool(a.get("preorder_active")):
        return jsonify({"ok": False, "error": "No active pre-order"}), 409

    clear_preorder(asset_key, status="CANCELLED_BY_USER")
    return jsonify({"ok": True})


@bp.route("/api/main-flip-exit", methods=["POST"])
@login_required
def toggle_main_flip_exit():
    data = request.get_json() or {}
    asset_key = (data.get("asset") or "").upper().strip()

    if asset_key not in state["assets"]:
        return jsonify({"ok": False, "error": f"Unknown asset {asset_key}"}), 400

    a = state["assets"][asset_key]

    if int(a.get("position", 0) or 0) == 0:
        return jsonify({"ok": False, "error": "No open position"}), 409
    if a.get("pending_order_id"):
        return jsonify({"ok": False, "error": "Cannot change exit toggle while order is pending"}), 409

    a["main_flip_exit_enabled"] = not bool(a.get("main_flip_exit_enabled", False))

    try:
        save_asset_state(asset_key, a)
    except Exception as e:
        print(f"Error saving state (main flip exit toggle) for {asset_key}:", e)
        return jsonify({"ok": False, "error": "Failed to persist toggle"}), 500

    return jsonify({
        "ok": True,
        "asset": asset_key,
        "main_flip_exit_enabled": a["main_flip_exit_enabled"],
        "state": scrub_nonfinite(state),
    })


@bp.route("/api/four-pt-invalidation", methods=["POST"])
@login_required
def api_toggle_four_pt_invalidation():
    payload = request.get_json(force=True) or {}
    asset_key = (payload.get("asset") or "").upper().strip()

    if asset_key not in state["assets"]:
        return jsonify({"ok": False, "error": "Unknown asset"}), 400

    a = state["assets"][asset_key]
    if int(a.get("position", 0) or 0) == 0:
        return jsonify({"ok": False, "error": "No open position"}), 409

    a["four_pt_invalidation_enabled"] = not bool(a.get("four_pt_invalidation_enabled", False))

    try:
        save_asset_state(asset_key, a)
    except Exception as e:
        print(f"Error saving state (4pt invalidation toggle) for {asset_key}:", e)
        return jsonify({"ok": False, "error": "Failed to persist state"}), 500

    return jsonify({
        "ok": True,
        "asset": asset_key,
        "four_pt_invalidation_enabled": bool(a.get("four_pt_invalidation_enabled", False)),
    })


@bp.route("/api/exit", methods=["POST"])
@login_required
def api_manual_exit():
    payload = request.get_json(force=True) or {}
    asset_key = (payload.get("asset") or "").upper().strip()

    if asset_key not in state["assets"]:
        return jsonify({"ok": False, "error": "Unknown asset"}), 400

    a = state["assets"][asset_key]
    pos = int(a.get("position", 0) or 0)

    if pos == 0:
        return jsonify({"ok": False, "error": "No open position"}), 409
    if a.get("pending_order_id"):
        return jsonify({"ok": False, "error": "Pending order already exists"}), 409

    force = bool(payload.get("force") or payload.get("override") or payload.get("manual_override"))

    if bool(a.get("initial_exit_lock_active", False)) and not force:
        return jsonify({
            "ok": False,
            "error": "INITIAL_EXIT_LOCK_ACTIVE — wait for fresh 6pt bar after entry"
        }), 409

    side = "SELL" if pos > 0 else "BUY"
    qty = abs(pos)
    env = (a.get("env") or state["global"].get("env") or "DEMO").upper().strip()
    rith_sym = a.get("rithmic_symbol") or resolve_rithmic_symbol(asset_key)
    rith_exch = resolve_rithmic_exchange(asset_key)

    try:
        order = create_order(
            symbol=rith_sym, exchange=rith_exch, side=side, qty=qty,
            source=("MANUAL_EXIT_FORCE" if force else "MANUAL_EXIT"),
            mode=a.get("exit_mode") or "A", kind="EXIT", env=env,
        )
    except Exception as e:
        print("Manual exit create_order failed:", e)
        return jsonify({"ok": False, "error": "Failed to create exit order"}), 500

    a["pending_order_id"] = order.get("id")
    a["pending_side"] = side
    a["pending_qty"] = qty
    a["pending_mode"] = a.get("exit_mode") or "A"
    a["pending_trade_mode"] = None

    ORDER_INDEX[order["id"]] = {
        "symbol": asset_key, "side": side, "qty": qty,
        "mode": a.get("exit_mode") or "A", "env": env, "kind": "EXIT",
    }

    try:
        save_asset_state(asset_key, a)
    except Exception as e:
        print(f"Error saving state (manual exit) for {asset_key}:", e)

    return jsonify({"ok": True, "order": order}), 201
