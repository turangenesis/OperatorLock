"""
Exit engine — automated exit path orchestration.

Handles: points take-profit, protect (soft stop), signal-count TP,
and daily-stop mass exit. No Flask imports.
"""

import time

from config import resolve_rithmic_symbol, resolve_rithmic_exchange
from services.state_manager import state, ORDER_INDEX
from stores.orders_store import create_order
from stores.state_store import save_asset_state
from utils.type_helpers import safe_float, safe_int


# ----------------------------------------------------------------
#  Points take-profit  (Rithmic open-PnL based)
# ----------------------------------------------------------------

def maybe_points_take_profit_exit(asset_key: str) -> bool:
    """Auto-exit when Rithmic-derived open points >= configured target."""
    if asset_key not in state["assets"]:
        return False
    a = state["assets"][asset_key]
    pos = int(a.get("position", 0) or 0)
    if pos == 0 or a.get("pending_order_id"):
        return False
    if not bool(a.get("points_tp_enabled", False)):
        return False

    target = safe_float(a.get("points_tp_target"), default=15.0, nonfinite_to=15.0)
    open_points = safe_float(a.get("rithmic_open_points"), default=0.0, nonfinite_to=0.0)
    if target <= 0 or open_points < target:
        return False

    return _send_exit_order(asset_key, a, pos, source="TAKE_PROFIT_POINTS", hit_ts_field="points_tp_hit_ts")


# ----------------------------------------------------------------
#  Protect  (soft stop / risk-free lock-in)
# ----------------------------------------------------------------

def maybe_protect_exit(asset_key: str) -> bool:
    """Auto-exit when protect is enabled and open points <= threshold."""
    if asset_key not in state["assets"]:
        return False
    a = state["assets"][asset_key]
    pos = int(a.get("position", 0) or 0)
    if pos == 0 or a.get("pending_order_id"):
        return False
    if not bool(a.get("protect_enabled", False)):
        return False
    if bool(a.get("two_half_tp_lock_enabled", False)):
        return False

    threshold = safe_float(a.get("protect_threshold_points"), default=-2.0, nonfinite_to=-2.0)
    open_points = safe_float(a.get("rithmic_open_points"), default=0.0, nonfinite_to=0.0)
    if open_points > threshold:
        return False

    return _send_exit_order(asset_key, a, pos, source="PROTECT", hit_ts_field="protect_hit_ts")


# ----------------------------------------------------------------
#  Signal-count take-profit  (6pt bar count based)
# ----------------------------------------------------------------

def maybe_take_profit_exit(asset_key: str) -> bool:
    """Exit when tp_count >= tp_target (6pt bar signal count)."""
    if asset_key not in state["assets"]:
        return False
    a = state["assets"][asset_key]
    pos = int(a.get("position", 0) or 0)
    if pos == 0 or a.get("pending_order_id"):
        return False
    if bool(a.get("initial_exit_lock_active", False)):
        return False
    if not bool(a.get("tp_armed", False)):
        return False

    tp_target = safe_int(a.get("tp_target"), default=3)
    tp_count = safe_int(a.get("tp_count"), default=0)
    if tp_target <= 0 or tp_count < tp_target:
        return False

    return _send_exit_order(asset_key, a, pos, source="TAKE_PROFIT")


# ----------------------------------------------------------------
#  Daily stop mass exit
# ----------------------------------------------------------------

def enqueue_daily_stop_exits(reason: str = "DAILY_STOP"):
    """Create EXIT orders for all open positions (one per asset, no duplicates)."""
    g = state["global"]
    env_global = (g.get("env") or "DEMO").upper().strip()

    for sym, a in state["assets"].items():
        pos = int(a.get("position", 0) or 0)
        if pos == 0 or a.get("pending_order_id"):
            continue

        side = "SELL" if pos > 0 else "BUY"
        qty = abs(pos)
        env = (a.get("env") or env_global).upper().strip()
        rith_sym = resolve_rithmic_symbol(sym)
        rith_exch = resolve_rithmic_exchange(sym)

        try:
            order = create_order(
                symbol=rith_sym, exchange=rith_exch,
                side=side, qty=qty, source=reason,
                mode=a.get("exit_mode") or "A", kind="EXIT", env=env,
            )
        except Exception as e:
            print(f"DAILY_STOP create_order failed for {sym}:", e)
            continue

        a["pending_order_id"] = order.get("id")
        a["pending_side"] = side
        a["pending_qty"] = qty
        a["pending_mode"] = a.get("exit_mode") or "A"
        a["pending_trade_mode"] = None

        ORDER_INDEX[order["id"]] = {
            "symbol": sym, "side": side, "qty": qty,
            "mode": a.get("exit_mode") or "A", "env": env, "kind": "EXIT",
        }

        try:
            save_asset_state(sym, a)
        except Exception as e:
            print(f"Error saving state (DAILY_STOP exit) for {sym}:", e)


# ----------------------------------------------------------------
#  Shared helper
# ----------------------------------------------------------------

def _send_exit_order(asset_key: str, a: dict, pos: int, source: str, hit_ts_field: str = None) -> bool:
    side = "SELL" if pos > 0 else "BUY"
    qty = abs(pos)
    env = (a.get("env") or state["global"].get("env") or "DEMO").upper().strip()
    rith_sym = a.get("rithmic_symbol") or resolve_rithmic_symbol(asset_key)
    rith_exch = resolve_rithmic_exchange(asset_key)

    try:
        order = create_order(
            symbol=rith_sym, exchange=rith_exch,
            side=side, qty=qty, source=source,
            mode=a.get("exit_mode") or "A", kind="EXIT", env=env,
        )
    except Exception as e:
        print(f"{source} create_order failed for {asset_key}:", e)
        return False

    a["pending_order_id"] = order.get("id")
    a["pending_side"] = side
    a["pending_qty"] = qty
    a["pending_mode"] = a.get("exit_mode") or "A"
    a["pending_trade_mode"] = None
    if hit_ts_field:
        a[hit_ts_field] = time.time()

    ORDER_INDEX[order["id"]] = {
        "symbol": asset_key, "side": side, "qty": qty,
        "mode": a.get("exit_mode") or "A", "env": env, "kind": "EXIT",
        "source": source,
    }

    try:
        save_asset_state(asset_key, a)
    except Exception as e:
        print(f"Error saving state ({source} exit) for {asset_key}:", e)

    return True
