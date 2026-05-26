"""
Execution engine — order lifecycle management.

Handles order creation, ORDER_INDEX bookkeeping, execution report
processing (WORKING / FILLED / REJECTED / CANCELLED), fill side-effects,
and trade closure.
"""

import time

from config import ASSET_CONFIG, POINT_VALUE_USD
from services.state_manager import state, ORDER_INDEX
from stores.orders_store import create_order, update_order_status, get_order
from stores.state_store import save_asset_state
from utils.type_helpers import safe_float, safe_int
from services.behavioral_engine import (
    activate_post_exit_lock,
    activate_five_min_lock,
    release_five_min_lock_if_same_bucket,
    compute_zone,
    has_new_main_bar_after_entry,
    point_value_for_contract,
    _trade_aligned_two_half_color,
    _trade_opposite_two_half_color,
    consume_trade_limit_on_fill,
)
from config import resolve_rithmic_symbol, resolve_rithmic_exchange


# ----------------------------------------------------------------
#  Trade closure
# ----------------------------------------------------------------

def close_trade(asset_key: str, reason: str = ""):
    """
    Finalise a closed position: write trade history, reset asset state,
    activate the post-exit cooldown lock.
    """
    if asset_key not in state["assets"]:
        return
    a = state["assets"][asset_key]
    pos = a.get("position", 0)
    if pos == 0:
        return

    side = "LONG" if pos > 0 else "SHORT"
    size = abs(pos)
    entry_price = a.get("avg_price") or a.get("entry_price") or a.get("last_price")
    exit_price = a.get("last_price") or entry_price
    if entry_price is None:
        entry_price = exit_price or 0.0
    if exit_price is None:
        exit_price = entry_price

    direction = 1 if pos > 0 else -1
    pnl_points = (exit_price - entry_price) * direction
    point_value = POINT_VALUE_USD.get(asset_key, 1.0)
    pnl_usd = pnl_points * point_value * size
    now_ts = time.time()

    state["history"].insert(0, {
        "asset": asset_key,
        "side": side,
        "size": size,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "entry_ts": a.get("last_entry_ts"),
        "exit_ts": now_ts,
        "pnl_points": pnl_points,
        "pnl_usd": pnl_usd,
        "mode": a.get("exit_mode"),
        "reason": reason,
        "env": a.get("env") or state["global"].get("env", "DEMO"),
        "stop_loss_price": a.get("stop_loss_price"),
        "stop_loss_status": a.get("stop_loss_status"),
    })

    # Same-direction re-entry lock removed — allow immediate re-entry.
    a["last_exit_direction"] = None
    a["reentry_lock_active"] = False

    # Snapshot whether the closing trade had at least one fresh 6pt bar after entry.
    a["last_trade_had_new_main_bar_after_entry"] = bool(
        a.get("last_trade_had_new_main_bar_after_entry", False)
        or has_new_main_bar_after_entry(a)
    )

    # Reset position and trade-level state
    a["position"] = 0
    a["avg_price"] = None
    a["entry_price"] = None
    a["pnl"] = 0.0
    a["last_entry_ts"] = None
    a["exit_mode"] = None
    a["last_exit_ts"] = now_ts
    a["tempo_4pt_unlock_ts"] = None
    a["opposite_locked"] = False
    a["color_changed"] = False
    a["stop_loss_price"] = None
    a["stop_loss_status"] = None
    a["env"] = None
    a["tp_count"] = 0
    a["tp_armed"] = False
    a["high_next_bar_exit_enabled"] = False
    a["high_next_bar_exit_started_ts"] = None
    a["high_next_bar_exit_base_ts"] = None
    a["main_flip_exit_enabled"] = False
    a["initial_exit_lock_active"] = False
    a["initial_exit_lock_released"] = False
    a["initial_exit_lock_started_ts"] = None
    a["initial_exit_lock_released_ts"] = None
    a["initial_exit_lock_base_main_ts"] = None
    a["trade_mode"] = None
    a["entry_zone"] = None
    a["runner_4pt_unlocked"] = False
    a["entry_main_renko_ts"] = None
    a["entry_high_renko_ts"] = None
    a["entry_renko_color"] = None
    a["entry_main_renko_color"] = None
    a["four_pt_invalidation_enabled"] = False
    a["next_bar_exit_allowed"] = False
    a["high_next_bar_exit_allowed"] = False
    a["four_pt_invalidation_allowed"] = False
    a["zone_type"] = None
    a["pending_trade_mode"] = None
    a["preorder_trade_mode"] = None
    a["points_tp_enabled"] = False
    a["points_tp_target"] = 15.0
    a["rithmic_open_points"] = 0.0
    a["rithmic_point_value"] = None
    a["points_tp_hit_ts"] = None
    a["protect_enabled"] = False
    a["protect_threshold_points"] = -2.0
    a["protect_hit_ts"] = None
    a["two_half_tp_lock_enabled"] = False
    a["two_half_tp_lock_base_color"] = None
    a["two_half_tp_lock_released"] = False
    a["two_half_tp_lock_started_ts"] = None
    a["two_half_tp_lock_released_ts"] = None
    a["scale_in_available"] = False
    a["scale_in_used"] = False
    a["scale_in_stage"] = None
    a["scale_in_last_ts"] = None

    if not a.get("tp_target"):
        a["tp_target"] = 3

    activate_post_exit_lock()

    try:
        save_asset_state(asset_key, a)
    except Exception as e:
        print(f"Error saving state (close_trade) for {asset_key}:", e)


# ----------------------------------------------------------------
#  Execution report handler
# ----------------------------------------------------------------

def handle_execution_report(order_id: str, status: str, extra: dict):
    """
    Process a VPS bridge execution report.

    status: WORKING | FILLED | REJECTED | CANCELLED
    extra: fill payload from Rithmic (fill_price, qty, etc.)
    """
    update_order_status(order_id, status, extra=extra)

    info = ORDER_INDEX.get(order_id, {}) or {}
    doc_order = {}
    if not info:
        try:
            doc_order = get_order(order_id) or {}
        except Exception as e:
            print("Error reading order doc:", e)

    def _pick(*keys_and_sources):
        for src, key in keys_and_sources:
            v = src.get(key)
            if v:
                return v
        return None

    symbol = (info.get("symbol") or extra.get("symbol") or extra.get("asset")
              or extra.get("sym") or doc_order.get("symbol"))
    side   = (info.get("side") or extra.get("side") or extra.get("direction") or doc_order.get("side"))
    qty    = (info.get("qty") or extra.get("qty") or extra.get("size") or doc_order.get("qty") or 1)
    mode   = (info.get("mode") or extra.get("mode") or doc_order.get("mode"))
    trade_mode = (info.get("trade_mode") or extra.get("trade_mode") or doc_order.get("trade_mode"))
    env    = (info.get("env") or extra.get("env") or doc_order.get("env") or state["global"].get("env", "DEMO"))
    kind   = str(info.get("kind") or extra.get("kind") or doc_order.get("kind") or "ENTRY").upper().strip()
    source = str(info.get("source") or extra.get("source") or doc_order.get("source") or "").upper().strip()

    if trade_mode is not None:
        trade_mode = str(trade_mode).upper().strip()
    if symbol:
        from config import CONTRACT_TO_UI
        symbol = str(symbol).upper().strip()
        if symbol not in state["assets"] and symbol in CONTRACT_TO_UI:
            symbol = CONTRACT_TO_UI[symbol]
    if side:
        side = str(side).upper().strip()
    try:
        qty = int(qty)
    except Exception:
        qty = 1
    if mode:
        mode = str(mode).upper().strip()
    if env:
        env = str(env).upper().strip()

    if not symbol or symbol not in state["assets"]:
        ORDER_INDEX.pop(order_id, None)
        return {"ok": True}

    asset_state = state["assets"][symbol]

    def clear_pending():
        if asset_state.get("pending_order_id") == order_id:
            asset_state["pending_order_id"] = None
            asset_state["pending_side"] = None
            asset_state["pending_qty"] = 0
            asset_state["pending_mode"] = None
            asset_state["pending_trade_mode"] = None

    # ---- WORKING ----
    if status == "WORKING":
        if order_id not in ORDER_INDEX:
            ORDER_INDEX[order_id] = {
                "symbol": symbol, "side": side, "qty": qty,
                "mode": mode, "trade_mode": trade_mode,
                "env": env, "kind": kind, "source": source,
            }
        return {"ok": True}

    # ---- REJECTED / CANCELLED ----
    if status in ("REJECTED", "CANCELLED"):
        clear_pending()
        if kind == "ENTRY" and source == "SCALE_IN":
            asset_state["scale_in_available"] = True
            asset_state["scale_in_used"] = False
            asset_state["scale_in_stage"] = "READY_6PT"
            asset_state["scale_in_last_ts"] = time.time()
        elif kind == "ENTRY":
            asset_state["tempo_spent_ts"] = None
            release_five_min_lock_if_same_bucket()

        ORDER_INDEX.pop(order_id, None)
        try:
            save_asset_state(symbol, asset_state)
        except Exception as e:
            print(f"Error saving state (REJECT/CANCEL) for {symbol}:", e)
        return {"ok": True}

    # ---- FILLED ----
    if status == "FILLED":
        current_pos = asset_state.get("position", 0)
        order_pos = qty if side == "BUY" else -qty if side == "SELL" else None
        if order_pos is None:
            clear_pending()
            ORDER_INDEX.pop(order_id, None)
            return {"ok": True}

        fill_price = _extract_fill_price(extra, asset_state)
        now_ts = time.time()

        # ---- EXIT fill when local position is already flat ----
        if kind == "EXIT" and current_pos == 0:
            _finalize_flat_local_exit(
                asset_state, symbol, order_id, qty, fill_price, mode,
                extra, clear_pending, now_ts
            )
            return {"ok": True}

        is_exit = (
            kind == "EXIT"
            and current_pos != 0
            and ((current_pos > 0 and order_pos < 0) or (current_pos < 0 and order_pos > 0))
        )

        # ---- EXIT fill against open position ----
        if is_exit:
            asset_state["last_price"] = fill_price
            if mode:
                asset_state["exit_mode"] = mode
            asset_state["last_trade_had_new_main_bar_after_entry"] = bool(
                has_new_main_bar_after_entry(asset_state)
            )
            reason = extra.get("reason") or "exit_order_filled"
            clear_pending()
            close_trade(symbol, reason=reason)
            ORDER_INDEX.pop(order_id, None)
            return {"ok": True}

        # ---- SCALE-IN fill ----
        if kind == "ENTRY" and source == "SCALE_IN":
            return _handle_scale_in_fill(
                asset_state, symbol, order_id, current_pos, order_pos,
                fill_price, clear_pending
            )

        if kind != "ENTRY":
            clear_pending()
            ORDER_INDEX.pop(order_id, None)
            try:
                save_asset_state(symbol, asset_state)
            except Exception as e:
                print(f"Error saving state (non-entry fill) for {symbol}:", e)
            return {"ok": True}

        # ---- Regular ENTRY fill ----
        _handle_entry_fill(
            asset_state, symbol, order_id, order_pos, fill_price, mode,
            env, trade_mode, info, extra, now_ts, clear_pending
        )
        return {"ok": True}

    return {"ok": True}


# ----------------------------------------------------------------
#  Internal fill handlers
# ----------------------------------------------------------------

def _extract_fill_price(extra: dict, asset_state: dict) -> float:
    raw = (extra.get("fill_price") or extra.get("avg_price")
           or extra.get("price") or asset_state.get("last_price") or 0.0)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _finalize_flat_local_exit(asset_state, symbol, order_id, qty, fill_price, mode,
                               extra, clear_pending, now_ts):
    if fill_price:
        asset_state["last_price"] = fill_price
    if mode:
        asset_state["exit_mode"] = mode

    if asset_state.get("last_entry_ts"):
        entry_price = asset_state.get("avg_price") or asset_state.get("entry_price") or asset_state.get("last_price")
        pending_side = (asset_state.get("pending_side") or "").upper().strip()
        asset_state["last_exit_direction"] = None
        asset_state["reentry_lock_active"] = False
        state["history"].insert(0, {
            "asset": symbol,
            "side": ("LONG" if pending_side == "SELL" else "SHORT" if pending_side == "BUY" else "UNKNOWN"),
            "size": abs(qty),
            "entry_price": entry_price,
            "exit_price": asset_state.get("last_price"),
            "entry_ts": asset_state.get("last_entry_ts"),
            "exit_ts": now_ts,
            "pnl_points": 0.0,
            "pnl_usd": 0.0,
            "mode": asset_state.get("exit_mode"),
            "reason": extra.get("reason") or "exit_order_filled_flat_local",
            "env": asset_state.get("env") or state["global"].get("env", "DEMO"),
            "stop_loss_price": asset_state.get("stop_loss_price"),
            "stop_loss_status": asset_state.get("stop_loss_status"),
        })

    _reset_asset_on_exit(asset_state, now_ts)
    clear_pending()
    activate_post_exit_lock()
    ORDER_INDEX.pop(order_id, None)
    try:
        save_asset_state(symbol, asset_state)
    except Exception as e:
        print(f"Error saving state (flat-local exit) for {symbol}:", e)


def _reset_asset_on_exit(a: dict, now_ts: float):
    a["position"] = 0
    a["avg_price"] = None
    a["entry_price"] = None
    a["pnl"] = 0.0
    a["last_entry_ts"] = None
    a["last_exit_ts"] = now_ts
    a["tempo_4pt_unlock_ts"] = None
    a["stop_loss_price"] = None
    a["stop_loss_status"] = None
    a["tp_count"] = 0
    a["tp_armed"] = False
    a["main_flip_exit_enabled"] = False
    a["high_next_bar_exit_enabled"] = False
    a["high_next_bar_exit_started_ts"] = None
    a["high_next_bar_exit_base_ts"] = None
    a["last_exit_direction"] = None
    a["reentry_lock_active"] = False
    a["exit_mode"] = None
    a["env"] = None
    a["trade_mode"] = None
    a["entry_zone"] = None
    a["runner_4pt_unlocked"] = False
    a["entry_main_renko_ts"] = None
    a["entry_high_renko_ts"] = None
    a["entry_renko_color"] = None
    a["entry_main_renko_color"] = None
    a["four_pt_invalidation_enabled"] = False
    a["next_bar_exit_allowed"] = False
    a["high_next_bar_exit_allowed"] = False
    a["four_pt_invalidation_allowed"] = False
    a["zone_type"] = None
    a["pending_trade_mode"] = None
    a["preorder_trade_mode"] = None
    a["points_tp_enabled"] = False
    a["points_tp_target"] = 15.0
    a["rithmic_open_points"] = 0.0
    a["rithmic_point_value"] = None
    a["points_tp_hit_ts"] = None
    a["protect_enabled"] = False
    a["protect_threshold_points"] = -2.0
    a["protect_hit_ts"] = None
    a["two_half_tp_lock_enabled"] = False
    a["two_half_tp_lock_base_color"] = None
    a["two_half_tp_lock_released"] = False
    a["two_half_tp_lock_started_ts"] = None
    a["two_half_tp_lock_released_ts"] = None
    a["scale_in_available"] = False
    a["scale_in_used"] = False
    a["scale_in_stage"] = None
    a["scale_in_last_ts"] = None
    if not a.get("tp_target"):
        a["tp_target"] = 3


def _handle_scale_in_fill(asset_state, symbol, order_id, current_pos, order_pos, fill_price, clear_pending):
    current_pos = int(current_pos or 0)
    same_direction = (current_pos > 0 and order_pos > 0) or (current_pos < 0 and order_pos < 0)
    if current_pos == 0 or not same_direction:
        clear_pending()
        ORDER_INDEX.pop(order_id, None)
        try:
            save_asset_state(symbol, asset_state)
        except Exception as e:
            print(f"Error saving state (bad SCALE_IN cleanup) for {symbol}:", e)
        return {"ok": False, "error": "Scale-in fill direction mismatch or no open position"}

    old_qty = abs(current_pos)
    add_qty = abs(order_pos)
    new_pos = current_pos + order_pos
    old_avg = safe_float(asset_state.get("avg_price"), default=fill_price, nonfinite_to=fill_price)
    new_avg = ((old_avg * old_qty) + (fill_price * add_qty)) / max(1, old_qty + add_qty)

    asset_state["position"] = new_pos
    asset_state["avg_price"] = new_avg
    asset_state["entry_price"] = new_avg
    asset_state["pnl"] = 0.0
    asset_state["order_count"] = (asset_state.get("order_count") or 0) + 1
    asset_state["scale_in_available"] = False
    asset_state["scale_in_used"] = True
    asset_state["scale_in_stage"] = None
    asset_state["scale_in_last_ts"] = time.time()

    clear_pending()
    ORDER_INDEX.pop(order_id, None)
    try:
        save_asset_state(symbol, asset_state)
    except Exception as e:
        print(f"Error saving state (SCALE_IN fill) for {symbol}:", e)
    return {"ok": True}


def _handle_entry_fill(asset_state, symbol, order_id, order_pos, fill_price,
                        mode, env, trade_mode, info, extra, now_ts, clear_pending):
    pending_trade_mode = (
        info.get("trade_mode") or extra.get("trade_mode")
        or asset_state.get("pending_trade_mode")
    )
    pending_trade_mode = str(pending_trade_mode or "").upper().strip()

    asset_state["just_filled_ts"] = time.time()
    asset_state["position"] = order_pos
    asset_state["avg_price"] = fill_price
    asset_state["entry_price"] = fill_price
    asset_state["last_entry_ts"] = now_ts
    asset_state["tempo_4pt_unlock_ts"] = None
    asset_state["pnl"] = 0.0
    asset_state["order_count"] = (asset_state.get("order_count") or 0) + 1
    asset_state["exit_mode"] = mode
    asset_state["env"] = env
    asset_state["five_min_ok"] = True
    asset_state["stop_loss_price"] = None
    asset_state["stop_loss_status"] = None
    asset_state["trade_mode"] = pending_trade_mode if pending_trade_mode in ("SCALP", "RUNNER") else None

    asset_state["entry_zone"] = compute_zone(asset_state)
    asset_state["runner_4pt_unlocked"] = False
    asset_state["entry_main_renko_ts"] = asset_state.get("main_renko_ts")
    asset_state["entry_high_renko_ts"] = asset_state.get("high_renko_ts")
    asset_state["entry_main_renko_color"] = asset_state.get("main_renko_color")
    asset_state["entry_renko_color"] = asset_state.get("renko_color")

    # Initial exit lock — blocks emotional exits until first fresh 6pt bar
    asset_state["initial_exit_lock_active"] = True
    asset_state["initial_exit_lock_released"] = False
    asset_state["initial_exit_lock_started_ts"] = now_ts
    asset_state["initial_exit_lock_released_ts"] = None
    asset_state["initial_exit_lock_base_main_ts"] = asset_state.get("entry_main_renko_ts")
    asset_state["last_trade_had_new_main_bar_after_entry"] = False
    asset_state["scale_in_available"] = False
    asset_state["scale_in_used"] = False
    asset_state["scale_in_stage"] = None
    asset_state["scale_in_last_ts"] = None
    asset_state["high_next_bar_exit_enabled"] = False
    asset_state["high_next_bar_exit_started_ts"] = None
    asset_state["high_next_bar_exit_base_ts"] = None
    asset_state["four_pt_invalidation_enabled"] = False
    asset_state["next_bar_exit_allowed"] = True
    asset_state["four_pt_invalidation_allowed"] = True
    asset_state["high_next_bar_exit_allowed"] = True
    asset_state["zone_type"] = compute_zone(asset_state)

    # Auto-enable 6pt flip exit only in FREE zone
    entry_zone = asset_state.get("entry_zone") or asset_state.get("zone_type")
    asset_state["main_flip_exit_enabled"] = (entry_zone == "FREE")

    # 2.5pt management lock initial state
    current_two_half = (asset_state.get("two_half_renko_color") or "neutral").lower()
    opposite_two_half = _trade_opposite_two_half_color(order_pos)

    if current_two_half == opposite_two_half:
        asset_state["two_half_tp_lock_enabled"] = False
        asset_state["two_half_tp_lock_base_color"] = None
        asset_state["two_half_tp_lock_released"] = True
        asset_state["two_half_tp_lock_started_ts"] = None
        asset_state["two_half_tp_lock_released_ts"] = now_ts
    else:
        asset_state["two_half_tp_lock_enabled"] = True
        asset_state["two_half_tp_lock_base_color"] = current_two_half if current_two_half in ("green", "red") else None
        asset_state["two_half_tp_lock_released"] = False
        asset_state["two_half_tp_lock_started_ts"] = now_ts
        asset_state["two_half_tp_lock_released_ts"] = None

    # Default profit capture: 15pt TP auto-enabled at entry
    asset_state["points_tp_enabled"] = True
    asset_state["points_tp_target"] = 15.0
    asset_state["rithmic_open_points"] = 0.0
    asset_state["rithmic_point_value"] = point_value_for_contract(
        asset_state.get("rithmic_symbol") or resolve_rithmic_symbol(symbol), symbol
    )
    asset_state["points_tp_hit_ts"] = None
    asset_state["protect_enabled"] = False
    asset_state["protect_threshold_points"] = -2.0
    asset_state["protect_hit_ts"] = None

    consume_ok = consume_trade_limit_on_fill()
    if not consume_ok:
        print(f"WARNING: trade limit consume failed on ENTRY fill for {symbol}, order_id={order_id}")

    clear_pending()
    ORDER_INDEX.pop(order_id, None)
    try:
        save_asset_state(symbol, asset_state)
    except Exception as e:
        print(f"Error saving state (ENTRY fill) for {symbol}:", e)
