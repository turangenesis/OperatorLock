"""
State routes — read-only state API and frontend entry point.
"""

import time

from flask import Blueprint, jsonify, send_from_directory, current_app

from config import FIXED_ENTRY_QTY
from services.state_manager import state
from services.behavioral_engine import (
    ensure_daily_stop_day_initialized,
    ensure_five_min_lock_initialized,
    ensure_post_exit_lock_initialized,
    release_initial_exit_lock_if_needed,
    release_two_half_tp_lock_if_needed,
    compute_zone,
    six_next_bar_exit_allowed,
    eight_next_bar_exit_allowed,
)
from utils.calendar import LOW_LIQUIDITY_DAYS, get_today_info
from utils.type_helpers import safe_float, scrub_nonfinite
from auth import login_required

bp = Blueprint("state", __name__)

DISABLE_SERVER_SIDE_SAFETY_STOPS = True


@bp.route("/")
@login_required
def index():
    import os
    static_folder = os.path.join(os.path.dirname(current_app.root_path), "static")
    return send_from_directory(static_folder, "index.html")


@bp.route("/api/state")
@login_required
def get_state():
    ensure_daily_stop_day_initialized()
    ensure_five_min_lock_initialized()
    ensure_post_exit_lock_initialized()

    now = time.time()

    # Server-side safety stops are disabled — Rithmic is sole exit authority.
    # Block kept as dead code to preserve the intent for future readers.
    if not DISABLE_SERVER_SIDE_SAFETY_STOPS:  # pragma: no cover
        from services.execution_engine import close_trade
        for sym, a in state["assets"].items():
            if a.get("pending_order_id"):
                continue
            just_filled_ts = a.get("just_filled_ts")
            if just_filled_ts and (now - just_filled_ts) < 0.5:
                continue
            pos = a.get("position", 0)
            last_price = a.get("last_price")
            if pos == 0 or last_price is None:
                continue
            stop_price = a.get("stop_loss_price")
            if stop_price is not None:
                if pos > 0 and last_price <= stop_price:
                    close_trade(sym, reason="safety_stop_hit")
                elif pos < 0 and last_price >= stop_price:
                    close_trade(sym, reason="safety_stop_hit")

    # Global computed fields
    state["global"]["open_pnl"] = safe_float(
        state["global"].get("rithmic_unrealized"), default=0.0, nonfinite_to=0.0,
    )
    state["global"]["total_orders"] = sum(
        a["order_count"] for a in state["assets"].values()
    )

    # Connectivity
    tv_connected = any(
        a.get("last_heartbeat_ts") and (now - a["last_heartbeat_ts"]) <= 600
        for a in state["assets"].values()
    )
    r_ts = state["global"].get("rithmic_last_ts")
    rith_connected = bool(r_ts and (now - r_ts) <= 60)

    state["global"]["tradingview_connected"] = tv_connected
    state["global"]["rithmic_connected"] = rith_connected
    state["global"]["connected_count"] = int(tv_connected) + int(rith_connected)
    state["global"]["connected_expected"] = 2
    state["global"]["connected"] = (state["global"]["connected_count"] == 2)

    # Per-asset backfills and computed flags
    for sym, a in state["assets"].items():
        # TP defaults
        a.setdefault("tp_target", 3)
        a.setdefault("tp_count", 0)
        a.setdefault("tp_armed", False)

        # 8pt next-bar exit
        a.setdefault("high_next_bar_exit_enabled", False)
        a.setdefault("high_next_bar_exit_started_ts", None)
        a.setdefault("high_next_bar_exit_base_ts", None)
        a.setdefault("entry_high_renko_ts", None)
        a.setdefault("high_next_bar_exit_allowed", False)

        # Pre-order
        a.setdefault("preorder_active", False)
        a.setdefault("preorder_direction", None)
        a.setdefault("preorder_qty", 0)
        a.setdefault("preorder_entry_size_mode", None)
        a.setdefault("preorder_created_ts", None)
        a.setdefault("preorder_bar_base_ts", None)
        a.setdefault("preorder_status", None)

        # Intent
        a.setdefault("intent_active", False)
        a.setdefault("intent_created_ts", None)
        a.setdefault("intent_bar_base_ts", None)
        a.setdefault("intent_ready_bar_ts", None)
        a.setdefault("intent_status", None)

        # Trade mode
        a.setdefault("pending_trade_mode", None)
        a.setdefault("preorder_trade_mode", None)

        # Points TP
        a.setdefault("points_tp_enabled", False)
        if a.get("points_tp_target") is None:
            a["points_tp_target"] = 15.0
        a.setdefault("rithmic_open_points", 0.0)
        a.setdefault("rithmic_point_value", None)
        a.setdefault("points_tp_hit_ts", None)

        # Protect
        a.setdefault("protect_enabled", False)
        if a.get("protect_threshold_points") is None:
            a["protect_threshold_points"] = -2.0
        a.setdefault("protect_hit_ts", None)

        # 2.5pt stream
        a.setdefault("two_half_renko_color", "neutral")
        a.setdefault("two_half_renko_ts", None)
        a.setdefault("two_half_color_changed", False)

        # 2.5pt TP/protect lock
        a.setdefault("two_half_tp_lock_enabled", False)
        a.setdefault("two_half_tp_lock_base_color", None)
        a.setdefault("two_half_tp_lock_released", False)
        a.setdefault("two_half_tp_lock_started_ts", None)
        a.setdefault("two_half_tp_lock_released_ts", None)

        # Manual exit always allowed
        a["manual_exit_allowed"] = True

        # Force-clear stale same-direction re-entry lock (removed feature)
        a["reentry_lock_active"] = False
        a["last_exit_direction"] = None

        # Misc
        a.setdefault("trade_mode", None)
        a.setdefault("entry_zone", None)
        a.setdefault("runner_4pt_unlocked", False)
        a.setdefault("entry_main_renko_ts", None)
        a.setdefault("entry_renko_color", None)
        a.setdefault("entry_main_renko_color", None)
        a.setdefault("four_pt_invalidation_enabled", False)
        a.setdefault("next_bar_exit_allowed", False)
        a.setdefault("zone_type", None)
        a.setdefault("four_pt_invalidation_allowed", False)
        a.setdefault("scale_in_available", False)
        a.setdefault("scale_in_used", False)
        a.setdefault("scale_in_stage", None)
        a.setdefault("scale_in_last_ts", None)
        a.setdefault("last_exit_ts", None)

        # 1pt visual
        a.setdefault("one_renko_color", "neutral")
        a.setdefault("one_renko_ts", None)
        a.setdefault("one_renko_color_changed", False)
        a.setdefault("tempo_4pt_unlock_ts", None)

        # Initial exit lock
        a.setdefault("last_trade_had_new_main_bar_after_entry", False)
        a.setdefault("initial_exit_lock_active", False)
        a.setdefault("initial_exit_lock_released", False)
        a.setdefault("initial_exit_lock_started_ts", None)
        a.setdefault("initial_exit_lock_released_ts", None)
        a.setdefault("initial_exit_lock_base_main_ts", None)

        release_initial_exit_lock_if_needed(sym)

        a.setdefault("macro_renko_color", "neutral")

        # Zone
        zone_type = compute_zone(a)
        a["zone_type"] = zone_type
        a["conflict_mode"] = (zone_type == "FREE")

        pos = int(a.get("position", 0) or 0)

        if pos == 0:
            a["next_bar_exit_allowed"] = False
            a["high_next_bar_exit_allowed"] = False
            a["four_pt_invalidation_allowed"] = False
        else:
            # Self-heal 2.5pt lock (no-op if color unchanged)
            release_two_half_tp_lock_if_needed(
                sym,
                a.get("two_half_renko_color"),
                a.get("two_half_renko_color"),
                False,
            )
            a["next_bar_exit_allowed"] = six_next_bar_exit_allowed(a)
            a["high_next_bar_exit_allowed"] = eight_next_bar_exit_allowed(a)
            a["four_pt_invalidation_allowed"] = True

    # Tempo token (one bar = one trade)
    tempo_source = state["assets"].get("ES", {})
    tempo_color = (tempo_source.get("main_renko_color") or "neutral").lower()
    tempo_bar_ts = tempo_source.get("main_renko_ts")

    for sym, a in state["assets"].items():
        if sym not in ("ES",):
            continue

        a["tempo_color"] = tempo_color
        a["tempo_ts"] = tempo_bar_ts
        a["tempo_last_bar_ts"] = tempo_bar_ts

        if tempo_bar_ts is None:
            a["tempo_ready"] = False
            a["tempo_age_s"] = None
            continue

        a["tempo_age_s"] = now - float(tempo_bar_ts)
        spent_ts = a.get("tempo_spent_ts")

        if spent_ts is None:
            a["tempo_ready"] = True
        else:
            try:
                spent_f = float(spent_ts)
            except Exception:
                spent_f = None

            main_rearmed = False
            fourpt_rearmed = False

            if spent_f is not None:
                try:
                    main_rearmed = float(tempo_bar_ts) > spent_f
                except Exception:
                    main_rearmed = False

                fourpt_ts = a.get("tempo_4pt_unlock_ts")
                if fourpt_ts is not None:
                    try:
                        fourpt_rearmed = float(fourpt_ts) > spent_f
                    except Exception:
                        fourpt_rearmed = False

            a["tempo_ready"] = bool(main_rearmed or fourpt_rearmed)

    # Trading day info
    today = get_today_info()
    state["global"]["today_date"] = today["date"]
    state["global"]["low_liquidity_today"] = today["low_liquidity"]
    state["global"]["low_liquidity_reason"] = today["reason"]
    state["global"]["low_liquidity_days"] = LOW_LIQUIDITY_DAYS
    state["global"]["manual_pnl_usd"] = None

    for sym, a in state["assets"].items():
        a["computed_entry_qty"] = FIXED_ENTRY_QTY

    ensure_five_min_lock_initialized()

    return jsonify(scrub_nonfinite(state))
