# VPS Bridge

The VPS bridge is a C# application that runs on a Windows server co-located with the Rithmic execution gateway. It polls the Flask backend for pending orders, executes them via Rithmic's native RAPI+ API, and reports fill/rejection status back.

## Why a Separate Bridge?

Rithmic's RAPI+ is a proprietary native API — not a REST API. It requires a persistent TCP connection and must run on Windows. The Flask backend runs on Heroku (Linux). The bridge decouples execution from the behavioral engine: the backend decides *whether* to trade, the bridge decides *how* to send the order.

## Architecture

```
Flask Backend (Heroku)
    ↑ POST /api/orders/{id}/execution-report (fills, rejects)
    ↑ POST /api/rithmic/pnl-snapshot (position, open PnL)
    ↑ POST /api/bridge/heartbeat (connection status)
    ↓ GET /api/orders/pending (poll for work)
VPS Bridge (Windows Server)
    ↕ Rithmic RAPI+ (proprietary TCP)
Rithmic Gateway
    ↕ CME exchange
```

## Files

| File | Purpose |
|---|---|
| `src/Program.cs` | Bridge entry point and main loop |
| `src/Config.cs` | Non-secret configuration scaffold (empty) |
| `Config.secrets.example.cs` | Template — copy to `Config.secrets.cs` and fill in |

`Config.secrets.cs` is gitignored. Never commit real credentials.

## Build

### Prerequisites

- .NET 6 SDK or later
- Rithmic RAPI+ DLLs (provided by your broker — place in `src/lib/`)

### Build and run

```cmd
cd vps\src
dotnet build -c Release
dotnet run
```

### Self-contained executable

```cmd
dotnet publish -c Release -r win-x64 --self-contained true -o ..\dist
```

## Configuration

Copy the template:

```cmd
copy Config.secrets.example.cs Config.secrets.cs
```

Edit `Config.secrets.cs` with your real values:

| Field | Description |
|---|---|
| `BackendUrl` | Your Heroku backend URL (no trailing slash) |
| `BridgeSecret` | Must match `BRIDGE_HEARTBEAT_SECRET` on the backend |
| `RithmicServer` | Broker-provided Rithmic server address |
| `RithmicUsername` | Rithmic account username |
| `RithmicPassword` | Rithmic account password |
| `RithmicSystemName` | System name from your Rithmic agreement |
| `RithmicGateway` | Exchange gateway (e.g., "CME Globex") |
| `AccountId` | Your Rithmic account ID |
| `TradeRoute` | Trade route (e.g., "PAPER" or live route) |

## Running as a Windows Service

For production, use NSSM (Non-Sucking Service Manager) to run the bridge as a Windows Service:

```cmd
nssm install OperatorLockBridge "C:\path\to\dist\Program.exe"
nssm set OperatorLockBridge AppRestartDelay 5000
nssm start OperatorLockBridge
```

## What the Bridge Logs

The bridge logs each action to stdout with timestamps:

```
[14:32:01] Polling backend for pending orders...
[14:32:01] Found 1 pending order: BUY 5 MESM6 (id: abc123)
[14:32:01] Submitting order abc123 to Rithmic...
[14:32:02] Order abc123 WORKING
[14:32:04] Order abc123 FILLED @ 5250.25
[14:32:04] Reported FILLED to backend
```
