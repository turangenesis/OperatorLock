
# 1 THE NEWEST VERSION APP.py

from operator import pos
import time
import os
import math
from liquidity_calendar import LOW_LIQUIDITY_DAYS, get_today_info
#from pnl_store import get_manual_pnl_and_qty, read_manual_pnl_usd, compute_qty
 
#newest add it all here!
from flask import Flask, render_template, jsonify, request
from orders_store import create_order, get_pending_orders, update_order_status, get_order

from auth import init_auth, login_required

from console_state_store import (
    save_asset_state,
    load_asset_states,
    save_global_state,
    load_global_state,
)

# Single shared TradingView webhook secret.
TV_WEBHOOK_SECRET = os.environ.get("TV_WEBHOOK_SECRET", "dev-secret")

# Secret for VPS -> Heroku heartbeat posts (and pending orders auth)
BRIDGE_HEARTBEAT_SECRET = os.environ.get("BRIDGE_HEARTBEAT_SECRET", "dev-bridge-secret")

LOG_MAX = 100  # max log entries to keep for TV / Rithmic
FIXED_ENTRY_QTY = 1 # changed on 2026-02-24 from 1 to 2 
SCALE_IN_QTY = 5  # 5 MES scale-in size



#FIXED_ENTRY_QTY = 5 # for micro use 5! margin changes if affected! its neww!

app = Flask(__name__)
init_auth(app)

# =========================================================
# ASSETS: ONLY ES + GC
# =========================================================
ASSETS = [
    {"symbol": "ES", "name": "S&P 500 Futures"},
   

]

# -----------------------------
# Rithmic exchange mapping
# -----------------------------
RITHMIC_EXCHANGES = {
    "ES": "CME",
    #"NQ": "CME",
    #"GC": "COMEX",
    #"YM":  "CBOT",
}

def resolve_rithmic_exchange(asset_key: str) -> str:
    return RITHMIC_EXCHANGES.get(asset_key, "CME")

# changed this 

RITHMIC_SYMBOLS = {
    # "ES": "ESM6",   # default main contract if needed
    "ES": "MESM6",   # default main contract if needed

} # after juen 15th swtich to U6--

CONTRACT_TO_UI = {
    #"ESM6": "ES",   # mini
    "MESM6": "ES",  # micro
}

def resolve_rithmic_symbol(asset_key: str) -> str:
    return RITHMIC_SYMBOLS.get(asset_key, asset_key)

state = {
    "global": {
        "equity": 50000.0,
        "open_pnl": 0.0,
        "connected": False,
        "trade_lock": False,
        "total_orders": 0,
        "env": "DEMO",

        # =====================================================
        # FIVE-MINUTE ENTRY LOCK
        # One entry max per 5-minute candle/window
        # =====================================================
        "five_min_trade_bucket": None,
        "five_min_trade_lock_active": False,
        "five_min_trade_lock_remaining_s": 0,
        # =====================================================
        # FIVE-MINUTE FULL LOCK AFTER EXIT
        # Separate from entry candle lock
        # =====================================================
        "post_exit_lock_active": False,
        "post_exit_lock_started_ts": None,
        "post_exit_lock_expires_ts": None,
        "post_exit_lock_remaining_s": 0,

        # =====================================================
        # DAILY STOP (server-side, driven by /api/rithmic/pnl-snapshot)
        # accountBalance is treated as net liq (moves with open PnL)
        # =====================================================
        "daily_stop_enabled": True,
        "daily_stop_limit_pct": 30.0,          # e.g. 3% max drawdown from daily_start_equity

        # "daily_stop_limit_pct": 30.0,          # e.g. 3% max drawdown from daily_start_equity
        #"daily_stop_limit_pct": 0.05,          # e.g. 3% max drawdown from daily_start_equity

        "daily_start_equity": None,           # set once per NY trading day
        "daily_stop_date": None,              # NY trading date string
        "daily_stop_triggered": False,
        "daily_stop_triggered_ts": None,
        "daily_stop_triggered_reason": None,
        "daily_stop_triggered_equity": None,
        "daily_stop_triggered_dd_pct": None,


        # =====================================================
        # DAILY TRADE LIMIT (UI-only persisted layer)
        # =====================================================
        "max_trades_per_day": 6,
        "trades_taken_today": 0,
        "trades_remaining_today": 6,
        "daily_trade_limit_date": None,


        # Connection flags
        "tradingview_connected": False,
        "rithmic_connected": False,
        "rithmic_last_ts": None,
        "connected_count": 0,
        "connected_expected": 2,
          

        # =====================================================
        # GLOBAL TEMPO TOKEN (6.5pt Renko on ES via /tv_tempo)
        # =====================================================
     # =====================================================
        # Tempo is now PER-ASSET (stored in state["assets"][sym])
        # Keep these only if you still want legacy debugging.
        # =====================================================
        


    },
    "assets": {},
    "history": [],
    "logs": {
        "tradingview": [],
        "rithmic": [],
    },
}

# In-memory index of Firestore orders so execution-report can update state.
ORDER_INDEX = {}

# Initialize per-asset state
for asset in ASSETS:
    sym = asset["symbol"]
    state["assets"][sym] = {
        "symbol": sym,
        "position": 0,
        "avg_price": None,
        "entry_price": None,
        "pnl": 0.0,
        "last_entry_ts": None,

        # BIG renko fields (4pt)
        "renko_color": "neutral",
        "color_changed": False,
        "last_renko_ts": None,

        # SMALL renko fields (1pt) 
        "small_renko_color": "neutral",
        "last_small_renko_ts": None,
        # =====================================================
        # 1PT RENKO VISUAL ONLY
        # Does NOT affect entries, exits, scale-in, locks, zones, or tempo.
        # =====================================================
        "one_renko_color": "neutral",
        "one_renko_ts": None,
        "one_renko_color_changed": False,
                # =====================================================
        # 2.5PT RENKO STREAM
        # Used for:
        # 1) entry direction filter
        # 2) TP/protect discipline lock release
        # Does NOT auto-exit by itself.
        # =====================================================
        "two_half_renko_color": "neutral",
        "two_half_renko_ts": None,
        "two_half_color_changed": False,

        # =====================================================
        # 2.5PT TP/PROTECT LOCK
        # When active:
        # - point TP is force-disabled
        # - protect is force-disabled
        # - user cannot re-enable TP/protect until 2.5pt color flips
        # =====================================================
        "two_half_tp_lock_enabled": False,
        "two_half_tp_lock_base_color": None,
        "two_half_tp_lock_released": False,
        "two_half_tp_lock_started_ts": None,
        "two_half_tp_lock_released_ts": None,
        # =====================================================
        # TEMPO (UI-only): derived from ES 6pt stream
        # =====================================================
        "tempo_color": "neutral",     # green/red/neutral
        "tempo_ts": None,             # last tempo update timestamp
        "tempo_age_s": None,          # computed in /api/state
        "tempo_ready": False,         # computed in /api/state

        # =====================================================
        # INTENT (soft next-bar permission, no auto-execution)
        # =====================================================
        "intent_active": False,
        "intent_created_ts": None,
        "intent_bar_base_ts": None,
        "intent_ready_bar_ts": None,
        "intent_status": None,
        

        "last_exit_direction": None,   # "BUY" or "SELL"
        "reentry_lock_active": False,  # persisted lock


      
       

        # =====================================================
        # =====================================================
   



        # heartbeat fields
        "last_heartbeat_ts": None,
        "last_price": None,

        "opposite_locked": False,
        "five_min_ok": True,
        "order_count": 0,
        "exit_mode": None,
        "auto_exit_renko": "6pt",
        "exit_tf": "main",  # NEW: "main" (6pt) or "high" (9pt)
        
        "main_flip_exit_enabled": False,  # NEW: optional 6pt assist exit while high exit remains active

        # =====================================================
        # TAKE PROFIT (signal-count based)
        # tp_target: how many TV signals until exit
        # tp_count:  how many signals received so far (while in trade)
        # =====================================================
       
        # "tp_armed": False,   # NEW: TP toggle
        # "tp_target": 3,
        # "tp_count": 0,
        # # POINTS TAKE PROFIT — Rithmic OpenPnL based
        # "points_tp_enabled": False,
        # "points_tp_target": 10.0,
        "tp_armed": False,   # 6pt next-bar exit toggle
        "tp_target": 3,
        "tp_count": 0,

        # 8pt next-bar exit toggle
        "high_next_bar_exit_enabled": False,
        "high_next_bar_exit_started_ts": None,
        "high_next_bar_exit_base_ts": None,

        # POINTS TAKE PROFIT — Rithmic OpenPnL based
        "points_tp_enabled": False,
        "points_tp_target": 15.0,
        "rithmic_open_points": 0.0,
        "rithmic_point_value": None,
        "points_tp_hit_ts": None,
        "protect_enabled": False,
        "protect_threshold_points": -2.0,
        "protect_hit_ts": None,

        # =====================================================
        # MAIN + HIGH Renko direction streams (independent of TEMPO)
        # main = 6pt (replaces old 5pt role)
        # high = 8pt (replaces old 6pt/higher role)
        # =====================================================
        # "main_renko_color": "neutral",     # green/red/neutral (6pt)
        # "main_renko_ts": None,

        # "high_renko_color": "neutral",     # green/red/neutral (8pt)
        # "high_renko_ts": None,
        "main_renko_color": "neutral",     # green/red/neutral (6pt)
        "main_renko_ts": None,

        "high_renko_color": "neutral",     # green/red/neutral (8pt)
        "high_renko_ts": None,

        "macro_renko_color": "neutral",    # green/red/neutral (12pt)
        "macro_renko_ts": None,



        "env": None,

        # safety stop fields
        "stop_loss_price": None,
        "stop_loss_status": None,

        # pending order fields
        "pending_order_id": None,
        "pending_side": None,
        "pending_qty": 0,
        "pending_mode": None,
        "pending_trade_mode": None,      # "SCALP" | "RUNNER" | None
        "preorder_trade_mode": None,     # "SCALP" | "RUNNER" | None

        # optional UI convenience (computed in /api/state)
        "manual_exit_allowed": True,
        "tempo_spent_ts": None,
        "tempo_last_bar_ts": None,
        "last_exit_ts": None,
        "tempo_4pt_unlock_ts": None,
        "last_trade_had_new_main_bar_after_entry": False,
        # =====================================================
        # INITIAL POST-ENTRY EXIT LOCK
        # Auto-locks on entry fill.
        # Releases only after first fresh 6pt MAIN bar after entry.
        # Blocks emotional manual exit + TP/protect during initial expansion.
        # =====================================================
        "initial_exit_lock_active": False,
        "initial_exit_lock_released": False,
        "initial_exit_lock_started_ts": None,
        "initial_exit_lock_released_ts": None,
        "initial_exit_lock_base_main_ts": None,

        # =====================================================
        # PRE-ORDER (one-shot next 6pt bar conditional entry)
        # =====================================================
        "preorder_active": False,
        "preorder_direction": None,     # "BUY" / "SELL"
        "preorder_qty": 0,
        "preorder_entry_size_mode": None,   # "6pt" / "9pt"
        "preorder_created_ts": None,
        "preorder_bar_base_ts": None,   # 6pt bar timestamp at moment preorder was placed
        "preorder_status": None,        # None / "PENDING" / "FILLED" / "CANCELLED"

        # =====================================================
        # TRADE MODE + ZONE STATE (UI-driven restrictions)
        # =====================================================
        "trade_mode": None,                 # "SCALP" | "RUNNER" | None
        "entry_zone": None,                 # "NORMAL" | "FREE" | None
        "runner_4pt_unlocked": False,       # True only after 4pt color change after entry
        # "entry_main_renko_ts": None,        # snapshot of 6pt bar ts at entry fill
        # "entry_renko_color": None,          # snapshot of 4pt color at entry fill
        # "entry_main_renko_color": None,      # snapshot of 6pt color at entry fill

        "entry_main_renko_ts": None,         # snapshot of 6pt bar ts at entry fill
        "entry_high_renko_ts": None,         # snapshot of 8pt bar ts at entry fill
        "entry_renko_color": None,           # snapshot of 4pt color at entry fill
        "entry_main_renko_color": None,      # snapshot of 6pt color at entry fill

        # UI helper toggles / state
        "four_pt_invalidation_enabled": False,   # new toggle button state
        # "next_bar_exit_allowed": False,          # computed in /api/state
        # "zone_type": None,                       # computed in /api/state: "NORMAL" | "FREE" | None
        # "four_pt_invalidation_allowed": False,
        "next_bar_exit_allowed": False,           # 6pt next-bar allowed; computed in /api/state
        "high_next_bar_exit_allowed": False,      # 8pt next-bar allowed; computed in /api/state
        "zone_type": None,
        "four_pt_invalidation_allowed": False,
        # =====================================================
        # SCALE-IN STATE
        # =====================================================
        "scale_in_available": False,
        "scale_in_used": False,
        "scale_in_stage": None,          # None / "WAIT_BACK"
        "scale_in_last_ts": None,


        
       

    }

# Config per asset
ASSET_CONFIG = {
    "ES": {"size": 1, "stop_points": 15.0, "breakeven_trigger": 15.0},
    #"GC": {"size": 1, "stop_points": 10.0, "breakeven_trigger": 10.0},
    #"YM": {"size": 1, "stop_points": 80.0, "breakeven_trigger": 80.0},
}

POINT_VALUE_USD = {
    "ES": 50.0,
    #"GC": 10.0,
    #"NQ": 2.0,
    #"YM": 5.0,
}

# =========================================================
# SAFETY STOPS (SERVER-SIDE) DISABLED
# Rithmic must be the only source of truth for exits.
# =========================================================
DISABLE_SERVER_SIDE_SAFETY_STOPS = True

_SENTINEL = object()

def safe_float(value, default=0.0, nonfinite_to=_SENTINEL):
    if value is None:
        return default
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(f):
        return default if nonfinite_to is _SENTINEL else nonfinite_to
    return f

def _point_value_for_contract(contract: str, asset_key: str = None) -> float:
    """
    Returns dollars per point per 1 contract.
    MES = $5/point, ES = $50/point.
    """
    c = (contract or "").upper().strip()

    if c.startswith("MES"):
        return 5.0
    if c.startswith("ES"):
        return 50.0

    return float(POINT_VALUE_USD.get(asset_key or "", 1.0) or 1.0)

def _release_initial_exit_lock_if_needed(asset_key: str):
    """
    Release the initial post-entry exit lock once a fresh 6pt MAIN bar forms
    after the entry's captured 6pt bar timestamp.
    """
    if asset_key not in state["assets"]:
        return

    a = state["assets"][asset_key]
    pos = int(a.get("position", 0) or 0)

    if pos == 0:
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
        now_ts = time.time()
        a["initial_exit_lock_active"] = False
        a["initial_exit_lock_released"] = True
        a["initial_exit_lock_released_ts"] = now_ts


def _update_scale_in_state_from_6pt(asset_key: str, new_color: str, bar_ts):
    """
    New scale-in unlock rule:
    - Only unlock on a fresh 6pt MAIN bar after entry.
    - LONG unlocks only on GREEN 6pt bar.
    - SHORT unlocks only on RED 6pt bar.
    - Once available, it stays available until used or trade closes.
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
        fresh_after_entry = float(bar_ts) > float(entry_main_ts)
    except Exception:
        fresh_after_entry = bar_ts != entry_main_ts

    if not fresh_after_entry:
        return

    target_color = "green" if pos > 0 else "red"

    if new_color == target_color:
        a["scale_in_available"] = True
        a["scale_in_stage"] = "READY_6PT"
        a["scale_in_last_ts"] = time.time()



def _update_scale_in_state_from_4pt(asset_key: str, prev_color: str, new_color: str, color_changed: bool):
    """
    4pt pullback scale-in unlock rule:

    - Must already be in a trade
    - Scale-in must not be used
    - Scale-in must not already be available
    - Must be a real 4pt color change
    - New 4pt color must be OPPOSITE the trade direction

    LONG:  4pt must flip RED
    SHORT: 4pt must flip GREEN

    This makes 4pt scale-in a pullback scale-in.
    6pt scale-in remains the continuation scale-in.
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

    if not color_changed:
        return

    if new_color not in ("green", "red"):
        return

    # LONG pullback = 4pt turns RED
    # SHORT pullback = 4pt turns GREEN
    opposite_color = "red" if pos > 0 else "green"

    if new_color != opposite_color:
        return

    a["scale_in_available"] = True
    a["scale_in_stage"] = "READY_4PT_PULLBACK"
    a["scale_in_last_ts"] = time.time()

def _compute_rithmic_open_points(contract: str, asset_key: str, position: int, open_pnl: float) -> float:
    """
    Converts Rithmic open PnL dollars into points.
    Positive = profitable. Negative = losing.
    """
    qty = abs(int(position or 0))
    if qty <= 0:
        return 0.0

    point_value = _point_value_for_contract(contract, asset_key)
    if point_value <= 0:
        return 0.0

    return float(open_pnl or 0.0) / (qty * point_value)

def maybe_protect_exit(asset_key: str, reason: str = "protect"):
    """
    Auto-exit when protect is enabled and Rithmic-derived open points <= threshold.

    Examples:
    - threshold -2.0 = max protective giveback/loss
    - threshold +0.5 = risk-free / fee-cover protection
    """
    if asset_key not in state["assets"]:
        return False

    a = state["assets"][asset_key]
    pos = int(a.get("position", 0) or 0)

    if pos == 0:
        return False

    if a.get("pending_order_id"):
        return False
    
    # if bool(a.get("initial_exit_lock_active", False)):
    #     return False

    # if not bool(a.get("protect_enabled", False)):
    #     return False

    # threshold = safe_float(a.get("protect_threshold_points"), default=-1.0, nonfinite_to=-1.0)
    if not bool(a.get("protect_enabled", False)):
        return False

    # 2.5pt lock disables protect until the 2.5pt color flips.
    if bool(a.get("two_half_tp_lock_enabled", False)):
        return False

    threshold = safe_float(a.get("protect_threshold_points"), default=-2.0, nonfinite_to=-2.0)
    open_points = safe_float(a.get("rithmic_open_points"), default=0.0, nonfinite_to=0.0)

    # For protection, trigger only at or below threshold, usually -1.0.
    if open_points > threshold:
        return False

    side = "SELL" if pos > 0 else "BUY"
    qty = abs(pos)
    env = (a.get("env") or state["global"].get("env") or "DEMO").upper().strip()

    rith_sym = a.get("rithmic_symbol") or resolve_rithmic_symbol(asset_key)
    rith_exch = resolve_rithmic_exchange(asset_key)

    try:
        order = create_order(
            symbol=rith_sym,
            exchange=rith_exch,
            side=side,
            qty=qty,
            #source="PROTECT_MINUS_ONE",
            source="PROTECT",
            mode=a.get("exit_mode") or "A",
            kind="EXIT",
            env=env,
        )
    except Exception as e:
        print("PROTECT create_order failed:", e)
        return False

    a["pending_order_id"] = order.get("id")
    a["pending_side"] = side
    a["pending_qty"] = qty
    a["pending_mode"] = a.get("exit_mode") or "A"
    a["pending_trade_mode"] = None
    a["protect_hit_ts"] = time.time()

    ORDER_INDEX[order["id"]] = {
        "symbol": asset_key,
        "side": side,
        "qty": qty,
        "mode": a.get("exit_mode") or "A",
        "env": env,
        "kind": "EXIT",
        #"source": "PROTECT_MINUS_ONE",
        "source": "PROTECT",
    }

    try:
        save_asset_state(asset_key, a)
    except Exception as e:
        print(f"Error saving console_state (protect exit) for {asset_key}:", e)

    return True


def maybe_points_take_profit_exit(asset_key: str, reason: str = "take_profit_points"):
    """
    Auto-exit when Rithmic-derived open points >= configured target.
    """
    if asset_key not in state["assets"]:
        return False

    a = state["assets"][asset_key]
    pos = int(a.get("position", 0) or 0)

    if pos == 0:
        return False

    if a.get("pending_order_id"):
        return False
    
    

    if not bool(a.get("points_tp_enabled", False)):
        return False
    # # 2.5pt lock disables point TP until the 2.5pt color flips.
    # if bool(a.get("two_half_tp_lock_enabled", False)):
    #     return False

    # target = safe_float(a.get("points_tp_target"), default=10.0, nonfinite_to=10.0)
    target = safe_float(a.get("points_tp_target"), default=15.0, nonfinite_to=15.0)
    open_points = safe_float(a.get("rithmic_open_points"), default=0.0, nonfinite_to=0.0)

    if target <= 0:
        return False

    if open_points < target:
        return False

    side = "SELL" if pos > 0 else "BUY"
    qty = abs(pos)
    env = (a.get("env") or state["global"].get("env") or "DEMO").upper().strip()

    rith_sym = a.get("rithmic_symbol") or resolve_rithmic_symbol(asset_key)
    rith_exch = resolve_rithmic_exchange(asset_key)

    try:
        order = create_order(
            symbol=rith_sym,
            exchange=rith_exch,
            side=side,
            qty=qty,
            source="TAKE_PROFIT_POINTS",
            mode=a.get("exit_mode") or "A",
            kind="EXIT",
            env=env,
        )
    except Exception as e:
        print("POINTS TAKE PROFIT create_order failed:", e)
        return False

    a["pending_order_id"] = order.get("id")
    a["pending_side"] = side
    a["pending_qty"] = qty
    a["pending_mode"] = a.get("exit_mode") or "A"
    a["pending_trade_mode"] = None
    a["points_tp_hit_ts"] = time.time()

    ORDER_INDEX[order["id"]] = {
        "symbol": asset_key,
        "side": side,
        "qty": qty,
        "mode": a.get("exit_mode") or "A",
        "env": env,
        "kind": "EXIT",
    }

    try:
        save_asset_state(asset_key, a)
    except Exception as e:
        print(f"Error saving console_state (points take profit exit) for {asset_key}:", e)

    return True



# def _compute_zone_from_asset(a: dict):
#     """
#     Zone logic:
#     - 6pt/main + 8pt/high are the base structure.
#     - 12pt/macro adds a higher-context conflict layer.
#     - Any valid color conflict between 6/8/12 = FREE.
#     - If 6 and 8 match, but 12 is opposite, still FREE.
#     - If 12 is neutral/missing, fallback to old 6-vs-8 behavior.
#     """
#     main = (a.get("main_renko_color") or "neutral").lower()    # 6pt
#     high = (a.get("high_renko_color") or "neutral").lower()    # 8pt
#     macro = (a.get("macro_renko_color") or "neutral").lower()  # 12pt

#     valid = ("green", "red")

#     # Need at least 6pt + 8pt to define the base zone.
#     if main not in valid or high not in valid:
#         return None

#     # Old behavior stays if 12pt is not ready yet.
#     if macro not in valid:
#         return "NORMAL" if main == high else "FREE"

#     # New behavior:
#     # if any of 6/8/12 disagree, zone is FREE.
#     if len({main, high, macro}) > 1:
#         return "FREE"

#     # All three aligned.
#     return "NORMAL"

def _compute_zone_from_asset(a: dict):
    """
    Zone logic:
    - 8pt/high + 12pt/macro decide entry structure.
    - 6pt/main is NOT part of entry permission anymore.
    - If 8pt and 12pt agree = NORMAL.
    - If 8pt and 12pt disagree = FREE.
    - If either 8pt or 12pt is neutral/missing = no valid zone.
    """
    high = (a.get("high_renko_color") or "neutral").lower()    # 8pt
    macro = (a.get("macro_renko_color") or "neutral").lower()  # 12pt

    valid = ("green", "red")

    if high not in valid or macro not in valid:
        return None

    if high == macro:
        return "NORMAL"

    return "FREE"



def _direction_to_required_two_half_color(direction: str):
    d = (direction or "").upper().strip()
    if d == "BUY":
        return "green"
    if d == "SELL":
        return "red"
    return None


def _two_half_allows_direction(a: dict, direction: str) -> bool:
    """
    2.5pt immediate filter:
    - BUY requires 2.5pt green.
    - SELL requires 2.5pt red.
    - neutral/missing blocks both.
    """
    required = _direction_to_required_two_half_color(direction)
    if required is None:
        return False

    two_half = (a.get("two_half_renko_color") or "neutral").lower()
    return two_half == required


# def _structure_allows_direction(a: dict, direction: str) -> bool:
#     """
#     Big-structure gate from 6/8/12:
#     - NORMAL zone: only the aligned direction is allowed.
#     - FREE zone: either direction is structurally allowed.
#     - no valid zone: blocked.
#     """
#     direction = (direction or "").upper().strip()
#     if direction not in ("BUY", "SELL"):
#         return False

#     zone = _compute_zone_from_asset(a)
#     main = (a.get("main_renko_color") or "neutral").lower()

#     if zone == "FREE":
#         return True

#     if zone == "NORMAL":
#         if main == "green":
#             return direction == "BUY"
#         if main == "red":
#             return direction == "SELL"

#     return False

def _structure_allows_direction(a: dict, direction: str) -> bool:
    """
    Entry structure gate from 8pt/12pt only:
    - NORMAL zone: direction must match 8pt/12pt.
    - FREE zone: either BUY or SELL is structurally allowed.
    - 6pt/main is ignored for entry permission.
    """
    direction = (direction or "").upper().strip()
    if direction not in ("BUY", "SELL"):
        return False

    zone = _compute_zone_from_asset(a)
    high = (a.get("high_renko_color") or "neutral").lower()  # 8pt

    if zone == "FREE":
        return True

    if zone == "NORMAL":
        if high == "green":
            return direction == "BUY"
        if high == "red":
            return direction == "SELL"

    return False


def _entry_direction_allowed(a: dict, direction: str) -> tuple[bool, str]:
    """
    Final backend entry gate.

    Big structure decides what is structurally possible.
    2.5pt must agree with the chosen direction.
    """
    direction = (direction or "").upper().strip()

    if direction not in ("BUY", "SELL"):
        return False, "Invalid direction"

    zone = _compute_zone_from_asset(a)
    # if zone not in ("NORMAL", "FREE"):
    #     return False, "6/8/12 zone unavailable"

    # if not _structure_allows_direction(a, direction):
    #     return False, "Blocked by 6/8/12 structure"
    if zone not in ("NORMAL", "FREE"):
        return False, "8/12 zone unavailable"

    if not _structure_allows_direction(a, direction):
        return False, "Blocked by 8/12 structure"

    if not _two_half_allows_direction(a, direction):
        two_half = (a.get("two_half_renko_color") or "neutral").lower()
        return False, f"Blocked by 2.5pt Renko: {two_half}"

    return True, ""



# def _release_two_half_tp_lock_if_needed(asset_key: str, prev_color: str, new_color: str, color_changed: bool):
#     """
#     Releases the 2.5 lock on the first opposite 2.5pt flip after entry.

#     LONG  -> release on RED 2.5
#     SHORT -> release on GREEN 2.5

#     Once released, it stays released for the rest of that trade.
#     It does NOT auto-relock if 2.5 realigns with trade direction.
#     """
#     if asset_key not in state["assets"]:
#         return

#     a = state["assets"][asset_key]
#     pos = int(a.get("position", 0) or 0)

#     if pos == 0:
#         return

#     if bool(a.get("two_half_tp_lock_released", False)):
#         return

#     if not color_changed:
#         return

#     if new_color not in ("green", "red"):
#         return

#     release_color = _trade_opposite_two_half_color(pos)
#     if release_color is None:
#         return

#     if new_color != release_color:
#         return

#     a["two_half_tp_lock_enabled"] = False
#     a["two_half_tp_lock_base_color"] = None
#     a["two_half_tp_lock_released"] = True
#     a["two_half_tp_lock_released_ts"] = time.time()


def _release_two_half_tp_lock_if_needed(asset_key: str, prev_color: str, new_color: str, color_changed: bool):
    """
    Dynamic 2.5 management lock.

    LONG:
    - 2.5 green = management locked
    - 2.5 red   = management unlocked

    SHORT:
    - 2.5 red   = management locked
    - 2.5 green = management unlocked

    This does NOT auto-exit.
    It only controls whether management tools are allowed to be clicked.
    Already-armed exits are NOT cancelled by relocking.
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
        # Management unlocked while 2.5 is opposite the trade.
        if bool(a.get("two_half_tp_lock_enabled", False)):
            a["two_half_tp_lock_released_ts"] = now_ts

        a["two_half_tp_lock_enabled"] = False
        a["two_half_tp_lock_base_color"] = None
        a["two_half_tp_lock_released"] = True

        if a.get("two_half_tp_lock_released_ts") is None:
            a["two_half_tp_lock_released_ts"] = now_ts

        return

    if color == aligned_color:
        # Management relocks when 2.5 realigns with the trade.
        if not bool(a.get("two_half_tp_lock_enabled", False)):
            a["two_half_tp_lock_started_ts"] = now_ts

        a["two_half_tp_lock_enabled"] = True
        a["two_half_tp_lock_base_color"] = color
        a["two_half_tp_lock_released"] = False
        return

    # Neutral/unknown = conservative lock while in a trade.
    a["two_half_tp_lock_enabled"] = True
    a["two_half_tp_lock_base_color"] = None
    a["two_half_tp_lock_released"] = False



def _has_new_main_bar_after_entry(a: dict) -> bool:
    """
    True only when a fresh 6pt/main bar has formed AFTER the entry bar snapshot.
    Works even during exit cleanup before/after position resets.
    """
    entry_main_ts = a.get("entry_main_renko_ts")
    current_main_ts = a.get("main_renko_ts")

    if entry_main_ts is None or current_main_ts is None:
        return False

    try:
        return float(current_main_ts) > float(entry_main_ts)
    except Exception:
        return current_main_ts != entry_main_ts
    


def _has_new_high_bar_after_entry(a: dict) -> bool:
    """
    True only when a fresh 8pt/high bar has formed AFTER the entry's captured 8pt timestamp.
    """
    entry_high_ts = a.get("entry_high_renko_ts")
    current_high_ts = a.get("high_renko_ts")

    if entry_high_ts is None or current_high_ts is None:
        return False

    try:
        return float(current_high_ts) > float(entry_high_ts)
    except Exception:
        return current_high_ts != entry_high_ts


def _two_half_management_unlocked(a: dict) -> bool:
    """
    Dynamic management permission.
    True only while 2.5 is opposite the trade direction.
    """
    pos = int(a.get("position", 0) or 0)
    if pos == 0:
        return False

    color = (a.get("two_half_renko_color") or "neutral").lower()
    return color == _trade_opposite_two_half_color(pos)


def _six_next_bar_exit_allowed(a: dict) -> bool:
    """
    6PT NEXT is allowed when:
    - trade is open, and
    - either first fresh 6pt bar has NOT formed yet,
      OR 2.5 management is currently unlocked.
    """
    pos = int(a.get("position", 0) or 0)
    if pos == 0:
        return False

    before_first_fresh_6pt = not _has_new_main_bar_after_entry(a)
    return bool(before_first_fresh_6pt or _two_half_management_unlocked(a))


def _eight_next_bar_exit_allowed(a: dict) -> bool:
    """
    8PT NEXT is allowed when:
    - trade is open, and
    - either first fresh 8pt bar has NOT formed yet,
      OR 2.5 management is currently unlocked.
    """
    pos = int(a.get("position", 0) or 0)
    if pos == 0:
        return False

    before_first_fresh_8pt = not _has_new_high_bar_after_entry(a)
    return bool(before_first_fresh_8pt or _two_half_management_unlocked(a))

# def _next_bar_exits_allowed(a: dict) -> bool:
#     """
#     6pt/8pt next-bar exits are allowed when:
#     - trade is open, and
#     - either first fresh 6pt bar has NOT formed yet,
#       OR the 2.5 lock has released.
#     """
#     pos = int(a.get("position", 0) or 0)
#     if pos == 0:
#         return False

#     before_first_fresh_6pt = not _has_new_main_bar_after_entry(a)
#     two_half_released = bool(a.get("two_half_tp_lock_released", False))

#     return bool(before_first_fresh_6pt or two_half_released)

def _next_bar_exits_allowed(a: dict) -> bool:
    """
    Backward-compatible alias for 6PT NEXT.
    Use _six_next_bar_exit_allowed and _eight_next_bar_exit_allowed directly
    where the distinction matters.
    """
    return _six_next_bar_exit_allowed(a)


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
        print(f"Error saving console_state (clear_intent) for {asset_key}:", e)

# def clear_preorder(asset_key: str, status: str = None):
#     if asset_key not in state["assets"]:
#         return

#     a = state["assets"][asset_key]
#     a["preorder_active"] = False
#     a["preorder_direction"] = None
#     a["preorder_qty"] = 0
#     a["preorder_entry_size_mode"] = None
#     a["preorder_created_ts"] = None
#     a["preorder_bar_base_ts"] = None
#     a["preorder_status"] = status

#     try:
#         save_asset_state(asset_key, a)
#     except Exception as e:
#         print(f"Error saving console_state (clear_preorder) for {asset_key}:", e)

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
        print(f"Error saving console_state (clear_preorder) for {asset_key}:", e)

def safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

def scrub_nonfinite(obj):
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: scrub_nonfinite(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub_nonfinite(v) for v in obj]
    if isinstance(obj, tuple):
        return [scrub_nonfinite(v) for v in obj]
    return obj


def _parse_direction(val):
    """
    Accepts: BUY/SELL, long/short, green/red
    Returns: "BUY" or "SELL" or None
    """
    if val is None:
        return None
    d = str(val).strip().lower()
    if d in ("buy", "long", "green", "g"):
        return "BUY"
    if d in ("sell", "short", "red", "r"):
        return "SELL"
    return None


def _normalize_bar_id(bar_id):
    if bar_id is None:
        return None
    try:
        if bar_id != "":
            return int(float(bar_id))
        return None
    except Exception:
        try:
            return str(bar_id)
        except Exception:
            return None

def hydrate_state_from_firestore():
    # Global
    try:
        global_snapshot = load_global_state()
        if global_snapshot:
            # keep these
            #for key in ("equity", "env", "trade_lock"):

            for key in (
                "equity",
                "env",
                "trade_lock",

                # ✅ daily stop fields
                "daily_stop_enabled",
                "daily_stop_limit_pct",
                "daily_start_equity",
                "daily_stop_date",
                "daily_stop_triggered",
                "daily_stop_triggered_ts",
                "daily_stop_triggered_reason",
                "daily_stop_triggered_equity",
                "daily_stop_triggered_dd_pct",

                 # ✅ daily trade limit fields
                "max_trades_per_day",
                "trades_taken_today",
                "trades_remaining_today",
                "daily_trade_limit_date",
                "five_min_trade_bucket",
                "five_min_trade_lock_active",
                "five_min_trade_lock_remaining_s",
                "post_exit_lock_active",
                "post_exit_lock_started_ts",
                "post_exit_lock_expires_ts",
                "post_exit_lock_remaining_s",
            ):

                if key in global_snapshot:
                    state["global"][key] = global_snapshot[key]

    except Exception as e:
        print("Error loading global console_state:", e)

    # Assets
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

        # Rebuild ORDER_INDEX for any asset that still has a pending order
        pending_id = a.get("pending_order_id")
        # if pending_id:
        #     kind = (a.get("pending_kind") or "").upper().strip()
        #     env = (a.get("env") or state["global"].get("env", "DEMO"))

        #     if not kind:
        if pending_id:
            od = {}
            kind = (a.get("pending_kind") or "").upper().strip()
            env = (a.get("env") or state["global"].get("env", "DEMO"))

            try:
                od = get_order(pending_id) or {}
            except Exception as e:
                print("hydrate: could not fetch order doc:", e)
                od = {}

            if not kind:
                kind = (od.get("kind") or "ENTRY")
                if not a.get("env") and od.get("env"):
                    env = od.get("env")
                # try:
                #     od = get_order(pending_id) or {}
                #     kind = (od.get("kind") or "ENTRY")
                #     if not a.get("env") and od.get("env"):
                #         env = od.get("env")
                # except Exception as e:
                #     print("hydrate: could not fetch order kind:", e)
                #     kind = "ENTRY"

            # ORDER_INDEX[pending_id] = {
            #     "symbol": sym,
            #     "side": a.get("pending_side"),
            #     "qty": a.get("pending_qty") or ASSET_CONFIG.get(sym, {}).get("size", 1),
            #     "mode": a.get("pending_mode"),
            #     "trade_mode": a.get("pending_trade_mode"),
            #     "env": str(env).upper().strip(),
            #     "kind": str(kind).upper().strip(),
            # }
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

    # scrub
    try:
        state["global"] = scrub_nonfinite(state["global"])
        for _sym in list(state["assets"].keys()):
            state["assets"][_sym] = scrub_nonfinite(state["assets"][_sym])
    except Exception as e:
        print("Error scrubbing non-finite values after hydration:", e)

# Run once at import time
hydrate_state_from_firestore()

def check_rithmic_connection():
    return True, "Rithmic connection OK (handled by C# bridge)."


def _ensure_post_exit_lock_initialized():
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


def _activate_post_exit_lock():
    g = state["global"]
    now = time.time()
    expires_ts = now + 180  # 3 minutes

    g["post_exit_lock_active"] = True
    g["post_exit_lock_started_ts"] = now
    g["post_exit_lock_expires_ts"] = expires_ts
    g["post_exit_lock_remaining_s"] = 180

    try:
        save_global_state(g)
    except Exception as e:
        print("Error saving global state (post-exit lock activate):", e)

def append_log(kind: str, entry: dict):
    logs = state["logs"].get(kind)
    if logs is None:
        return
    logs.insert(0, entry)
    if len(logs) > LOG_MAX:
        logs.pop()


def _update_daily_stop_metrics():
    """
    Compute and persist DAILY VISIBILITY metrics.
    This does NOT trigger anything.
    """
    g = state["global"]

    start = g.get("daily_start_equity")
    eq = g.get("rithmic_account_balance")

    if start is None or eq is None:
        return

    try:
        start = float(start)
        eq = float(eq)
    except Exception:
        return

    if start <= 0:
        return

    daily_pnl_usd = eq - start
    daily_pnl_pct = daily_pnl_usd / start
    limit_pct = float(g.get("daily_stop_limit_pct", 0.0)) / 100.0

    remaining_pct = max(0.0, limit_pct + daily_pnl_pct)

    # Persist for observability
    g["daily_pnl_usd"] = daily_pnl_usd
    g["daily_pnl_pct"] = daily_pnl_pct
    g["daily_stop_remaining_pct"] = remaining_pct

    try:
        save_global_state(g)
    except Exception as e:
        print("Error saving daily stop metrics:", e)


def close_trade(asset_key: str, reason: str = ""):
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

    trade_record = {
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
        # tempo debug
      
    }

    state["history"].insert(0, trade_record)


    # ============================================
    # SAME-DIRECTION REENTRY LOCK (NEW RULE)
    # ============================================

    # # Determine direction of the trade we are closing
    # if pos > 0:
    #     # was LONG → block future BUY
    #     a["last_exit_direction"] = "BUY"
    # elif pos < 0:
    #     # was SHORT → block future SELL
    #     a["last_exit_direction"] = "SELL"
    # else:
    #     a["last_exit_direction"] = None

    # # Activate lock
    # a["reentry_lock_active"] = True

    # ============================================
    # SAME-DIRECTION REENTRY LOCK REMOVED
    # Same-direction market re-entry is allowed after exit.
    # Keep other locks/cooldowns untouched.
    # ============================================
    a["last_exit_direction"] = None
    a["reentry_lock_active"] = False

    # Remember whether the closed trade had at least one fresh 6pt bar after entry.
    # This survives while flat so 4pt post-exit unlock can decide correctly.
    a["last_trade_had_new_main_bar_after_entry"] = bool(
        a.get("last_trade_had_new_main_bar_after_entry", False)
        or _has_new_main_bar_after_entry(a)
    )




    # reset
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
    # a["entry_main_renko_ts"] = None
    # a["entry_renko_color"] = None
    # a["entry_main_renko_color"] = None
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
    # a["protect_enabled"] = False
    # a["protect_threshold_points"] = -1.0
    # a["protect_hit_ts"] = None
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
        
    # Full 5-minute post-exit lock stays active after any close_trade path.
    _activate_post_exit_lock()

    try:
        save_asset_state(asset_key, a)
    except Exception as e:
        print(f"Error saving console_state for {asset_key} in close_trade:", e)




def _ny_trading_date_str() -> str:
    """
    Use your existing liquidity_calendar's NY day logic.
    get_today_info() already returns today's NY trading date string.
    """
    try:
        return str(get_today_info().get("date"))
    except Exception:
        return None
    

def _current_5m_bucket() -> int:
    """
    Same 5-minute bucket logic as frontend countdown:
    floor(epoch / 300)
    """
    return int(time.time() // 300)

def _seconds_until_next_5m() -> int:
    now = int(time.time())
    return 300 - (now % 300)

def _ensure_five_min_lock_initialized():
    """
    Keep the 5-minute lock self-healing.
    If stored bucket is from an older candle, clear the active flag.
    """
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

def _activate_five_min_lock():
    g = state["global"]
    bucket = _current_5m_bucket()

    g["five_min_trade_bucket"] = bucket
    g["five_min_trade_lock_active"] = True
    g["five_min_trade_lock_remaining_s"] = _seconds_until_next_5m()

    try:
        save_global_state(g)
    except Exception as e:
        print("Error saving global state (5m lock activate):", e)

def _release_five_min_lock_if_same_bucket():
    """
    Use this when an ENTRY gets REJECTED/CANCELLED, so a failed entry
    does not burn the whole 5-minute candle.
    """
    g = state["global"]
    current_bucket = _current_5m_bucket()

    try:
        stored_bucket = int(g.get("five_min_trade_bucket")) if g.get("five_min_trade_bucket") is not None else None
    except Exception:
        stored_bucket = None

    if stored_bucket == current_bucket:
        g["five_min_trade_bucket"] = None
        g["five_min_trade_lock_active"] = False
        g["five_min_trade_lock_remaining_s"] = 0

        try:
            save_global_state(g)
        except Exception as e:
            print("Error saving global state (5m lock release):", e)




def _ensure_daily_stop_day_initialized():
    g = state["global"]
    today = _ny_trading_date_str()
    if not today:
        return False

    eq = g.get("rithmic_account_balance")

    changed = False

    # New day rollover
    if g.get("daily_stop_date") != today:
        g["daily_stop_date"] = today

        if eq is not None:
            g["daily_start_equity"] = float(eq)

        g["daily_stop_triggered"] = False
        g["daily_stop_triggered_ts"] = None
        g["daily_stop_triggered_reason"] = None
        g["daily_stop_triggered_equity"] = None
        g["daily_stop_triggered_dd_pct"] = None

        # =====================================================
        # DAILY TRADE LIMIT RESET
        # =====================================================
        max_trades = safe_int(g.get("max_trades_per_day"), default=6)
        if max_trades <= 0:
            max_trades = 6

        g["max_trades_per_day"] = max_trades
        g["trades_taken_today"] = 0
        g["trades_remaining_today"] = max_trades
        g["daily_trade_limit_date"] = today

        changed = True

    # Same day, but start equity never got set
    if g.get("daily_stop_date") == today and g.get("daily_start_equity") is None and eq is not None:
        g["daily_start_equity"] = float(eq)
        changed = True

    # Same day, but trade limit fields were never initialized
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
        try:
            save_global_state(g)
        except Exception as e:
            print("Error saving global state (daily stop / trade limit init):", e)
        return True

    return False







def _enqueue_daily_stop_exits(reason: str = "DAILY_STOP"):
    """
    Create EXIT orders for all open positions (no duplicates).
    Uses your existing pending_order_id gate.
    """
    g = state["global"]
    env_global = (g.get("env") or "DEMO").upper().strip()

    for sym, a in state["assets"].items():
        pos = int(a.get("position", 0) or 0)
        if pos == 0:
            continue

        # Don't spam duplicates
        if a.get("pending_order_id"):
            continue

        side = "SELL" if pos > 0 else "BUY"
        qty = abs(pos)

        env = (a.get("env") or env_global).upper().strip()
        rith_sym = resolve_rithmic_symbol(sym)
        rith_exch = resolve_rithmic_exchange(sym)

        try:
            order = create_order(
                symbol=rith_sym,
                exchange=rith_exch,
                side=side,
                qty=qty,
                source=reason,             # e.g. "DAILY_STOP"
                mode=a.get("exit_mode") or "A",
                kind="EXIT",
                env=env,
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
            "symbol": sym,
            "side": side,
            "qty": qty,
            "mode": a.get("exit_mode") or "A",
            "env": env,
            "kind": "EXIT",
        }

        try:
            save_asset_state(sym, a)
        except Exception as e:
            print(f"Error saving asset state (DAILY_STOP exit) for {sym}:", e)


def _maybe_trigger_daily_stop():
    """
    Core rule:
    - equity = rithmic_account_balance (already includes open PnL)
    - start = daily_start_equity
    - drawdown% = (start - equity) / start * 100
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
        start = float(start)
        eq = float(eq)
    except Exception:
        return False

    if start <= 0:
        return False

    dd_pct = (start - eq) / start * 100.0
    limit_pct = safe_float(g.get("daily_stop_limit_pct"), default=0.0, nonfinite_to=0.0)

    if limit_pct <= 0:
        return False

    if dd_pct < limit_pct:
        return False

    # ✅ TRIGGER ONCE (sticky)
    now = time.time()
    g["daily_stop_triggered"] = True
    g["daily_stop_triggered_ts"] = now
    g["daily_stop_triggered_reason"] = f"Drawdown {dd_pct:.2f}% >= limit {limit_pct:.2f}%"
    g["daily_stop_triggered_equity"] = eq
    g["daily_stop_triggered_dd_pct"] = dd_pct

    # hard lock entries
    g["trade_lock"] = True

    try:
        save_global_state(g)
    except Exception as e:
        print("Error saving global state (DAILY_STOP trigger):", e)

    # enqueue exits
    _enqueue_daily_stop_exits(reason="DAILY_STOP")

    append_log("rithmic", {
        "ts": now,
        "type": "daily_stop_trigger",
        "symbol": "RITHMIC",
        "payload": {
            "start_equity": start,
            "equity": eq,
            "dd_pct": dd_pct,
            "limit_pct": limit_pct,
        }
    })

    return True


def maybe_take_profit_exit(asset_key: str, reason: str = "take_profit"):
    """
    If tp_count >= tp_target and position is open, send an EXIT order.
    """
    if asset_key not in state["assets"]:
        return

    a = state["assets"][asset_key]
    pos = int(a.get("position", 0) or 0)
    if pos == 0:
        return

    if a.get("pending_order_id"):
        return
    
    if bool(a.get("initial_exit_lock_active", False)):
        return
    
    # NEW: TP does nothing unless armed
    if not bool(a.get("tp_armed", False)):
        return


    tp_target = safe_int(a.get("tp_target"), default=3)
    tp_count = safe_int(a.get("tp_count"), default=0)

    if tp_target <= 0:
        return

    if tp_count < tp_target:
        return

    side = "SELL" if pos > 0 else "BUY"
    qty = abs(pos)
    env = (a.get("env") or state["global"].get("env") or "DEMO").upper().strip()

    #rith_sym = resolve_rithmic_symbol(asset_key)
    rith_sym = a.get("rithmic_symbol") or resolve_rithmic_symbol(asset_key)
    rith_exch = resolve_rithmic_exchange(asset_key)

    try:
        order = create_order(
            symbol=rith_sym,
            exchange=rith_exch,
            side=side,
            qty=qty,
            source="TAKE_PROFIT",
            mode=a.get("exit_mode") or "A",
            kind="EXIT",
            env=env,
        )
    except Exception as e:
        print("TAKE_PROFIT create_order failed:", e)
        return

    a["pending_order_id"] = order.get("id")
    a["pending_side"] = side
    a["pending_qty"] = qty
    a["pending_mode"] = a.get("exit_mode") or "A"
    a["pending_trade_mode"] = None

    ORDER_INDEX[order["id"]] = {
        "symbol": asset_key,
        "side": side,
        "qty": qty,
        "mode": a.get("exit_mode") or "A",
        "env": env,
        "kind": "EXIT",
    }

    try:
        save_asset_state(asset_key, a)
    except Exception as e:
        print(f"Error saving console_state (take profit exit) for {asset_key}:", e)


@app.route("/")
@login_required
def index():
    return render_template("index.html", assets=ASSETS)

@app.route("/api/rithmic/pnl-snapshot", methods=["POST"])
def rithmic_pnl_snapshot():
    data = request.get_json(force=True, silent=True) or {}

    if data.get("secret") != BRIDGE_HEARTBEAT_SECRET:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    now = time.time()
    state["global"]["rithmic_last_ts"] = now

    state["global"]["rithmic_realized"] = safe_float(
        data.get("realized"), default=0.0, nonfinite_to=0.0
    )
    state["global"]["rithmic_unrealized"] = safe_float(
        data.get("unrealized"), default=0.0, nonfinite_to=0.0
    )

    bal = data.get("accountBalance")
    if bal is None:
        state["global"]["rithmic_account_balance"] = None
    else:
        state["global"]["rithmic_account_balance"] = safe_float(
            bal, default=None, nonfinite_to=None
        )

    if state["global"]["rithmic_account_balance"] is not None:
        state["global"]["equity"] = state["global"]["rithmic_account_balance"]

    state["global"]["open_pnl"] = safe_float(
        state["global"].get("rithmic_unrealized"), default=0.0, nonfinite_to=0.0
    )

    symbols = data.get("symbols", []) or []
    for p in symbols:
        contract = (p.get("symbol") or "").upper()
        exch = (p.get("exchange") or "").upper()

        ui_sym = CONTRACT_TO_UI.get(contract)
        if not ui_sym:
            continue

        a = state["assets"][ui_sym]

        r_pos = safe_int(p.get("position"), default=0)
        r_open = safe_float(p.get("openPnl"), default=0.0, nonfinite_to=0.0)
        r_avg = safe_float(p.get("avgOpenFill"), default=None, nonfinite_to=None)

        a["rithmic_symbol"] = contract
        a["rithmic_exchange"] = exch
        a["rithmic_position"] = r_pos
        a["rithmic_open_pnl"] = r_open
        a["rithmic_avg_open_fill"] = r_avg
        a["rithmic_last_pnl_ts"] = now

        a["position"] = r_pos
        a["avg_price"] = (r_avg if r_pos != 0 else None)
        a["entry_price"] = (r_avg if r_pos != 0 else None)

        # r_open_safe = safe_float(r_open, 0.0)
        # a["pnl"] = r_open_safe
        # a["pnl_points"] = r_open_safe
        r_open_safe = safe_float(r_open, 0.0)
        open_points = _compute_rithmic_open_points(contract, ui_sym, r_pos, r_open_safe)
        point_value = _point_value_for_contract(contract, ui_sym)

        a["pnl"] = r_open_safe
        a["rithmic_open_points"] = open_points
        a["rithmic_point_value"] = point_value

        # Backward-compatible display/history helper:
        # now this is true points, not dollars.
        a["pnl_points"] = open_points

        maybe_points_take_profit_exit(ui_sym, reason="take_profit_points_rithmic_snapshot")
        maybe_protect_exit(ui_sym, reason="protect_rithmic_snapshot")

        try:
            if a["position"] != 0 or a.get("pending_order_id"):
                save_asset_state(ui_sym, a)
        except Exception as e:
            print(f"Error saving console_state (pnl snapshot) for {ui_sym}:", e)

    append_log("rithmic", {"ts": now, "type": "pnl_snapshot", "symbol": "RITHMIC", "payload": data})
    # ✅ Daily stop day init + trigger check
    _ensure_daily_stop_day_initialized()
    _update_daily_stop_metrics()
    _maybe_trigger_daily_stop()

    return jsonify({"ok": True})


@app.route("/api/state")
@login_required
def get_state():
    _ensure_daily_stop_day_initialized()
    _ensure_five_min_lock_initialized()
    _ensure_post_exit_lock_initialized()

    now = time.time()

    if not DISABLE_SERVER_SIDE_SAFETY_STOPS:
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

            pnl_points = safe_float(a.get("pnl_points"), default=0.0, nonfinite_to=0.0)
            cfg = ASSET_CONFIG.get(sym, {})
            breakeven_trigger = cfg.get("breakeven_trigger")

            if (
                breakeven_trigger is not None
                and a.get("stop_loss_price") is not None
                and a.get("stop_loss_status") in (None, "armed")
                and pnl_points >= breakeven_trigger
            ):
                entry_price = a.get("avg_price") or a.get("entry_price")
                if entry_price is not None:
                    a["stop_loss_price"] = entry_price
                    a["stop_loss_status"] = "breakeven"

            stop_price = a.get("stop_loss_price")
            if stop_price is not None:
                if pos > 0 and last_price <= stop_price:
                    close_trade(sym, reason="safety_stop_hit")
                elif pos < 0 and last_price >= stop_price:
                    close_trade(sym, reason="safety_stop_hit")

                    

    # Global
    state["global"]["open_pnl"] = safe_float(
        state["global"].get("rithmic_unrealized"),
        default=0.0,
        nonfinite_to=0.0,
    )

    state["global"]["total_orders"] = sum(a["order_count"] for a in state["assets"].values())

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



    # =====================================================
    # Manual exit gate + backfills + per-asset computed flags
    # =====================================================
    for sym, a in state["assets"].items():         
        

        # Ensure TP fields exist for older snapshots
        if "tp_target" not in a or a.get("tp_target") is None:
            a["tp_target"] = 3
        if "tp_count" not in a or a.get("tp_count") is None:
            a["tp_count"] = 0
        if "tp_armed" not in a or a.get("tp_armed") is None:
            a["tp_armed"] = False


        if "high_next_bar_exit_enabled" not in a or a.get("high_next_bar_exit_enabled") is None:
            a["high_next_bar_exit_enabled"] = False
        if "high_next_bar_exit_started_ts" not in a:
            a["high_next_bar_exit_started_ts"] = None
        if "high_next_bar_exit_base_ts" not in a:
            a["high_next_bar_exit_base_ts"] = None

        if "entry_high_renko_ts" not in a:
            a["entry_high_renko_ts"] = None
        if "high_next_bar_exit_allowed" not in a or a.get("high_next_bar_exit_allowed") is None:
            a["high_next_bar_exit_allowed"] = False

        if "preorder_active" not in a or a.get("preorder_active") is None:     
            a["preorder_active"] = False
        if "preorder_direction" not in a:
            a["preorder_direction"] = None
        if "preorder_qty" not in a or a.get("preorder_qty") is None:
            a["preorder_qty"] = 0
        if "preorder_entry_size_mode" not in a:
            a["preorder_entry_size_mode"] = None
        if "preorder_created_ts" not in a:
            a["preorder_created_ts"] = None
        if "preorder_bar_base_ts" not in a:
            a["preorder_bar_base_ts"] = None
        if "preorder_status" not in a:
            a["preorder_status"] = None

        if "intent_active" not in a or a.get("intent_active") is None:
            a["intent_active"] = False
        if "intent_created_ts" not in a:
            a["intent_created_ts"] = None
        if "intent_bar_base_ts" not in a:
            a["intent_bar_base_ts"] = None
        if "intent_ready_bar_ts" not in a:
            a["intent_ready_bar_ts"] = None
        if "intent_status" not in a:
            a["intent_status"] = None

        if "pending_trade_mode" not in a:
            a["pending_trade_mode"] = None
        if "preorder_trade_mode" not in a:
            a["preorder_trade_mode"] = None

        if "points_tp_enabled" not in a:
            a["points_tp_enabled"] = False
        # if "points_tp_target" not in a or a.get("points_tp_target") is None:
        #     a["points_tp_target"] = 10.0

        if "points_tp_target" not in a or a.get("points_tp_target") is None:
            a["points_tp_target"] = 15.0
        if "rithmic_open_points" not in a or a.get("rithmic_open_points") is None:
            a["rithmic_open_points"] = 0.0
        if "rithmic_point_value" not in a:
            a["rithmic_point_value"] = None
        if "points_tp_hit_ts" not in a:
            a["points_tp_hit_ts"] = None
        # if "protect_enabled" not in a:
        #     a["protect_enabled"] = False
        # if "protect_threshold_points" not in a or a.get("protect_threshold_points") is None:
        #     a["protect_threshold_points"] = -1.0
        # if "protect_hit_ts" not in a:
        #     a["protect_hit_ts"] = None
        if "protect_enabled" not in a:
            a["protect_enabled"] = False
        if "protect_threshold_points" not in a or a.get("protect_threshold_points") is None:
            a["protect_threshold_points"] = -2.0
        if "protect_hit_ts" not in a:
            a["protect_hit_ts"] = None


                # 2.5pt Renko backfills for older Firestore snapshots
        if "two_half_renko_color" not in a or a.get("two_half_renko_color") is None:
            a["two_half_renko_color"] = "neutral"
        if "two_half_renko_ts" not in a:
            a["two_half_renko_ts"] = None
        if "two_half_color_changed" not in a or a.get("two_half_color_changed") is None:
            a["two_half_color_changed"] = False

        # 2.5pt TP/protect lock backfills
        if "two_half_tp_lock_enabled" not in a or a.get("two_half_tp_lock_enabled") is None:
            a["two_half_tp_lock_enabled"] = False
        if "two_half_tp_lock_base_color" not in a:
            a["two_half_tp_lock_base_color"] = None
        if "two_half_tp_lock_released" not in a or a.get("two_half_tp_lock_released") is None:
            a["two_half_tp_lock_released"] = False
        if "two_half_tp_lock_started_ts" not in a:
            a["two_half_tp_lock_started_ts"] = None
        if "two_half_tp_lock_released_ts" not in a:
            a["two_half_tp_lock_released_ts"] = None

        # # New protect default
        # if "protect_threshold_points" not in a or a.get("protect_threshold_points") is None:
        #     a["protect_threshold_points"] = -2.0


        # manual_exit_allowed no longer depends on 2pt
        a["manual_exit_allowed"] = True



        # Same-direction re-entry lock is removed.
        # Force-clear stale Firestore values from older versions.
        a["reentry_lock_active"] = False
        a["last_exit_direction"] = None

        main = (a.get("main_renko_color") or "neutral").lower()    # 6pt
        high = (a.get("high_renko_color") or "neutral").lower()    # 8pt
        macro = (a.get("macro_renko_color") or "neutral").lower()  # 12pt

        if "trade_mode" not in a:
            a["trade_mode"] = None
        if "entry_zone" not in a:
            a["entry_zone"] = None
        if "runner_4pt_unlocked" not in a:
            a["runner_4pt_unlocked"] = False
        if "entry_main_renko_ts" not in a:
            a["entry_main_renko_ts"] = None
        if "entry_renko_color" not in a:
            a["entry_renko_color"] = None

        if "entry_main_renko_color" not in a:
            a["entry_main_renko_color"] = None
        if "four_pt_invalidation_enabled" not in a:
            a["four_pt_invalidation_enabled"] = False
        if "next_bar_exit_allowed" not in a:
            a["next_bar_exit_allowed"] = False
        if "zone_type" not in a:
            a["zone_type"] = None
        if "four_pt_invalidation_allowed" not in a:
            a["four_pt_invalidation_allowed"] = False

        if "scale_in_available" not in a:
            a["scale_in_available"] = False
        if "scale_in_used" not in a:
            a["scale_in_used"] = False
        if "scale_in_stage" not in a:
            a["scale_in_stage"] = None
        if "scale_in_last_ts" not in a:
            a["scale_in_last_ts"] = None

        if "last_exit_ts" not in a:
            a["last_exit_ts"] = None

        # =====================================================
        # 1PT RENKO VISUAL-ONLY BACKFILLS
        # =====================================================
        if "one_renko_color" not in a or a.get("one_renko_color") is None:
            a["one_renko_color"] = "neutral"
        if "one_renko_ts" not in a:
            a["one_renko_ts"] = None
        if "one_renko_color_changed" not in a or a.get("one_renko_color_changed") is None:
            a["one_renko_color_changed"] = False
        # if "tempo_4pt_unlock_ts" not in a:
        #     a["tempo_4pt_unlock_ts"] = None

        # if "macro_renko_color" not in a or a.get("macro_renko_color") is None:
        #     a["macro_renko_color"] = "neutral"
        # if "macro_renko_ts" not in a:
        #     a["macro_renko_ts"] = None
        if "last_exit_ts" not in a:
            a["last_exit_ts"] = None
        if "tempo_4pt_unlock_ts" not in a:
            a["tempo_4pt_unlock_ts"] = None

        # =====================================================
        # INITIAL EXIT LOCK + POST-EXIT 4PT REARM BACKFILLS
        # =====================================================
        if "last_trade_had_new_main_bar_after_entry" not in a or a.get("last_trade_had_new_main_bar_after_entry") is None:
            a["last_trade_had_new_main_bar_after_entry"] = False

        if "initial_exit_lock_active" not in a or a.get("initial_exit_lock_active") is None:
            a["initial_exit_lock_active"] = False
        if "initial_exit_lock_released" not in a or a.get("initial_exit_lock_released") is None:
            a["initial_exit_lock_released"] = False
        if "initial_exit_lock_started_ts" not in a:
            a["initial_exit_lock_started_ts"] = None
        if "initial_exit_lock_released_ts" not in a:
            a["initial_exit_lock_released_ts"] = None
        if "initial_exit_lock_base_main_ts" not in a:
            a["initial_exit_lock_base_main_ts"] = None

        # Self-heal: if a fresh 6pt bar already arrived, release the lock.
        _release_initial_exit_lock_if_needed(sym)

        if "macro_renko_color" not in a or a.get("macro_renko_color") is None:
            a["macro_renko_color"] = "neutral"


      
        zone_type = _compute_zone_from_asset(a)
        a["zone_type"] = zone_type

        a["conflict_mode"] = (zone_type == "FREE")

        
        
        pos = int(a.get("position", 0) or 0)

        
        # pos = int(a.get("position", 0) or 0)
        # initial_lock_active = bool(a.get("initial_exit_lock_active", False))

        # if pos == 0:
        #     a["next_bar_exit_allowed"] = False
        #     a["four_pt_invalidation_allowed"] = False
        # else:
        #     # Manual/normal exit is blocked during initial lock.
        #     a["next_bar_exit_allowed"] = not initial_lock_active

        #     # 4pt invalidation is structural/emergency.
        #     # It stays available immediately after entry.
        #     a["four_pt_invalidation_allowed"] = True
        # if pos == 0:
        #     a["next_bar_exit_allowed"] = False
        #     a["four_pt_invalidation_allowed"] = False
        # else:
        #     # NEXT BAR is a structural/manual planned exit.
        #     # It should be available immediately after entry.
        #     a["next_bar_exit_allowed"] = True

        #     # 4pt invalidation is also structural/emergency.
        #     # It should be available immediately after entry.
        #     a["four_pt_invalidation_allowed"] = True

        # if pos == 0:
        #     a["next_bar_exit_allowed"] = False
        #     a["four_pt_invalidation_allowed"] = False
        # else:
        #     # 6pt/8pt next-bar exits:
        #     # available before first fresh 6pt bar OR after 2.5 release.
        #     a["next_bar_exit_allowed"] = _next_bar_exits_allowed(a)

        #     # 4pt invalidation is structural/emergency.
        #     # It stays available immediately after entry.
        #     a["four_pt_invalidation_allowed"] = True

        if pos == 0:
            a["next_bar_exit_allowed"] = False
            a["high_next_bar_exit_allowed"] = False
            a["four_pt_invalidation_allowed"] = False
        else:
            # Keep the dynamic 2.5 lock self-healing even if a webhook was missed.
            _release_two_half_tp_lock_if_needed(
                sym,
                a.get("two_half_renko_color"),
                a.get("two_half_renko_color"),
                False,
            )

            # Separate windows:
            # 6PT NEXT depends on first fresh 6pt bar.
            # 8PT NEXT depends on first fresh 8pt bar.
            a["next_bar_exit_allowed"] = _six_next_bar_exit_allowed(a)
            a["high_next_bar_exit_allowed"] = _eight_next_bar_exit_allowed(a)

            # 4pt invalidation is structural/emergency.
            # It stays available immediately after entry.
            a["four_pt_invalidation_allowed"] = True

    
    # =====================================================
    # TEMPO TOKEN (one bar = one trade)
    # Source: ES main_renko (6pt)
    # - READY if never spent OR if a NEW bar arrived after spent
    # - NOT time based (no 90s window)
    # =====================================================
    tempo_source = state["assets"].get("ES", {})
    tempo_color = (tempo_source.get("main_renko_color") or "neutral").lower()
    tempo_bar_ts = tempo_source.get("main_renko_ts")  # bar timestamp

    for sym, a in state["assets"].items():
        #if sym not in ("ES", "YM"):
        if sym not in ("ES"):

            continue

        a["tempo_color"] = tempo_color
        a["tempo_ts"] = tempo_bar_ts
        a["tempo_last_bar_ts"] = tempo_bar_ts

        if tempo_bar_ts is None:
            a["tempo_ready"] = False
            a["tempo_age_s"] = None
            continue

        a["tempo_age_s"] = now - float(tempo_bar_ts)

        # spent_ts = a.get("tempo_spent_ts")

        # # never spent -> first bar makes it ready
        # if spent_ts is None:
        #     a["tempo_ready"] = True
        # else:
        #     # re-arm only if this bar is newer than when we spent
        #     a["tempo_ready"] = float(tempo_bar_ts) > float(spent_ts)

        spent_ts = a.get("tempo_spent_ts")

        # never spent -> first bar makes it ready
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
                # Original rule: new 6pt bar after spent token re-arms tempo.
                try:
                    main_rearmed = float(tempo_bar_ts) > spent_f
                except Exception:
                    main_rearmed = False

                # New rule: 4pt color change after exit can also re-arm tempo.
                fourpt_ts = a.get("tempo_4pt_unlock_ts")
                if fourpt_ts is not None:
                    try:
                        fourpt_rearmed = float(fourpt_ts) > spent_f
                    except Exception:
                        fourpt_rearmed = False

            a["tempo_ready"] = bool(main_rearmed or fourpt_rearmed)



    # =====================================================
    # DATE + LOW LIQUIDITY (Trading day = New York time)
    # =====================================================
    today = get_today_info()
    state["global"]["today_date"] = today["date"]
    state["global"]["low_liquidity_today"] = today["low_liquidity"]
    state["global"]["low_liquidity_reason"] = today["reason"]

    # For UI calendar highlighting
    state["global"]["low_liquidity_days"] = LOW_LIQUIDITY_DAYS

   

    state["global"]["manual_pnl_usd"] = None  # no longer used

    for sym, a in state["assets"].items():
        a["computed_entry_qty"] = FIXED_ENTRY_QTY

    _ensure_five_min_lock_initialized()

    return jsonify(scrub_nonfinite(state))


def _is_free_zone_opposite_6pt_entry(a: dict) -> bool:
    """
    True when the trade was entered in FREE zone and the position direction
    is opposite to the 6pt/main Renko color at entry.
    """
    pos = int(a.get("position", 0) or 0)
    if pos == 0:
        return False

    entry_zone = (a.get("entry_zone") or "").upper()
    if entry_zone != "FREE":
        return False

    entry_main_color = (a.get("entry_main_renko_color") or "").lower()

    if entry_main_color == "green" and pos < 0:
        return True

    if entry_main_color == "red" and pos > 0:
        return True

    return False


def _consume_trade_limit_on_fill():
    g = state["global"]

    _ensure_daily_stop_day_initialized()

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

    try:
        save_global_state(g)
    except Exception as e:
        print("Error saving global state (trade limit consume on fill):", e)
        return False

    return True

@app.route("/api/trade-limit/consume", methods=["POST"])
@login_required
def api_trade_limit_consume():
    g = state["global"]

    # make sure today's date is initialized before consuming
    _ensure_daily_stop_day_initialized()
    ok = _consume_trade_limit_on_fill()
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


@app.route("/api/exit-all", methods=["POST"])
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
                symbol=rith_sym,
                exchange=rith_exch,
                side=side,
                qty=qty,
                source=("MANUAL_EXIT_ALL_FORCE" if force else "MANUAL_EXIT_ALL"),
                mode=a.get("exit_mode") or "A",
                kind="EXIT",
                env=env,
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
            "symbol": asset_key,
            "side": side,
            "qty": qty,
            "mode": a.get("exit_mode") or "A",
            "env": env,
            "kind": "EXIT",
        }

        try:
            save_asset_state(asset_key, a)
        except Exception as e:
            print(f"Error saving console_state (manual exit all) for {asset_key}:", e)

        created_orders.append({
            "asset": asset_key,
            "order_id": order.get("id"),
            "side": side,
            "qty": qty,
        })

    if not created_orders:
        return jsonify({
            "ok": False,
            "error": "No open positions available to exit",
            "skipped": skipped,
        }), 409

    return jsonify({
        "ok": True,
        "orders": created_orders,
        "skipped": skipped,
    }), 201


@app.route("/api/env", methods=["POST"])
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
        ok, msg = check_rithmic_connection()
        if not ok:
            return jsonify({"ok": False, "error": msg or "Rithmic connection failed – cannot enable LIVE."}), 503
        state["global"]["env"] = "LIVE"
        message = msg or "Live mode enabled (execution via C# Rithmic bridge)."

    try:
        save_global_state(state["global"])
    except Exception as e:
        print("Error saving global console_state:", e)

    return jsonify({
        "ok": True,
        "env": state["global"]["env"],
        "rithmic_connected": state["global"]["rithmic_connected"],
        "message": message,
    })

@app.route("/api/order", methods=["POST"])
@login_required
def place_order():
    return jsonify({"ok": False, "error": "Legacy /api/order is DISABLED. Use /api/orders instead."}), 410



# =========================================================
# tv_renko_macro = MACRO direction stream (12pt)
# Used only for zone/free-zone logic.
# Does NOT trigger exits.
# =========================================================
@app.route("/tv_renko_macro", methods=["POST"])
def tv_renko_macro():
    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception:
        payload = {}

    if payload.get("secret") != TV_WEBHOOK_SECRET:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    raw_asset = (payload.get("asset") or payload.get("symbol") or "").upper()
    asset_key = raw_asset.split(":")[-1] if raw_asset else ""

    if asset_key not in state["assets"]:
        return jsonify({"ok": False, "error": f"Unknown asset '{asset_key}'"}), 400

    color = (payload.get("color") or "neutral").lower()
    if color not in ("green", "red", "neutral"):
        color = "neutral"

    now = time.time()
    a = state["assets"][asset_key]

    a["macro_renko_color"] = color
    a["macro_renko_ts"] = now

    # Keep zone fields fresh immediately after 12pt updates.
    a["zone_type"] = _compute_zone_from_asset(a)
    a["conflict_mode"] = (a["zone_type"] == "FREE")

    try:
        save_asset_state(asset_key, a)
    except Exception as e:
        print("Error saving asset console_state (tv_renko_macro):", e)

    append_log("tradingview", {
        "ts": now,
        "type": "renko_macro",
        "asset": asset_key,
        "payload": payload,
    })

    return jsonify({
        "ok": True,
        "asset": asset_key,
        "macro_renko_color": color,
        "macro_renko_ts": now,
        "zone_type": a.get("zone_type"),
        "conflict_mode": a.get("conflict_mode"),
    })


# =========================================================
# tv_renko_two_half = 2.5pt per-asset signal
# Used for entry filtering + TP/protect lock release.
# Does NOT trigger exits.
# =========================================================
@app.route("/tv_renko_two_half", methods=["POST"])
def tv_renko_two_half():
    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception:
        payload = {}

    if payload.get("secret") != TV_WEBHOOK_SECRET:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    raw_asset = (payload.get("asset") or payload.get("symbol") or "").upper()
    asset_key = raw_asset.split(":")[-1] if raw_asset else ""

    if asset_key not in state["assets"]:
        return jsonify({"ok": False, "error": f"Unknown asset '{asset_key}'"}), 400

    color = (payload.get("color") or "neutral").lower()
    if color not in ("green", "red", "neutral"):
        color = "neutral"

    now = time.time()
    a = state["assets"][asset_key]

    prev_color = (a.get("two_half_renko_color") or "neutral").lower()
    new_color = color
    color_changed = (
        prev_color in ("green", "red")
        and new_color in ("green", "red")
        and prev_color != new_color
    )

    a["two_half_renko_color"] = new_color
    a["two_half_renko_ts"] = now
    a["two_half_color_changed"] = color_changed

    # If the user activated the 2.5pt TP/protect lock, release it only on flip.
    _release_two_half_tp_lock_if_needed(asset_key, prev_color, new_color, color_changed)

    try:
        save_asset_state(asset_key, a)
    except Exception as e:
        print("Error saving asset console_state (tv_renko_two_half):", e)

    append_log("tradingview", {
        "ts": now,
        "type": "renko_two_half",
        "asset": asset_key,
        "payload": payload,
    })

    return jsonify({
        "ok": True,
        "asset": asset_key,
        "two_half_renko_color": a.get("two_half_renko_color"),
        "two_half_renko_ts": a.get("two_half_renko_ts"),
        "two_half_color_changed": a.get("two_half_color_changed"),
        "two_half_tp_lock_enabled": a.get("two_half_tp_lock_enabled"),
        "two_half_tp_lock_released": a.get("two_half_tp_lock_released"),
    })


# @app.route("/api/two-half-tp-lock", methods=["POST"])
# @login_required
# def api_two_half_tp_lock():
#     payload = request.get_json(force=True, silent=True) or {}
#     asset_key = (payload.get("asset") or "").upper().strip()

#     if asset_key not in state["assets"]:
#         return jsonify({"ok": False, "error": "Unknown asset"}), 400

#     a = state["assets"][asset_key]
#     pos = int(a.get("position", 0) or 0)

#     if pos == 0:
#         return jsonify({"ok": False, "error": "No open position"}), 409

#     enabled = payload.get("enabled", None)
#     if enabled is None:
#         enabled = not bool(a.get("two_half_tp_lock_enabled", False))

#     enabled = bool(enabled)

#     # if enabled:
#     #     base_color = (a.get("two_half_renko_color") or "neutral").lower()
#     #     if base_color not in ("green", "red"):
#     #         return jsonify({
#     #             "ok": False,
#     #             "error": "2.5pt Renko color unavailable"
#     #         }), 409

#     #     a["two_half_tp_lock_enabled"] = True
#     if enabled:
#         base_color = (a.get("two_half_renko_color") or "neutral").lower()
#         if base_color not in ("green", "red"):
#             return jsonify({
#                 "ok": False,
#                 "error": "2.5pt Renko color unavailable"
#             }), 409

#         required_color = "green" if pos > 0 else "red"

#         if base_color != required_color:
#             return jsonify({
#                 "ok": False,
#                 "error": (
#                     "Cannot activate 2.5 lock: already opposite to trade "
#                     f"({base_color.upper()})"
#                 )
#             }), 409

#         a["two_half_tp_lock_enabled"] = True
#         a["two_half_tp_lock_base_color"] = base_color
#         a["two_half_tp_lock_released"] = False
#         a["two_half_tp_lock_started_ts"] = time.time()
#         a["two_half_tp_lock_released_ts"] = None

#         # Critical: activating this lock force-disarms TP/protect.
#         a["points_tp_enabled"] = False
#         a["protect_enabled"] = False

#     else:
#         a["two_half_tp_lock_enabled"] = False
#         a["two_half_tp_lock_base_color"] = None
#         a["two_half_tp_lock_released"] = False
#         a["two_half_tp_lock_released_ts"] = None

#     try:
#         save_asset_state(asset_key, a)
#     except Exception as e:
#         print(f"Error saving console_state (two_half_tp_lock) for {asset_key}:", e)
#         return jsonify({"ok": False, "error": "Failed to save 2.5pt lock state"}), 500

#     return jsonify({
#         "ok": True,
#         "asset": asset_key,
#         "two_half_tp_lock_enabled": bool(a.get("two_half_tp_lock_enabled", False)),
#         "two_half_tp_lock_base_color": a.get("two_half_tp_lock_base_color"),
#         "two_half_tp_lock_released": bool(a.get("two_half_tp_lock_released", False)),
#         "points_tp_enabled": bool(a.get("points_tp_enabled", False)),
#         "protect_enabled": bool(a.get("protect_enabled", False)),
#         "state": scrub_nonfinite(state),
#     })


@app.route("/api/two-half-tp-lock", methods=["POST"])
@login_required
def api_two_half_tp_lock():
    return jsonify({
        "ok": False,
        "error": "Manual 2.5 lock is disabled. 2.5 lock is automatic only."
    }), 423



@app.route("/tv_heartbeat", methods=["POST"])
def tv_heartbeat():
    data = request.get_json(force=True, silent=True) or {}

    if data.get("secret") != TV_WEBHOOK_SECRET:
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
        print(f"Error saving console_state (heartbeat) for {sym}:", e)

    return jsonify({"ok": True})

# =========================================================
# tv_renko = 4pt per-asset signal
# =========================================================
@app.route("/tv_renko", methods=["POST"])
def tv_renko():
    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception:
        payload = {}

    if payload.get("secret") != TV_WEBHOOK_SECRET:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    raw_asset = (payload.get("asset") or payload.get("symbol") or "").upper()
    asset_key = raw_asset.split(":")[-1]

    color = (payload.get("color") or "neutral").lower()
    if color not in ("green", "red", "neutral"):
        color = "neutral"

    if asset_key not in state["assets"]:
        return jsonify({"ok": False, "error": f"Unknown asset '{asset_key}'"}), 400

    a = state["assets"][asset_key]
    now = time.time()

    # prev_color = (a.get("renko_color") or "neutral").lower()
    # new_color = color

    # a["renko_color"] = new_color
    # a["last_renko_ts"] = now
    prev_color = (a.get("renko_color") or "neutral").lower()
    new_color = color

    a["renko_color"] = new_color
    a["last_renko_ts"] = now

    # Track whether a real 4pt color change occurred
    color_changed = (
        prev_color in ("green", "red")
        and new_color in ("green", "red")
        and prev_color != new_color
    )
    # a["color_changed"] = color_changed
    # _update_scale_in_state_from_4pt(asset_key, prev_color, new_color, color_changed)
    # a["color_changed"] = color_changed
    # # Scale-in is no longer unlocked by 4pt color changes.

    a["color_changed"] = color_changed

    # 4pt color change can now unlock scale-in.
    _update_scale_in_state_from_4pt(asset_key, prev_color, new_color, color_changed)
    # 4pt color change after exit can also re-arm tempo/re-entry permission.
    # if color_changed and int(a.get("position", 0) or 0) == 0:
    #     last_exit_ts = a.get("last_exit_ts")
    #     if last_exit_ts is not None:
    #         try:
    #             if now > float(last_exit_ts):
    #                 a["tempo_4pt_unlock_ts"] = now
    #         except Exception:
    #             a["tempo_4pt_unlock_ts"] = now

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
    


    # # RUNNER unlock:
    # # once a real 4pt color change happens during an open trade,
    # # next-bar exit becomes allowed for RUNNER trades
    # if int(a.get("position", 0) or 0) != 0:
    #     if (a.get("trade_mode") or "").upper() == "RUNNER" and color_changed:
    #         a["runner_4pt_unlocked"] = True

    # Unlock NEXT BAR only after:
    # 1) a fresh 6pt/main bar formed after entry
    # 2) then a real 4pt flip happens AGAINST the trade direction
    if int(a.get("position", 0) or 0) != 0 and _has_new_main_bar_after_entry(a):
        pos = int(a.get("position", 0) or 0)

        opposite_4pt_flip = (
            (pos > 0 and color_changed and new_color == "red") or
            (pos < 0 and color_changed and new_color == "green")
        )

        if opposite_4pt_flip:
            a["runner_4pt_unlocked"] = True

    # =========================================================
    # 4PT INVALIDATION EXIT LOGIC
    # Real auto-exit when toggle is ON
    # =========================================================
    pos = int(a.get("position", 0) or 0)
    exit_mode = a.get("exit_mode")
    four_pt_enabled = bool(a.get("four_pt_invalidation_enabled"))

    #if pos != 0 and exit_mode == "A" and four_pt_enabled and _has_new_main_bar_after_entry(a):
    #if pos != 0 and exit_mode == "A" and four_pt_enabled:
    # if (
    #         pos != 0
    #         and exit_mode == "A"
    #         and four_pt_enabled
    #         and not bool(a.get("initial_exit_lock_active", False))
    #     ):
    if (
            pos != 0
            and exit_mode == "A"
            and four_pt_enabled
        ):
        opposite_signal = (
            (pos > 0 and new_color == "red") or
            (pos < 0 and new_color == "green")
        )

        # require a real color flip, not just repeated same-color webhook
        #if color_changed and opposite_signal:
        if opposite_signal:
            if a.get("pending_order_id"):
                print(f"[RENKO_4PT EXIT] skip: pending exists for {asset_key}")
            else:
                side = "SELL" if pos > 0 else "BUY"
                qty = abs(pos)
                env = (state["global"].get("env") or "DEMO").upper()

                rith_sym = a.get("rithmic_symbol") or resolve_rithmic_symbol(asset_key)
                rith_exch = resolve_rithmic_exchange(asset_key)

                try:
                    order = create_order(
                        symbol=rith_sym,
                        exchange=rith_exch,
                        side=side,
                        qty=qty,
                        source="EXIT_ENGINE_4PT",
                        mode=exit_mode,
                        kind="EXIT",
                        env=env,
                    )
                except Exception as e:
                    print("Error creating EXIT Firestore order (renko_4pt):", e)
                    close_trade(asset_key, reason="renko_4pt_flip_fallback")
                else:
                    a["pending_order_id"] = order.get("id")
                    a["pending_side"] = side
                    a["pending_qty"] = qty
                    a["pending_mode"] = exit_mode
                    a["pending_trade_mode"] = None

                    ORDER_INDEX[order["id"]] = {
                        "symbol": asset_key,
                        "side": side,
                        "qty": qty,
                        "mode": exit_mode,
                        "env": env,
                        "kind": "EXIT",
                    }

    

    # try:
    #     if a.get("position", 0) != 0 or a.get("pending_order_id"):
    #         save_asset_state(asset_key, a)
    # except Exception as e:
    #     print(f"Error saving console_state (renko) for {asset_key}:", e)
    try:
        if (
            a.get("position", 0) != 0
            or a.get("pending_order_id")
            or a.get("tempo_4pt_unlock_ts") is not None
        ):
            save_asset_state(asset_key, a)
    except Exception as e:
        print(f"Error saving console_state (renko) for {asset_key}:", e)

    append_log("tradingview", {"ts": now, "type": "renko", "asset": asset_key, "payload": payload})
    return jsonify({"ok": True})




# =========================================================
# tv_level_exit = horizontal level exit trigger
# =========================================================
@app.route("/tv_level_exit", methods=["POST"])
def tv_level_exit():
    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception:
        payload = {}

    # --- security ---
    if payload.get("secret") != TV_WEBHOOK_SECRET:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    raw_asset = (payload.get("asset") or payload.get("symbol") or "").upper()
    asset_key = raw_asset.split(":")[-1]

    if asset_key not in state["assets"]:
        return jsonify({"ok": False, "error": f"Unknown asset '{asset_key}'"}), 400

    a = state["assets"][asset_key]
    pos = int(a.get("position", 0) or 0)

    # -------------------------------------------------
    # CASE 1: no position → ignore safely
    # -------------------------------------------------
    if pos == 0:
        return jsonify({"ok": True, "ignored": "no_position"})

    # -------------------------------------------------
    # CASE 2: already exiting → ignore
    # -------------------------------------------------
    if a.get("pending_order_id"):
        return jsonify({"ok": True, "ignored": "pending_exit_exists"})

    side = "SELL" if pos > 0 else "BUY"
    qty = abs(pos)

    env = (a.get("env") or state["global"].get("env") or "DEMO").upper().strip()

    #rith_sym = resolve_rithmic_symbol(asset_key)
    rith_sym = a.get("rithmic_symbol") or resolve_rithmic_symbol(asset_key)
    rith_exch = resolve_rithmic_exchange(asset_key)

    try:
        order = create_order(
            symbol=rith_sym,
            exchange=rith_exch,
            side=side,
            qty=qty,
            source="TV_LEVEL_EXIT",
            mode=a.get("exit_mode") or "A",
            kind="EXIT",
            env=env,
        )
    except Exception as e:
        print("TV_LEVEL_EXIT create_order failed:", e)
        return jsonify({"ok": False})

    # register pending exit
    a["pending_order_id"] = order.get("id")
    a["pending_side"] = side
    a["pending_qty"] = qty
    a["pending_mode"] = a.get("exit_mode") or "A"
    a["pending_trade_mode"] = None

    ORDER_INDEX[order["id"]] = {
        "symbol": asset_key,
        "side": side,
        "qty": qty,
        "mode": a.get("exit_mode") or "A",
        "env": env,
        "kind": "EXIT",
    }

    try:
        save_asset_state(asset_key, a)
    except Exception as e:
        print("Error saving asset state (TV_LEVEL_EXIT):", e)

    append_log("tradingview", {
        "ts": time.time(),
        "type": "tv_level_exit",
        "asset": asset_key,
        "payload": payload,
    })

    return jsonify({"ok": True, "action": "exit_order_created"})



@app.route("/api/protect", methods=["POST"])
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
    
    # if bool(a.get("two_half_tp_lock_enabled", False)):
    #     return jsonify({
    #         "ok": False,
    #         "error": "Protect locked until 2.5pt Renko flips"
    #     }), 423

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

        # Keep this protective. Do not allow positive protect thresholds.
        # if threshold > 0 or threshold < -50:
        #     return jsonify({"ok": False, "error": "Protect threshold must be between -50 and 0 points"}), 400
        # Allow only the protective presets we actually use:
        # -2.0 = loss/giveback protect
        # +0.5 = risk-free / fee-cover protect
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
        print(f"Error saving console_state (protect toggle) for {asset_key}:", e)
        return jsonify({"ok": False, "error": "Failed to save protect state"}), 500

    # Important: if user turns it on while already <= -1, exit immediately.
    # If current is +2 or 0, this does nothing.
    maybe_protect_exit(asset_key, reason="protect_manual_toggle")

    return jsonify({
        "ok": True,
        "asset": asset_key,
        "protect_enabled": bool(a.get("protect_enabled", False)),
        "protect_threshold_points": a.get("protect_threshold_points"),
        "rithmic_open_points": a.get("rithmic_open_points"),
        "pending_order_id": a.get("pending_order_id"),
        "state": scrub_nonfinite(state),
    })


@app.route("/api/points-take-profit", methods=["POST"])
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
    
    # if bool(a.get("two_half_tp_lock_enabled", False)):
    #     return jsonify({
    #         "ok": False,
    #         "error": "Point TP locked until 2.5pt Renko flips"
    #     }), 423

    # enabled = payload.get("enabled", True)
    # a["points_tp_enabled"] = bool(enabled)
    # enabled = payload.get("enabled", a.get("points_tp_enabled", False))
    # a["points_tp_enabled"] = bool(enabled)

    # value = payload.get("target", payload.get("points_tp_target", None))
    # # TP ON/OFF is always allowed.
    # # TP target changes are management changes, so they remain locked
    # # while 2.5 management is locked.
    # if value is not None and bool(a.get("two_half_tp_lock_enabled", False)):
    #     return jsonify({
    #         "ok": False,
    #         "error": "TP target changes locked until 2.5pt management unlocks"
    #     }), 423

    enabled = payload.get("enabled", a.get("points_tp_enabled", False))
    value = payload.get("target", payload.get("points_tp_target", None))

    # TP ON/OFF is always allowed.
    # TP target changes are management changes, so they remain locked
    # while 2.5 management is locked.
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
        print(f"Error saving console_state (points TP set) for {asset_key}:", e)
        return jsonify({"ok": False, "error": "Failed to save points TP"}), 500

    # Important: if already above target, exit immediately after user changes it.
    maybe_points_take_profit_exit(asset_key, reason="take_profit_points_manual_update")

    return jsonify({
        "ok": True,
        "asset": asset_key,
        "points_tp_enabled": bool(a.get("points_tp_enabled", False)),
        "points_tp_target": a.get("points_tp_target"),
        "rithmic_open_points": a.get("rithmic_open_points"),
        "pending_order_id": a.get("pending_order_id"),
        "state": scrub_nonfinite(state),
    })



@app.route("/api/high-next-bar-exit", methods=["POST"])
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

    # if new_val and not _next_bar_exits_allowed(a):
    #     return jsonify({
    #         "ok": False,
    #         "error": "8pt next-bar exit locked until 2.5pt Renko flips"
    #     }), 423

    if new_val and not _eight_next_bar_exit_allowed(a):
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
        print(f"Error saving console_state (8pt next-bar exit toggle) for {asset_key}:", e)
        return jsonify({"ok": False, "error": "Failed to save 8pt next-bar exit"}), 500

    return jsonify({
        "ok": True,
        "asset": asset_key,
        "high_next_bar_exit_enabled": bool(a.get("high_next_bar_exit_enabled", False)),
        "high_next_bar_exit_base_ts": a.get("high_next_bar_exit_base_ts"),
        # "next_bar_exit_allowed": _next_bar_exits_allowed(a),
        "next_bar_exit_allowed": _six_next_bar_exit_allowed(a),
        "high_next_bar_exit_allowed": _eight_next_bar_exit_allowed(a),
        "state": scrub_nonfinite(state),
    })

@app.route("/api/take-profit", methods=["POST"])
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

    # Ensure defaults exist
    if "tp_armed" not in a or a.get("tp_armed") is None:
        a["tp_armed"] = False
    if "tp_target" not in a or a.get("tp_target") is None:
        a["tp_target"] = 3
    if "tp_count" not in a or a.get("tp_count") is None:
        a["tp_count"] = 0

    # ----------------------------
    # NEW: Toggle arm/disarm
    # UI sends: { asset: "ES", tp_arm: "toggle" }
    # ----------------------------
    # if payload.get("tp_arm") == "toggle":
    #     new_val = not bool(a.get("tp_armed", False))
    #     a["tp_armed"] = new_val

    if payload.get("tp_arm") == "toggle":
        new_val = not bool(a.get("tp_armed", False))

        # if new_val and not _next_bar_exits_allowed(a):
        #     return jsonify({
        #         "ok": False,
        #         "error": "6pt next-bar exit locked until 2.5pt Renko flips"
        #     }), 423
        if new_val and not _six_next_bar_exit_allowed(a):
            return jsonify({
                "ok": False,
                "error": "6pt next-bar exit locked until 2.5pt management unlocks"
            }), 423

        a["tp_armed"] = new_val

        if new_val:
            # "Exit on next bar"
            a["tp_target"] = 1
            a["tp_count"] = 0  # start fresh when arming
        else:
            # Disarm = do nothing (keep default visual target if you want)
            #a["tp_target"] = 3
            a["tp_count"] = 0

        try:
            save_asset_state(asset_key, a)
        except Exception as e:
            print(f"Error saving console_state (tp toggle) for {asset_key}:", e)

        return jsonify({
            "ok": True,
            "asset": asset_key,
            "tp_armed": a["tp_armed"],
            "tp_target": a["tp_target"],
            "tp_count": a["tp_count"],
        })

    # ----------------------------
    # Legacy support (optional):
    # accept {tp_target:N} or {value:N}
    # ----------------------------
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

    # if someone uses old control, you can decide whether to arm automatically:
    # a["tp_armed"] = True

    if bool(payload.get("reset")):
        a["tp_count"] = 0

    try:
        save_asset_state(asset_key, a)
    except Exception as e:
        print(f"Error saving console_state (take-profit set) for {asset_key}:", e)

    return jsonify({
        "ok": True,
        "asset": asset_key,
        "tp_armed": bool(a.get("tp_armed", False)),
        "tp_target": a["tp_target"],
        "tp_count": a.get("tp_count", 0),
    })

@app.route("/tv_renko_small", methods=["POST"])
def tv_renko_small():
    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception:
        payload = {}

    if payload.get("secret") != TV_WEBHOOK_SECRET:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    raw_asset = (payload.get("asset") or payload.get("symbol") or "").upper()
    asset_key = raw_asset.split(":")[-1]

    color = (payload.get("color") or "neutral").lower()
    if color not in ("green", "red", "neutral"):
        color = "neutral"

    if asset_key not in state["assets"]:
        return jsonify({"ok": False, "error": f"Unknown asset '{asset_key}'"}), 400

    a = state["assets"][asset_key]
    now = time.time()

    a["small_renko_color"] = color
    a["last_small_renko_ts"] = now

    try:
        if a.get("position", 0) != 0 or a.get("pending_order_id"):
            save_asset_state(asset_key, a)
    except Exception as e:
        print(f"Error saving console_state (small renko) for {asset_key}:", e)

    append_log("tradingview", {"ts": now, "type": "renko_small", "asset": asset_key, "payload": payload})
    return jsonify({"ok": True})



@app.route("/api/auto-exit", methods=["POST"])
@login_required
def api_set_auto_exit():
    payload = request.get_json(force=True) or {}
    asset_key = (payload.get("asset") or "").upper().strip()

    if asset_key not in state["assets"]:
        return jsonify({"ok": False, "error": "Unknown asset"}), 400

    a = state["assets"][asset_key]
    pos = int(a.get("position", 0) or 0)
    if pos == 0:
        return jsonify({"ok": False, "error": "No open position"}), 409

    # Auto-exit is permanently 6pt now
    a["auto_exit_renko"] = "6pt"

    try:
        save_asset_state(asset_key, a)
    except Exception as e:
        print(f"Error saving console_state (auto-exit) for {asset_key}:", e)

    return jsonify({"ok": True, "asset": asset_key, "auto_exit_renko": "6pt"})







# =========================================================
# tv_renko_main = MAIN direction stream (6pt)
# =========================================================
@app.route("/tv_renko_main", methods=["POST"])
def tv_renko_main():
    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception:
        payload = {}

    if payload.get("secret") != TV_WEBHOOK_SECRET:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    raw_asset = (payload.get("asset") or payload.get("symbol") or "").upper()
    asset_key = raw_asset.split(":")[-1] if raw_asset else ""

    if asset_key not in state["assets"]:
        return jsonify({"ok": False, "error": f"Unknown asset '{asset_key}'"}), 400

    color = (payload.get("color") or "neutral").lower()
    if color not in ("green", "red", "neutral"):
        color = "neutral"

    now = time.time()
    a = state["assets"][asset_key]

    prev_color = (a.get("main_renko_color") or "neutral").lower()
    new_color = color

    a["main_renko_color"] = new_color
    a["main_renko_ts"] = now # shodul i change this to bar_ts??
    _update_scale_in_state_from_6pt(asset_key, new_color, now)
    _release_initial_exit_lock_if_needed(asset_key)

    # =========================================================
    # INTENT: soft next-bar permission
    # - first next 6pt bar after creation => READY
    # - one bar later, if unused => EXPIRED
    # =========================================================
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

    # =========================================================
    # PRE-ORDER: one-shot next 6pt bar decision
    # =========================================================
    if bool(a.get("preorder_active")):
        base_ts = a.get("preorder_bar_base_ts")
        preorder_dir = a.get("preorder_direction")
        preorder_qty = safe_int(a.get("preorder_qty"), default=0)
        # preorder_mode = "A"
        # preorder_entry_size_mode = a.get("preorder_entry_size_mode") or "6pt"
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

                    # if preorder_entry_size_mode == "6pt":
                    #     rith_sym = "MESM6"
                    #     exit_tf = "main"
                    # elif preorder_entry_size_mode == "9pt":
                    #     rith_sym = "MESM6"
                    #     exit_tf = "high"
                    # else:
                    #     rith_sym = "MESM6"
                    #     exit_tf = "main"
                    
                    if preorder_entry_size_mode == "6pt":
                        rith_sym = "MESM6"
                        exit_tf = "high"
                    elif preorder_entry_size_mode == "9pt":
                        rith_sym = "MESM6"
                        exit_tf = "high"
                    else:
                        rith_sym = "MESM6"
                        exit_tf = "high"

                    rith_exch = resolve_rithmic_exchange(asset_key)

                    try:
                        order = create_order(
                            symbol=rith_sym,
                            exchange=rith_exch,
                            side=preorder_dir,
                            qty=preorder_qty,
                            source="PREORDER_TRIGGER",
                            mode=preorder_mode,
                            kind="ENTRY",
                            env=env,
                        )
                    except Exception as e:
                        print("Error creating Firestore order from preorder:", e)
                        clear_preorder(asset_key, status="CANCELLED")
                    else:  
                        
                        a["pending_order_id"] = order.get("id")
                        a["pending_side"] = preorder_dir
                        a["pending_qty"] = preorder_qty
                        a["pending_mode"] = preorder_mode                 # still "A"
                        a["pending_trade_mode"] = preorder_trade_mode     # NEW
                        a["exit_tf"] = exit_tf
                        a["rithmic_symbol"] = rith_sym
                        a["main_flip_exit_enabled"] = False

                        _activate_five_min_lock()

                        a["tempo_ready"] = False
                        a["tempo_spent_ts"] = time.time()
                        a["tempo_last_bar_ts"] = a.get("tempo_ts")

                        a["tp_count"] = 0
                        a["tp_armed"] = False
                        a["tp_target"] = 3
                        a["points_tp_enabled"] = False ###
                        a["points_tp_target"] = 10.0
                        a["rithmic_open_points"] = 0.0
                        a["rithmic_point_value"] = _point_value_for_contract(rith_sym, asset_key)
                        a["points_tp_hit_ts"] = None

                        a["protect_enabled"] = False
                        a["protect_threshold_points"] = -2.0
                        a["protect_hit_ts"] = None

                        a["scale_in_available"] = False
                        a["scale_in_used"] = False
                        a["scale_in_stage"] = None
                        a["scale_in_last_ts"] = None

                        # a["preorder_active"] = False
                        # a["preorder_status"] = "FILLED"
                        clear_preorder(asset_key, status="FILLED")

                        # ORDER_INDEX[order["id"]] = {
                        #     "symbol": asset_key,
                        #     "side": preorder_dir,
                        #     "qty": preorder_qty,
                        #     "mode": preorder_mode,
                        #     "env": env,
                        #     "kind": "ENTRY",
                        # }
                        ORDER_INDEX[order["id"]] = {
                            "symbol": asset_key,
                            "side": preorder_dir,
                            "qty": preorder_qty,
                            "mode": preorder_mode,                    # still "A"
                            "trade_mode": preorder_trade_mode,        # NEW
                            "env": env,
                            "kind": "ENTRY",
                        }
                else:
                    clear_preorder(asset_key, status="CANCELLED")
            else:
                clear_preorder(asset_key, status="CANCELLED")

   
    if int(a.get("position", 0) or 0) != 0 and bool(a.get("tp_armed", False)):
        a["tp_count"] = safe_int(a.get("tp_count"), default=0) + 1
        try:
            save_asset_state(asset_key, a)
        except Exception as e:
            print(f"Error saving console_state (tp_count) for {asset_key}:", e)

        maybe_take_profit_exit(asset_key, reason="take_profit_6pt_signal_count")



    # pos = a.get("position", 0)
    # exit_mode = a.get("exit_mode")

    # if pos != 0 and exit_mode == "A":
    pos = a.get("position", 0)
    exit_mode = a.get("exit_mode")
    exit_tf = a.get("exit_tf", "main")  # NEW

    # ONLY trigger if this TF is active
    #if pos != 0 and exit_mode == "A" and exit_tf == "main":
    main_flip_exit_enabled = bool(a.get("main_flip_exit_enabled", False))

    if pos != 0 and exit_mode == "A" and (exit_tf == "main" or main_flip_exit_enabled):
        opposite_signal = ((pos > 0 and new_color == "red") or (pos < 0 and new_color == "green"))

        if opposite_signal:
            if a.get("pending_order_id"):
                print(f"[RENKO_MAIN EXIT] skip: pending_order already exists for {asset_key}: {a.get('pending_order_id')}")
            else:
                side = "SELL" if pos > 0 else "BUY"
                qty = abs(pos)
                env = (state["global"].get("env") or "DEMO").upper()

                #rith_sym = resolve_rithmic_symbol(asset_key)
                rith_sym = a.get("rithmic_symbol") or resolve_rithmic_symbol(asset_key)
                rith_exch = resolve_rithmic_exchange(asset_key)

                try:
                    order = create_order(
                        symbol=rith_sym,
                        exchange=rith_exch,
                        side=side,
                        qty=qty,
                        source="EXIT_ENGINE",
                        mode=exit_mode,
                        kind="EXIT",
                        env=env,
                    )
                except Exception as e:
                    print("Error creating EXIT Firestore order (renko_main):", e)
                    close_trade(asset_key, reason="renko_main_flip_mode_A_fallback")
                    #a["main_flip_exit_enabled"] = False
                else:
                    a["pending_order_id"] = order.get("id")
                    a["pending_side"] = side
                    a["pending_qty"] = qty
                    a["pending_mode"] = exit_mode
                    a["pending_trade_mode"] = None

                    ORDER_INDEX[order["id"]] = {
                        "symbol": asset_key,
                        "side": side,
                        "qty": qty,
                        "mode": exit_mode,
                        "env": env,
                        "kind": "EXIT",
                    }


    try:
        save_asset_state(asset_key, a)
    except Exception as e:
        print("Error saving asset console_state (tv_renko_main):", e)

    append_log("tradingview", {"ts": now, "type": "renko_main", "asset": asset_key, "payload": payload})
    return jsonify({"ok": True, "asset": asset_key, "main_renko_color": color, "main_renko_ts": now})




@app.route("/api/intent", methods=["POST"])
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

    _ensure_five_min_lock_initialized()
    _ensure_post_exit_lock_initialized()

    if bool(g.get("post_exit_lock_active")):
        remaining = safe_int(g.get("post_exit_lock_remaining_s"), default=0)
        mm = remaining // 60
        ss = remaining % 60
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
        print(f"Error saving console_state (create intent) for {asset_key}:", e)
        return jsonify({"ok": False, "error": "Failed to save intent"}), 500

    return jsonify({"ok": True}), 201



@app.route("/api/preorder/cancel", methods=["POST"])
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
# =========================================================
# tv_renko_high = HIGH direction stream (8pt)
# =========================================================
@app.route("/tv_renko_high", methods=["POST"])
def tv_renko_high():

    
    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception:
        payload = {}

    if payload.get("secret") != TV_WEBHOOK_SECRET:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    raw_asset = (payload.get("asset") or payload.get("symbol") or "").upper()
    asset_key = raw_asset.split(":")[-1] if raw_asset else ""

    if asset_key not in state["assets"]:
        return jsonify({"ok": False, "error": f"Unknown asset '{asset_key}'"}), 400

    color = (payload.get("color") or "neutral").lower()
    if color not in ("green", "red", "neutral"):
        color = "neutral"

    now = time.time()
    a = state["assets"][asset_key]
    a["high_renko_color"] = color
    a["high_renko_ts"] = now


    # =========================================================
    # 8PT NEXT-BAR EXIT
    # If armed, exit on the next fresh 8pt bar after arming.
    # This is independent from 8pt opposite-color flip exit.
    # =========================================================
    if int(a.get("position", 0) or 0) != 0 and bool(a.get("high_next_bar_exit_enabled", False)):
        base_ts = a.get("high_next_bar_exit_base_ts")
        is_next_high_bar = (base_ts is not None and now != base_ts)

        if is_next_high_bar:
            if a.get("pending_order_id"):
                print(f"[HIGH NEXT BAR EXIT] skip: pending exists for {asset_key}")
            else:
                pos = int(a.get("position", 0) or 0)
                side = "SELL" if pos > 0 else "BUY"
                qty = abs(pos)
                env = (a.get("env") or state["global"].get("env") or "DEMO").upper().strip()

                rith_sym = a.get("rithmic_symbol") or resolve_rithmic_symbol(asset_key)
                rith_exch = resolve_rithmic_exchange(asset_key)

                try:
                    order = create_order(
                        symbol=rith_sym,
                        exchange=rith_exch,
                        side=side,
                        qty=qty,
                        source="HIGH_NEXT_BAR_EXIT",
                        mode=a.get("exit_mode") or "A",
                        kind="EXIT",
                        env=env,
                    )
                except Exception as e:
                    print("Error creating EXIT Firestore order (8pt next-bar):", e)
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
                        "symbol": asset_key,
                        "side": side,
                        "qty": qty,
                        "mode": a.get("exit_mode") or "A",
                        "env": env,
                        "kind": "EXIT",
                        "source": "HIGH_NEXT_BAR_EXIT",
                    }

    # =========================================================
    # HIGH TF EXIT LOGIC (8pt)
    # =========================================================
    pos = a.get("position", 0)
    exit_mode = a.get("exit_mode")
    exit_tf = a.get("exit_tf", "main")

    if pos != 0 and exit_mode == "A" and exit_tf == "high":
        opposite_signal = ((pos > 0 and color == "red") or (pos < 0 and color == "green"))

        if opposite_signal:
            if a.get("pending_order_id"):
                print(f"[RENKO_HIGH EXIT] skip: pending exists for {asset_key}")
            else:
                side = "SELL" if pos > 0 else "BUY"
                qty = abs(pos)
                env = (state["global"].get("env") or "DEMO").upper()

                #rith_sym = resolve_rithmic_symbol(asset_key)
                rith_sym = a.get("rithmic_symbol") or resolve_rithmic_symbol(asset_key)
                rith_exch = resolve_rithmic_exchange(asset_key)

                try:
                    order = create_order(
                        symbol=rith_sym,
                        exchange=rith_exch,
                        side=side,
                        qty=qty,
                        source="EXIT_ENGINE_HIGH",
                        mode=exit_mode,
                        kind="EXIT",
                        env=env,
                    )
                except Exception as e:
                    print("Error creating EXIT Firestore order (renko_high):", e)
                    close_trade(asset_key, reason="renko_high_flip_fallback")
                    #a["main_flip_exit_enabled"] = False
                else:
                    a["pending_order_id"] = order.get("id")
                    a["pending_side"] = side
                    a["pending_qty"] = qty
                    a["pending_mode"] = exit_mode
                    a["pending_trade_mode"] = None

                    ORDER_INDEX[order["id"]] = {
                        "symbol": asset_key,
                        "side": side,
                        "qty": qty,
                        "mode": exit_mode,
                        "env": env,
                        "kind": "EXIT",
                    }

    try:
        save_asset_state(asset_key, a)
    except Exception as e:
        print("Error saving asset console_state (tv_renko_high):", e)

    append_log("tradingview", {"ts": now, "type": "renko_high", "asset": asset_key, "payload": payload})
    return jsonify({"ok": True, "asset": asset_key, "high_renko_color": color, "high_renko_ts": now})



@app.route("/rithmic/md-ticks", methods=["POST"])
def rithmic_md_ticks():
    data = request.get_json(force=True, silent=True) or {}
    now = time.time()
    state["global"]["rithmic_last_ts"] = now

    symbol = (data.get("symbol") or data.get("Symbol") or "").upper()
    append_log("rithmic", {"ts": now, "type": "tick", "symbol": symbol, "payload": data})
    return jsonify({"ok": True})

@app.route("/api/bridge/heartbeat", methods=["POST"])
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


@app.route("/api/orders", methods=["POST"])
@login_required
def api_create_order():
    payload = request.get_json(force=True) or {}

    order_type = (payload.get("order_type") or "market").lower().strip()
    if order_type not in ("market", "preorder"):
        return jsonify({"ok": False, "error": "Invalid order_type"}), 400

    # asset = payload.get("asset")
    # mode = payload.get("mode")
    # exit_tf = "main"
    asset = payload.get("asset")
    mode = "A"  # keep backend exit engine mode unchanged
    trade_mode = (payload.get("trade_mode") or "").upper().strip()
    exit_tf = "main"

    # --- UI FLOW BRANCH (asset provided) ---
    if asset:
        sym = str(asset).upper().strip()
        if sym not in state["assets"]:
            return jsonify({"ok": False, "error": "Unknown asset"}), 400

        # if mode not in ("A", "B"):
        #     return jsonify({"ok": False, "error": "Invalid mode"}), 400

       
        
        if trade_mode not in ("SCALP", "RUNNER"):
            return jsonify({"ok": False, "error": "Invalid trade_mode"}), 400

        a = state["assets"][sym]
        g = state["global"]
        # ✅ DAILY STOP hard block (server-side)
        if bool(g.get("daily_stop_triggered")):
            return jsonify({"ok": False, "error": "Daily stop TRIGGERED — entries blocked for today"}), 403
        
        _ensure_five_min_lock_initialized()
        _ensure_post_exit_lock_initialized()

        if bool(g.get("post_exit_lock_active")):
            remaining = safe_int(g.get("post_exit_lock_remaining_s"), default=0)
            mm = remaining // 60
            ss = remaining % 60
            return jsonify({
                "ok": False,
                "error": f"POST-EXIT 5 MIN LOCK ACTIVE ({mm:02d}:{ss:02d} remaining)"
            }), 403

        if bool(g.get("five_min_trade_lock_active")):
            remaining = safe_int(g.get("five_min_trade_lock_remaining_s"), default=0)
            mm = remaining // 60
            ss = remaining % 60
            return jsonify({
                "ok": False,
                "error": f"5 MIN LOCK ACTIVE — one trade already used this candle ({mm:02d}:{ss:02d} remaining)"
            }), 403



    


     


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
        
        # =====================================================
        # TEMPO TOKEN GATE: one bar = one trade
        # Block entry unless token is READY
        # =====================================================
        if not bool(a.get("tempo_ready")):
            return jsonify({"ok": False, "error": "Tempo token WAIT — need a new 6pt bar"}), 403


       
      
        
    
        # =====================================================
        # DIRECTION + FREE ZONE LOGIC (6pt main vs 8pt high)
        # Tempo is independent and NOT used for direction.
        # =====================================================
        entry_override = bool(
            payload.get("entry_override")
            or payload.get("override")
            or payload.get("override_entry")
            or payload.get("manual_entry_override")
        )

        direction_in = payload.get("direction") or payload.get("side") or payload.get("direction_override")
        chosen_direction = _parse_direction(direction_in)

        # Auto-exit is ALWAYS 6pt now (override does not change exits)
        a["auto_exit_renko"] = "6pt"

        # main = (a.get("main_renko_color") or "neutral").lower()   # 6pt
        # high = (a.get("high_renko_color") or "neutral").lower()   # 8pt

        # conflict_mode = (
        #     (main in ("green", "red"))
        #     and (high in ("green", "red"))
        #     and (main != high)
        # )
        main = (a.get("main_renko_color") or "neutral").lower()    # 6pt
        high = (a.get("high_renko_color") or "neutral").lower()    # 8pt
        macro = (a.get("macro_renko_color") or "neutral").lower()  # 12pt

        zone_type = _compute_zone_from_asset(a)
        a["zone_type"] = zone_type
        a["conflict_mode"] = (zone_type == "FREE")

        conflict_mode = (zone_type == "FREE")

        # if entry_override:
        #     if chosen_direction is None:
        #         return jsonify({"ok": False, "error": "Override entry requires direction=BUY/SELL (or green/red)"}), 403
        #     direction = chosen_direction

        # else:
        #     # NORMAL MODE:
        #     # - direction is locked to MAIN (6pt) when not in conflict
        #     # - FREE ZONE when MAIN conflicts with HIGH -> user must choose
        #     if main not in ("green", "red"):
        #         return jsonify({"ok": False, "error": "Main Renko (6pt) neutral/unknown – cannot trade"}), 403

        #     if not conflict_mode:
        #         direction = "BUY" if main == "green" else "SELL"
        #     else:
        #         if chosen_direction is None:
        #             return jsonify({
        #                 "ok": False,
        #                 "error": "Free zone: 6pt vs 8pt conflict. Choose direction (send direction=BUY/SELL or green/red)"
        #             }), 403
        #         direction = chosen_direction

        if entry_override:
            if chosen_direction is None:
                return jsonify({"ok": False, "error": "Override entry requires direction=BUY/SELL (or green/red)"}), 403
            direction = chosen_direction

       
        # elif order_type == "market":
        #     if main not in ("green", "red"):
        #         return jsonify({"ok": False, "error": "Main Renko (6pt) neutral/unknown – cannot trade"}), 403

        #     if not conflict_mode:
        #         # normal market entry follows MAIN automatically
        #         direction = "BUY" if main == "green" else "SELL"
        #     else:
        #         # FREE ZONE / conflict: require explicit manual direction
        #         if chosen_direction is None:
        #             return jsonify({
        #                 "ok": False,
        #                 "error": "Free zone: choose direction=BUY/SELL"
        #             }), 403
        #         direction = chosen_direction

        elif order_type == "market":
            if high not in ("green", "red") or macro not in ("green", "red"):
                return jsonify({
                    "ok": False,
                    "error": "8pt/12pt structure unavailable – cannot trade"
                }), 403

            if not conflict_mode:
                # NORMAL: market entry follows 8pt/12pt direction automatically.
                direction = "BUY" if high == "green" else "SELL"
            else:
                # FREE: 8pt and 12pt disagree, so user must choose.
                if chosen_direction is None:
                    return jsonify({
                        "ok": False,
                        "error": "Free zone: choose direction=BUY/SELL"
                    }), 403
                direction = chosen_direction

        elif order_type == "preorder":
            if chosen_direction is None:
                return jsonify({
                    "ok": False,
                    "error": "Pre-order requires direction=BUY/SELL"
                }), 403



            

       


        # entry_size_mode = payload.get("entry_size_mode", "6pt")

        # if entry_size_mode == "6pt":
        #     size = 8
        #     rith_sym = "MESM6"
        #     exit_tf = "main"
        # elif entry_size_mode == "9pt":
        #     size = 5
        #     rith_sym = "MESM6"
        #     exit_tf = "high"
        # else:
        #     size = 8
        #     rith_sym = "MESM6"
        #     exit_tf = "main"


        # =====================================================
        ## FINAL ENTRY DIRECTION GUARD
        # 8/12 decides structural permission.
        # 2.5pt must agree with the final direction.
        # 6pt is ignored for entry permission.
        # =====================================================
        if order_type == "market":
            allowed, reason = _entry_direction_allowed(a, direction)
            if not allowed:
                return jsonify({
                    "ok": False,
                    "error": reason or "Entry blocked by structure/2.5pt filter",
                    "zone_type": _compute_zone_from_asset(a),
                    "main_renko_color": a.get("main_renko_color"),
                    "high_renko_color": a.get("high_renko_color"),
                    "macro_renko_color": a.get("macro_renko_color"),
                    "two_half_renko_color": a.get("two_half_renko_color"),
                }), 409
        entry_size_mode = payload.get("entry_size_mode", "6pt")

        # if entry_size_mode == "6pt":
        #     size = 5
        #     rith_sym = "MESM6"
        #     exit_tf = "high"
        # elif entry_size_mode == "9pt":
        #     size = 3
        #     rith_sym = "MESM6"
        #     exit_tf = "high"
        # else:
        #     size = 5
        #     rith_sym = "MESM6"
        #     exit_tf = "high"

        # if entry_size_mode == "6pt":
        #     size = 5
        #     rith_sym = "MESM6"
        #     exit_tf = "high"
        # elif entry_size_mode == "9pt":
        #     size = 5
        #     rith_sym = "MESM6"
        #     exit_tf = "high"
        # else:
        #     size = 5
        #     rith_sym = "MESM6"
        #     exit_tf = "high"

        if entry_size_mode == "6pt":
            size = 5
            rith_sym = "MESM6"
            exit_tf = "high"
        elif entry_size_mode == "9pt":
            size = 3
            rith_sym = "MESM6"
            exit_tf = "high"
        else:
            size = 5
            rith_sym = "MESM6"
            exit_tf = "high"

        if order_type == "preorder":
            if a.get("preorder_active"):  
                return jsonify({
                    "ok": False,
                    "error": "Pre-order already active"
                }), 403

            # a["preorder_active"] = True
            # a["preorder_direction"] = chosen_direction
            # a["preorder_qty"] = size
            # a["preorder_entry_size_mode"] = entry_size_mode
            # a["preorder_created_ts"] = time.time()
            # a["preorder_bar_base_ts"] = a.get("main_renko_ts")
            # a["preorder_status"] = "PENDING"
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
                print(f"Error saving console_state (create preorder) for {sym}:", e)

            return jsonify({
                "ok": True,
                "preorder": True,
                "state": state,
            }), 201

        env = (g.get("env") or "DEMO").upper()

        rith_exch = resolve_rithmic_exchange(sym)



        try:
            order = create_order(
                symbol=rith_sym,
                exchange=rith_exch,
                side=direction,
                qty=size,
                source="UI",
                mode=mode,
                kind="ENTRY",
                env=env,
            )
        except Exception as e:
            print("Error creating Firestore order:", e)
            return jsonify({"ok": False, "error": "Failed to create order"}), 500

      
        a["pending_order_id"] = order.get("id")
        a["pending_side"] = direction
        a["pending_qty"] = size
        a["pending_mode"] = mode                 # keep as "A"
        a["pending_trade_mode"] = trade_mode     # NEW
        a["exit_tf"] = exit_tf  # NEW
        a["rithmic_symbol"] = rith_sym
        a["main_flip_exit_enabled"] = False
        _activate_five_min_lock()

        # =====================================================
        # TEMPO TOKEN: spend immediately on entry intent
        # (one tempo bar = one trade)
        # =====================================================
        a["tempo_ready"] = False
        a["tempo_spent_ts"] = time.time()
        a["tempo_last_bar_ts"] = a.get("tempo_ts")



        
        # # Reset TP counter on new entry intent
        # a["tp_count"] = 0
        # if not a.get("tp_target"):
        #     a["tp_target"] = 3

        # Reset TP state on new entry intent (default OFF)
        a["tp_count"] = 0
        a["tp_armed"] = False
        a["tp_target"] = 3
        a["intent_active"] = False
        a["intent_created_ts"] = None
        a["intent_bar_base_ts"] = None
        a["intent_ready_bar_ts"] = None
        a["intent_status"] = None
        # a["points_tp_enabled"] = True
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


       


        # ORDER_INDEX[order["id"]] = {
        #     "symbol": sym,
        #     "side": direction,
        #     "qty": size,
        #     "mode": mode,
        #     "env": env,
        #     "kind": "ENTRY",
        # }
        ORDER_INDEX[order["id"]] = {
            "symbol": sym,
            "side": direction,
            "qty": size,
            "mode": mode,                    # still "A"
            "trade_mode": trade_mode,        # NEW
            "env": env,
            "kind": "ENTRY",
        }

        try:
            save_asset_state(sym, a)
        except Exception as e:
            print(f"Error saving console_state (create_order UI) for {sym}:", e)

        return jsonify({"ok": True, "order": order, "state": state}), 201

    # --- RAW GENERIC BRANCH ---
    symbol = payload.get("symbol")
    side = payload.get("side")
    qty = payload.get("qty", 1)
    #mode = payload.get("mode")
    mode = "A"
    trade_mode = (payload.get("trade_mode") or "").upper().strip()
    source = payload.get("source") or "UI"

    if not symbol or side not in ("BUY", "SELL"):
        return jsonify({"error": "symbol and side are required"}), 400

    kind = (payload.get("kind") or "ENTRY")
    kind_u = str(kind).upper().strip()
    if kind_u == "ENTRY" and bool(state["global"].get("daily_stop_triggered")):
        return jsonify({"ok": False, "error": "Daily stop TRIGGERED — entries blocked for today"}), 403

    env = (payload.get("env") or state["global"].get("env", "DEMO"))
    exchange = payload.get("exchange") or "CME"

    order = create_order(
        symbol=symbol,
        exchange=exchange,
        side=side,
        qty=qty,
        source=source,
        mode=mode,
        kind=kind,
        env=env,
    )

    ui_sym = CONTRACT_TO_UI.get(str(symbol).upper())
    if ui_sym and ui_sym in state["assets"]:
        a = state["assets"][ui_sym]
        # if not a.get("pending_order_id"):
        #     a["pending_order_id"] = order.get("id")
        #     a["pending_side"] = side
        #     a["pending_qty"] = qty
        #     a["pending_mode"] = mode
        if not a.get("pending_order_id"):
            a["pending_order_id"] = order.get("id")
            a["pending_side"] = side
            a["pending_qty"] = qty
            a["pending_mode"] = mode
            a["pending_trade_mode"] = trade_mode if trade_mode in ("SCALP", "RUNNER") else None

        # ORDER_INDEX[order["id"]] = {
        #     "symbol": ui_sym,
        #     "side": side,
        #     "qty": qty,
        #     "mode": mode,
        #     "env": env,
        #     "kind": str(kind).upper().strip(),
        # }
        ORDER_INDEX[order["id"]] = {
            "symbol": ui_sym,
            "side": side,
            "qty": qty,
            "mode": mode,
            "trade_mode": trade_mode if trade_mode in ("SCALP", "RUNNER") else None,
            "env": env,
            "kind": str(kind).upper().strip(),
        }

        try:
            save_asset_state(ui_sym, a)
        except Exception as e:
            print(f"Error saving console_state (create_order RAW) for {ui_sym}:", e)

    return jsonify(order), 201



@app.route("/api/scale-in", methods=["POST"])
@login_required
def api_scale_in():
    payload = request.get_json(force=True, silent=True) or {}
    asset_key = (payload.get("asset") or "").upper().strip()

    if asset_key not in state["assets"]:
        return jsonify({"ok": False, "error": "Unknown asset"}), 400

    a = state["assets"][asset_key]
    g = state["global"]

    if bool(g.get("daily_stop_triggered")):
        return jsonify({"ok": False, "error": "Daily stop TRIGGERED — scale-in blocked"}), 403

    if g.get("trade_lock"):
        return jsonify({"ok": False, "error": "Global trade lock active"}), 403

    pos = int(a.get("position", 0) or 0)
    if pos == 0:
        return jsonify({"ok": False, "error": "No open position to scale into"}), 409

    if a.get("pending_order_id"):
        return jsonify({"ok": False, "error": "Pending order already exists"}), 409

    if bool(a.get("scale_in_used", False)):
        return jsonify({"ok": False, "error": "Scale-in already used for this trade"}), 409

    if not bool(a.get("scale_in_available", False)):
        return jsonify({"ok": False, "error": "Scale-in not available yet"}), 403

    now = time.time()
    hb_ts = a.get("last_heartbeat_ts")
    if hb_ts is None or (now - hb_ts) > 10 * 60:
        return jsonify({"ok": False, "error": "Heartbeat stale or missing – cannot scale in"}), 403

    side = "BUY" if pos > 0 else "SELL"
    qty = SCALE_IN_QTY

    env = (a.get("env") or g.get("env") or "DEMO").upper().strip()
    rith_sym = a.get("rithmic_symbol") or resolve_rithmic_symbol(asset_key)
    rith_exch = resolve_rithmic_exchange(asset_key)

    try:
        order = create_order(
            symbol=rith_sym,
            exchange=rith_exch,
            side=side,
            qty=qty,
            source="SCALE_IN",
            mode=a.get("exit_mode") or "A",
            kind="ENTRY",
            env=env,
        )
    except Exception as e:
        print("Scale-in create_order failed:", e)
        return jsonify({"ok": False, "error": "Failed to create scale-in order"}), 500

    a["pending_order_id"] = order.get("id")
    a["pending_side"] = side
    a["pending_qty"] = qty
    a["pending_mode"] = a.get("exit_mode") or "A"
    a["pending_trade_mode"] = a.get("trade_mode")

    # Lock button immediately after sending order.
    a["scale_in_available"] = False
    a["scale_in_used"] = True
    a["scale_in_stage"] = None
    a["scale_in_last_ts"] = now

    ORDER_INDEX[order["id"]] = {
        "symbol": asset_key,
        "side": side,
        "qty": qty,
        "mode": a.get("exit_mode") or "A",
        "trade_mode": a.get("trade_mode"),
        "env": env,
        "kind": "ENTRY",
        "source": "SCALE_IN",
    }

    try:
        save_asset_state(asset_key, a)
    except Exception as e:
        print(f"Error saving console_state (scale-in) for {asset_key}:", e)

    return jsonify({
        "ok": True,
        "asset": asset_key,
        "order": order,
        "state": scrub_nonfinite(state),
    }), 201


@app.route("/tv_renko_one", methods=["POST"])
def tv_renko_one():
    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception:
        payload = {}

    if payload.get("secret") != TV_WEBHOOK_SECRET:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    raw_asset = (payload.get("asset") or payload.get("symbol") or "").upper()
    asset_key = raw_asset.split(":")[-1]

    color = (payload.get("color") or "neutral").lower()
    if color not in ("green", "red", "neutral"):
        color = "neutral"

    if asset_key not in state["assets"]:
        return jsonify({"ok": False, "error": f"Unknown asset '{asset_key}'"}), 400

    a = state["assets"][asset_key]
    now = time.time()

    prev_color = (a.get("one_renko_color") or "neutral").lower()
    color_changed = color != prev_color

    a["one_renko_color"] = color
    a["one_renko_ts"] = now
    a["one_renko_color_changed"] = color_changed
    #a["last_renko_ts"] = now

    # Visual/log only. No execution logic here.
    try:
        save_asset_state(asset_key, a)
    except Exception as e:
        print(f"Error saving console_state one_renko for {asset_key}:", e)

    state["logs"]["tradingview"].insert(0, {
        "ts": now,
        "type": "renko_1pt_visual",
        "asset": asset_key,
        "color": color,
        "color_changed": color_changed,
    })
    state["logs"]["tradingview"] = state["logs"]["tradingview"][:LOG_MAX]

    return jsonify({
        "ok": True,
        "asset": asset_key,
        "one_renko_color": color,
        "one_renko_ts": now,
        "one_renko_color_changed": color_changed,
    })


@app.route("/api/main-flip-exit", methods=["POST"])
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

    current = bool(a.get("main_flip_exit_enabled", False))
    a["main_flip_exit_enabled"] = not current

    try:
        save_asset_state(asset_key, a)
    except Exception as e:
        print(f"Error saving console_state (main flip exit toggle) for {asset_key}:", e)
        return jsonify({"ok": False, "error": "Failed to persist toggle"}), 500

    return jsonify({
        "ok": True,
        "asset": asset_key,
        "main_flip_exit_enabled": a["main_flip_exit_enabled"],
        "state": scrub_nonfinite(state),
    })


@app.route("/api/intent/cancel", methods=["POST"])
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



@app.route("/api/four-pt-invalidation", methods=["POST"])
@login_required
def api_toggle_four_pt_invalidation():
    payload = request.get_json(force=True) or {}
    asset_key = (payload.get("asset") or "").upper().strip()

    if asset_key not in state["assets"]:
        return jsonify({"ok": False, "error": "Unknown asset"}), 400

    a = state["assets"][asset_key]
    pos = int(a.get("position", 0) or 0)
    if pos == 0:
        return jsonify({"ok": False, "error": "No open position"}), 409

    #a["four_pt_invalidation_enabled"] = not bool(a.get("four_pt_invalidation_enabled", False))
    # current = bool(a.get("four_pt_invalidation_enabled", False))

    # # Turning ON is only allowed after one fresh 6pt/main bar after entry.
    # if not current:
    #     if not _has_new_main_bar_after_entry(a):
    #         return jsonify({
    #             "ok": False,
    #             "error": "4PT invalidation locked until a new 6pt bar forms after entry"
    #         }), 403

    # a["four_pt_invalidation_enabled"] = not current
    current = bool(a.get("four_pt_invalidation_enabled", False))

    # 4PT invalidation can be toggled immediately after entry.
    a["four_pt_invalidation_enabled"] = not current

    try:
        save_asset_state(asset_key, a)
    except Exception as e:
        print(f"Error saving console_state (4pt invalidation toggle) for {asset_key}:", e)
        return jsonify({"ok": False, "error": "Failed to persist state"}), 500

    return jsonify({
        "ok": True,
        "asset": asset_key,
        "four_pt_invalidation_enabled": bool(a.get("four_pt_invalidation_enabled", False)),
    })

@app.route("/api/exit", methods=["POST"])
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

    # Optional override: allow manual exit even if 2pt gate is not satisfied
    force = bool(payload.get("force") or payload.get("override") or payload.get("manual_override"))


    side = "SELL" if pos > 0 else "BUY"
    qty = abs(pos)

    env = (a.get("env") or state["global"].get("env") or "DEMO").upper().strip()

    #rith_sym = resolve_rithmic_symbol(asset_key)
    rith_sym = a.get("rithmic_symbol") or resolve_rithmic_symbol(asset_key)
    rith_exch = resolve_rithmic_exchange(asset_key)

    if bool(a.get("initial_exit_lock_active", False)) and not force:
        return jsonify({
            "ok": False,
            "error": "INITIAL_EXIT_LOCK_ACTIVE — wait for fresh 6pt bar after entry"
        }), 409

    try:
        order = create_order(
            symbol=rith_sym,
            exchange=rith_exch,
            side=side,
            qty=qty,
            source=("MANUAL_EXIT_FORCE" if force else "MANUAL_EXIT"),
            mode=a.get("exit_mode") or "A",
            kind="EXIT",
            env=env,
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
        "symbol": asset_key,
        "side": side,
        "qty": qty,
        "mode": a.get("exit_mode") or "A",
        "env": env,
        "kind": "EXIT",
    }

    try:
        save_asset_state(asset_key, a)
    except Exception as e:
        print(f"Error saving console_state (manual exit) for {asset_key}:", e)

    return jsonify({"ok": True, "order": order}), 201


@app.route("/api/orders/pending", methods=["GET"])
def api_pending_orders():
    # 🔐 AUTH: require secret so only your bridge can claim orders
    secret = request.args.get("secret")
    if secret != BRIDGE_HEARTBEAT_SECRET:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    max_items = int(request.args.get("limit", 20))
    owner = request.args.get("owner") or "bridge"

    raw_orders = get_pending_orders(limit=max_items, owner=owner)

    converted = []
    for o in raw_orders:
        # converted.append({
        #     "id": o["id"],
        #     "asset": o.get("symbol"),
        #     "exchange": o.get("exchange") or "CME",
        #     "side": o.get("side"),
        #     "qty": o.get("qty"),
        #     "mode": o.get("mode"),
        #     "env": (o.get("env") or state["global"].get("env", "DEMO")),
        #     "kind": (o.get("kind") or "ENTRY"),
        # })
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

@app.route("/api/orders/<order_id>/execution-report", methods=["POST"])
def api_execution_report(order_id):
    payload = request.get_json(force=True) or {}
    status = payload.get("status")
    extra = payload.get("extra", {}) or {}

    if status not in ("WORKING", "FILLED", "REJECTED", "CANCELLED"):
        return jsonify({"error": "invalid status"}), 400

    update_order_status(order_id, status, extra=extra)

    info = ORDER_INDEX.get(order_id, {}) or {}

    doc_order = {}
    if not info:
        try:
            doc_order = get_order(order_id) or {}
        except Exception as e:
            print("Error reading order doc for execution-report:", e)
            doc_order = {}

    symbol = (
        info.get("symbol")
        or extra.get("symbol")
        or extra.get("asset")
        or extra.get("sym")
        or doc_order.get("symbol")
    )
    side = (
        info.get("side")
        or extra.get("side")
        or extra.get("direction")
        or doc_order.get("side")
    )
    qty = (
        info.get("qty")
        or extra.get("qty")
        or extra.get("size")
        or doc_order.get("qty")
        or 1
    )
    mode = (info.get("mode") or extra.get("mode") or doc_order.get("mode"))
    trade_mode = (
        info.get("trade_mode")
        or extra.get("trade_mode")
        or doc_order.get("trade_mode")
    )

    if trade_mode is not None:
        trade_mode = str(trade_mode).upper().strip()

    env = (
        info.get("env")
        or extra.get("env")
        or doc_order.get("env")
        or state["global"].get("env", "DEMO")
    )

    kind = (info.get("kind") or extra.get("kind") or doc_order.get("kind") or "ENTRY")
    kind = str(kind).upper().strip()

    source = (
        info.get("source")
        or extra.get("source")
        or doc_order.get("source")
        or ""
    )
    source = str(source).upper().strip()

    if symbol:
        symbol = str(symbol).upper().strip()
        if symbol not in state["assets"] and symbol in CONTRACT_TO_UI:
            symbol = CONTRACT_TO_UI[symbol]

    if side:
        side = str(side).upper().strip()

    try:
        qty = int(qty)
    except Exception:
        qty = 1

    if mode is not None:
        mode = str(mode).upper().strip()

    if env is not None:
        env = str(env).upper().strip()

    if not symbol or symbol not in state["assets"]:
        ORDER_INDEX.pop(order_id, None)
        return jsonify({"ok": True})

    asset_state = state["assets"][symbol]

    # def clear_pending():
    #     if asset_state.get("pending_order_id") == order_id:
    #         asset_state["pending_order_id"] = None
    #         asset_state["pending_side"] = None
    #         asset_state["pending_qty"] = 0
    #         asset_state["pending_mode"] = None

    def clear_pending():
        if asset_state.get("pending_order_id") == order_id:
            asset_state["pending_order_id"] = None
            asset_state["pending_side"] = None
            asset_state["pending_qty"] = 0
            asset_state["pending_mode"] = None
            asset_state["pending_trade_mode"] = None

    # WORKING
    # if status == "WORKING":
    #     if order_id not in ORDER_INDEX:
    #         ORDER_INDEX[order_id] = {
    #             "symbol": symbol,
    #             "side": side,
    #             "qty": qty,
    #             "mode": mode,
    #             "trade_mode": trade_mode,
    #             "env": env,
    #             "kind": kind,
    #         }
    #     return jsonify({"ok": True})

    if status == "WORKING":
        if order_id not in ORDER_INDEX:
            ORDER_INDEX[order_id] = {
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "mode": mode,
                "trade_mode": trade_mode,
                "env": env,
                "kind": kind,
                "source": source,
            }
        return jsonify({"ok": True})

    # REJECTED / CANCELLED -> un-consume TEMPO token if this was ENTRY
    # if status in ("REJECTED", "CANCELLED"):
    #     clear_pending()
    #     # If entry was rejected/cancelled, don't lock token forever
    #     if kind == "ENTRY":
    #         asset_state["tempo_spent_ts"] = None
    #         _release_five_min_lock_if_same_bucket()
    if status in ("REJECTED", "CANCELLED"):
        clear_pending()

        # if kind == "ENTRY" and source == "SCALE_IN":
        #     asset_state["scale_in_available"] = True
        #     asset_state["scale_in_used"] = False
        #     asset_state["scale_in_stage"] = None
        #     asset_state["scale_in_last_ts"] = time.time()
        if kind == "ENTRY" and source == "SCALE_IN":
            asset_state["scale_in_available"] = True
            asset_state["scale_in_used"] = False
            asset_state["scale_in_stage"] = "READY_6PT"
            asset_state["scale_in_last_ts"] = time.time()

        elif kind == "ENTRY":
            # Regular entry rejected/cancelled: release entry locks.
            asset_state["tempo_spent_ts"] = None
            _release_five_min_lock_if_same_bucket()



      

        ORDER_INDEX.pop(order_id, None)

        try:
            save_asset_state(symbol, asset_state)
        except Exception as e:
            print(f"Error saving console_state (REJECT/CANCEL) for {symbol}:", e)

        return jsonify({"ok": True})

    # FILLED
    if status == "FILLED":
        current_pos = asset_state.get("position", 0)

        if side == "BUY":
            order_pos = qty
        elif side == "SELL":
            order_pos = -qty
        else:
            clear_pending()
            ORDER_INDEX.pop(order_id, None)
            return jsonify({"ok": True})

        
        if kind == "EXIT" and current_pos == 0:
            print(f"[EXEC-EXIT-FLAT-LOCAL] order_id={order_id} symbol={symbol} kind=EXIT current_pos=0 -> finalize exit side-effects")

            fill_price = (
                extra.get("fill_price")
                or extra.get("avg_price")
                or extra.get("price")
                or asset_state.get("last_price")
                or 0.0
            )
            try:
                fill_price = float(fill_price)
            except (TypeError, ValueError):
                fill_price = 0.0

            if fill_price:
                asset_state["last_price"] = fill_price
            if mode:
                asset_state["exit_mode"] = mode

            # If we still have entry context, force the same post-exit bookkeeping
            # that close_trade() normally performs.
            if asset_state.get("last_entry_ts"):
                entry_price = asset_state.get("avg_price") or asset_state.get("entry_price") or asset_state.get("last_price")

                # inferred_exit_dir = None
                # pending_side = (asset_state.get("pending_side") or "").upper().strip()

                # # EXIT order SELL means we were long -> block BUY reentry
                # if pending_side == "SELL":
                #     inferred_exit_dir = "BUY"
                # # EXIT order BUY means we were short -> block SELL reentry
                # elif pending_side == "BUY":
                #     inferred_exit_dir = "SELL"

                # if inferred_exit_dir:
                #     asset_state["last_exit_direction"] = inferred_exit_dir
                #     asset_state["reentry_lock_active"] = True
                pending_side = (asset_state.get("pending_side") or "").upper().strip()

                # Same-direction re-entry lock removed.
                # Do not block BUY after long exit or SELL after short exit.
                asset_state["last_exit_direction"] = None
                asset_state["reentry_lock_active"] = False

                # write a minimal history row if you want the close reflected even on this path
                state["history"].insert(0, {
                    "asset": symbol,
                    "side": ("LONG" if pending_side == "SELL" else "SHORT" if pending_side == "BUY" else "UNKNOWN"),
                    "size": abs(qty),
                    "entry_price": entry_price,
                    "exit_price": asset_state.get("last_price"),
                    "entry_ts": asset_state.get("last_entry_ts"),
                    "exit_ts": time.time(),
                    "pnl_points": 0.0,
                    "pnl_usd": 0.0,
                    "mode": asset_state.get("exit_mode"),
                    "reason": extra.get("reason") or "exit_order_filled_flat_local",
                    "env": asset_state.get("env") or state["global"].get("env", "DEMO"),
                    "stop_loss_price": asset_state.get("stop_loss_price"),
                    "stop_loss_status": asset_state.get("stop_loss_status"),
                })

            # keep the asset flat/reset
            # asset_state["position"] = 0
            # asset_state["avg_price"] = None
            # asset_state["entry_price"] = None
            # asset_state["pnl"] = 0.0
            # asset_state["last_entry_ts"] = None
            exit_now_ts = time.time()

            asset_state["position"] = 0
            asset_state["avg_price"] = None
            asset_state["entry_price"] = None
            asset_state["pnl"] = 0.0
            asset_state["last_entry_ts"] = None
            asset_state["last_exit_ts"] = exit_now_ts
            asset_state["tempo_4pt_unlock_ts"] = None
            asset_state["stop_loss_price"] = None
            asset_state["stop_loss_status"] = None
            asset_state["tp_count"] = 0
            asset_state["tp_armed"] = False
            asset_state["main_flip_exit_enabled"] = False

            asset_state["high_next_bar_exit_enabled"] = False
            asset_state["high_next_bar_exit_started_ts"] = None
            asset_state["high_next_bar_exit_base_ts"] = None
            asset_state["last_exit_direction"] = None
            asset_state["reentry_lock_active"] = False

            # reset trade-context fields on flat-local exit finalize
            asset_state["exit_mode"] = None
            asset_state["env"] = None
            asset_state["trade_mode"] = None
            asset_state["entry_zone"] = None
            asset_state["runner_4pt_unlocked"] = False
            # asset_state["entry_main_renko_ts"] = None
            # asset_state["entry_renko_color"] = None
            # asset_state["entry_main_renko_color"] = None
            asset_state["entry_main_renko_ts"] = None
            asset_state["entry_high_renko_ts"] = None
            asset_state["entry_renko_color"] = None
            asset_state["entry_main_renko_color"] = None
            asset_state["four_pt_invalidation_enabled"] = False
            asset_state["next_bar_exit_allowed"] = False
            asset_state["high_next_bar_exit_allowed"] = False
            asset_state["four_pt_invalidation_allowed"] = False
            asset_state["zone_type"] = None
            asset_state["pending_trade_mode"] = None
            asset_state["preorder_trade_mode"] = None

            
           
            asset_state["points_tp_enabled"] = False
            asset_state["points_tp_target"] = 15.0
            asset_state["rithmic_open_points"] = 0.0
            asset_state["rithmic_point_value"] = None
            asset_state["points_tp_hit_ts"] = None

            asset_state["protect_enabled"] = False
            asset_state["protect_threshold_points"] = -2.0
            asset_state["protect_hit_ts"] = None
            asset_state["two_half_tp_lock_enabled"] = False
            asset_state["two_half_tp_lock_base_color"] = None
            asset_state["two_half_tp_lock_released"] = False
            asset_state["two_half_tp_lock_started_ts"] = None
            asset_state["two_half_tp_lock_released_ts"] = None

            asset_state["scale_in_available"] = False
            asset_state["scale_in_used"] = False
            asset_state["scale_in_stage"] = None
            asset_state["scale_in_last_ts"] = None
            if not asset_state.get("tp_target"):
                asset_state["tp_target"] = 3

            clear_pending()
            _activate_post_exit_lock()
            ORDER_INDEX.pop(order_id, None)

            try:
                save_asset_state(symbol, asset_state)
            except Exception as e:
                print(f"Error saving console_state (EXIT flat-local finalize) for {symbol}:", e)

            return jsonify({"ok": True})

        fill_price = (
            extra.get("fill_price")
            or extra.get("avg_price")
            or extra.get("price")
            or asset_state.get("last_price")
            or 0.0
        )
        try:
            fill_price = float(fill_price)
        except (TypeError, ValueError):
            fill_price = 0.0

        now_ts = time.time()

        is_exit = (
            kind == "EXIT"
            and current_pos != 0
            and ((current_pos > 0 and order_pos < 0) or (current_pos < 0 and order_pos > 0))
        )

        

        # if is_exit:
        #     asset_state["last_price"] = fill_price
        #     if mode:
        #         asset_state["exit_mode"] = mode

        #     reason = extra.get("reason") or "exit_order_filled"
        #     print(f"[EXEC-EXIT] order_id={order_id} symbol={symbol} kind={kind} side={side} qty={qty} price={fill_price} reason={reason}")

        #     clear_pending()
        #     close_trade(symbol, reason=reason)
        #     _activate_post_exit_lock()
        #     ORDER_INDEX.pop(order_id, None)
        #     return jsonify({"ok": True})
        if is_exit:
            asset_state["last_price"] = fill_price
            if mode:
                asset_state["exit_mode"] = mode

            # Snapshot this BEFORE close_trade() clears entry_main_renko_ts.
            asset_state["last_trade_had_new_main_bar_after_entry"] = bool(
                _has_new_main_bar_after_entry(asset_state)
            )

            reason = extra.get("reason") or "exit_order_filled"
            print(f"[EXEC-EXIT] order_id={order_id} symbol={symbol} kind={kind} side={side} qty={qty} price={fill_price} reason={reason}")

            clear_pending()
            close_trade(symbol, reason=reason)
            ORDER_INDEX.pop(order_id, None)
            return jsonify({"ok": True})

        # print(f"[EXEC-ENTRY] order_id={order_id} symbol={symbol} kind={kind} side={side} qty={qty} price={fill_price} mode={mode} env={env}")

        # asset_state["just_filled_ts"] = time.time()
        # asset_state["position"] = order_pos
        # asset_state["avg_price"] = fill_price
        # asset_state["entry_price"] = fill_price
        # asset_state["last_entry_ts"] = now_ts
        # asset_state["pnl"] = 0.0
        # asset_state["order_count"] = (asset_state.get("order_count") or 0) + 1
        # asset_state["exit_mode"] = mode
        # asset_state["env"] = env
        # asset_state["five_min_ok"] = True
        # asset_state["stop_loss_price"] = None
        # asset_state["stop_loss_status"] = None

        # clear_pending()
        # ORDER_INDEX.pop(order_id, None)

        # try:
        #     save_asset_state(symbol, asset_state)
        # except Exception as e:
        #     print(f"Error saving console_state (ENTRY) for {symbol}:", e)

        # return jsonify({"ok": True})

                # =====================================================
        # SCALE-IN FILL: add to existing position, do not reset trade state
        # =====================================================
        if kind == "ENTRY" and source == "SCALE_IN":
            current_pos = int(current_pos or 0)

            same_direction = (
                (current_pos > 0 and order_pos > 0) or
                (current_pos < 0 and order_pos < 0)
            )

            if current_pos == 0 or not same_direction:
                clear_pending()
                ORDER_INDEX.pop(order_id, None)
                try:
                    save_asset_state(symbol, asset_state)
                except Exception as e:
                    print(f"Error saving console_state (bad SCALE_IN cleanup) for {symbol}:", e)
                return jsonify({"ok": False, "error": "Scale-in fill direction mismatch or no open position"}), 409

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

            # Keep existing exit/TP structure untouched.
            asset_state["scale_in_available"] = False
            asset_state["scale_in_used"] = True
            asset_state["scale_in_stage"] = None
            asset_state["scale_in_last_ts"] = time.time()

            clear_pending()
            ORDER_INDEX.pop(order_id, None)

            try:
                save_asset_state(symbol, asset_state)
            except Exception as e:
                print(f"Error saving console_state (SCALE_IN fill) for {symbol}:", e)

            return jsonify({"ok": True})




        if kind != "ENTRY":
            clear_pending()
            ORDER_INDEX.pop(order_id, None)
            try:
                save_asset_state(symbol, asset_state)
            except Exception as e:
                print(f"Error saving console_state (non-entry fill cleanup) for {symbol}:", e)
            return jsonify({"ok": True})
        print(f"[EXEC-ENTRY] order_id={order_id} symbol={symbol} kind={kind} side={side} qty={qty} price={fill_price} mode={mode} env={env}")

        asset_state["just_filled_ts"] = time.time()
        asset_state["position"] = order_pos
        asset_state["avg_price"] = fill_price
        asset_state["entry_price"] = fill_price
        asset_state["last_entry_ts"] = now_ts
        asset_state["tempo_4pt_unlock_ts"] = None # added this
        asset_state["pnl"] = 0.0
        asset_state["order_count"] = (asset_state.get("order_count") or 0) + 1
        asset_state["exit_mode"] = mode
        asset_state["env"] = env
        asset_state["five_min_ok"] = True
        asset_state["stop_loss_price"] = None
        asset_state["stop_loss_status"] = None

        pending_trade_mode = (
            info.get("trade_mode")
            or extra.get("trade_mode")
            or asset_state.get("pending_trade_mode")
        )
        pending_trade_mode = str(pending_trade_mode or "").upper().strip()

        if pending_trade_mode in ("SCALP", "RUNNER"):
            asset_state["trade_mode"] = pending_trade_mode
        else:
            asset_state["trade_mode"] = None

        
        asset_state["entry_zone"] = _compute_zone_from_asset(asset_state)
        asset_state["runner_4pt_unlocked"] = False
        # asset_state["entry_main_renko_ts"] = asset_state.get("main_renko_ts")
        # asset_state["entry_main_renko_color"] = asset_state.get("main_renko_color")
        # asset_state["entry_renko_color"] = asset_state.get("renko_color")
        asset_state["entry_main_renko_ts"] = asset_state.get("main_renko_ts")
        asset_state["entry_high_renko_ts"] = asset_state.get("high_renko_ts")
        asset_state["entry_main_renko_color"] = asset_state.get("main_renko_color")
        asset_state["entry_renko_color"] = asset_state.get("renko_color")
        # =====================================================
        # INITIAL POST-ENTRY EXIT LOCK
        # Regular entry only. Do NOT run for scale-in fills.
        # Locks emotional/manual/TP/protect exits until first fresh 6pt bar.
        # =====================================================
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


        # asset_state["four_pt_invalidation_enabled"] = False
        # asset_state["next_bar_exit_allowed"] = False
        # asset_state["four_pt_invalidation_allowed"] = False
        asset_state["four_pt_invalidation_enabled"] = False
        asset_state["next_bar_exit_allowed"] = True
        asset_state["four_pt_invalidation_allowed"] = True
        asset_state["high_next_bar_exit_allowed"] = True

        asset_state["zone_type"] = _compute_zone_from_asset(asset_state)

        # # 6pt optional exit should start OFF by default
        # asset_state["main_flip_exit_enabled"] = False
        # # 2.5pt TP/protect lock starts OFF by default on new entry.
        # asset_state["two_half_tp_lock_enabled"] = False
        # asset_state["two_half_tp_lock_base_color"] = None
        # asset_state["two_half_tp_lock_released"] = False
        # asset_state["two_half_tp_lock_started_ts"] = None
        # asset_state["two_half_tp_lock_released_ts"] = None

        # asset_state["points_tp_enabled"] = False
        # asset_state["points_tp_target"] = 10.0

        # FREE-zone protection:
        # If entry happens in FREE zone, auto-enable 6pt flip exit.
        # In NORMAL zone, keep it manually controlled/off.
        entry_zone = asset_state.get("entry_zone") or asset_state.get("zone_type")
        asset_state["main_flip_exit_enabled"] = (entry_zone == "FREE")

        # 2.5 lock starts ON by default if 2.5 agrees with the trade.
        # First opposite 2.5 flip releases it permanently for this trade.
        # current_two_half = (asset_state.get("two_half_renko_color") or "neutral").lower()
        # aligned_two_half = _trade_aligned_two_half_color(order_pos)

        # if current_two_half == aligned_two_half:
        #     asset_state["two_half_tp_lock_enabled"] = True
        #     asset_state["two_half_tp_lock_base_color"] = current_two_half
        #     asset_state["two_half_tp_lock_released"] = False
        #     asset_state["two_half_tp_lock_started_ts"] = now_ts
        #     asset_state["two_half_tp_lock_released_ts"] = None
        # else:
        #     # If already opposite/neutral at fill, do not cage the trade.
        #     asset_state["two_half_tp_lock_enabled"] = False
        #     asset_state["two_half_tp_lock_base_color"] = None
        #     asset_state["two_half_tp_lock_released"] = (current_two_half == _trade_opposite_two_half_color(order_pos))
        #     asset_state["two_half_tp_lock_started_ts"] = None
        #     asset_state["two_half_tp_lock_released_ts"] = now_ts if asset_state["two_half_tp_lock_released"] else None

        # Dynamic 2.5 management lock starts from the current 2.5 color.
        current_two_half = (asset_state.get("two_half_renko_color") or "neutral").lower()
        aligned_two_half = _trade_aligned_two_half_color(order_pos)
        opposite_two_half = _trade_opposite_two_half_color(order_pos)

        if current_two_half == opposite_two_half:
            # Rare edge case: if fill happens while 2.5 is already opposite,
            # management starts unlocked.
            asset_state["two_half_tp_lock_enabled"] = False
            asset_state["two_half_tp_lock_base_color"] = None
            asset_state["two_half_tp_lock_released"] = True
            asset_state["two_half_tp_lock_started_ts"] = None
            asset_state["two_half_tp_lock_released_ts"] = now_ts
        else:
            # Normal case: backend entry gate already required 2.5 agreement,
            # so management starts locked.
            asset_state["two_half_tp_lock_enabled"] = True
            asset_state["two_half_tp_lock_base_color"] = current_two_half if current_two_half in ("green", "red") else None
            asset_state["two_half_tp_lock_released"] = False
            asset_state["two_half_tp_lock_started_ts"] = now_ts
            asset_state["two_half_tp_lock_released_ts"] = None

        # Default profit capture: always ON at 15 points.
        asset_state["points_tp_enabled"] = True
        asset_state["points_tp_target"] = 15.0
        asset_state["rithmic_open_points"] = 0.0
        asset_state["rithmic_point_value"] = _point_value_for_contract(
            asset_state.get("rithmic_symbol") or resolve_rithmic_symbol(symbol),
            symbol
        )
        asset_state["points_tp_hit_ts"] = None
        asset_state["protect_enabled"] = False
        asset_state["protect_threshold_points"] = -2.0
        asset_state["protect_hit_ts"] = None

        # consume daily trade limit ONLY on real filled ENTRY
        consume_ok = _consume_trade_limit_on_fill()
        if not consume_ok:
            print(f"WARNING: trade limit consume failed on ENTRY fill for {symbol}, order_id={order_id}")

        clear_pending()
        ORDER_INDEX.pop(order_id, None)

        try:
            save_asset_state(symbol, asset_state)
        except Exception as e:
            print(f"Error saving console_state (ENTRY) for {symbol}:", e)

        return jsonify({"ok": True})

    return jsonify({"ok": True})

if __name__ == "__main__":
    app.run(debug=True)