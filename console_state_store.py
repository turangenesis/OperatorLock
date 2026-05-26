

# console_state_store.py
import math
from datetime import datetime, timezone

from orders_store import get_client

COLLECTION_NAME = "console_state"




_ASSET_TS_FIELDS = (
    "last_heartbeat_ts",
    "one_renko_ts",
    "last_renko_ts",
    "last_entry_ts",
    "scale_in_last_ts",
    "last_exit_ts",
    "tempo_4pt_unlock_ts",
    "initial_exit_lock_started_ts",
    "initial_exit_lock_released_ts",
    "initial_exit_lock_base_main_ts",

        # 8pt next-bar exit
    "high_next_bar_exit_started_ts",
    "high_next_bar_exit_base_ts",

    # Renko streams
    "two_half_renko_ts",    # 2.5pt
    "main_renko_ts",        # 6pt
    # "entry_main_renko_ts",
    # "high_renko_ts",        # 8pt
    # "macro_renko_ts",       # 12pt

    "entry_main_renko_ts",
    "entry_high_renko_ts",
    "high_renko_ts",        # 8pt
    "macro_renko_ts",       # 12pt

    # 2.5pt TP/protect discipline lock
    "two_half_tp_lock_started_ts",
    "two_half_tp_lock_released_ts",

    "tempo_ts",
    "tempo_spent_ts",
    "tempo_last_bar_ts",
    "preorder_created_ts",
    "preorder_bar_base_ts",
    "intent_created_ts",
    "intent_bar_base_ts",
    "intent_ready_bar_ts",
)






def _asset_doc(client, symbol: str):
    return client.collection(COLLECTION_NAME).document(symbol)


def _global_doc(client):
    return client.collection(COLLECTION_NAME).document("_global")


def _sanitize_value(v):
    """
    Firestore-safe sanitizer:
    - Convert float NaN/Inf to None
    - Recursively sanitize dicts/lists
    """
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, dict):
        return {k: _sanitize_value(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_sanitize_value(x) for x in v]
    if isinstance(v, tuple):
        return [_sanitize_value(x) for x in v]
    return v


def save_asset_state(symbol: str, asset_state: dict):
    """
    Persist a single asset's live console state to Firestore.

    We keep it lightweight and only store primitive fields. Time-like fields
    are stored as floats (epoch seconds) for easy math on reload.
    """
    client = get_client()

    payload = {}
    for key, value in asset_state.items():
        if key in _ASSET_TS_FIELDS:
            if value is None:
                payload[key] = None
            elif isinstance(value, (int, float)):
                fv = float(value)
                payload[key] = fv if math.isfinite(fv) else None
            else:
                # Support older datetime-style values if they ever appear.
                try:
                    fv = float(value)
                    payload[key] = fv if math.isfinite(fv) else None
                except Exception:
                    try:
                        ts = value.timestamp()
                        ts = float(ts)
                        payload[key] = ts if math.isfinite(ts) else None
                    except Exception:
                        payload[key] = None
        else:
            payload[key] = _sanitize_value(value)

    payload["updatedAt"] = datetime.now(timezone.utc)
    _asset_doc(client, symbol).set(payload, merge=True)


def load_asset_states() -> dict:
    """
    Load all per-asset console state snapshots from Firestore.

    Returns: {symbol: {field: value, ...}, ...}
    """
    client = get_client()
    snapshots = {}

    for doc in client.collection(COLLECTION_NAME).stream():
        if doc.id == "_global":
            continue

        data = doc.to_dict() or {}

        # Normalize time-like fields back to floats if Firestore stored Timestamps
        for field in _ASSET_TS_FIELDS:
            v = data.get(field)
            if hasattr(v, "timestamp"):
                try:
                    data[field] = v.timestamp()
                except Exception:
                    pass

        # Also sanitize any non-finite floats that might exist from older runs
        data = _sanitize_value(data)

        snapshots[doc.id] = data

    return snapshots


# def save_global_state(global_state: dict):
#     """
#     Persist global console-level settings that you care about surviving
#     a dyno restart (env, equity, trade_lock).
#     """
#     client = get_client()
#     payload = {
#         "equity": _sanitize_value(global_state.get("equity")),
#         "env": global_state.get("env"),
#         "trade_lock": global_state.get("trade_lock"),
#         "updatedAt": datetime.now(timezone.utc),
#     }
#     _global_doc(client).set(payload, merge=True)

def save_global_state(global_state: dict):
    """
    Persist global console-level settings that must survive dyno restart.
    """
    client = get_client()

    payload = {
        # existing
        "equity": _sanitize_value(global_state.get("equity")),
        "env": global_state.get("env"),
        "trade_lock": bool(global_state.get("trade_lock")),
        "daily_pnl_usd": _sanitize_value(global_state.get("daily_pnl_usd")),
        "daily_pnl_pct": _sanitize_value(global_state.get("daily_pnl_pct")),
        "daily_stop_remaining_pct": _sanitize_value(global_state.get("daily_stop_remaining_pct")),


        # ✅ daily stop fields
        "daily_stop_enabled": bool(global_state.get("daily_stop_enabled", False)), # changed this!
        "daily_stop_limit_pct": _sanitize_value(global_state.get("daily_stop_limit_pct")),
        "daily_start_equity": _sanitize_value(global_state.get("daily_start_equity")),
        "daily_stop_date": global_state.get("daily_stop_date"),
        "daily_stop_triggered": bool(global_state.get("daily_stop_triggered", False)),
        "daily_stop_triggered_ts": _sanitize_value(global_state.get("daily_stop_triggered_ts")),
        "daily_stop_triggered_reason": global_state.get("daily_stop_triggered_reason"),
        "daily_stop_triggered_equity": _sanitize_value(global_state.get("daily_stop_triggered_equity")),
        "daily_stop_triggered_dd_pct": _sanitize_value(global_state.get("daily_stop_triggered_dd_pct")),

#######
        # ✅ daily trade limit fields
        "max_trades_per_day": _sanitize_value(global_state.get("max_trades_per_day")),
        "trades_taken_today": _sanitize_value(global_state.get("trades_taken_today")),
        "trades_remaining_today": _sanitize_value(global_state.get("trades_remaining_today")),
        "daily_trade_limit_date": global_state.get("daily_trade_limit_date"),
#####

        "updatedAt": datetime.now(timezone.utc),

        "five_min_trade_bucket": _sanitize_value(global_state.get("five_min_trade_bucket")),
        "five_min_trade_lock_active": bool(global_state.get("five_min_trade_lock_active", False)),
        "five_min_trade_lock_remaining_s": _sanitize_value(global_state.get("five_min_trade_lock_remaining_s")),
        "post_exit_lock_active": bool(global_state.get("post_exit_lock_active", False)),
        "post_exit_lock_started_ts": _sanitize_value(global_state.get("post_exit_lock_started_ts")),
        "post_exit_lock_expires_ts": _sanitize_value(global_state.get("post_exit_lock_expires_ts")),
        "post_exit_lock_remaining_s": _sanitize_value(global_state.get("post_exit_lock_remaining_s")),
    }

    _global_doc(client).set(payload, merge=True)



def load_global_state() -> dict:
    """
    Load the global console snapshot if present.

    Returns {} if nothing is stored yet.
    """
    client = get_client()
    doc = _global_doc(client).get()
    if not doc.exists:
        return {}
    data = doc.to_dict() or {}
    return _sanitize_value(data)
