"""
State manager — owns the single in-memory state dict and ORDER_INDEX.

All other services import `state` and `ORDER_INDEX` from here.
Firestore hydration runs once at import time via hydrate_state_from_firestore().
"""

import time

from config import ASSETS, ASSET_CONFIG, POINT_VALUE_USD, FIXED_ENTRY_QTY
from stores.orders_store import get_order
from stores.state_store import (
    load_global_state,
    load_asset_states,
    save_global_state,
    save_asset_state,
)
from utils.type_helpers import scrub_nonfinite, safe_int


# ----------------------------------------------------------------
#  In-memory global state
# ----------------------------------------------------------------

state = {
    "global": {
        "equity": 50000.0,
        "open_pnl": 0.0,
        "connected": False,
        "trade_lock": False,
        "total_orders": 0,
        "env": "DEMO",

        "five_min_trade_bucket": None,
        "five_min_trade_lock_active": False,
        "five_min_trade_lock_remaining_s": 0,

        "post_exit_lock_active": False,
        "post_exit_lock_started_ts": None,
        "post_exit_lock_expires_ts": None,
        "post_exit_lock_remaining_s": 0,

        "daily_stop_enabled": True,
        "daily_stop_limit_pct": 30.0,
        "daily_start_equity": None,
        "daily_stop_date": None,
        "daily_stop_triggered": False,
        "daily_stop_triggered_ts": None,
        "daily_stop_triggered_reason": None,
        "daily_stop_triggered_equity": None,
        "daily_stop_triggered_dd_pct": None,

        "max_trades_per_day": 6,
        "trades_taken_today": 0,
        "trades_remaining_today": 6,
        "daily_trade_limit_date": None,

        "tradingview_connected": False,
        "rithmic_connected": False,
        "rithmic_last_ts": None,
        "connected_count": 0,
        "connected_expected": 2,
    },
    "assets": {},
    "history": [],
    "logs": {
        "tradingview": [],
        "rithmic": [],
    },
}

# In-memory index: order_id → {symbol, side, qty, mode, trade_mode, env, kind, source}
ORDER_INDEX = {}


def _init_asset(sym: str) -> dict:
    return {
        "symbol": sym,
        "position": 0,
        "avg_price": None,
        "entry_price": None,
        "pnl": 0.0,
        "last_entry_ts": None,

        # Renko streams
        "renko_color": "neutral",
        "color_changed": False,
        "last_renko_ts": None,
        "small_renko_color": "neutral",
        "last_small_renko_ts": None,
        "one_renko_color": "neutral",
        "one_renko_ts": None,
        "one_renko_color_changed": False,
        "two_half_renko_color": "neutral",
        "two_half_renko_ts": None,
        "two_half_color_changed": False,
        "main_renko_color": "neutral",
        "main_renko_ts": None,
        "high_renko_color": "neutral",
        "high_renko_ts": None,
        "macro_renko_color": "neutral",
        "macro_renko_ts": None,

        # 2.5pt management lock
        "two_half_tp_lock_enabled": False,
        "two_half_tp_lock_base_color": None,
        "two_half_tp_lock_released": False,
        "two_half_tp_lock_started_ts": None,
        "two_half_tp_lock_released_ts": None,

        # Tempo
        "tempo_color": "neutral",
        "tempo_ts": None,
        "tempo_age_s": None,
        "tempo_ready": False,

        # Intent
        "intent_active": False,
        "intent_created_ts": None,
        "intent_bar_base_ts": None,
        "intent_ready_bar_ts": None,
        "intent_status": None,

        "last_exit_direction": None,
        "reentry_lock_active": False,

        # Heartbeat
        "last_heartbeat_ts": None,
        "last_price": None,

        "opposite_locked": False,
        "five_min_ok": True,
        "order_count": 0,
        "exit_mode": None,
        "auto_exit_renko": "6pt",
        "exit_tf": "main",
        "main_flip_exit_enabled": False,

        # TP (signal-count)
        "tp_armed": False,
        "tp_target": 3,
        "tp_count": 0,

        # 8pt next-bar exit
        "high_next_bar_exit_enabled": False,
        "high_next_bar_exit_started_ts": None,
        "high_next_bar_exit_base_ts": None,

        # Points TP / protect
        "points_tp_enabled": False,
        "points_tp_target": 15.0,
        "rithmic_open_points": 0.0,
        "rithmic_point_value": None,
        "points_tp_hit_ts": None,
        "protect_enabled": False,
        "protect_threshold_points": -2.0,
        "protect_hit_ts": None,

        "env": None,
        "stop_loss_price": None,
        "stop_loss_status": None,

        # Pending order
        "pending_order_id": None,
        "pending_side": None,
        "pending_qty": 0,
        "pending_mode": None,
        "pending_trade_mode": None,
        "preorder_trade_mode": None,

        # UI computed flags
        "manual_exit_allowed": True,
        "tempo_spent_ts": None,
        "tempo_last_bar_ts": None,
        "last_exit_ts": None,
        "tempo_4pt_unlock_ts": None,
        "last_trade_had_new_main_bar_after_entry": False,

        # Initial exit lock
        "initial_exit_lock_active": False,
        "initial_exit_lock_released": False,
        "initial_exit_lock_started_ts": None,
        "initial_exit_lock_released_ts": None,
        "initial_exit_lock_base_main_ts": None,

        # Pre-order
        "preorder_active": False,
        "preorder_direction": None,
        "preorder_qty": 0,
        "preorder_entry_size_mode": None,
        "preorder_created_ts": None,
        "preorder_bar_base_ts": None,
        "preorder_status": None,

        # Trade mode / zone
        "trade_mode": None,
        "entry_zone": None,
        "runner_4pt_unlocked": False,
        "entry_main_renko_ts": None,
        "entry_high_renko_ts": None,
        "entry_renko_color": None,
        "entry_main_renko_color": None,

        "four_pt_invalidation_enabled": False,
        "next_bar_exit_allowed": False,
        "high_next_bar_exit_allowed": False,
        "zone_type": None,
        "four_pt_invalidation_allowed": False,

        # Scale-in
        "scale_in_available": False,
        "scale_in_used": False,
        "scale_in_stage": None,
        "scale_in_last_ts": None,
    }


# Initialise per-asset state
for _asset in ASSETS:
    _sym = _asset["symbol"]
    state["assets"][_sym] = _init_asset(_sym)


# ----------------------------------------------------------------
#  Firestore hydration
# ----------------------------------------------------------------

def hydrate_state_from_firestore():
    # Global fields to restore
    try:
        global_snapshot = load_global_state()
        if global_snapshot:
            for key in (
                "equity", "env", "trade_lock",
                "daily_stop_enabled", "daily_stop_limit_pct",
                "daily_start_equity", "daily_stop_date",
                "daily_stop_triggered", "daily_stop_triggered_ts",
                "daily_stop_triggered_reason", "daily_stop_triggered_equity",
                "daily_stop_triggered_dd_pct",
                "max_trades_per_day", "trades_taken_today",
                "trades_remaining_today", "daily_trade_limit_date",
                "five_min_trade_bucket", "five_min_trade_lock_active",
                "five_min_trade_lock_remaining_s",
                "post_exit_lock_active", "post_exit_lock_started_ts",
                "post_exit_lock_expires_ts", "post_exit_lock_remaining_s",
            ):
                if key in global_snapshot:
                    state["global"][key] = global_snapshot[key]
    except Exception as e:
        print("Error loading global console_state:", e)

    # Asset fields
    try:
        asset_snapshots = load_asset_states()
    except Exception as e:
        print("Error loading asset console_state:", e)
        asset_snapshots = {}

    for sym, snap in asset_snapshots.items():
        if sym not in state["assets"]:
            continue
        a = state["assets"][sym]
        for key, value in (snap or {}).items():
            if key in ("symbol", "updatedAt"):
                continue
            a[key] = value

        # Rebuild ORDER_INDEX for any asset with a pending order
        pending_id = a.get("pending_order_id")
        if pending_id:
            od = {}
            kind = (a.get("pending_kind") or "").upper().strip()
            env = (a.get("env") or state["global"].get("env", "DEMO"))
            try:
                od = get_order(pending_id) or {}
            except Exception as e:
                print("hydrate: could not fetch order doc:", e)

            if not kind:
                kind = (od.get("kind") or "ENTRY")
                if not a.get("env") and od.get("env"):
                    env = od.get("env")

            ORDER_INDEX[pending_id] = {
                "symbol": sym,
                "side": a.get("pending_side"),
                "qty": a.get("pending_qty") or ASSET_CONFIG.get(sym, {}).get("size", 1),
                "mode": a.get("pending_mode"),
                "trade_mode": a.get("pending_trade_mode"),
                "env": str(env).upper().strip(),
                "kind": str(kind).upper().strip(),
                "source": (od.get("source") if isinstance(od, dict) else None),
            }

    # Scrub non-finite values
    try:
        state["global"] = scrub_nonfinite(state["global"])
        for _sym in list(state["assets"].keys()):
            state["assets"][_sym] = scrub_nonfinite(state["assets"][_sym])
    except Exception as e:
        print("Error scrubbing non-finite values after hydration:", e)


# ----------------------------------------------------------------
#  Log helpers
# ----------------------------------------------------------------

def append_log(kind: str, entry: dict):
    from config import LOG_MAX
    logs = state["logs"].get(kind)
    if logs is None:
        return
    logs.insert(0, entry)
    if len(logs) > LOG_MAX:
        logs.pop()
