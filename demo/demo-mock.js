/* ============================================================================
 *  OperatorLock — DEMO MOCK ENGINE
 *  ----------------------------------------------------------------------------
 *  Self-contained, zero-backend simulation. Intercepts window.fetch so the real
 *  dashboard renders against a simulated /api/state that evolves like a live
 *  trading session. No Firestore, no Rithmic, no webhooks, no secrets.
 *
 *  It also drives an autonomous "narrative" so the demo is alive hands-off, and
 *  responds to real button clicks (entry / exit / take-profit) so you can demo
 *  interactively. To hide the corner banner: append ?banner=0 to the URL.
 * ========================================================================== */
(function () {
  "use strict";

  const POINT_VALUE = 50.0;     // ES: $50 / point
  const ENTRY_QTY = 1;
  const POST_EXIT_LOCK_S = 180; // 180s cooldown after any close
  const FIVE_MIN_S = 300;
  const MAX_TRADES = 6;

  const nowS = () => Date.now() / 1000;
  const rnd = (a, b) => a + Math.random() * (b - a);
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const todayStr = () => new Date().toISOString().slice(0, 10);

  // -- market model ----------------------------------------------------------
  let price = 5302.25;
  let drift = 0;                 // short-term directional bias (set on entry)

  function stepPrice() {
    // random walk + mean-reverting drift bias while in a position
    const noise = rnd(-0.55, 0.55);
    price = +(price + noise + drift).toFixed(2);
    drift *= 0.97;
  }

  // -- renko streams ---------------------------------------------------------
  // Each stream holds a color (green/red/neutral) + last-flip timestamp.
  function mkStream(color) { return { color, ts: nowS(), changed: false }; }
  const renko = {
    one:      mkStream("green"),  // 1pt   visual
    twoHalf:  mkStream("green"),  // 2.5pt management lock
    four:     mkStream("green"),  // 4pt   scale-in / invalidation
    main:     mkStream("green"),  // 6pt   tempo token
    high:     mkStream("green"),  // 8pt   zone input
    macro:    mkStream("green"),  // 12pt  zone input
  };
  // flip cadence (seconds) per stream — slower TF flips less often
  const flipEvery = { one: 4, twoHalf: 7, four: 11, main: 22, high: 40, macro: 70 };
  const lastFlip = {};
  Object.keys(flipEvery).forEach(k => (lastFlip[k] = nowS()));

  function maybeFlip(now) {
    for (const k of Object.keys(flipEvery)) {
      if (now - lastFlip[k] >= flipEvery[k] * rnd(0.6, 1.4)) {
        lastFlip[k] = now;
        const s = renko[k];
        // bias the flip toward current price drift so streams look coherent
        const wantGreen = drift >= 0 ? Math.random() < 0.62 : Math.random() < 0.38;
        const next = wantGreen ? "green" : "red";
        s.changed = next !== s.color;
        s.color = next;
        s.ts = now;
        if (s.changed) pushLog("tradingview", "renko", {
          tf: k, color: next, price,
        });
      } else {
        renko[k].changed = false;
      }
    }
  }

  // zone from 8pt + 12pt agreement
  function computeZone() {
    const h = renko.high.color, m = renko.macro.color;
    if (h === m && h === "green") return "LONG";
    if (h === m && h === "red") return "SHORT";
    return "FREE";
  }

  // -- logs ------------------------------------------------------------------
  function pushLog(stream, type, payload) {
    S.logs[stream].unshift({
      ts: nowS(), type, asset: "ES", symbol: "ES", payload,
    });
    if (S.logs[stream].length > 40) S.logs[stream].length = 40;
  }

  // -- full asset object (mirrors backend _init_asset schema) ----------------
  function freshAsset() {
    return {
      symbol: "ES", position: 0, avg_price: null, entry_price: null, pnl: 0.0,
      last_entry_ts: null,
      renko_color: "green", color_changed: false, last_renko_ts: nowS(),
      small_renko_color: "green", last_small_renko_ts: nowS(),
      one_renko_color: "green", one_renko_ts: nowS(), one_renko_color_changed: false,
      two_half_renko_color: "green", two_half_renko_ts: nowS(), two_half_color_changed: false,
      main_renko_color: "green", main_renko_ts: nowS(),
      high_renko_color: "green", high_renko_ts: nowS(),
      macro_renko_color: "green", macro_renko_ts: nowS(),
      two_half_tp_lock_enabled: false, two_half_tp_lock_base_color: null,
      two_half_tp_lock_released: false, two_half_tp_lock_started_ts: null,
      two_half_tp_lock_released_ts: null,
      tempo_color: "green", tempo_ts: nowS(), tempo_age_s: 0, tempo_ready: true,
      intent_active: false, intent_created_ts: null, intent_bar_base_ts: null,
      intent_ready_bar_ts: null, intent_status: null,
      last_exit_direction: null, reentry_lock_active: false,
      last_heartbeat_ts: nowS(), last_price: price,
      opposite_locked: false, five_min_ok: true, order_count: 0,
      exit_mode: null, auto_exit_renko: "6pt", exit_tf: "main",
      main_flip_exit_enabled: false,
      tp_armed: false, tp_target: 3, tp_count: 0,
      high_next_bar_exit_enabled: false, high_next_bar_exit_started_ts: null,
      high_next_bar_exit_base_ts: null, entry_high_renko_ts: null,
      high_next_bar_exit_allowed: false,
      points_tp_enabled: true, points_tp_target: 15.0,
      rithmic_open_points: 0.0, rithmic_point_value: POINT_VALUE, points_tp_hit_ts: null,
      protect_enabled: true, protect_threshold_points: -2.0, protect_hit_ts: null,
      env: "DEMO", stop_loss_price: null, stop_loss_status: null,
      pending_order_id: null, pending_side: null, pending_qty: 0,
      pending_mode: null, pending_trade_mode: null, preorder_trade_mode: null,
      manual_exit_allowed: true, tempo_spent_ts: null, tempo_last_bar_ts: nowS(),
      last_exit_ts: null, tempo_4pt_unlock_ts: null,
      last_trade_had_new_main_bar_after_entry: false,
      initial_exit_lock_active: false, initial_exit_lock_released: false,
      initial_exit_lock_started_ts: null, initial_exit_lock_released_ts: null,
      initial_exit_lock_base_main_ts: null,
      preorder_active: false, preorder_direction: null, preorder_qty: 0,
      preorder_entry_size_mode: null, preorder_created_ts: null,
      preorder_bar_base_ts: null, preorder_status: null,
      trade_mode: null, entry_zone: null, runner_4pt_unlocked: false,
      entry_main_renko_ts: null, entry_renko_color: null, entry_main_renko_color: null,
      four_pt_invalidation_enabled: false, next_bar_exit_allowed: false,
      zone_type: "LONG", four_pt_invalidation_allowed: false, conflict_mode: false,
      scale_in_available: false, scale_in_used: false, scale_in_stage: null,
      scale_in_last_ts: null, computed_entry_qty: ENTRY_QTY,
    };
  }

  // -- top-level state -------------------------------------------------------
  const S = {
    global: {
      equity: 50000.0, open_pnl: 0.0, rithmic_unrealized: 0.0, connected: true,
      trade_lock: false, total_orders: 0, env: "DEMO",
      five_min_trade_bucket: null, five_min_trade_lock_active: false,
      five_min_trade_lock_remaining_s: 0,
      post_exit_lock_active: false, post_exit_lock_started_ts: null,
      post_exit_lock_expires_ts: null, post_exit_lock_remaining_s: 0,
      daily_stop_enabled: true, daily_stop_limit_pct: 30.0,
      daily_start_equity: 50000.0, daily_stop_date: todayStr(),
      daily_stop_triggered: false, daily_stop_triggered_ts: null,
      daily_stop_triggered_reason: null, daily_stop_triggered_equity: null,
      daily_stop_triggered_dd_pct: null,
      max_trades_per_day: MAX_TRADES, trades_taken_today: 0,
      trades_remaining_today: MAX_TRADES, daily_trade_limit_date: todayStr(),
      tradingview_connected: true, rithmic_connected: true, rithmic_last_ts: nowS(),
      connected_count: 2, connected_expected: 2,
      today_date: todayStr(), low_liquidity_today: false,
      low_liquidity_reason: null, low_liquidity_days: [], manual_pnl_usd: null,
    },
    assets: { ES: freshAsset() },
    history: [],
    logs: { tradingview: [], rithmic: [] },
  };

  const A = () => S.assets.ES;
  let orderSeq = 1000;

  // -- trade lifecycle -------------------------------------------------------
  function openTrade(side /* "BUY"|"SELL" */, source) {
    recompute(); // refresh lock countdowns before gating
    const a = A();
    if (a.position !== 0) return { ok: false, error: "Already in a position" };
    if (S.global.post_exit_lock_active)
      return { ok: false, error: `Post-exit cooldown active — ${S.global.post_exit_lock_remaining_s}s remaining` };
    if (S.global.five_min_trade_lock_active)
      return { ok: false, error: `5-minute candle lock — ${S.global.five_min_trade_lock_remaining_s}s remaining` };
    if (S.global.trades_remaining_today <= 0)
      return { ok: false, error: "Daily trade limit reached (6/6)" };
    if (S.global.daily_stop_triggered)
      return { ok: false, error: "Daily stop triggered — locked for the day" };

    const dir = side === "BUY" ? 1 : -1;
    const t = nowS();
    a.position = dir * ENTRY_QTY;
    a.entry_price = price;
    a.avg_price = price;
    a.last_entry_ts = t;
    a.last_price = price;
    a.entry_zone = a.zone_type;
    a.trade_mode = "normal";
    a.entry_main_renko_ts = renko.main.ts;
    a.entry_main_renko_color = renko.main.color;
    a.entry_high_renko_ts = renko.high.ts;
    a.entry_renko_color = renko.four.color;
    a.order_count += 1;

    // tempo token spent
    a.tempo_spent_ts = t;
    a.tempo_ready = false;

    // engage locks
    a.initial_exit_lock_active = true;
    a.initial_exit_lock_released = false;
    a.initial_exit_lock_started_ts = t;
    a.initial_exit_lock_base_main_ts = renko.main.ts;

    a.two_half_tp_lock_enabled = true;
    a.two_half_tp_lock_released = false;
    a.two_half_tp_lock_base_color = renko.twoHalf.color;
    a.two_half_tp_lock_started_ts = t;

    a.points_tp_enabled = true;
    a.protect_enabled = true;
    a.rithmic_open_points = 0;
    a.stop_loss_price = +(price - dir * 15).toFixed(2);
    a.stop_loss_status = "working";
    a.scale_in_available = false;

    // drift favors the trade (mostly winners, with noise)
    drift = dir * rnd(0.10, 0.22);

    // 5-min candle lock — until next 5-min boundary
    S.global.five_min_trade_bucket = Math.floor(t / FIVE_MIN_S);
    S.global.five_min_trade_lock_active = true;

    // daily trade limit
    S.global.trades_taken_today += 1;
    S.global.trades_remaining_today = Math.max(0, MAX_TRADES - S.global.trades_taken_today);
    S.global.total_orders += 1;

    const oid = "ORD-" + (orderSeq++);
    a.pending_order_id = null; // fills instantly in demo
    pushLog("tradingview", "intent", { side, zone: a.entry_zone, price });
    pushLog("rithmic", "order", { id: oid, side, qty: ENTRY_QTY, status: "WORKING", price });
    setTimeout(() => pushLog("rithmic", "fill", { id: oid, side, qty: ENTRY_QTY, status: "FILLED", price: a.entry_price }), 350);

    lastNarrativeTs = t;
    return { ok: true, state: clone(S) };
  }

  function closeTrade(reason) {
    const a = A();
    if (a.position === 0) return { ok: false, error: "No open position" };
    const dir = a.position > 0 ? 1 : -1;
    const side = dir > 0 ? "SELL" : "BUY";
    const pts = +((price - a.entry_price) * dir).toFixed(2);
    const usd = +(pts * POINT_VALUE * ENTRY_QTY).toFixed(2);
    const t = nowS();

    S.history.unshift({
      id: "ORD-" + (orderSeq++), symbol: "ES",
      side: dir > 0 ? "LONG" : "SHORT", qty: ENTRY_QTY,
      entry_price: a.entry_price, exit_price: price,
      points: pts, pnl: usd, pnl_usd: usd, reason,
      opened_ts: a.last_entry_ts, closed_ts: t,
    });
    if (S.history.length > 30) S.history.length = 30;

    S.global.equity = +(S.global.equity + usd).toFixed(2);

    const oid = "ORD-" + (orderSeq++);
    pushLog("rithmic", "exit", { id: oid, side, qty: ENTRY_QTY, status: "FILLED", price, points: pts, pnl: usd, reason });

    // reset position
    a.position = 0; a.entry_price = null; a.avg_price = null; a.pnl = 0;
    a.rithmic_open_points = 0; a.stop_loss_price = null; a.stop_loss_status = null;
    a.last_exit_ts = t; a.last_exit_direction = dir > 0 ? "long" : "short";
    a.initial_exit_lock_active = false; a.two_half_tp_lock_enabled = false;
    a.points_tp_hit_ts = null; a.protect_hit_ts = null; a.scale_in_available = false;
    a.exit_mode = reason;
    drift = 0;

    // 180s post-exit cooldown
    S.global.post_exit_lock_active = true;
    S.global.post_exit_lock_started_ts = t;
    S.global.post_exit_lock_expires_ts = t + POST_EXIT_LOCK_S;
    S.global.post_exit_lock_remaining_s = POST_EXIT_LOCK_S;

    S.global.open_pnl = 0; S.global.rithmic_unrealized = 0;
    lastNarrativeTs = t;
    return { ok: true, state: clone(S), points: pts, pnl: usd };
  }

  // -- per-poll recompute (countdowns, derived fields) -----------------------
  function recompute() {
    const now = nowS();
    const a = A();

    // heartbeat / connectivity always fresh
    a.last_heartbeat_ts = now;
    a.last_price = price;
    S.global.rithmic_last_ts = now;
    S.global.tradingview_connected = true;
    S.global.rithmic_connected = true;
    S.global.connected = true;
    S.global.connected_count = 2;

    // mirror renko streams onto asset fields the UI reads
    a.one_renko_color = renko.one.color; a.one_renko_ts = renko.one.ts;
    a.one_renko_color_changed = renko.one.changed;
    a.small_renko_color = renko.one.color;
    a.two_half_renko_color = renko.twoHalf.color; a.two_half_renko_ts = renko.twoHalf.ts;
    a.two_half_color_changed = renko.twoHalf.changed;
    a.renko_color = renko.four.color; a.last_renko_ts = renko.four.ts;
    a.color_changed = renko.four.changed;
    a.main_renko_color = renko.main.color; a.main_renko_ts = renko.main.ts;
    a.high_renko_color = renko.high.color; a.high_renko_ts = renko.high.ts;
    a.macro_renko_color = renko.macro.color; a.macro_renko_ts = renko.macro.ts;

    // zone
    a.zone_type = computeZone();
    a.entry_zone = a.position !== 0 ? a.entry_zone : a.zone_type;
    a.conflict_mode = a.zone_type === "FREE";

    // tempo token
    a.tempo_color = renko.main.color;
    a.tempo_ts = renko.main.ts;
    a.tempo_last_bar_ts = renko.main.ts;
    a.tempo_age_s = now - renko.main.ts;
    if (a.tempo_spent_ts == null) {
      a.tempo_ready = a.position === 0;
    } else {
      // re-arms when a NEW 6pt bar prints after the token was spent
      a.tempo_ready = renko.main.ts > a.tempo_spent_ts && a.position === 0;
    }

    // post-exit cooldown countdown
    if (S.global.post_exit_lock_active) {
      const rem = Math.max(0, Math.ceil(S.global.post_exit_lock_expires_ts - now));
      S.global.post_exit_lock_remaining_s = rem;
      if (rem <= 0) {
        S.global.post_exit_lock_active = false;
        S.global.post_exit_lock_started_ts = null;
        S.global.post_exit_lock_expires_ts = null;
      }
    } else {
      S.global.post_exit_lock_remaining_s = 0;
    }

    // 5-min candle lock countdown (until next boundary)
    if (S.global.five_min_trade_lock_active && S.global.five_min_trade_bucket != null) {
      const boundary = (S.global.five_min_trade_bucket + 1) * FIVE_MIN_S;
      const rem = Math.max(0, Math.ceil(boundary - now));
      S.global.five_min_trade_lock_remaining_s = rem;
      if (rem <= 0) S.global.five_min_trade_lock_active = false;
    } else {
      S.global.five_min_trade_lock_remaining_s = 0;
    }
    a.five_min_ok = !S.global.five_min_trade_lock_active;

    // in-position management
    if (a.position !== 0) {
      const dir = a.position > 0 ? 1 : -1;
      const pts = +((price - a.entry_price) * dir).toFixed(2);
      a.rithmic_open_points = pts;
      a.pnl = +(pts * POINT_VALUE * ENTRY_QTY).toFixed(2);
      S.global.open_pnl = a.pnl;
      S.global.rithmic_unrealized = a.pnl;

      // initial exit lock releases on first NEW 6pt bar after entry
      if (a.initial_exit_lock_active && renko.main.ts > a.initial_exit_lock_base_main_ts) {
        a.initial_exit_lock_active = false;
        a.initial_exit_lock_released = true;
        a.initial_exit_lock_released_ts = now;
      }
      // 2.5pt management lock releases when 2.5 confirms trade direction
      if (a.two_half_tp_lock_enabled && !a.two_half_tp_lock_released) {
        const want = dir > 0 ? "green" : "red";
        if (renko.twoHalf.color === want && now - a.two_half_tp_lock_started_ts > 4) {
          a.two_half_tp_lock_released = true;
          a.two_half_tp_lock_released_ts = now;
        }
      }
      // scale-in unlocks once 4pt confirms in trade direction
      a.scale_in_available = !a.scale_in_used &&
        renko.four.color === (dir > 0 ? "green" : "red") && pts > 1.5;
    } else {
      S.global.open_pnl = 0; S.global.rithmic_unrealized = 0;
    }

    S.global.total_orders = a.order_count;
  }

  // -- autonomous narrative (keeps the demo alive hands-off) -----------------
  let lastNarrativeTs = nowS();
  function narrative() {
    const a = A();
    const now = nowS();
    if (a.position === 0) {
      // look for an entry: zone agrees, tempo ready, not locked, waited a beat
      const canEnter = !S.global.post_exit_lock_active &&
        !S.global.five_min_trade_lock_active &&
        S.global.trades_remaining_today > 0 &&
        a.tempo_ready && (a.zone_type === "LONG" || a.zone_type === "SHORT") &&
        now - lastNarrativeTs > rnd(4, 9);
      if (canEnter) openTrade(a.zone_type === "LONG" ? "BUY" : "SELL", "auto");
    } else {
      const dir = a.position > 0 ? 1 : -1;
      const pts = (price - a.entry_price) * dir;
      const held = now - a.last_entry_ts;
      // exit rules: points TP hit, protect breach, or 8pt flips against us late
      if (a.initial_exit_lock_active) return; // forced to hold through first bar
      if (pts >= a.points_tp_target) { a.points_tp_hit_ts = now; closeTrade("points_tp"); }
      else if (pts <= a.protect_threshold_points) { a.protect_hit_ts = now; closeTrade("protect"); }
      else if (held > rnd(26, 40) && pts > 2) closeTrade("tempo_exit");
      else if (renko.high.color !== (dir > 0 ? "green" : "red") && held > 18 && pts > 0)
        closeTrade("8pt_flip");
    }
    // reset daily counter at local midnight rollover (keeps long demos sane)
    if (S.global.daily_trade_limit_date !== todayStr()) {
      S.global.daily_trade_limit_date = todayStr();
      S.global.daily_stop_date = todayStr();
      S.global.trades_taken_today = 0;
      S.global.trades_remaining_today = MAX_TRADES;
      S.global.equity = 50000; S.global.daily_start_equity = 50000;
      S.history.length = 0;
    }
  }

  // -- main tick (4 Hz) ------------------------------------------------------
  setInterval(() => {
    const now = nowS();
    stepPrice();
    maybeFlip(now);
    narrative();
    recompute();
  }, 250);

  // initial compute so first poll is fully populated
  recompute();
  pushLog("rithmic", "heartbeat", { mdConnected: true, tsConnected: true, repoOk: true, account: "DEMO-ACCT", tradeRoute: "Rithmic Paper Trading" });

  // -- fetch interception ----------------------------------------------------
  function clone(o) { return JSON.parse(JSON.stringify(o)); }
  function jsonResp(obj, status) {
    return new Response(JSON.stringify(obj), {
      status: status || 200, headers: { "Content-Type": "application/json" },
    });
  }

  const realFetch = window.fetch ? window.fetch.bind(window) : null;

  window.fetch = function (input, init) {
    const url = (typeof input === "string" ? input : (input && input.url) || "").toString();
    const method = ((init && init.method) || (input && input.method) || "GET").toUpperCase();
    let body = {};
    try { if (init && init.body) body = JSON.parse(init.body); } catch (e) {}

    // only intercept our API; pass anything else through (there is nothing else)
    if (url.indexOf("/api/") === -1) {
      return realFetch ? realFetch(input, init) : jsonResp({ ok: true });
    }

    // ---- reads
    if (url.indexOf("/api/state") !== -1) {
      recompute();
      return Promise.resolve(jsonResp(clone(S)));
    }
    if (url.indexOf("/api/session-window") !== -1) {
      // always "open" during the trading session for the demo
      return Promise.resolve(jsonResp({ open: true, seconds_until_close: 3600, seconds_until_open: 0 }));
    }
    if (url.indexOf("/api/orders/pending") !== -1) {
      return Promise.resolve(jsonResp({ ok: true, orders: [] }));
    }

    // ---- entry
    if (url.indexOf("/api/orders") !== -1 && method === "POST") {
      const side = (body.side || body.direction || body.action || "BUY").toString().toUpperCase();
      const norm = side === "SELL" || side === "SHORT" || side === "RED" ? "SELL" : "BUY";
      return Promise.resolve(jsonResp(openTrade(norm, "manual")));
    }
    if (url.indexOf("/api/intent") !== -1 && url.indexOf("cancel") === -1) {
      const a = A();
      a.intent_active = true; a.intent_created_ts = nowS();
      a.intent_status = "armed"; a.intent_bar_base_ts = renko.main.ts;
      return Promise.resolve(jsonResp({ ok: true, state: clone(S) }));
    }

    // ---- exits
    if (url.indexOf("/api/exit-all") !== -1 || url.indexOf("/api/exit") !== -1) {
      const r = A().position !== 0 ? closeTrade("manual_exit") : { ok: true, state: clone(S) };
      return Promise.resolve(jsonResp(r));
    }
    if (url.indexOf("/api/take-profit") !== -1 || url.indexOf("/api/points-take-profit") !== -1) {
      const r = A().position !== 0 ? closeTrade("take_profit") : { ok: true, state: clone(S) };
      return Promise.resolve(jsonResp(r));
    }
    if (url.indexOf("auto-exit") !== -1 || url.indexOf("invalidation") !== -1 ||
        url.indexOf("next-bar-exit") !== -1 || url.indexOf("flip-exit") !== -1) {
      const a = A();
      // toggle the relevant auto-exit flag for visual feedback
      if (url.indexOf("high-next-bar") !== -1) a.high_next_bar_exit_enabled = !a.high_next_bar_exit_enabled;
      else if (url.indexOf("four-pt") !== -1) a.four_pt_invalidation_enabled = !a.four_pt_invalidation_enabled;
      else if (url.indexOf("main-flip") !== -1) a.main_flip_exit_enabled = !a.main_flip_exit_enabled;
      return Promise.resolve(jsonResp({ ok: true, state: clone(S) }));
    }

    // ---- management toggles
    if (url.indexOf("/api/protect") !== -1) {
      const a = A(); a.protect_enabled = !a.protect_enabled;
      return Promise.resolve(jsonResp({ ok: true, state: clone(S) }));
    }
    if (url.indexOf("/api/two-half-tp-lock") !== -1) {
      const a = A(); a.two_half_tp_lock_enabled = !a.two_half_tp_lock_enabled;
      return Promise.resolve(jsonResp({ ok: true, state: clone(S) }));
    }
    if (url.indexOf("/api/scale-in") !== -1) {
      const a = A();
      if (a.position !== 0 && a.scale_in_available && !a.scale_in_used) {
        a.position += (a.position > 0 ? 1 : -1);
        a.scale_in_used = true; a.scale_in_available = false; a.scale_in_stage = "added";
        a.scale_in_last_ts = nowS();
        pushLog("rithmic", "scale_in", { qty: 1, price });
      }
      return Promise.resolve(jsonResp({ ok: true, state: clone(S) }));
    }
    if (url.indexOf("/api/trade-limit/consume") !== -1) {
      return Promise.resolve(jsonResp({
        ok: true,
        max_trades_per_day: S.global.max_trades_per_day,
        trades_taken_today: S.global.trades_taken_today,
        trades_remaining_today: S.global.trades_remaining_today,
        daily_trade_limit_date: S.global.daily_trade_limit_date,
      }));
    }
    if (url.indexOf("cancel") !== -1) {
      const a = A();
      a.intent_active = false; a.intent_status = null;
      a.preorder_active = false; a.preorder_status = null;
      return Promise.resolve(jsonResp({ ok: true, state: clone(S) }));
    }

    // default: accept and echo state
    return Promise.resolve(jsonResp({ ok: true, state: clone(S) }));
  };

  // -- DEMO banner -----------------------------------------------------------
  function addBanner() {
    const params = new URLSearchParams(location.search);
    if (params.get("banner") === "0") return;
    const el = document.createElement("div");
    el.textContent = "DEMO — simulated data";
    el.style.cssText = [
      "position:fixed", "bottom:10px", "right:12px", "z-index:99999",
      "background:rgba(255,176,32,0.92)", "color:#1a1205", "font:600 11px/1 system-ui,sans-serif",
      "padding:6px 10px", "border-radius:999px", "letter-spacing:0.4px",
      "box-shadow:0 2px 10px rgba(0,0,0,0.35)", "pointer-events:none",
    ].join(";");
    document.body.appendChild(el);
  }
  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", addBanner);
  else addBanner();

  console.log("%cOperatorLock DEMO MODE active — all data simulated, no backend.", "color:#ffb020;font-weight:bold");
})();
