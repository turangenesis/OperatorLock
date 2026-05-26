# Behavioral Constraints

This document catalogues every structural constraint enforced by OperatorLock, with the design rationale for each.

The guiding principle: **constraints must be structural, not advisory.** A constraint that can be clicked past under stress provides no protection when protection is most needed.

---

## 1. 5-Minute Candle Lock

**What it does:** After any entry, all subsequent entry requests are rejected until the next 5-minute candle boundary (New York time).

**Why:** The most common form of overtrading is re-entering immediately after a stopped-out trade. The operator is still reactive, still in the emotional state that caused the first entry. The 5-minute candle lock creates a mandatory structural gap between entries — by the next candle, the setup must be re-evaluated from a clean slate.

**How it works:**
- On entry fill, `activate_five_min_lock()` stores the current 5-minute bucket (timestamp floored to 5-minute boundary).
- On each `GET /api/state` call, `ensure_five_min_lock_initialized()` checks whether a new bucket has started and auto-releases the lock if the candle has turned over.
- `POST /api/orders` checks `five_min_trade_lock_active` before accepting any entry.

---

## 2. Post-Exit Cooldown (3-Minute Lock)

**What it does:** After any position close (fill, stop, or manual exit), all entry requests are blocked for 180 seconds.

**Why:** The first 3 minutes after an exit are the most dangerous. If the trade was profitable, the operator wants to "ride the streak." If it was a loss, the operator wants to "make it back immediately." Both states produce impulsive re-entries that violate the original setup criteria. The 180-second wall forces a minimum reset period.

**How it works:**
- `close_trade()` in `execution_engine.py` calls `activate_post_exit_lock()`, which sets `post_exit_lock_expires_ts = now + 180`.
- The lock reports `post_exit_lock_remaining_s` countdown to the dashboard.
- `ensure_post_exit_lock_initialized()` computes remaining time on each state poll and auto-releases when expired.

---

## 3. Tempo Token (One Entry Per 6pt Bar)

**What it does:** Each 6pt Renko bar "mints" one entry token. Spending the token on an entry requires waiting for the next 6pt bar to re-arm before another entry is allowed.

**Why:** The 6pt bar is the primary structural timeframe. Entering multiple times within a single 6pt bar means the operator is reacting to noise below the structural level — typically because a trade stopped out and they immediately re-entered in the same direction or opposite. The tempo token enforces one decision per structural bar.

**How it works:**
- Each `POST /tv_renko_main` (6pt bar update) stores `main_renko_ts`.
- On entry, `tempo_ready` is set to `False` and `tempo_spent_ts = now`.
- Tempo re-arms when a new 6pt bar arrives with `ts > tempo_spent_ts`, OR when the 4pt color changes after the exit (which acts as an early structural signal that conditions have changed).

---

## 4. Initial Exit Lock

**What it does:** After entry fill, manual exits are blocked until the first fresh 6pt bar after entry.

**Why:** The first few seconds/minutes after entry are the highest-risk period for premature exits. The operator sees any tick against them and wants out before the trade has had time to work. The initial exit lock enforces a minimum holding period defined by the structural bar — not an arbitrary time — so the lock respects the actual timeframe being traded.

**How it works:**
- `_handle_entry_fill()` in `execution_engine.py` sets `initial_exit_lock_active = True` and stores `initial_exit_lock_base_main_ts = current_main_renko_ts`.
- Each `POST /tv_renko_main` calls `release_initial_exit_lock_if_needed()`, which releases the lock when a new bar arrives with `ts != base_ts`.
- `POST /api/exit` checks `initial_exit_lock_active` and returns 409 unless `force=true` is passed (for emergency manual override).

---

## 5. 2.5pt TP/Protect Lock

**What it does:** After entry, TP target adjustments and the protect/stop feature are automatically locked until the 2.5pt Renko confirms alignment with the trade direction. Once locked, they remain locked until the 2.5pt Renko flips to the opposite color.

**Why:** The 2.5pt stream is the immediate reaction timeframe — the closest thing to tick-level structure. Immediately after entry, the 2.5pt may be against the position (e.g., entered on a 6pt green bar but the 2.5pt just turned red). Allowing the operator to tighten their protect or lower their TP target in this window rewards capitulation to noise. The lock forces holding through the 2.5pt reaction until the immediate structure confirms the trade.

**How it works:**
- `_handle_entry_fill()` calls `release_two_half_tp_lock_if_needed()` with `color_changed=False` to initialize lock state.
- Each `POST /tv_renko_two_half` calls `release_two_half_tp_lock_if_needed()` with the actual previous/new color and whether a flip occurred.
- `POST /api/protect` and `POST /api/points-take-profit` check `two_half_tp_lock_enabled` before allowing target adjustments.

---

## 6. Zone Entry Gate (8pt + 12pt Structure)

**What it does:** Before any entry, the 8pt and 12pt Renko structures are evaluated. If they agree, the direction is locked (NORMAL zone). If they disagree, the operator must explicitly choose direction (FREE zone). If either is neutral, entries are blocked.

**Why:** Entering against the macro and structural timeframe while following the 6pt alone is the most common source of "fighting the trend" losses. Requiring 8pt/12pt agreement acts as a mandatory higher-timeframe check before every entry. The FREE zone (disagreement) is permitted because conflicting structure is a valid setup — but it requires deliberate commitment rather than automatic direction assignment.

**How it works:**
- `compute_zone(a)` in `behavioral_engine.py` evaluates `high_renko_color` (8pt) and `macro_renko_color` (12pt).
- Returns `"NORMAL"` (agree), `"FREE"` (disagree), or `None` (blocked).
- `POST /api/orders` checks zone and rejects entries if zone is `None`, and requires `direction=BUY/SELL` in FREE zone.

---

## 7. Daily Trade Limit

**What it does:** A configurable maximum number of entries (default: 6) per NY trading day. Once the limit is reached, all entry requests return 409 until the next trading day.

**Why:** Overtrading is the primary mechanism by which a single bad day becomes a catastrophic loss. A trader who would stop after 3 trades normally will take 10+ trades when trying to recover from a drawdown. The daily limit is an absolute structural cap — not a soft limit, not a warning — that prevents the "recovery spiral" from starting.

**How it works:**
- `ensure_daily_stop_day_initialized()` resets counters at NY midnight if the date has changed.
- `consume_trade_limit_on_fill()` decrements `trades_remaining_today` on every entry fill.
- `POST /api/orders` checks `trades_remaining_today <= 0` before accepting any entry.

---

## 8. Daily Stop Loss

**What it does:** A configurable maximum equity drawdown per NY trading day (default: configurable %). When the threshold is hit, all open positions are closed immediately and all entries are blocked for the rest of the day.

**Why:** The daily stop is the last line of defense. Every other constraint reduces the probability of reaching it — but if it's reached anyway, the system must act without asking for confirmation. An operator who has already lost X% in a day is not in the psychological state to decide whether to keep trading.

**How it works:**
- `rithmic_pnl_snapshot` calls `update_daily_stop_metrics()` and `maybe_trigger_daily_stop()` on every PnL update.
- `maybe_trigger_daily_stop()` computes drawdown from `daily_start_equity` and triggers if it exceeds `daily_stop_limit_pct`.
- Trigger is sticky — it persists through restarts by writing `daily_stop_triggered: true` to Firestore.
- `enqueue_daily_stop_exits()` creates exit orders for all open positions.
- `POST /api/orders` checks `daily_stop_triggered` first (highest priority gate).

---

## 9. Scale-In Gate

**What it does:** Adding to an open position is only permitted after specific bar-level confirmation. The scale-in button is only enabled when `scale_in_available = True`.

**Why:** Averaging down (adding to a losing position) is a common path to account destruction. OperatorLock makes scale-in structurally gated — it's not available on demand, and it requires explicit bar-level confirmation before the UI even shows the button as enabled. This separates "a valid add to a working trade" from "adding because I don't want to accept the loss."

**How it works:**
- `update_scale_in_from_6pt()` and `update_scale_in_from_4pt()` in `behavioral_engine.py` evaluate whether bar conditions have advanced enough to permit scale-in.
- `POST /api/scale-in` checks `scale_in_available` and returns 409 if not ready.
- Scale-in is a one-shot per trade: `scale_in_used` prevents a second add-on.
