# OperatorLock — Architecture & Engineering Notes

Architecture and engineering reference for OperatorLock: running the app, module layout, state ownership, the constraint layer, and repo conventions.

## Running the Backend

All commands run from `backend/`:

```bash
cd backend

# Run locally
flask run          # uses FLASK_APP=app.py by default
gunicorn app:app   # production-equivalent

# Import check (no server start needed)
python3 -c "from app import app; print('OK')"
```

No test suite exists. Correctness verification is done by importing the app and checking that routes are registered.

## Environment Setup

The app starts without Firestore — state operations will silently fail but the server runs:

```bash
cd backend
cp ../.env.example .env
# Edit .env — FLASK_SECRET_KEY and BRIDGE_HEARTBEAT_SECRET are the minimum needed locally
flask run
```

`GOOGLE_CLOUD_CREDENTIALS` must be a single-line JSON string of the service account key. Without it, `hydrate_state_from_firestore()` logs a warning and returns empty state. All other functionality works.

## Architecture

### Module Dependency Order

```
config.py
  ↓
stores/ (orders_store, state_store, pnl_store)  — Firestore only, no business logic
  ↓
utils/ (type_helpers, direction, calendar)       — pure functions, no state
  ↓
services/state_manager.py                        — owns `state` dict and `ORDER_INDEX`
  ↓
services/behavioral_engine.py                   — constraint logic, reads/writes state
services/exit_engine.py                         — auto-exit paths, reads/writes state
services/execution_engine.py                    — order lifecycle, imports behavioral + exit
  ↓
routes/ (blueprints)                             — HTTP layer, imports services
  ↓
app.py                                           — factory, registers blueprints
```

**Circular import risk:** `behavioral_engine` needs `exit_engine` (daily stop → enqueue exits), and `exit_engine` imports from `behavioral_engine` indirectly. This is resolved by a lazy import inside `maybe_trigger_daily_stop()` — do not move it to module level.

### State Ownership

A single `state` dict lives in `services/state_manager.py` and is imported directly by every service and route. There is no state passed through function arguments — all modules mutate the shared dict.

```python
state = {
    "global": { "env": "DEMO"|"LIVE", "daily_stop_triggered": bool, ... },
    "assets": { "ES": { "position": int, "pending_order_id": str, ... } },
    "logs":   { "tradingview": [...], "rithmic": [...] },
}
ORDER_INDEX = {}  # order_id → {symbol, side, qty, mode, env, kind}
```

`ORDER_INDEX` is in-memory only. It maps order IDs to context needed when execution reports arrive from the bridge — if it's missing an entry (e.g., after a restart), `execution_engine` falls back to reading the order from Firestore.

### The Constraint Layer

`services/behavioral_engine.py` is the core of the system. Before any entry order is accepted, `routes/orders.py` checks constraints in this order:
1. `daily_stop_triggered` (global)
2. `post_exit_lock_active` (global, 180s cooldown after any close)
3. `five_min_trade_lock_active` (global, one entry per 5-min candle)
4. `trade_lock` (global)
5. `position != 0` (per-asset)
6. `pending_order_id` exists (per-asset)
7. Heartbeat freshness (per-asset, < 10 min)
8. `tempo_ready` (per-asset, one entry per 6pt bar)
9. Zone gate: `compute_zone(a)` from 8pt+12pt Renko agreement

### Renko Webhook Streams

Six independent TradingView webhook streams drive the behavioral engine. Each maps to a route in `routes/webhooks.py`:

| Endpoint | Timeframe | Role |
|---|---|---|
| `/tv_renko` | 4pt | Scale-in unlock, tempo re-arm, 4pt invalidation exit |
| `/tv_renko_two_half` | 2.5pt | TP/protect management lock |
| `/tv_renko_main` | 6pt | Tempo token, initial exit lock release, intent/preorder trigger |
| `/tv_renko_high` | 8pt | Zone input, 8pt next-bar exit, 8pt flip exit |
| `/tv_renko_macro` | 12pt | Zone input |
| `/tv_renko_small` / `/tv_renko_one` | 1pt | Visual only |

### Order Lifecycle

1. `POST /api/orders` → constraint checks → `stores/orders_store.create_order()` writes Firestore doc with `status: PENDING`
2. VPS bridge polls `GET /api/orders/pending` → claims order → executes via Rithmic RAPI+
3. Bridge posts `POST /api/orders/{id}/execution-report` with status WORKING / FILLED / REJECTED / CANCELLED
4. `execution_engine.handle_execution_report()` routes to fill handlers; FILLED on EXIT calls `close_trade()` which activates the 180s post-exit lock

### Firestore Collections

| Collection | Key | Purpose |
|---|---|---|
| `console_state` | `"ES"`, `"_global"` | Per-asset and global runtime state |
| `orders` | order ID | Order lifecycle documents (PENDING → FILLED) |
| `pnl_snapshots` | date string | Daily PnL history |

`state_store.save_asset_state()` and `save_global_state()` are called after every meaningful state mutation. `hydrate_state_from_firestore()` in `state_manager.py` runs once at startup to restore state across dyno restarts.

## Key Conventions

- **No behavior changes without explicit instruction.** The constraint logic in `behavioral_engine.py` is the product — edit it only when specifically requested.
- **`scrub_nonfinite(state)`** must wrap any `state` dict returned in a JSON response — Firestore can write `NaN`/`Inf` that would break JSON serialization.
- **Asset keys are always uppercase UI symbols** (`"ES"`), not Rithmic contract symbols (`"MESM6"`). `config.py` has the mapping tables (`RITHMIC_SYMBOLS`, `CONTRACT_TO_UI`).
- **`ORDER_INDEX` is not persisted** — it is rebuilt from Firestore only when a fill arrives for an unknown order ID.


## Repo Workflow Rules

- Never push directly to `main`.
- Never merge PRs without explicit approval.
- When I say “open PR”, follow `docs/playbooks/OPEN_PR.md`.
- Before committing, run available tests/build checks.
- Do not expose secrets or commit `.env` files.
