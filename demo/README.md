# OperatorLock — Live Demo

**▶ Live:** https://turangenesis.github.io/OperatorLock/

A fully self-contained, **zero-backend** simulation of the OperatorLock operator console.
It runs the *real* dashboard UI, but a small mock engine intercepts the network layer
and feeds it a simulated trading session — so every behavioral constraint is visible in
real time without Firestore, Rithmic, webhooks, logins, or API keys.

## What it shows

The demo plays an autonomous trading session (and also responds to button clicks), exercising
every headline constraint:

- 6 independent Renko streams (1 / 2.5 / 4 / 6 / 8 / 12 pt) flipping live
- Zone entry gate (8pt + 12pt agreement → LONG / SHORT / FREE)
- Tempo token (one entry per 6pt bar)
- 5-minute candle lock & 180-second post-exit cooldown counting down
- Initial-exit lock and 2.5pt management lock
- Take-profit / protect auto-exits booking PnL to history
- Daily trade limit (6/day) and daily-stop configuration
- Live equity, open PnL, connectivity badges, and streaming logs

## How it works

- [index.html](index.html) — the production dashboard, with one line added:
  `<script src="demo-mock.js"></script>`, and the server-rendered asset loop pre-rendered for `ES`.
- [demo-mock.js](demo-mock.js) — overrides `window.fetch` to serve a simulated `/api/state`
  (matching the backend schema) and to handle order/exit/management actions locally.

All data is simulated. Nothing leaves the browser. To hide the corner banner, append `?banner=0`.

## Run it locally

```bash
cd demo
python3 -m http.server 8077
# open http://localhost:8077/
```
