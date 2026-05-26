"""
Behavioral constraint engine.

This module is the core innovation of the platform: structural enforcement of
operator discipline through hard gates, locks, and cooldown periods.

All constraint logic lives here — zone computation, entry direction gates,
temporal locks, and scale-in unlock rules. No Flask imports.
"""

import math
import time

from services.state_manager import state, ORDER_INDEX
from stores.state_store import save_asset_state
from utils.type_helpers import safe_int, safe_float


# ----------------------------------------------------------------
#  Internal helpers — not exported
# ----------------------------------------------------------------

def _save_global():
    from stores.state_store import save_global_state
    try:
        save_global_state(state["global"])
    except Exception as e:
        print("Error saving global state:", e)


# ----------------------------------------------------------------
#  Zone computation (8pt + 12pt structure)
# ----------------------------------------------------------------

def compute_zone(a: dict):
    """
    Entry zone from 8pt/12pt structure only.
    - Both agree  → NORMAL (direction is locked to 8pt color)
    - They disagree → FREE (operator chooses direction)
    - Either neutral/missing → None (entry blocked)
    """
    high = (a.get("high_renko_color") or "neutral").lower()
    macro = (a.get("macro_renko_color") or "neutral").lower()
    valid = ("green", "red")
    if high not in valid or macro not in valid:
        return None
    return "NORMAL" if high == macro else "FREE"


# ----------------------------------------------------------------
#  Entry direction gates
# ----------------------------------------------------------------

def _structure_allows_direction(a: dict, direction: str) -> bool:
    direction = (direction or "").upper().strip()
    if direction not in ("BUY", "SELL"):
        return False
    zone = compute_zone(a)
    high = (a.get("high_renko_color") or "neutral").lower()
    if zone == "FREE":
        return True
    if zone == "NORMAL":
        if high == "green":
            return direction == "BUY"
        if high == "red":
            return direction == "SELL"
    return False


def _two_half_allows_direction(a: dict, direction: str) -> bool:
    """BUY requires 2.5pt green; SELL requires 2.5pt red."""
    d = (direction or "").upper().strip()
    required = "green" if d == "BUY" else "red" if d == "SELL" else None
    if required is None:
        return False
    return (a.get("two_half_renko_color") or "neutral").lower() == required


def entry_direction_allowed(a: dict, direction: str) -> tuple:
    """
    Final backend entry gate combining structural and immediate filters.

    Returns (allowed: bool, reason: str).
    """
    direction = (direction or "").upper().strip()
    if direction not in ("BUY", "SELL"):
        return False, "Invalid direction"

    zone = compute_zone(a)
    if zone not in ("NORMAL", "FREE"):
        return False, "8/12 zone unavailable"

    if not _structure_allows_direction(a, direction):
        return False, "Blocked by 8/12 structure"

    if not _two_half_allows_direction(a, direction):
        two_half = (a.get("two_half_renko_color") or "neutral").lower()
        return False, f"Blocked by 2.5pt Renko: {two_half}"

    return True, ""


# ----------------------------------------------------------------
#  2.5pt aligned/opposite helpers
# ----------------------------------------------------------------

def _trade_aligned_two_half_color(pos: int):
    if pos > 0:
        return "green"
    if pos < 0:
        return "red"
    return None


def _trade_opposite_two_half_color(pos: int):
    if pos > 0:
        return "red"
    if pos < 0:
        return "green"
    return None


def _two_half_management_unlocked(a: dict) -> bool:
    pos = int(a.get("position", 0) or 0)
    if pos == 0:
        return False
    return (a.get("two_half_renko_color") or "neutral").lower() == _trade_opposite_two_half_color(pos)


# ----------------------------------------------------------------
#  Bar progression helpers
# ----------------------------------------------------------------

def has_new_main_bar_after_entry(a: dict) -> bool:
    entry_main_ts = a.get("entry_main_renko_ts")
    current_main_ts = a.get("main_renko_ts")
    if entry_main_ts is None or current_main_ts is None:
        return False
    try:
        return float(current_main_ts) > float(entry_main_ts)
    except Exception:
        return current_main_ts != entry_main_ts


def has_new_high_bar_after_entry(a: dict) -> bool:
    entry_high_ts = a.get("entry_high_renko_ts")
    current_high_ts = a.get("high_renko_ts")
    if entry_high_ts is None or current_high_ts is None:
        return False
    try:
        return float(current_high_ts) > float(entry_high_ts)
    except Exception:
        return current_high_ts != entry_high_ts


# ----------------------------------------------------------------
#  Next-bar exit permission gates
# ----------------------------------------------------------------

def six_next_bar_exit_allowed(a: dict) -> bool:
    """
    6PT NEXT allowed when trade is open AND
    either the first fresh 6pt bar has not yet formed,
    OR 2.5 management is currently unlocked.
    """
    if int(a.get("position", 0) or 0) == 0:
        return False
    before_first_fresh = not has_new_main_bar_after_entry(a)
    return bool(before_first_fresh or _two_half_management_unlocked(a))


def eight_next_bar_exit_allowed(a: dict) -> bool:
    """
    8PT NEXT allowed when trade is open AND
    either the first fresh 8pt bar has not yet formed,
    OR 2.5 management is currently unlocked.
    """
    if int(a.get("position", 0) or 0) == 0:
        return False
    before_first_fresh = not has_new_high_bar_after_entry(a)
    return bool(before_first_fresh or _two_half_management_unlocked(a))


# ----------------------------------------------------------------
#  Initial exit lock  (blocks emotional exits in first bar)
# ----------------------------------------------------------------

def release_initial_exit_lock_if_needed(asset_key: str):
    """
    Release the post-entry exit lock once a fresh 6pt bar forms after entry.
    """
    if asset_key not in state["assets"]:
        return
    a = state["assets"][asset_key]
    if int(a.get("position", 0) or 0) == 0:
        a["initial_exit_lock_active"] = False
        return
    if not bool(a.get("initial_exit_lock_active", False)):
        return

    entry_main_ts = a.get("entry_main_renko_ts")
    current_main_ts = a.get("main_renko_ts")
    if entry_main_ts is None or current_main_ts is None:
        return

    try:
        fresh_after_entry = float(current_main_ts) > float(entry_main_ts)
    except Exception:
        fresh_after_entry = current_main_ts != entry_main_ts

    if fresh_after_entry:
        a["initial_exit_lock_active"] = False
        a["initial_exit_lock_released"] = True
        a["initial_exit_lock_released_ts"] = time.time()


# ----------------------------------------------------------------
#  2.5pt TP/protect management lock  (dynamic per-bar gate)
# ----------------------------------------------------------------

def release_two_half_tp_lock_if_needed(asset_key: str, prev_color: str, new_color: str, color_changed: bool):
    """
    Dynamic 2.5pt management lock.

    LONG:  green=locked, red=unlocked
    SHORT: red=locked,   green=unlocked

    Controls whether TP/protect buttons are available.
    Does NOT trigger exits. Does NOT cancel already-armed exits.
    """
    if asset_key not in state["assets"]:
        return
    a = state["assets"][asset_key]
    pos = int(a.get("position", 0) or 0)

    if pos == 0:
        a["two_half_tp_lock_enabled"] = False
        a["two_half_tp_lock_base_color"] = None
        a["two_half_tp_lock_released"] = False
        return

    color = (new_color or a.get("two_half_renko_color") or "neutral").lower()
    aligned_color = _trade_aligned_two_half_color(pos)
    opposite_color = _trade_opposite_two_half_color(pos)
    now_ts = time.time()

    if color == opposite_color:
        if bool(a.get("two_half_tp_lock_enabled", False)):
            a["two_half_tp_lock_released_ts"] = now_ts
        a["two_half_tp_lock_enabled"] = False
        a["two_half_tp_lock_base_color"] = None
        a["two_half_tp_lock_released"] = True
        if a.get("two_half_tp_lock_released_ts") is None:
            a["two_half_tp_lock_released_ts"] = now_ts
        return

    if color == aligned_color:
        if not bool(a.get("two_half_tp_lock_enabled", False)):
            a["two_half_tp_lock_started_ts"] = now_ts
        a["two_half_tp_lock_enabled"] = True
        a["two_half_tp_lock_base_color"] = color
        a["two_half_tp_lock_released"] = False
        return

    # Neutral/unknown: conservatively lock while in a trade
    a["two_half_tp_lock_enabled"] = True
    a["two_half_tp_lock_base_color"] = None
    a["two_half_tp_lock_released"] = False


# ----------------------------------------------------------------
#  Scale-in unlock rules
# ----------------------------------------------------------------

def update_scale_in_from_6pt(asset_key: str, new_color: str, bar_ts):
    """
    Unlock scale-in on a fresh 6pt continuation bar after entry.
    LONG unlocks on GREEN; SHORT unlocks on RED.
    """
    if asset_key not in state["assets"]:
        return
    a = state["assets"][asset_key]
    pos = int(a.get("position", 0) or 0)
    if pos == 0:
        a["scale_in_available"] = False
        a["scale_in_stage"] = None
        return
    if bool(a.get("scale_in_used", False)):
        a["scale_in_available"] = False
        return
    if bool(a.get("scale_in_available", False)):
        return
    if new_color not in ("green", "red"):
        return

    entry_main_ts = a.get("entry_main_renko_ts")
    if entry_main_ts is None or bar_ts is None:
        return
    try:
        fresh = float(bar_ts) > float(entry_main_ts)
    except Exception:
        fresh = bar_ts != entry_main_ts
    if not fresh:
        return

    target_color = "green" if pos > 0 else "red"
    if new_color == target_color:
        a["scale_in_available"] = True
        a["scale_in_stage"] = "READY_6PT"
        a["scale_in_last_ts"] = time.time()


def update_scale_in_from_4pt(asset_key: str, prev_color: str, new_color: str, color_changed: bool):
    """
    Unlock scale-in on a 4pt pullback bar after entry.
    LONG unlocks when 4pt flips RED; SHORT unlocks when 4pt flips GREEN.
    """
    if asset_key not in state["assets"]:
        return
    a = state["assets"][asset_key]
    pos = int(a.get("position", 0) or 0)
    if pos == 0:
        return
    if bool(a.get("scale_in_used", False)):
        a["scale_in_available"] = False
        return
    if bool(a.get("scale_in_available", False)):
        return
    if not color_changed or new_color not in ("green", "red"):
        return

    opposite_color = "red" if pos > 0 else "green"
    if new_color != opposite_color:
        return

    a["scale_in_available"] = True
    a["scale_in_stage"] = "READY_4PT_PULLBACK"
    a["scale_in_last_ts"] = time.time()


# ----------------------------------------------------------------
#  5-minute candle lock
# ----------------------------------------------------------------

def _current_5m_bucket() -> int:
    return int(time.time() // 300)


def _seconds_until_next_5m() -> int:
    return 300 - (int(time.time()) % 300)


def ensure_five_min_lock_initialized():
    g = state["global"]
    current_bucket = _current_5m_bucket()
    stored_bucket = g.get("five_min_trade_bucket")
    if stored_bucket is None:
        g["five_min_trade_bucket"] = None
        g["five_min_trade_lock_active"] = False
        g["five_min_trade_lock_remaining_s"] = 0
        return
    try:
        stored_bucket = int(stored_bucket)
    except Exception:
        stored_bucket = None
    if stored_bucket is None or stored_bucket != current_bucket:
        g["five_min_trade_bucket"] = None
        g["five_min_trade_lock_active"] = False
        g["five_min_trade_lock_remaining_s"] = 0
    else:
        g["five_min_trade_lock_active"] = True
        g["five_min_trade_lock_remaining_s"] = _seconds_until_next_5m()


def activate_five_min_lock():
    g = state["global"]
    bucket = _current_5m_bucket()
    g["five_min_trade_bucket"] = bucket
    g["five_min_trade_lock_active"] = True
    g["five_min_trade_lock_remaining_s"] = _seconds_until_next_5m()
    _save_global()


def release_five_min_lock_if_same_bucket():
    """Release the 5-min lock on rejected/cancelled entries so the candle isn't wasted."""
    g = state["global"]
    current_bucket = _current_5m_bucket()
    try:
        stored_bucket = int(g["five_min_trade_bucket"]) if g.get("five_min_trade_bucket") is not None else None
    except Exception:
        stored_bucket = None
    if stored_bucket == current_bucket:
        g["five_min_trade_bucket"] = None
        g["five_min_trade_lock_active"] = False
        g["five_min_trade_lock_remaining_s"] = 0
        _save_global()


# ----------------------------------------------------------------
#  Post-exit cooldown lock  (3-minute hard block after any close)
# ----------------------------------------------------------------

def ensure_post_exit_lock_initialized():
    g = state["global"]
    now = time.time()
    expires_ts = g.get("post_exit_lock_expires_ts")
    if expires_ts is None:
        g["post_exit_lock_active"] = False
        g["post_exit_lock_started_ts"] = None
        g["post_exit_lock_expires_ts"] = None
        g["post_exit_lock_remaining_s"] = 0
        return
    try:
        expires_ts = float(expires_ts)
    except Exception:
        expires_ts = None
    if expires_ts is None or now >= expires_ts:
        g["post_exit_lock_active"] = False
        g["post_exit_lock_started_ts"] = None
        g["post_exit_lock_expires_ts"] = None
        g["post_exit_lock_remaining_s"] = 0
    else:
        g["post_exit_lock_active"] = True
        g["post_exit_lock_remaining_s"] = max(0, int(math.ceil(expires_ts - now)))


def activate_post_exit_lock():
    g = state["global"]
    now = time.time()
    expires_ts = now + 180
    g["post_exit_lock_active"] = True
    g["post_exit_lock_started_ts"] = now
    g["post_exit_lock_expires_ts"] = expires_ts
    g["post_exit_lock_remaining_s"] = 180
    _save_global()


# ----------------------------------------------------------------
#  Daily stop
# ----------------------------------------------------------------

def _ny_trading_date_str() -> str:
    try:
        from utils.calendar import get_today_info
        return str(get_today_info().get("date"))
    except Exception:
        return None


def ensure_daily_stop_day_initialized():
    g = state["global"]
    today = _ny_trading_date_str()
    if not today:
        return False
    eq = g.get("rithmic_account_balance")
    changed = False

    if g.get("daily_stop_date") != today:
        g["daily_stop_date"] = today
        if eq is not None:
            g["daily_start_equity"] = float(eq)
        g["daily_stop_triggered"] = False
        g["daily_stop_triggered_ts"] = None
        g["daily_stop_triggered_reason"] = None
        g["daily_stop_triggered_equity"] = None
        g["daily_stop_triggered_dd_pct"] = None
        max_trades = safe_int(g.get("max_trades_per_day"), default=6)
        if max_trades <= 0:
            max_trades = 6
        g["max_trades_per_day"] = max_trades
        g["trades_taken_today"] = 0
        g["trades_remaining_today"] = max_trades
        g["daily_trade_limit_date"] = today
        changed = True

    if g.get("daily_stop_date") == today and g.get("daily_start_equity") is None and eq is not None:
        g["daily_start_equity"] = float(eq)
        changed = True

    if g.get("daily_trade_limit_date") != today:
        max_trades = safe_int(g.get("max_trades_per_day"), default=6)
        if max_trades <= 0:
            max_trades = 6
        g["max_trades_per_day"] = max_trades
        g["trades_taken_today"] = 0
        g["trades_remaining_today"] = max_trades
        g["daily_trade_limit_date"] = today
        changed = True

    if changed:
        _save_global()
    return changed


def maybe_trigger_daily_stop() -> bool:
    """
    Check drawdown against daily stop limit.
    Triggers once per day, sticky — locks all entries and enqueues exits.
    """
    g = state["global"]
    if not bool(g.get("daily_stop_enabled", False)):
        return False
    if bool(g.get("daily_stop_triggered", False)):
        return False

    start = g.get("daily_start_equity")
    eq = g.get("rithmic_account_balance")
    if start is None or eq is None:
        return False
    try:
        start, eq = float(start), float(eq)
    except Exception:
        return False
    if start <= 0:
        return False

    dd_pct = (start - eq) / start * 100.0
    limit_pct = safe_float(g.get("daily_stop_limit_pct"), default=0.0, nonfinite_to=0.0)
    if limit_pct <= 0 or dd_pct < limit_pct:
        return False

    now = time.time()
    g["daily_stop_triggered"] = True
    g["daily_stop_triggered_ts"] = now
    g["daily_stop_triggered_reason"] = f"Drawdown {dd_pct:.2f}% >= limit {limit_pct:.2f}%"
    g["daily_stop_triggered_equity"] = eq
    g["daily_stop_triggered_dd_pct"] = dd_pct
    g["trade_lock"] = True
    _save_global()

    from services.exit_engine import enqueue_daily_stop_exits
    enqueue_daily_stop_exits(reason="DAILY_STOP")

    from services.state_manager import append_log
    append_log("rithmic", {
        "ts": now,
        "type": "daily_stop_trigger",
        "symbol": "RITHMIC",
        "payload": {"start_equity": start, "equity": eq, "dd_pct": dd_pct, "limit_pct": limit_pct},
    })

    return True


def update_daily_stop_metrics():
    g = state["global"]
    start = g.get("daily_start_equity")
    eq = g.get("rithmic_account_balance")
    if start is None or eq is None:
        return
    try:
        start, eq = float(start), float(eq)
    except Exception:
        return
    if start <= 0:
        return

    daily_pnl_usd = eq - start
    daily_pnl_pct = daily_pnl_usd / start
    limit_pct = float(g.get("daily_stop_limit_pct", 0.0)) / 100.0
    remaining_pct = max(0.0, limit_pct + daily_pnl_pct)

    g["daily_pnl_usd"] = daily_pnl_usd
    g["daily_pnl_pct"] = daily_pnl_pct
    g["daily_stop_remaining_pct"] = remaining_pct
    _save_global()


# ----------------------------------------------------------------
#  Daily trade limit
# ----------------------------------------------------------------

def consume_trade_limit_on_fill() -> bool:
    g = state["global"]
    ensure_daily_stop_day_initialized()
    today = _ny_trading_date_str()
    max_trades = safe_int(g.get("max_trades_per_day"), default=6)
    if max_trades <= 0:
        max_trades = 6
    taken = safe_int(g.get("trades_taken_today"), default=0)
    remaining = safe_int(g.get("trades_remaining_today"), default=max_trades)
    if remaining <= 0:
        return False
    taken += 1
    remaining = max(0, max_trades - taken)
    g["max_trades_per_day"] = max_trades
    g["trades_taken_today"] = taken
    g["trades_remaining_today"] = remaining
    g["daily_trade_limit_date"] = today
    _save_global()
    return True


# ----------------------------------------------------------------
#  Intent and pre-order helpers
# ----------------------------------------------------------------

def clear_intent(asset_key: str, status: str = None):
    if asset_key not in state["assets"]:
        return
    a = state["assets"][asset_key]
    a["intent_active"] = False
    a["intent_created_ts"] = None
    a["intent_bar_base_ts"] = None
    a["intent_ready_bar_ts"] = None
    a["intent_status"] = status
    try:
        save_asset_state(asset_key, a)
    except Exception as e:
        print(f"Error saving state (clear_intent) for {asset_key}:", e)


def clear_preorder(asset_key: str, status: str = None):
    if asset_key not in state["assets"]:
        return
    a = state["assets"][asset_key]
    a["preorder_active"] = False
    a["preorder_direction"] = None
    a["preorder_qty"] = 0
    a["preorder_entry_size_mode"] = None
    a["preorder_trade_mode"] = None
    a["preorder_created_ts"] = None
    a["preorder_bar_base_ts"] = None
    a["preorder_status"] = status
    try:
        save_asset_state(asset_key, a)
    except Exception as e:
        print(f"Error saving state (clear_preorder) for {asset_key}:", e)


# ----------------------------------------------------------------
#  Point value helper
# ----------------------------------------------------------------

def point_value_for_contract(contract: str, asset_key: str = None) -> float:
    c = (contract or "").upper().strip()
    if c.startswith("MES"):
        return 5.0
    if c.startswith("ES"):
        return 50.0
    return float(POINT_VALUE_USD.get(asset_key or "", 1.0) or 1.0)
