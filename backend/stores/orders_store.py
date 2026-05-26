# orders_store.py
import json
import os
from datetime import datetime, timezone, timedelta


from google.cloud import firestore
from google.oauth2 import service_account

_firestore_client = None
# LEASE_SECONDS = int(os.environ.get("ORDER_LEASE_SECONDS", "20"))
LEASE_SECONDS = int(os.environ.get("ORDER_LEASE_SECONDS", "200")) # to prevent duplicate orders!

LEASE_OWNER_DEFAULT = "bridge"

def _now_utc():
    return datetime.now(timezone.utc)



def get_client():
    global _firestore_client
    if _firestore_client is not None:
        return _firestore_client

    creds_json = os.environ.get("GOOGLE_CLOUD_CREDENTIALS")
    if not creds_json:
        raise RuntimeError("GOOGLE_CLOUD_CREDENTIALS env var not set")

    info = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(info)
    _firestore_client = firestore.Client(
        project=info["project_id"],
        credentials=creds,
    )
    return _firestore_client

def get_order(order_id: str):
    client = get_client()
    doc = client.collection("orders").document(order_id).get()
    if not doc.exists:
        return None
    data = doc.to_dict() or {}
    data["id"] = doc.id
    return data



def create_order(symbol, exchange, side, qty, source="UI", mode=None, kind="ENTRY", env=None):
    """
    Create a new order in Firestore and return its ID + data.

    kind: "ENTRY" or "EXIT" (critical for execution-report exit guard)
    env: optional "DEMO"/"LIVE" if you want per-order env later
    """
    client = get_client()
    now = datetime.now(timezone.utc)

    # Normalize
    symbol = str(symbol).upper().strip()
    side = str(side).upper().strip()
    exchange = str(exchange or "").upper().strip() or "CME"

    try:
        qty = int(qty)
    except Exception:
        qty = 1

    kind = (kind or "ENTRY")
    kind = str(kind).upper().strip()
    if kind not in ("ENTRY", "EXIT"):
        kind = "ENTRY"

    if env is not None:
        env = str(env).upper().strip()
        if env not in ("DEMO", "LIVE"):
            env = None

    doc_ref = client.collection("orders").document()
    data = {
        "symbol": symbol,
        "exchange": exchange,
        "side": side,           # "BUY" / "SELL"
        "qty": qty,
        "status": "PENDING",    # PENDING / WORKING / FILLED / REJECTED / CANCELLED
        "source": source,       # "UI", "EXIT_ENGINE", etc.
        "mode": mode,           # Mode A/B etc
        "kind": kind,           # ✅ ENTRY / EXIT (PERSISTED)
        "env": env,             # optional
        "createdAt": now,
        "updatedAt": now,
    }
    doc_ref.set(data)
    data["id"] = doc_ref.id
    return data




def get_pending_orders(limit=20, owner=None):
    """
    Atomically claim (lease) PENDING orders so they are returned only once
    per lease window.

    - owner: a string that identifies who claimed it (bridge instance).
    - lease expires automatically after LEASE_SECONDS, so stuck claims recover.

    Returns: list[dict] orders claimed in this call
    """
    client = get_client()
    owner = owner or LEASE_OWNER_DEFAULT

    now = _now_utc()
    lease_until = now + timedelta(seconds=LEASE_SECONDS)

    # Query PENDING orders oldest-first
    docs = (
        client.collection("orders")
        .where("status", "==", "PENDING")
        .order_by("createdAt")
        .limit(limit)
        .stream()
    )

    claimed = []

    for snap in docs:
        doc_ref = snap.reference

        @firestore.transactional
        def _try_claim(tx):
            #fresh = tx.get(doc_ref)
            fresh = next(tx.get(doc_ref), None)

            if not fresh.exists:
                return None

            data = fresh.to_dict() or {}
            if data.get("status") != "PENDING":
                return None

            # If already leased and not expired, do not return it
            existing_until = data.get("leaseUntil")
            if existing_until is not None:
                # Firestore may return Timestamp; normalize
                try:
                    if hasattr(existing_until, "to_datetime"):
                        existing_until_dt = existing_until.to_datetime().replace(tzinfo=timezone.utc)
                    elif hasattr(existing_until, "timestamp"):
                        existing_until_dt = datetime.fromtimestamp(existing_until.timestamp(), tz=timezone.utc)
                    else:
                        existing_until_dt = existing_until
                except Exception:
                    existing_until_dt = None

                if existing_until_dt and existing_until_dt > now:
                    return None  # still leased

            # Claim it
            tx.update(doc_ref, {
                "leaseOwner": owner,
                "leaseUntil": lease_until,
                "leasedAt": now,
                "updatedAt": now,
            })

            data["id"] = fresh.id
            data["leaseOwner"] = owner
            data["leaseUntil"] = lease_until
            data["leasedAt"] = now
            return data

        tx = client.transaction()
        item = _try_claim(tx)
        if item:
            claimed.append(item)

    return claimed




def update_order_status(order_id, status, extra=None):
    client = get_client()
    doc_ref = client.collection("orders").document(order_id)

    update_data = {
        "status": status,
        "updatedAt": _now_utc(),
    }

    # Clear lease once order is no longer pending/working
    # (even if you keep WORKING in pending query later, this is safe)
    if status in ("FILLED", "REJECTED", "CANCELLED"):
        update_data["leaseOwner"] = firestore.DELETE_FIELD
        update_data["leaseUntil"] = firestore.DELETE_FIELD
        update_data["leasedAt"] = firestore.DELETE_FIELD

    if extra:
        update_data.update(extra)

    doc_ref.update(update_data)

