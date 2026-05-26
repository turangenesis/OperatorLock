# pnl_store.py
import json
import os
import time
from typing import Optional, Tuple

from google.cloud import firestore
from google.oauth2 import service_account

_DOC_PATH = ("risk_inputs", "main")  # collection, doc_id

_firestore_client = None

# small cache so /api/state polling every 1s doesn't hammer Firestore
_CACHE_TTL_SEC = 5
_cached = {"ts": 0.0, "pnl_usd": 0.0}


def _get_client():
    global _firestore_client
    if _firestore_client is not None:
        return _firestore_client

    creds_json = os.environ.get("GOOGLE_CLOUD_CREDENTIALS")
    if not creds_json:
        raise RuntimeError("GOOGLE_CLOUD_CREDENTIALS env var not set")

    info = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(info)
    _firestore_client = firestore.Client(project=info["project_id"], credentials=creds)
    return _firestore_client


def read_manual_pnl_usd() -> float:
    """
    Returns the user's manually entered PnL (USD) from Firestore.
    If missing/invalid, returns 0.0.
    Cached for a few seconds.
    """
    now = time.time()
    if (now - _cached["ts"]) <= _CACHE_TTL_SEC:
        return float(_cached["pnl_usd"] or 0.0)

    try:
        client = _get_client()
        doc = client.collection(_DOC_PATH[0]).document(_DOC_PATH[1]).get()
        if not doc.exists:
            pnl = 0.0
        else:
            data = doc.to_dict() or {}
            pnl_raw = data.get("pnl_usd", 0.0)
            pnl = float(pnl_raw) if pnl_raw is not None else 0.0
    except Exception as e:
        print("read_manual_pnl_usd error:", e)
        pnl = 0.0

    _cached["ts"] = now
    _cached["pnl_usd"] = pnl
    return pnl


def compute_qty(asset: str, pnl_usd: float) -> int:
    """
    Your rule:
    
    qty = max(1, floor(pnl / step))
    """
    asset = (asset or "").upper().strip()
    step = 25000.0 if asset == "ES" else 25000.0 if asset == "GC" else 25000.0

    try:
        pnl = float(pnl_usd)
    except Exception:
        pnl = 0.0

    if pnl < 0:
        pnl = 0.0

    qty = int(pnl // step)
    if qty < 1:
        qty = 1

    # optional hard cap safety (pick any cap you want)
    if qty > 50:
        qty = 50

    return qty


def get_manual_pnl_and_qty(asset: str) -> Tuple[float, int]:
    pnl = read_manual_pnl_usd()
    qty = compute_qty(asset, pnl)
    return pnl, qty
