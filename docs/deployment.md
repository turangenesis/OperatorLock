# Deployment

## Prerequisites

- **Heroku account** (or any server that can run Python/gunicorn)
- **Google Cloud project** with Firestore enabled
- **TradingView** account with Renko alerts configured (Pro or higher for webhook alerts)
- **Rithmic account** with live or paper trading access (for VPS bridge)
- **Windows VPS** with .NET 6+ (for VPS bridge)

---

## 1. Google Firestore Setup

### Create Service Account

1. Go to [Google Cloud Console → IAM & Admin → Service Accounts](https://console.cloud.google.com/iam-admin/serviceaccounts)
2. Create a new service account (e.g., `operatorlock-backend`)
3. Grant the **Cloud Datastore User** role
4. Create a JSON key and download it

### Required Firestore Collections

OperatorLock creates these automatically on first write:

| Collection | Purpose |
|---|---|
| `console_state` | Per-asset and global runtime state |
| `orders` | Order lifecycle documents |
| `pnl_snapshots` | Daily PnL history |

No schema setup required — Firestore is schemaless.

---

## 2. Backend Deployment (Heroku)

### Initial Setup

```bash
heroku create your-app-name
heroku buildpacks:set heroku/python
```

### Set Config Vars

```bash
# Authentication
heroku config:set FLASK_SECRET_KEY="$(openssl rand -hex 32)"
heroku config:set DASHBOARD_PASSWORD="your-secure-password"
heroku config:set BRIDGE_HEARTBEAT_SECRET="$(openssl rand -hex 32)"
heroku config:set TV_WEBHOOK_SECRET="your-tradingview-webhook-secret"

# Firestore — paste the service account JSON as a single line
heroku config:set GOOGLE_CLOUD_CREDENTIALS='{"type":"service_account","project_id":"...","private_key":"...","client_email":"...",...}'
```

### Deploy

```bash
cd backend/
git subtree push --prefix backend heroku main
# or if deploying from the backend directory:
heroku git:remote -a your-app-name
git push heroku main
```

The `Procfile` contains:
```
web: gunicorn app:app
```

### Verify

```bash
heroku logs --tail
curl https://your-app-name.herokuapp.com/api/state
```

---

## 3. TradingView Webhook Setup

For each Renko bar stream, create a TradingView alert with:

**Webhook URL:** `https://your-app-name.herokuapp.com/tv_renko_main` (adjust endpoint per stream)

**Alert message (JSON body):**
```json
{
  "secret": "{{your-tv-webhook-secret}}",
  "asset": "{{ticker}}",
  "color": "{{plot_0}}",
  "bar_ts": "{{time}}"
}
```

### Endpoints per stream

| Stream | Endpoint |
|---|---|
| 4pt Renko | `POST /tv_renko` |
| 2.5pt Renko | `POST /tv_renko_two_half` |
| 6pt Renko (main) | `POST /tv_renko_main` |
| 8pt Renko (high) | `POST /tv_renko_high` |
| 12pt Renko (macro) | `POST /tv_renko_macro` |
| 1pt Renko (visual) | `POST /tv_renko_small` |
| Price heartbeat | `POST /tv_heartbeat` |

The heartbeat alert should fire every minute and include `"price": {{close}}` so the dashboard can show the last known price and detect connectivity loss.

---

## 4. VPS Bridge Deployment (Windows)

### Prerequisites

- Windows Server 2019+ or Windows 10+
- .NET 6 SDK
- Rithmic RAPI+ DLLs (provided by your broker — not included in this repo)

### Build

```cmd
cd vps\src
dotnet build -c Release
```

### Configure

Copy the secrets template:
```cmd
copy ..\Config.secrets.example.cs Config.secrets.cs
```

Edit `Config.secrets.cs` and fill in:
- `BackendUrl` — your Heroku backend URL
- `BridgeSecret` — must match `BRIDGE_HEARTBEAT_SECRET` on the backend
- `RithmicServer` — your broker's Rithmic server address
- `RithmicUsername`, `RithmicPassword`
- `RithmicSystemName`, `RithmicGateway`
- `AccountId`, `TradeRoute`

### Run

```cmd
dotnet run --project vps\src\Program.cs
```

Or build a self-contained executable:
```cmd
dotnet publish -c Release -r win-x64 --self-contained true
```

The bridge runs as a console application. For production, run it as a Windows Service using `sc.exe` or NSSM.

### What the bridge does

- Every ~1 second: `GET {BackendUrl}/api/orders/pending?secret={BridgeSecret}` — claims pending orders
- For each claimed order: submits to Rithmic via RAPI+
- On WORKING/FILLED/REJECTED/CANCELLED events from Rithmic: `POST {BackendUrl}/api/orders/{id}/execution-report`
- Every ~5 seconds: `POST {BackendUrl}/api/rithmic/pnl-snapshot` — current position, open PnL, account balance
- Every ~10 seconds: `POST {BackendUrl}/api/bridge/heartbeat` — connection status

---

## 5. Local Development

```bash
cd backend/
pip install -r requirements.txt

# Create a .env file (see .env.example)
cp ../.env.example .env
# Edit .env — at minimum set FLASK_SECRET_KEY and BRIDGE_HEARTBEAT_SECRET
# Leave GOOGLE_CLOUD_CREDENTIALS empty to run without Firestore (state won't persist)

flask run
# App available at http://localhost:5000
```

Without `GOOGLE_CLOUD_CREDENTIALS`, Firestore operations will silently fail and state will be in-memory only. All other functionality works normally for local testing.

Without `DASHBOARD_PASSWORD`, the dashboard is open with no authentication.
