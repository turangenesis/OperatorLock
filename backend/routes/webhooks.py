"""
TradingView webhook routes.

All Renko stream endpoints + heartbeat + level exit.
These fire on every bar close from TradingView alerts.
"""

import time

from flask import Blueprint, jsonify, request

from config import TV_WEBHOOK_SECRET, resolve_rithmic_symbol, resolve_rithmic_exchange
from services.state_manager import state, ORDER_INDEX, append_log
from services.behavioral_engine import (
    compute_zone,
    activate_five_min_lock,
    update_scale_in_from_4pt,
    update_scale_in_from_6pt,
    release_initial_exit_lock_if_needed,
    release_two_half_tp_lock_if_needed,
    clear_intent, clear_preorder,
    point_value_for_contract,
    has_new_main_bar_after_entry,
)
from services.execution_engine import close_trade
from services.exit_engine import maybe_take_profit_exit
from stores.orders_store import create_order
from stores.state_store import save_asset_state
from utils.type_helpers import safe_int

bp = Blueprint("webhooks", __name__)


def _parse_asset(payload: dict) -> str:
    raw = (payload.get("asset") or payload.get("symbol") or "").upper()
    return raw.split(":")[-1]


def _validate_tv_secret(payload: dict):
    return payload.get("secret") == TV_WEBHOOK_SECRET


def _parse_color(payload: dict) -> str:
    c = (payload.get("color") or "neutral").lower()
    return c if c in ("green", "red", "neutral") else "neutral"


# ----------------------------------------------------------------
#  Heartbeat — price + connectivity signal
# ----------------------------------------------------------------

@bp.route("/tv_heartbeat", methods=["POST"])
def tv_heartbeat():
    data = request.get_json(force=True, silent=True) or {}
    if not _validate_tv_secret(data):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    sym = (data.get("symbol") or "").upper()
    if sym not in state["assets"]:
        return jsonify({"ok": False, "error": f"Unknown symbol {sym}"}), 400

    asset_state = state["assets"][sym]
    now = time.time()
    hb_ts = data.get("timestamp")
    try:
        hb_ts = float(hb_ts) if hb_ts is not None else now
    except (TypeError, ValueError):
        hb_ts = now

    asset_state["last_heartbeat_ts"] = hb_ts
    price = data.get("price")
    if price is not None:
        try:
            asset_state["last_price"] = float(price)
        except (TypeError, ValueError):
            pass

    append_log("tradingview", {"ts": hb_ts, "type": "heartbeat", "asset": sym, "payload": data})

    try:
        if asset_state.get("position", 0) != 0 or asset_state.get("pending_order_id"):
            save_asset_state(sym, asset_state)
    except Exception as e:
        print(f"Error saving state (heartbeat) for {sym}:", e)

    return jsonify({"ok": True})


# ----------------------------------------------------------------
#  4pt Renko — behavioral invalidation, scale-in, tempo re-arm
# ----------------------------------------------------------------

@bp.route("/tv_renko", methods=["POST"])
def tv_renko():
    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception:
        payload = {}

    if not _validate_tv_secret(payload):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    asset_key = _parse_asset(payload)
    if asset_key not in state["assets"]:
        return jsonify({"ok": False, "error": f"Unknown asset '{asset_key}'"}), 400

    color = _parse_color(payload)
    a = state["assets"][asset_key]
    now = time.time()

    prev_color = (a.get("renko_color") or "neutral").lower()
    new_color = color
    a["renko_color"] = new_color
    a["last_renko_ts"] = now

    color_changed = (
        prev_color in ("green", "red") and new_color in ("green", "red") and prev_color != new_color
    )
    a["color_changed"] = color_changed

    update_scale_in_from_4pt(asset_key, prev_color, new_color, color_changed)

    # 4pt color change post-exit can re-arm tempo if trade had a fresh 6pt bar
    if (
        color_changed
        and int(a.get("position", 0) or 0) == 0
        and bool(a.get("last_trade_had_new_main_bar_after_entry", False))
    ):
        last_exit_ts = a.get("last_exit_ts")
        if last_exit_ts is not None:
            try:
                if now > float(last_exit_ts):
                    a["tempo_4pt_unlock_ts"] = now
            except Exception:
                a["tempo_4pt_unlock_ts"] = now

    # RUNNER unlock: opposite 4pt flip after first fresh 6pt bar
    if int(a.get("position", 0) or 0) != 0 and has_new_main_bar_after_entry(a):
        pos = int(a.get("position", 0) or 0)
        opposite_4pt_flip = (
            (pos > 0 and color_changed and new_color == "red") or
            (pos < 0 and color_changed and new_color == "green")
        )
        if opposite_4pt_flip:
            a["runner_4pt_unlocked"] = True

    # 4pt invalidation exit
    pos = int(a.get("position", 0) or 0)
    exit_mode = a.get("exit_mode")
    four_pt_enabled = bool(a.get("four_pt_invalidation_enabled"))

    if pos != 0 and exit_mode == "A" and four_pt_enabled:
        opposite_signal = (pos > 0 and new_color == "red") or (pos < 0 and new_color == "green")
        if opposite_signal and not a.get("pending_order_id"):
            side = "SELL" if pos > 0 else "BUY"
            qty = abs(pos)
            env = (state["global"].get("env") or "DEMO").upper()
            rith_sym = a.get("rithmic_symbol") or resolve_rithmic_symbol(asset_key)
            rith_exch = resolve_rithmic_exchange(asset_key)
            try:
                order = create_order(
                    symbol=rith_sym, exchange=rith_exch, side=side, qty=qty,
                    source="EXIT_ENGINE_4PT", mode=exit_mode, kind="EXIT", env=env,
                )
            except Exception as e:
                print("Error creating 4pt exit order:", e)
                close_trade(asset_key, reason="renko_4pt_flip_fallback")
            else:
                a["pending_order_id"] = order.get("id")
                a["pending_side"] = side
                a["pending_qty"] = qty
                a["pending_mode"] = exit_mode
                a["pending_trade_mode"] = None
                ORDER_INDEX[order["id"]] = {
                    "symbol": asset_key, "side": side, "qty": qty,
                    "mode": exit_mode, "env": env, "kind": "EXIT",
                }

    try:
        if a.get("position", 0) != 0 or a.get("pending_order_id") or a.get("tempo_4pt_unlock_ts") is not None:
            save_asset_state(asset_key, a)
    except Exception as e:
        print(f"Error saving state (tv_renko) for {asset_key}:", e)

    append_log("tradingview", {"ts": now, "type": "renko", "asset": asset_key, "payload": payload})
    return jsonify({"ok": True})


# ----------------------------------------------------------------
#  12pt Macro — zone computation update
# ----------------------------------------------------------------

@bp.route("/tv_renko_macro", methods=["POST"])
def tv_renko_macro():
    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception:
        payload = {}

    if not _validate_tv_secret(payload):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    asset_key = _parse_asset(payload)
    if asset_key not in state["assets"]:
        return jsonify({"ok": False, "error": f"Unknown asset '{asset_key}'"}), 400

    color = _parse_color(payload)
    now = time.time()
    a = state["assets"][asset_key]
    a["macro_renko_color"] = color
    a["macro_renko_ts"] = now
    a["zone_type"] = compute_zone(a)
    a["conflict_mode"] = (a["zone_type"] == "FREE")

    try:
        save_asset_state(asset_key, a)
    except Exception as e:
        print("Error saving state (tv_renko_macro):", e)

    append_log("tradingview", {"ts": now, "type": "renko_macro", "asset": asset_key, "payload": payload})
    return jsonify({"ok": True, "asset": asset_key, "macro_renko_color": color, "zone_type": a.get("zone_type")})


# ----------------------------------------------------------------
#  2.5pt — entry filter + TP/protect management lock
# ----------------------------------------------------------------

@bp.route("/tv_renko_two_half", methods=["POST"])
def tv_renko_two_half():
    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception:
        payload = {}

    if not _validate_tv_secret(payload):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    asset_key = _parse_asset(payload)
    if asset_key not in state["assets"]:
        return jsonify({"ok": False, "error": f"Unknown asset '{asset_key}'"}), 400

    color = _parse_color(payload)
    now = time.time()
    a = state["assets"][asset_key]

    prev_color = (a.get("two_half_renko_color") or "neutral").lower()
    new_color = color
    color_changed = (
        prev_color in ("green", "red") and new_color in ("green", "red") and prev_color != new_color
    )
    a["two_half_renko_color"] = new_color
    a["two_half_renko_ts"] = now
    a["two_half_color_changed"] = color_changed

    release_two_half_tp_lock_if_needed(asset_key, prev_color, new_color, color_changed)

    try:
        save_asset_state(asset_key, a)
    except Exception as e:
        print("Error saving state (tv_renko_two_half):", e)

    append_log("tradingview", {"ts": now, "type": "renko_two_half", "asset": asset_key, "payload": payload})
    return jsonify({
        "ok": True, "asset": asset_key,
        "two_half_renko_color": a.get("two_half_renko_color"),
        "two_half_tp_lock_enabled": a.get("two_half_tp_lock_enabled"),
        "two_half_tp_lock_released": a.get("two_half_tp_lock_released"),
    })


# ----------------------------------------------------------------
#  6pt Main — tempo, pre-order trigger, TP count, auto-exit
# ----------------------------------------------------------------

@bp.route("/tv_renko_main", methods=["POST"])
def tv_renko_main():
    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception:
        payload = {}

    if not _validate_tv_secret(payload):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    asset_key = _parse_asset(payload)
    if asset_key not in state["assets"]:
        return jsonify({"ok": False, "error": f"Unknown asset '{asset_key}'"}), 400

    color = _parse_color(payload)
    now = time.time()
    a = state["assets"][asset_key]

    prev_color = (a.get("main_renko_color") or "neutral").lower()
    new_color = color
    a["main_renko_color"] = new_color
    a["main_renko_ts"] = now

    update_scale_in_from_6pt(asset_key, new_color, now)
    release_initial_exit_lock_if_needed(asset_key)

    # Intent lifecycle
    if bool(a.get("intent_active")):
        base_ts = a.get("intent_bar_base_ts")
        ready_bar_ts = a.get("intent_ready_bar_ts")
        is_next_bar = (base_ts is not None and now != base_ts)
        if is_next_bar:
            if ready_bar_ts is None:
                a["intent_ready_bar_ts"] = now
                a["intent_status"] = "READY"
            elif ready_bar_ts != now:
                clear_intent(asset_key, status="EXPIRED")

    # Pre-order: fire on next 6pt bar that matches direction
    if bool(a.get("preorder_active")):
        base_ts = a.get("preorder_bar_base_ts")
        preorder_dir = a.get("preorder_direction")
        preorder_qty = safe_int(a.get("preorder_qty"), default=0)
        preorder_mode = "A"
        preorder_trade_mode = (a.get("preorder_trade_mode") or "").upper().strip()
        preorder_entry_size_mode = a.get("preorder_entry_size_mode") or "6pt"
        is_next_bar = (base_ts is not None and now != base_ts)

        if is_next_bar:
            should_fill = (
                (preorder_dir == "BUY" and new_color == "green") or
                (preorder_dir == "SELL" and new_color == "red")
            )
            if should_fill:
                if (
                    not a.get("pending_order_id")
                    and int(a.get("position", 0) or 0) == 0
                    and not bool(state["global"].get("trade_lock"))
                ):
                    env = (state["global"].get("env") or "DEMO").upper()
                    rith_sym = "MESM6"
                    exit_tf = "high"
                    rith_exch = resolve_rithmic_exchange(asset_key)
                    try:
                        order = create_order(
                            symbol=rith_sym, exchange=rith_exch, side=preorder_dir,
                            qty=preorder_qty, source="PREORDER_TRIGGER",
                            mode=preorder_mode, kind="ENTRY", env=env,
                        )
                    except Exception as e:
                        print("Error creating preorder order:", e)
                        clear_preorder(asset_key, status="CANCELLED")
                    else:
                        a["pending_order_id"] = order.get("id")
                        a["pending_side"] = preorder_dir
                        a["pending_qty"] = preorder_qty
                        a["pending_mode"] = preorder_mode
                        a["pending_trade_mode"] = preorder_trade_mode
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
                        a["points_tp_enabled"] = False
                        a["points_tp_target"] = 10.0
                        a["rithmic_open_points"] = 0.0
                        a["rithmic_point_value"] = point_value_for_contract(rith_sym, asset_key)
                        a["points_tp_hit_ts"] = None
                        a["protect_enabled"] = False
                        a["protect_threshold_points"] = -2.0
                        a["protect_hit_ts"] = None
                        a["scale_in_available"] = False
                        a["scale_in_used"] = False
                        a["scale_in_stage"] = None
                        a["scale_in_last_ts"] = None
                        clear_preorder(asset_key, status="FILLED")
                        ORDER_INDEX[order["id"]] = {
                            "symbol": asset_key, "side": preorder_dir, "qty": preorder_qty,
                            "mode": preorder_mode, "trade_mode": preorder_trade_mode, "env": env, "kind": "ENTRY",
                        }
                else:
                    clear_preorder(asset_key, status="CANCELLED")
            else:
                clear_preorder(asset_key, status="CANCELLED")

    # TP signal count increment
    if int(a.get("position", 0) or 0) != 0 and bool(a.get("tp_armed", False)):
        a["tp_count"] = safe_int(a.get("tp_count"), default=0) + 1
        try:
            save_asset_state(asset_key, a)
        except Exception as e:
            print(f"Error saving state (tp_count) for {asset_key}:", e)
        maybe_take_profit_exit(asset_key)

    # 6pt flip auto-exit
    pos = a.get("position", 0)
    exit_mode = a.get("exit_mode")
    exit_tf = a.get("exit_tf", "main")
    main_flip_exit_enabled = bool(a.get("main_flip_exit_enabled", False))

    if pos != 0 and exit_mode == "A" and (exit_tf == "main" or main_flip_exit_enabled):
        opposite_signal = (pos > 0 and new_color == "red") or (pos < 0 and new_color == "green")
        if opposite_signal and not a.get("pending_order_id"):
            side = "SELL" if pos > 0 else "BUY"
            qty = abs(pos)
            env = (state["global"].get("env") or "DEMO").upper()
            rith_sym = a.get("rithmic_symbol") or resolve_rithmic_symbol(asset_key)
            rith_exch = resolve_rithmic_exchange(asset_key)
            try:
                order = create_order(
                    symbol=rith_sym, exchange=rith_exch, side=side, qty=qty,
                    source="EXIT_ENGINE", mode=exit_mode, kind="EXIT", env=env,
                )
            except Exception as e:
                print("Error creating 6pt exit order:", e)
                close_trade(asset_key, reason="renko_main_flip_mode_A_fallback")
            else:
                a["pending_order_id"] = order.get("id")
                a["pending_side"] = side
                a["pending_qty"] = qty
                a["pending_mode"] = exit_mode
                a["pending_trade_mode"] = None
                ORDER_INDEX[order["id"]] = {
                    "symbol": asset_key, "side": side, "qty": qty,
                    "mode": exit_mode, "env": env, "kind": "EXIT",
                }

    try:
        save_asset_state(asset_key, a)
    except Exception as e:
        print("Error saving state (tv_renko_main):", e)

    append_log("tradingview", {"ts": now, "type": "renko_main", "asset": asset_key, "payload": payload})
    return jsonify({"ok": True, "asset": asset_key, "main_renko_color": color, "main_renko_ts": now})


# ----------------------------------------------------------------
#  8pt High — zone updates, next-bar exit, high-TF flip exit
# ----------------------------------------------------------------

@bp.route("/tv_renko_high", methods=["POST"])
def tv_renko_high():
    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception:
        payload = {}

    if not _validate_tv_secret(payload):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    asset_key = _parse_asset(payload)
    if asset_key not in state["assets"]:
        return jsonify({"ok": False, "error": f"Unknown asset '{asset_key}'"}), 400

    color = _parse_color(payload)
    now = time.time()
    a = state["assets"][asset_key]
    a["high_renko_color"] = color
    a["high_renko_ts"] = now

    # 8pt next-bar exit
    if int(a.get("position", 0) or 0) != 0 and bool(a.get("high_next_bar_exit_enabled", False)):
        base_ts = a.get("high_next_bar_exit_base_ts")
        is_next_high_bar = (base_ts is not None and now != base_ts)
        if is_next_high_bar and not a.get("pending_order_id"):
            pos = int(a.get("position", 0) or 0)
            side = "SELL" if pos > 0 else "BUY"
            qty = abs(pos)
            env = (a.get("env") or state["global"].get("env") or "DEMO").upper().strip()
            rith_sym = a.get("rithmic_symbol") or resolve_rithmic_symbol(asset_key)
            rith_exch = resolve_rithmic_exchange(asset_key)
            try:
                order = create_order(
                    symbol=rith_sym, exchange=rith_exch, side=side, qty=qty,
                    source="HIGH_NEXT_BAR_EXIT", mode=a.get("exit_mode") or "A", kind="EXIT", env=env,
                )
            except Exception as e:
                print("Error creating 8pt next-bar exit order:", e)
                close_trade(asset_key, reason="high_next_bar_exit_fallback")
            else:
                a["pending_order_id"] = order.get("id")
                a["pending_side"] = side
                a["pending_qty"] = qty
                a["pending_mode"] = a.get("exit_mode") or "A"
                a["pending_trade_mode"] = None
                a["high_next_bar_exit_enabled"] = False
                a["high_next_bar_exit_started_ts"] = None
                a["high_next_bar_exit_base_ts"] = None
                ORDER_INDEX[order["id"]] = {
                    "symbol": asset_key, "side": side, "qty": qty,
                    "mode": a.get("exit_mode") or "A", "env": env, "kind": "EXIT", "source": "HIGH_NEXT_BAR_EXIT",
                }

    # 8pt flip auto-exit
    pos = a.get("position", 0)
    exit_mode = a.get("exit_mode")
    exit_tf = a.get("exit_tf", "main")

    if pos != 0 and exit_mode == "A" and exit_tf == "high":
        opposite_signal = (pos > 0 and color == "red") or (pos < 0 and color == "green")
        if opposite_signal and not a.get("pending_order_id"):
            side = "SELL" if pos > 0 else "BUY"
            qty = abs(pos)
            env = (state["global"].get("env") or "DEMO").upper()
            rith_sym = a.get("rithmic_symbol") or resolve_rithmic_symbol(asset_key)
            rith_exch = resolve_rithmic_exchange(asset_key)
            try:
                order = create_order(
                    symbol=rith_sym, exchange=rith_exch, side=side, qty=qty,
                    source="EXIT_ENGINE_HIGH", mode=exit_mode, kind="EXIT", env=env,
                )
            except Exception as e:
                print("Error creating 8pt exit order:", e)
                close_trade(asset_key, reason="renko_high_flip_fallback")
            else:
                a["pending_order_id"] = order.get("id")
                a["pending_side"] = side
                a["pending_qty"] = qty
                a["pending_mode"] = exit_mode
                a["pending_trade_mode"] = None
                ORDER_INDEX[order["id"]] = {
                    "symbol": asset_key, "side": side, "qty": qty,
                    "mode": exit_mode, "env": env, "kind": "EXIT",
                }

    try:
        save_asset_state(asset_key, a)
    except Exception as e:
        print("Error saving state (tv_renko_high):", e)

    append_log("tradingview", {"ts": now, "type": "renko_high", "asset": asset_key, "payload": payload})
    return jsonify({"ok": True, "asset": asset_key, "high_renko_color": color, "high_renko_ts": now})


# ----------------------------------------------------------------
#  1pt visual-only streams
# ----------------------------------------------------------------

@bp.route("/tv_renko_small", methods=["POST"])
def tv_renko_small():
    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception:
        payload = {}

    if not _validate_tv_secret(payload):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    asset_key = _parse_asset(payload)
    if asset_key not in state["assets"]:
        return jsonify({"ok": True})  # silently ignore unknown assets for visual streams

    color = _parse_color(payload)
    now = time.time()
    a = state["assets"][asset_key]
    a["small_renko_color"] = color
    a["last_small_renko_ts"] = now
    return jsonify({"ok": True})


@bp.route("/tv_renko_one", methods=["POST"])
def tv_renko_one():
    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception:
        payload = {}

    if not _validate_tv_secret(payload):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    asset_key = _parse_asset(payload)
    if asset_key not in state["assets"]:
        return jsonify({"ok": True})

    color = _parse_color(payload)
    now = time.time()
    a = state["assets"][asset_key]
    prev_color = (a.get("one_renko_color") or "neutral").lower()
    a["one_renko_color"] = color
    a["one_renko_ts"] = now
    a["one_renko_color_changed"] = (
        prev_color in ("green", "red") and color in ("green", "red") and prev_color != color
    )
    return jsonify({"ok": True})


# ----------------------------------------------------------------
#  Horizontal level exit trigger
# ----------------------------------------------------------------

@bp.route("/tv_level_exit", methods=["POST"])
def tv_level_exit():
    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception:
        payload = {}

    if not _validate_tv_secret(payload):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    asset_key = _parse_asset(payload)
    if asset_key not in state["assets"]:
        return jsonify({"ok": False, "error": f"Unknown asset '{asset_key}'"}), 400

    a = state["assets"][asset_key]
    pos = int(a.get("position", 0) or 0)

    if pos == 0:
        return jsonify({"ok": True, "ignored": "no_position"})
    if a.get("pending_order_id"):
        return jsonify({"ok": True, "ignored": "pending_exit_exists"})

    side = "SELL" if pos > 0 else "BUY"
    qty = abs(pos)
    env = (a.get("env") or state["global"].get("env") or "DEMO").upper().strip()
    rith_sym = a.get("rithmic_symbol") or resolve_rithmic_symbol(asset_key)
    rith_exch = resolve_rithmic_exchange(asset_key)

    try:
        order = create_order(
            symbol=rith_sym, exchange=rith_exch, side=side, qty=qty,
            source="TV_LEVEL_EXIT", mode=a.get("exit_mode") or "A", kind="EXIT", env=env,
        )
    except Exception as e:
        print("TV_LEVEL_EXIT create_order failed:", e)
        return jsonify({"ok": False})

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
        print("Error saving state (tv_level_exit):", e)

    append_log("tradingview", {"ts": time.time(), "type": "tv_level_exit", "asset": asset_key, "payload": payload})
    return jsonify({"ok": True, "action": "exit_order_created"})
