# Architecture

## Component Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         TradingView Cloud                               │
│  Renko alerts fire on each new bar.                                     │
│  6 independent webhook streams → POST /tv_renko_*                       │
└──────────────────────────────┬──────────────────────────────────────────┘
                               │ HTTPS webhooks (bar color updates)
                               ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                    Flask Backend (Heroku dyno)                          │
│                                                                         │
│  routes/webhooks.py   → behavioral_engine.py                           │
│  routes/orders.py     → execution_engine.py → stores/orders_store.py   │
│  routes/controls.py   → exit_engine.py      → stores/state_store.py    │
│  routes/bridge.py     → state_manager.py                               │
│  routes/state.py      → (read-only)                                    │
│                                                                         │
│  services/state_manager.py  — single in-memory state dict              │
│  services/behavioral_engine.py — constraint gates and locks            │
│  services/execution_engine.py  — order lifecycle and position mgmt     │
│  services/exit_engine.py       — auto-exit orchestration               │
└───────────┬──────────────────────────────────────┬──────────────────────┘
            │ Firestore read/write                  │ JSON REST
            ▼                                       ▼
┌───────────────────────┐              ┌────────────────────────────────┐
│   Google Firestore    │              │   Operator Dashboard (Browser) │
│                       │              │                                │
│  collection:          │              │  GET /api/state  (poll ~2s)    │
│    console_state      │              │  POST /api/orders              │
│    orders             │              │  POST /api/exit                │
│    pnl_snapshots      │              │  POST /api/protect             │
└───────────────────────┘              │  ... (see api-reference.md)   │
                                       └────────────────────────────────┘
                                                                        
┌─────────────────────────────────────────────────────────────────────────┐
│                    C# VPS Bridge (Windows Server)                       │
│                                                                         │
│  Polls GET /api/orders/pending every ~1s                                │
│  Executes claimed orders via Rithmic RAPI+                              │
│  Posts execution reports: POST /api/orders/{id}/execution-report        │
│  Sends heartbeat: POST /api/bridge/heartbeat                            │
│  Sends PnL snapshots: POST /api/rithmic/pnl-snapshot                   │
└────────────────────────────────────────┬────────────────────────────────┘
                                         │ Rithmic RAPI+ (proprietary TCP)
                                         ▼
                               ┌──────────────────┐
                               │  Rithmic Gateway  │
                               │  (CME execution)  │
                               └──────────────────┘
```

---

## Data Flow: From Signal to Executed Order

1. **TradingView** fires a webhook when a new Renko bar closes. The payload contains the asset symbol and bar color (green/red/neutral).

2. **`routes/webhooks.py`** validates the secret, identifies the asset, and calls into `behavioral_engine.py` to update bar colors and evaluate any lock state changes (tempo re-arm, initial exit lock release, 2.5pt lock release, scale-in unlock).

3. **Operator** sees updated state in the dashboard (polled every ~2 seconds from `GET /api/state`) and decides to enter a trade.

4. **`POST /api/orders`** is received by `routes/orders.py`, which runs the full constraint gauntlet:
   - Daily stop not triggered
   - No post-exit cooldown active
   - No 5-minute candle lock active
   - No open position (no pyramid entries)
   - No pending order already queued
   - Heartbeat fresh (< 10 minutes)
   - Tempo token READY
   - Zone entry gate passes (8pt/12pt agreement or explicit FREE zone direction)

5. If all gates pass, `stores/orders_store.py` writes a new order document to Firestore with `status: "PENDING"`. The order ID is stored in `ORDER_INDEX` (in-memory) and `state["assets"][sym]["pending_order_id"]`.

6. **VPS Bridge** polls `GET /api/orders/pending` (authenticated with secret), claims the order, and submits it to Rithmic.

7. **Rithmic** confirms the order is working (`WORKING`) and eventually fills it (`FILLED`).

8. **VPS Bridge** posts `POST /api/orders/{id}/execution-report` with the fill price and status.

9. **`routes/orders.py`** delegates to `execution_engine.handle_execution_report()`, which:
   - Updates `state["assets"][sym]["position"]` and `avg_price`
   - Records trade history
   - Activates the initial exit lock
   - Initializes the 2.5pt TP/protect lock
   - Consumes the daily trade limit counter
   - Clears the pending order

---

## State Machine: Position Lifecycle

```
IDLE ──────────────────────────────────────────────────────────────────────┐
  │                                                                         │
  │ POST /api/orders (all gates pass)                                       │
  ▼                                                                         │
PENDING (order written to Firestore)                                        │
  │                                                                         │
  │ VPS bridge claims → Rithmic submits                                     │
  ▼                                                                         │
WORKING (order in Rithmic order book)                                       │
  │                                                                         │
  │ FILLED execution-report                                                 │
  ▼                                                                         │
OPEN (position > 0 or < 0)                                                  │
  │                                                                         │
  │ Any of:                                                                 │
  │  - POST /api/exit (manual, respects initial exit lock)                  │
  │  - POST /api/exit-all (force bypass initial lock)                       │
  │  - Auto: points TP target hit (from PnL snapshot)                       │
  │  - Auto: protect threshold hit (from PnL snapshot)                      │
  │  - Auto: 4pt invalidation (opposite 4pt bar while enabled)              │
  │  - Auto: 6pt flip exit (opposite 6pt bar while main_flip_exit_enabled)  │
  │  - Auto: 8pt flip exit (opposite 8pt bar while exit_tf == "high")       │
  │  - Auto: 8pt next-bar exit (next 8pt bar after arming)                  │
  │  - Auto: TP signal count (tp_count >= tp_target while tp_armed)         │
  │  - Auto: daily stop (equity drawdown trigger)                           │
  ▼                                                                         │
PENDING EXIT (exit order written to Firestore)                              │
  │                                                                         │
  │ FILLED execution-report                                                 │
  ▼                                                                         │
CLOSED → close_trade() → 180s post-exit cooldown activated ───────────────┘
```

---

## Multi-Timeframe Renko Architecture

Six independent Renko streams drive the behavioral engine. Each stream fires its own TradingView webhook alert.

| Stream | Size | Role |
|---|---|---|
| `tv_renko_one` | 1pt | Visual only — no behavioral effect |
| `tv_renko` | 4pt | Scale-in unlock, tempo re-arm, 4pt invalidation exit |
| `tv_renko_two_half` | 2.5pt | Entry filter, TP/protect management lock |
| `tv_renko_main` | 6pt | Tempo token, initial exit lock release, intent/preorder trigger, 6pt flip exit |
| `tv_renko_high` | 8pt | Zone (with 12pt), 8pt next-bar exit, 8pt flip exit |
| `tv_renko_macro` | 12pt | Zone (with 8pt) |

The 8pt and 12pt streams together define the **zone**:
- Both green or both red → **NORMAL** (direction locked to 8pt color)
- Disagree → **FREE** (operator must explicitly choose direction)
- Either neutral → entry blocked

---

## In-Memory State Model

All runtime state lives in a single dict owned by `services/state_manager.py`:

```python
state = {
    "global": {
        "equity": float,
        "env": "DEMO" | "LIVE",
        "five_min_trade_lock_active": bool,
        "post_exit_lock_active": bool,
        "daily_stop_triggered": bool,
        "trades_remaining_today": int,
        # ... ~40 more fields
    },
    "assets": {
        "ES": {
            "position": int,           # +N = long, -N = short, 0 = flat
            "avg_price": float,
            "pending_order_id": str,
            "main_renko_color": str,   # "green" | "red" | "neutral"
            "high_renko_color": str,
            "macro_renko_color": str,
            "two_half_renko_color": str,
            "tempo_ready": bool,
            "initial_exit_lock_active": bool,
            "zone_type": "NORMAL" | "FREE" | None,
            # ... ~80 more fields
        }
    },
    "logs": {
        "tradingview": [...],   # ring buffer, last 100 entries
        "rithmic": [...],
    }
}
```

The state dict is hydrated from Firestore on startup and written back on every meaningful state change. If the dyno restarts, the next request triggers re-hydration from Firestore.

`ORDER_INDEX` is a separate in-memory dict mapping `order_id → {symbol, side, qty, mode, env, kind}` used to look up context when execution reports arrive without the original payload.
