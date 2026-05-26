# API Reference

## Authentication

### Operator Dashboard Routes (login_required)
All `/api/*` routes marked **[auth]** require an active session cookie. Obtain one via `POST /api/login`.

### Bridge Routes
Bridge endpoints authenticate via a shared secret (`BRIDGE_HEARTBEAT_SECRET`) sent in the request body or as a query parameter. No session cookie required.

### TradingView Webhook Routes
Webhook endpoints authenticate via `TV_WEBHOOK_SECRET` in the JSON payload body (`"secret": "..."`).

---

## Auth

### `POST /api/login`
Authenticate the operator session.

**Request:**
```json
{ "password": "your-dashboard-password" }
```

**Response:**
```json
{ "ok": true }
```
Sets a session cookie. If `DASHBOARD_PASSWORD` is not configured, always succeeds.

---

### `POST /api/logout`
Clear the session.

**Response:** `{ "ok": true }`

---

### `GET /api/auth/status`
Check authentication state.

**Response:**
```json
{ "authenticated": true, "password_required": true }
```

---

## State

### `GET /api/state` [auth]
Returns the full in-memory state dict with all computed flags, lock states, connectivity indicators, and tempo token status.

This endpoint also runs self-healing logic (releases expired locks, backfills missing fields for older Firestore snapshots, computes zone type).

**Response:** Full `state` dict — see `services/state_manager.py` for the complete structure.

---

## Orders

### `POST /api/orders` [auth]
Create a market or pre-order entry.

**Request (asset-aware entry):**
```json
{
  "asset": "ES",
  "trade_mode": "SCALP",        // or "RUNNER"
  "order_type": "market",       // or "preorder"
  "direction": "BUY",           // required in FREE zone; omit in NORMAL zone
  "entry_size_mode": "6pt",     // or "9pt"
  "entry_override": false       // true = bypass structural gates (use with care)
}
```

**Constraint checks (in order):**
1. Daily stop not triggered
2. Post-exit cooldown not active
3. 5-minute candle lock not active
4. No existing pending order
5. No open position (no pyramiding)
6. Heartbeat fresh (< 10 minutes)
7. Tempo token READY
8. Zone entry gate: 8pt/12pt structure agrees or FREE zone with explicit direction

**Response (success):**
```json
{ "ok": true, "order": { "id": "...", "symbol": "MESM6", "side": "BUY", "qty": 5 }, "state": {...} }
```

**Response (blocked):**
```json
{ "ok": false, "error": "5 MIN LOCK ACTIVE — one trade already used this candle (04:23 remaining)" }
```

---

### `GET /api/orders/pending`
Bridge polling endpoint. Returns orders with `status: "PENDING"` for the bridge to claim and execute.

**Query params:**
- `secret` — must equal `BRIDGE_HEARTBEAT_SECRET`
- `limit` — max results (default: 20)
- `owner` — claimant identifier (default: "bridge")

**Response:**
```json
{
  "ok": true,
  "orders": [
    { "id": "...", "asset": "MESM6", "exchange": "CME", "side": "BUY", "qty": 5, "mode": "A", "env": "LIVE", "kind": "ENTRY" }
  ]
}
```

---

### `POST /api/orders/<order_id>/execution-report`
Report execution status for an order. Called by the VPS bridge.

**Request:**
```json
{
  "status": "FILLED",           // WORKING | FILLED | REJECTED | CANCELLED
  "extra": {
    "fill_price": 5250.25,
    "symbol": "MESM6",
    "side": "BUY",
    "qty": 5
  }
}
```

---

## Controls

### `POST /api/exit` [auth]
Manually exit an open position.

**Request:**
```json
{ "asset": "ES", "force": false }
```
`force: true` bypasses the initial exit lock (emergency override).

**Response:** `{ "ok": true, "order": { "id": "..." } }`

---

### `POST /api/exit-all` [auth]
Close all open positions across all assets.

**Request:**
```json
{ "force": true }
```

**Response:**
```json
{
  "ok": true,
  "orders": [{ "asset": "ES", "order_id": "...", "side": "SELL", "qty": 5 }],
  "skipped": []
}
```

---

### `POST /api/scale-in` [auth]
Add to an open position (only available when `scale_in_available = true`).

**Request:**
```json
{ "asset": "ES" }
```

---

### `POST /api/protect` [auth]
Enable/disable the protect feature (soft stop based on open points).

**Request:**
```json
{
  "asset": "ES",
  "enabled": true,
  "threshold": -2.0    // allowed: -2.0 or 0.5 points
}
```

---

### `POST /api/points-take-profit` [auth]
Enable/configure the points TP (auto-exit at target open points).

**Request:**
```json
{ "asset": "ES", "enabled": true, "target": 15.0 }
```

---

### `POST /api/take-profit` [auth]
Arm/disarm the signal-count TP (exits after N opposite 6pt bars).

**Request (toggle arm):**
```json
{ "asset": "ES", "tp_arm": "toggle" }
```

**Request (set target):**
```json
{ "asset": "ES", "tp_target": 3, "reset": true }
```

---

### `POST /api/high-next-bar-exit` [auth]
Toggle the 8pt next-bar exit (exits on the next 8pt bar after arming).

**Request:**
```json
{ "asset": "ES" }
```

---

### `POST /api/main-flip-exit` [auth]
Toggle the 6pt flip exit (exits when 6pt bar flips opposite to position).

**Request:**
```json
{ "asset": "ES" }
```

---

### `POST /api/four-pt-invalidation` [auth]
Toggle the 4pt invalidation exit (exits when 4pt bar flips opposite to position).

**Request:**
```json
{ "asset": "ES" }
```

---

### `POST /api/auto-exit` [auth]
Set the auto-exit timeframe. Currently locked to "6pt" — this endpoint is a no-op that confirms the setting.

**Request:**
```json
{ "asset": "ES" }
```

---

### `POST /api/intent` [auth]
Create a next-bar entry intent. The system marks READY on the next 6pt bar and auto-expires one bar later if unused.

**Request:**
```json
{ "asset": "ES" }
```

---

### `POST /api/intent/cancel` [auth]
Cancel an active entry intent.

**Request:**
```json
{ "asset": "ES" }
```

---

### `POST /api/preorder/cancel` [auth]
Cancel an active pre-order.

**Request:**
```json
{ "asset": "ES" }
```

---

### `POST /api/two-half-tp-lock` [auth]
Always returns 423 — manual 2.5pt lock management is disabled. The lock is automatic only.

---

### `POST /api/trade-limit/consume` [auth]
Manually consume one daily trade counter slot. Used for reconciliation.

---

### `POST /api/env` [auth]
Switch between DEMO and LIVE execution modes.

**Request:**
```json
{ "env": "LIVE" }
```

---

## Bridge

### `POST /api/bridge/heartbeat`
VPS bridge status update. Requires `secret` in body.

**Request:**
```json
{
  "secret": "...",
  "tsConnected": true,
  "mdConnected": true,
  "repoOk": true,
  "account": "U12345",
  "tradeRoute": "PAPER",
  "lastRapiEventUtc": "2024-01-15T14:32:00Z"
}
```

---

### `POST /api/rithmic/pnl-snapshot`
PnL and position update from the VPS bridge. Triggers daily stop check and auto-exit evaluation.

**Request:**
```json
{
  "secret": "...",
  "realized": 250.0,
  "unrealized": -125.0,
  "accountBalance": 50125.0,
  "symbols": [
    { "symbol": "MESM6", "position": 5, "openPnl": -125.0 }
  ]
}
```

---

## TradingView Webhooks

All webhook endpoints accept `POST` and require `"secret": "{{TV_WEBHOOK_SECRET}}"` in the body.

### `POST /tv_heartbeat`
Price connectivity heartbeat. Updates `last_heartbeat_ts` and `last_price`.

```json
{ "secret": "...", "symbol": "ES", "price": 5250.25, "timestamp": 1705329120 }
```

### `POST /tv_renko`
4pt Renko bar update.

```json
{ "secret": "...", "asset": "ES", "color": "green", "bar_id": "2024-01-15T14:30:00" }
```

### `POST /tv_renko_two_half`
2.5pt Renko bar update.

### `POST /tv_renko_main`
6pt Renko bar update (main/tempo timeframe).

### `POST /tv_renko_high`
8pt Renko bar update (structure timeframe).

### `POST /tv_renko_macro`
12pt Renko bar update (macro timeframe).

### `POST /tv_renko_small`
1pt Renko visual update (no behavioral effect).

### `POST /tv_renko_one`
1pt Renko visual update (alternate endpoint alias).

### `POST /tv_level_exit`
Horizontal price level exit trigger. Creates an exit order for the named asset.

```json
{ "secret": "...", "asset": "ES" }
```
